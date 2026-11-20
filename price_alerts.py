import json
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
 
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
SEEN_FILE = os.path.join(HERE, "seen.json")
USER_AGENT = "price-error-alerts/1.0 (personal deal alerts)"
PRICE_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
WAS_RE = re.compile(r"(?:was|rrp|usually|normally|reg(?:ular)?)\s*:?\s*£\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
 
 
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
 
 
def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
 
 
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()
 
 
def text_of(el, *names):
    for n in names:
        found = el.find(n)
        if found is not None:
            if found.text:
                return found.text.strip()
            href = found.get("href")
            if href:
                return href.strip()
    return ""
 
 
def parse_feed(raw):
    """Return a list of {id, title, link, summary} from RSS or Atom."""
    items = []
    root = ET.fromstring(raw)
    atom = "{http://www.w3.org/2005/Atom}"
    nodes = root.findall(".//item") or root.findall(f".//{atom}entry")
    for n in nodes:
        title = text_of(n, "title", f"{atom}title")
        link = text_of(n, "link", f"{atom}link")
        guid = text_of(n, "guid", f"{atom}id") or link or title
        summary = text_of(n, "description", f"{atom}summary", f"{atom}content")
        summary = html.unescape(TAG_RE.sub(" ", summary))
        items.append({"id": guid, "title": html.unescape(title), "link": link, "summary": " ".join(summary.split())})
    return items
 
 
def to_number(s):
    return float(s.replace(",", ""))
 
 
def first_price(text):
    m = PRICE_RE.search(text)
    return to_number(m.group(1)) if m else None
 
 
def was_price(text):
    m = WAS_RE.search(text)
    return to_number(m.group(1)) if m else None
 
 
def word_in(word, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", text) is not None
 
 
def check_item(item, cfg):
    """Return a list of reasons this item looks like a price error (empty = ignore)."""
    title = item["title"]
    text = f"{title} {item['summary']}".lower()
    reasons = []
 
    if any(word_in(w, text) for w in cfg.get("ignore_words", [])):
        return []
 
    for w in cfg.get("error_words", []):
        if word_in(w, text):
            reasons.append(f'mentions "{w}"')
            break
 
    price = first_price(title)
    if price is None:
        price = first_price(item["summary"])
 
    if price is not None:
        for name, limit in cfg.get("watch", {}).items():
            if word_in(name, text) and price <= float(limit):
                reasons.append(f"{name} at £{price:,.2f} (your limit £{float(limit):,.0f})")
                break
 
        was = was_price(text)
        max_share = cfg.get("max_share_of_was_price", 0.2)
        if was and was > 0 and price <= was * max_share and was >= cfg.get("min_was_price", 50):
            reasons.append(f"£{price:,.2f}, was £{was:,.2f} ({100 - price / was * 100:.0f}% off)")
 
    return reasons
 
 
def send_telegram(text, token, chat_id):
    url = fhttps://api.telegram.org/bot{token}/sendMessage
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "false"}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram refused the message: {body}")
 
 
def format_alert(item, reasons, feed_name):
    return (
        "Possible price error\n\n"
        f"{item['title']}\n\n"
        f"Why: {'; '.join(reasons)}\n"
        f"Found on: {feed_name}\n\n"
        f"{item['link']}\n\n"
        "Check it's the real retailer's site before you buy. Pay by card."
    )
 
 
def main(argv):
    dry = "--dry-run" in argv
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        print("config.json is missing or not valid JSON.")
        return 1
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
 
    if "--test" in argv:
        if not token or not chat_id:
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
            return 1
        send_telegram("Test from your price error alerts. If you can read this, Telegram is set up.", token, chat_id)
        print("Test message sent.")
        return 0
 
    if not dry and (not token or not chat_id):
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set. Use --dry-run to test without Telegram.")
        return 1
 
    seen = load_json(SEEN_FILE, {"ids": []})
    seen_ids = list(seen.get("ids", []))
    seen_set = set(seen_ids)
    first_run = not seen_ids
    sent = 0
    errors = 0
 
    for feed in cfg.get("feeds", []):
        name, url = feed.get("name", feed.get("url")), feed.get("url")
        try:
            items = parse_feed(fetch(url))
        except Exception as e:  # keep going if one feed fails
            errors += 1
            print(f"[{name}] could not read feed: {e}")
            continue
        print(f"[{name}] {len(items)} items")
        for it in items:
            key = it["id"]
            if key in seen_set:
                continue
            seen_set.add(key)
            seen_ids.append(key)
            reasons = check_item(it, cfg)
            if not reasons:
                continue
            msg = format_alert(it, reasons, name)
            if dry:
                print("\n--- ALERT ---\n" + msg)
            elif first_run and cfg.get("skip_alerts_on_first_run", True):
                print(f"(first run, not sent) {it['title']}")
            else:
                try:
                    send_telegram(msg, token, chat_id)
                    sent += 1
                    time.sleep(1)
                except Exception as e:
                    errors += 1
                    print(f"Could not send to Telegram: {e}")
 
    if not dry:
        save_json(SEEN_FILE, {"ids": seen_ids[-int(cfg.get("remember_last", 3000)):]})
    print(f"Done. Alerts sent: {sent}. Problems: {errors}.")
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
