#!/opt/homebrew/bin/python3
"""
UAP Archive — secondary news coverage poster.

Posts journalism coverage of the PURSUE/UAP disclosure story, clearly
tagged "COVERAGE" so it is never confused with the primary declassified
files posted by uap_files.py.

Source: The Debrief's UAP category feed (thedebrief.org/category/uap/feed/)
— a real, direct-link RSS feed from a reputable science/defense outlet,
not a Google News aggregator. Google News RSS was tried first and
rejected: its article links are unresolvable client-side redirects (no
crawler, including Telegram's own, can follow them to the real page), so
"verify the source" would have been impossible. Each post here carries
the outlet's own real article URL plus their published one-line dek —
never a full article reproduction (that would be a copyright problem for
third-party journalism; the primary files pipeline can reproduce text in
full only because those government documents are public domain).

Runs 2x/day via launchd — see com.uaparchive.coverage.plist.
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = "/Users/maxim/.claude/projects/-Users-maxim-ObsidianBrain/memory/.telegram_bot_token"
CHANNEL = "@uap_archive"

SEEN_FILE = os.path.join(HERE, "seen_coverage.json")
LOG_FILE = os.path.join(HERE, "logs", "coverage.log")

MAX_PER_RUN = 4
RECENCY_HOURS = 24 * 21  # this outlet posts on this topic every 1-3 weeks, not daily
REQUEST_TIMEOUT = 20

FEED_LABEL = "The Debrief"
FEED_URL = "https://thedebrief.org/category/uap/feed/"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


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


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return r.read()


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def short_dek(text, max_words=18):
    """A short attributed snippet of the outlet's own dek — never the full
    article, just enough to identify the story before the reader clicks
    through to verify at the source."""
    text = re.sub(r"\s+", " ", text or "").strip()
    words = text.split(" ")
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "…"


def parse_feed(raw):
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"  parse error: {e}")
        return items
    for node in root.iter("item"):
        title_el = node.find("title")
        link_el = node.find("link")
        pub_el = node.find("pubDate")
        guid_el = node.find("guid")
        desc_el = node.find("description")
        if title_el is None or link_el is None:
            continue
        items.append({
            "headline": (title_el.text or "").strip(),
            "link": (link_el.text or "").strip(),
            "dek": short_dek(desc_el.text if desc_el is not None else ""),
            "guid": (guid_el.text if guid_el is not None else link_el.text or "").strip(),
            "published": parse_date(pub_el.text if pub_el is not None else None),
        })
    return items


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_seen(seen):
    seen = seen[-3000:]
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f)
    os.replace(tmp, SEEN_FILE)


def tg_call(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "description": f"HTTPError {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def build_post(item):
    headline = html.escape(item["headline"], quote=False)
    dek = html.escape(item["dek"], quote=False)
    link = html.escape(item["link"], quote=True)
    return (
        f"\U0001F4F0 <b>COVERAGE</b> — via {FEED_LABEL}\n\n"
        f"<b>{headline}</b>\n"
        f"{dek}\n\n"
        f"\U0001F517 <a href=\"{link}\">Read the full article — verify at source</a>\n\n"
        f"<i>News coverage of the disclosure story, not an official file. "
        f"Primary documents are posted separately, tagged by file type.</i>"
    )


def main():
    token = read_token()
    seen = load_seen()
    seen_set = set(seen)

    try:
        raw = http_get(FEED_URL)
        items = parse_feed(raw)
        log(f"fetched {len(items)} items from {FEED_LABEL}")
    except Exception as e:
        log(f"FEED FAIL: {e}")
        return

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RECENCY_HOURS)

    fresh = []
    for it in items:
        key = it["guid"] or it["link"]
        if key in seen_set:
            continue
        pub = it["published"] or now
        if pub < cutoff:
            continue
        it["_key"] = key
        it["_pub"] = pub
        fresh.append(it)

    fresh.sort(key=lambda x: x["_pub"].timestamp())
    to_post = fresh[:MAX_PER_RUN]
    log(f"candidates: {len(fresh)} fresh, posting {len(to_post)}")

    posted = 0
    for it in to_post:
        resp = tg_call(token, "sendMessage", {
            "chat_id": CHANNEL,
            "text": build_post(it),
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        })
        if resp.get("ok"):
            seen.append(it["_key"])
            seen_set.add(it["_key"])
            posted += 1
            log(f"  POSTED {it['headline'][:70]}")
            time.sleep(3)
        else:
            log(f"  FAILED {it['headline'][:70]}: {resp.get('description')}")

    save_seen(seen)
    log(f"done: posted {posted}, seen cache {len(seen)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
