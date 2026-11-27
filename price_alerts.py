# Price error and great-deal alerts to Telegram (version 2).
#
# Checks two kinds of source every run:
#   1. Deal feeds (RSS), e.g. HotUKDeals: alerts on error words, watched
#      items under your limit, and big discounts.
#   2. Shop pages you choose (e.g. "sofas, cheapest first"): alerts when the
#      lowest price on the page drops below your limit.
#
# It never fails silently: problems are sent to Telegram, and a daily
# "still running" summary arrives at the hour set in config.json.
#
# Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set as GitHub secrets.
# Run with --dry-run to print alerts instead of sending them,
# or --test to send one test message.
 
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
 
try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:
    UK = None
 
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "seen.json")
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TG = "https" + ":" + "//" + "api.telegram.org" + "/bot"
ATOM = "{" + "http" + ":" + "//" + "www.w3.org/2005/Atom" + "}"
NUM = r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
POUND = "£"
PRICE = re.compile(POUND + r"\s?" + NUM)
WAS = re.compile(r"(?:was|rrp|usually|normally|reg)\s*:?\s*" + POUND + r"\s?" + NUM, re.I)
TAGS = re.compile(r"<[^>]+>")
JSONLD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
EMBEDDED_PRICE = re.compile(r'"(?:price|salePrice|currentPrice|lowPrice|nowPrice)"\s*:\s*"?' + NUM + r'"?')
DATA_PRICE = re.compile(r'data-(?:price|product-price|sale-price)\s*=\s*"' + POUND + r'?\s?' + NUM + '"', re.I)
 
 
# ---------------------------------------------------------------- helpers
def now_uk():
    return dt.datetime.now(UK) if UK else dt.datetime.utcnow()
 
 
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default
 
 
def fetch(url, accept="*/*"):
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER,
        "Accept": accept,
        "Accept-Language": "en-GB,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()
 
 
def number(s):
    return float(str(s).replace(",", ""))
 
 
def has_word(word, text):
    pattern = r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None
 
 
def money(x):
    return POUND + format(x, ",.2f")
 
 
def send(text, token, chat):
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(TG + token + "/sendMessage", data=data,
                                 headers={"User-Agent": "price-alerts/2"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if not json.loads(r.read()).get("ok"):
            raise RuntimeError("Telegram refused the message")
 
 
# ---------------------------------------------------------------- feeds
def text_of(el, names):
    for n in names:
        x = el.find(n)
        if x is not None:
            if x.text and x.text.strip():
                return x.text.strip()
            if x.get("href"):
                return x.get("href").strip()
    return ""
 
 
def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for n in root.findall(".//item") or root.findall(".//" + ATOM + "entry"):
        title = html.unescape(text_of(n, ["title", ATOM + "title"]))
        link = text_of(n, ["link", ATOM + "link"])
        key = text_of(n, ["guid", ATOM + "id"]) or link or title
        summary = text_of(n, ["description", ATOM + "summary", ATOM + "content"])
        summary = " ".join(html.unescape(TAGS.sub(" ", summary)).split())
        items.append({"id": key, "title": title, "link": link, "summary": summary})
    return items
 
 
def feed_reasons(item, cfg):
    """Why this deal is worth an alert. Empty list = ignore it."""
    text = (item["title"] + " " + item["summary"]).lower()
    if any(has_word(w, text) for w in cfg.get("ignore_words", [])):
        return []
    why = []
    for w in cfg.get("error_words", []):
        if has_word(w, text):
            why.append('mentions "' + w + '"')
            break
    m = PRICE.search(item["title"]) or PRICE.search(item["summary"])
    if not m:
        return why
    price = number(m.group(1))
    for name, limit in cfg.get("watch", {}).items():
        if has_word(name, text) and price <= float(limit):
            why.append(name + " at " + money(price) + " (your limit " + money(float(limit)) + ")")
            break
    w = WAS.search(text)
    if w:
        was = number(w.group(1))
        if was >= cfg.get("min_was_price", 50) and was > 0:
            off = 1 - price / was
            if off >= cfg.get("error_discount", 0.8):
                why.append(money(price) + ", was " + money(was) + " (" + str(round(off * 100)) + "% off: possible error)")
            elif off >= cfg.get("great_deal_discount", 0.6):
                why.append(money(price) + ", was " + money(was) + " (" + str(round(off * 100)) + "% off: great deal)")
    return why
 
 
# ---------------------------------------------------------------- shop pages
def walk_json(obj, found):
    """Collect prices from JSON-LD (Product / Offer / AggregateOffer / ItemList)."""
    if isinstance(obj, list):
        for x in obj:
            walk_json(x, found)
    elif isinstance(obj, dict):
        for key in ("price", "lowPrice"):
            if key in obj:
                try:
                    found.append(number(obj[key]))
                except Exception:
                    pass
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_json(v, found)
 
 
def prices_on_page(raw_html):
    page = raw_html.decode("utf-8", "replace")
    found = []
    for block in JSONLD.findall(page):
        try:
            walk_json(json.loads(html.unescape(block).strip()), found)
        except Exception:
            continue
    if not found:
        found += [number(m) for m in EMBEDDED_PRICE.findall(page)]
    if not found:
        found += [number(m) for m in DATA_PRICE.findall(page)]
    return found
 
 
def check_page(page_cfg, cfg):
    """Return (lowest price found or None, status text)."""
    raw = fetch(page_cfg["url"], "text/html,application/xhtml+xml")
    prices = [p for p in prices_on_page(raw) if p >= cfg.get("min_believable_price", 0.5)]
    if not prices:
        return None, "read the page but found no prices"
    return min(prices), "lowest " + money(min(prices)) + " (" + str(len(prices)) + " prices)"
 
 
# ---------------------------------------------------------------- main
def main(args):
    dry = "--dry-run" in args
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    out = []  # (message, page url or None, page price or None) to send this run
 
    def say(msg, page_key=None, page_price=None):
        out.append((msg, page_key, page_price))
 
    cfg = load_json(CONFIG, None)
    if cfg is None:
        msg = "Price alerts: config.json can't be read (a comma or bracket is probably wrong). Nothing was checked."
        print(msg)
        if token and chat and not dry:
            send(msg, token, chat)
        return 0
 
    if "--test" in args:
        if token and chat:
            send("Test from your price alerts (version 2). Telegram is set up.", token, chat)
            print("Test message sent.")
        else:
            print("Telegram secrets are missing.")
        return 0
 
    state = load_json(STATE, {})
    seen = state.get("ids", [])
    known = set(seen)
    first_run = not seen and not state.get("pages")
    stats = state.get("stats", {"checked": 0, "alerts": 0, "problems": []})
    page_state = state.get("pages", {})
 
    # 1. deal feeds
    feed_failures = 0
    for feed in cfg.get("feeds", []):
        name = feed.get("name", feed.get("url", "feed"))
        try:
            items = parse_feed(fetch(feed["url"], "application/rss+xml, application/xml, text/xml"))
        except Exception as e:
            problem = name + ": could not read (" + type(e).__name__ + ": " + str(e)[:80] + ")"
            print(problem)
            stats["problems"].append(problem)
            feed_failures += 1
            continue
        print("[" + name + "] " + str(len(items)) + " items")
        for it in items:
            if it["id"] in known:
                continue
            known.add(it["id"])
            seen.append(it["id"])
            stats["checked"] += 1
            why = feed_reasons(it, cfg)
            if why and not first_run:
                say("Possible price error or great deal\n\n" + it["title"] + "\n\nWhy: " + "; ".join(why)
                    + "\nFrom: " + name + "\n\n" + it["link"])
 
    # 2. shop pages
    for p in cfg.get("pages", []):
        if not p.get("url") or not p.get("max_price"):
            continue
        name = p.get("name", p["url"])
        key = p["url"]
        last = page_state.get(key, {})
        every = int(p.get("every_minutes", cfg.get("page_every_minutes", 30)))
        last_time = last.get("checked_at", 0)
        if time.time() - last_time < every * 60 - 60:
            continue
        try:
            lowest, status = check_page(p, cfg)
        except Exception as e:
            lowest, status = None, "blocked or failed (" + type(e).__name__ + ": " + str(e)[:60] + ")"
            stats["problems"].append(name + ": " + status)
        print("[page] " + name + ": " + status)
        stats["checked"] += 1
        alerted_at = last.get("alerted_price")
        if lowest is not None and lowest <= float(p["max_price"]) and lowest != alerted_at:
            say("Shop page alert\n\n" + name + ": cheapest item now " + money(lowest)
                + " (your limit " + money(float(p["max_price"])) + ")\n\n" + p["url"], key, lowest)
        page_state[key] = {"checked_at": time.time(), "status": status, "alerted_price": alerted_at}
 
    # 3. send alerts
    sent = 0
    for msg, page_key, page_price in out:
        if dry:
            print("\nALERT:\n" + msg)
            continue
        try:
            send(msg, token, chat)
            sent += 1
            if page_key:
                page_state[page_key]["alerted_price"] = page_price
            time.sleep(1)
        except Exception as e:
            print("Could not send to Telegram: " + str(e))
    stats["alerts"] += sent
 
    # 4. daily summary (and an early warning if every feed failed)
    t = now_uk()
    today = t.strftime("%Y-%m-%d")
    feeds_failed = bool(cfg.get("feeds")) and feed_failures == len(cfg["feeds"])
    if t.hour == int(cfg.get("summary_hour", 8)) and state.get("summary_sent") != today:
        lines = ["Daily price alerts summary",
                 "Checked: " + str(stats["checked"]) + " deals and pages since the last summary",
                 "Alerts sent: " + str(stats["alerts"])]
        if page_state:
            lines.append("\nShop pages:")
            for pc in cfg.get("pages", []):
                if pc.get("url") in page_state:
                    lines.append("- " + pc.get("name", pc["url"]) + ": " + page_state[pc["url"]]["status"])
        problems = sorted(set(stats["problems"]))[:8]
        if problems:
            lines.append("\nProblems:")
            lines += ["- " + pr for pr in problems]
        if not dry and token and chat:
            try:
                send("\n".join(lines), token, chat)
            except Exception as e:
                print("Could not send summary: " + str(e))
        else:
            print("\n".join(lines))
        state["summary_sent"] = today
        stats = {"checked": 0, "alerts": 0, "problems": []}
    elif feeds_failed and state.get("warned") != today and not dry and token and chat:
        try:
            send("Price alerts warning: none of the deal feeds could be read this run. "
                 "Details will be in the daily summary.", token, chat)
            state["warned"] = today
        except Exception:
            pass
 
    stats["problems"] = stats["problems"][-50:]
    state.update({"ids": seen[-int(cfg.get("remember_last", 4000)):], "stats": stats, "pages": page_state})
    if not dry:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    print("Done. Alerts: " + str(len(out)) + ", sent: " + str(sent) + (" (first run: recorded only)" if first_run else ""))
    return 0
 
 
if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # last resort: report instead of a silent red cross
        print("Unexpected problem: " + repr(e))
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if tok and cid:
            try:
                send("Price alerts hit a problem: " + repr(e)[:300], tok, cid)
            except Exception:
                pass
        sys.exit(0)
 
