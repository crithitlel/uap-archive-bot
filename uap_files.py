#!/opt/homebrew/bin/python3
"""
UAP Archive — official file poster.

Pulls the live dataset behind war.gov/ufo (the Department of War's PURSUE
disclosure portal) and posts each declassified file to the channel using
the government's OWN description text verbatim — no LLM rewriting, so
there is no hallucination risk on the core content.

The site's Akamai bot-protection blocks plain HTTP clients (curl/urllib)
even with browser-matching headers — confirmed via direct testing, it's a
TLS/automation fingerprint check, not a header check. A real installed
Chrome (via Playwright's channel="chrome", not the bundled test Chromium)
is required; bundled Chromium and headless-Chromium both get blocked.

Runs periodically via launchd (new tranches drop every few weeks, not on
a fixed schedule) — see com.uaparchive.files.plist.
"""

import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = "/Users/maxim/.claude/projects/-Users-maxim-ObsidianBrain/memory/.telegram_bot_token"
CHANNEL = "@uap_archive"

SEEN_FILE = os.path.join(HERE, "seen_files.json")
LOG_FILE = os.path.join(HERE, "logs", "files.log")

PORTAL_URL = "https://www.war.gov/ufo/"
CSV_URL_PATTERN = "uap-data.csv"

MAX_PER_RUN = 40          # generous cap; backfill run will need several passes
SLEEP_BETWEEN_POSTS = 3   # seconds, stays under Telegram's per-chat flood limit
FETCH_RETRIES = 4

TYPE_EMOJI = {"PDF": "\U0001F4C4", "IMG": "\U0001F5BC", "VID": "\U0001F3A5", "AUD": "\U0001F50A"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_token():
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env_token:
        return env_token.strip()
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def fetch_csv():
    """Fetch the live dataset by capturing the CSV response the portal page
    itself triggers on load — avoids guessing a release-number query param,
    and auto-adapts whenever a new tranche is published."""
    last_err = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                context = browser.new_context(user_agent=UA)
                page = context.new_page()

                captured = {}

                def on_response(resp):
                    if CSV_URL_PATTERN in resp.url and resp.status == 200:
                        try:
                            captured["text"] = resp.text()
                        except Exception:
                            pass

                page.on("response", on_response)
                page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
                time.sleep(2)

                if "text" not in captured:
                    # fallback: page loaded but we missed the request; ask it directly
                    captured["text"] = page.evaluate(
                        "async () => (await fetch('https://www.war.gov/Portals/1/Interactive/2026/UFO/uap-data.csv?release=4', {cache:'no-store'})).text()"
                    )

                browser.close()

                text = captured.get("text", "")
                if text and not text.lstrip().startswith("<"):
                    return text
                last_err = "got HTML (blocked) instead of CSV"
        except Exception as e:
            last_err = str(e)
        log(f"  fetch attempt {attempt} failed: {last_err}")
        time.sleep(5)
    raise RuntimeError(f"could not fetch CSV after {FETCH_RETRIES} attempts: {last_err}")


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_seen(seen):
    seen = seen[-5000:]
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f)
    os.replace(tmp, SEEN_FILE)


def tg_call(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "description": f"HTTPError {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def dvids_link(record_type, video_id):
    kind = "audio" if record_type == "AUD" else "video"
    return f"https://www.dvidshub.net/{kind}/{video_id}"


CAPTION_LIMIT = 1024  # Telegram's hard limit for photo/video captions


def row_link(row):
    rtype = row["Type"].strip()
    if rtype in ("PDF", "IMG"):
        return row["PDF | Image Link"].strip()
    return dvids_link(rtype, row["DVIDS Video ID"].strip())


def build_header(row):
    rtype = row["Type"].strip()
    emoji = TYPE_EMOJI.get(rtype, "\U0001F6F8")
    title = html.escape(row["Title"], quote=False)
    agency = html.escape(row["Agency"], quote=False)
    inc_date = row["Incident Date"].strip()
    inc_loc = row["Incident Location"].strip()
    meta_bits = [b for b in [agency, inc_date, inc_loc] if b]
    meta = " · ".join(html.escape(b, quote=False) for b in meta_bits)
    return f"{emoji} <b>{title}</b>\n{meta}"


def build_full_text(row):
    """Full, untruncated official description — verbatim government text,
    never paraphrased or summarized, always paired with the source link."""
    desc = re.sub(r"\s+", " ", row["Description Blurb"]).strip()
    desc = html.escape(desc, quote=False)
    link = html.escape(row_link(row), quote=True)
    return (
        f"{build_header(row)}\n\n"
        f"{desc}\n\n"
        f"\U0001F4C1 <a href=\"{link}\">Original file — war.gov/ufo</a>"
    )


def post_row(token, row):
    rtype = row["Type"].strip()
    thumb = row.get("Modal Image", "").strip()
    full_text = build_full_text(row)

    if rtype in ("PDF", "IMG") and thumb:
        if len(full_text) <= CAPTION_LIMIT:
            resp = tg_call(token, "sendPhoto", {
                "chat_id": CHANNEL, "photo": thumb,
                "caption": full_text, "parse_mode": "HTML",
            })
            if resp.get("ok"):
                return True
            log(f"    sendPhoto failed ({resp.get('description')}), falling back to text")
        else:
            # Description too long for a caption: send the image on its own,
            # then the full untruncated text as a follow-up message so
            # nothing gets cut short.
            resp = tg_call(token, "sendPhoto", {
                "chat_id": CHANNEL, "photo": thumb,
                "caption": build_header(row), "parse_mode": "HTML",
            })
            if resp.get("ok"):
                time.sleep(1)
                resp2 = tg_call(token, "sendMessage", {
                    "chat_id": CHANNEL, "text": full_text,
                    "parse_mode": "HTML", "disable_web_page_preview": "true",
                })
                return resp2.get("ok", False)
            log(f"    sendPhoto failed ({resp.get('description')}), falling back to text")

    resp = tg_call(token, "sendMessage", {
        "chat_id": CHANNEL,
        "text": full_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    })
    return resp.get("ok", False)


def parse_release_date(s):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%y")
    except Exception:
        return datetime.min


def main():
    token = read_token()
    seen = load_seen()
    seen_set = set(seen)

    log("fetching live dataset from war.gov/ufo ...")
    csv_text = fetch_csv()
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    rows = [r for r in rows if r.get("Title")]
    log(f"parsed {len(rows)} total records from portal")

    fresh = [r for r in rows if r["Title"].strip() not in seen_set]
    fresh.sort(key=lambda r: (parse_release_date(r["Release Date"]), r["Title"]))
    to_post = fresh[:MAX_PER_RUN]
    log(f"new records: {len(fresh)}, posting {len(to_post)} this run")

    posted = 0
    for row in to_post:
        key = row["Title"].strip()
        ok = post_row(token, row)
        if ok:
            seen.append(key)
            seen_set.add(key)
            posted += 1
            log(f"  POSTED [{row['Type'].strip()}] {key[:70]}")
            time.sleep(SLEEP_BETWEEN_POSTS)
        else:
            log(f"  FAILED {key[:70]}")

    save_seen(seen)
    log(f"done: posted {posted}, seen cache {len(seen)}, remaining backlog {len(fresh) - posted}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
