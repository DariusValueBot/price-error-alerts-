import json, os, re, sys, time, html
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
 
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SEEN = os.path.join(HERE, "seen.json")
AGENT = "price-error-alerts/1.1"
NUM = r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
PRICE = re.compile("£" + r"\s?" + NUM)
WAS = re.compile(r"(?:was|rrp|usually|normally)\s*:?\s*" + "£" + r"\s?" + NUM, re.I)
TAGS = re.compile(r"<[^>]+>")
TG = "https" + ":" + "//" + "api.telegram.org" + "/bot"
 
 
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default
 
 
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()
 
 
def txt(el, names):
    for n in names:
        x = el.find(n)
        if x is not None:
            if x.text:
                return x.text.strip()
            if x.get("href"):
                return x.get("href").strip()
    return ""
 
 
def parse(raw):
    a = "{" + "http" + ":" + "//" + www.w3.org/2005/Atom + "}"
    root = ET.fromstring(raw)
    out = []
    for n in root.findall(".//item") or root.findall(".//" + a + "entry"):
        title = html.unescape(txt(n, ["title", a + "title"]))
        link = txt(n, ["link", a + "link"])
        key = txt(n, ["guid", a + "id"]) or link or title
        summ = html.unescape(TAGS.sub(" ", txt(n, ["description", a + "summary"])))
        out.append({"id": key, "title": title, "link": link, "summary": " ".join(summ.split())})
    return out
 
 
def num(s):
    return float(s.replace(",", ""))
 
 
def has(word, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", text) is not None
 
 
def reasons_for(item, cfg):
    text = (item["title"] + " " + item["summary"]).lower()
    if any(has(w, text) for w in cfg.get("ignore_words", [])):
        return []
    why = []
    for w in cfg.get("error_words", []):
        if has(w, text):
            why.append("mentions " + w)
            break
    m = PRICE.search(item["title"]) or PRICE.search(item["summary"])
    if m:
        price = num(m.group(1))
        for name, limit in cfg.get("watch", {}).items():
            if has(name, text) and price <= float(limit):
                why.append(name + " at £" + format(price, ",.2f") + " (limit £" + str(limit) + ")")
                break
        w = WAS.search(text)
        if w:
            was = num(w.group(1))
            if was >= cfg.get("min_was_price", 50) and price <= was * cfg.get("max_share_of_was_price", 0.2):
                why.append("£" + format(price, ",.2f") + " was £" + format(was, ",.2f"))
    return why
 
 
def send(text, token, chat):
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(TG + token + "/sendMessage", data=data, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        if not json.loads(r.read()).get("ok"):
            raise RuntimeError("Telegram said no")
 
 
def main(args):
    cfg = load(CONFIG, None)
    if cfg is None:
        print("config.json is missing or not valid JSON.")
        return 1
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    dry = "--dry-run" in args
    if not dry and (not token or not chat):
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set.")
        return 1
    if "--test" in args:
        send("Test from your price error alerts. Telegram is set up.", token, chat)
        print("Test message sent.")
        return 0
    seen = load(SEEN, {"ids": []}).get("ids", [])
    known = set(seen)
    first = not seen
    sent = 0
    for feed in cfg.get("feeds", []):
        name = feed.get("name", feed["url"])
        try:
            items = parse(get(feed["url"]))
        except Exception as e:
            print("[" + name + "] could not read feed: " + str(e))
            continue
        print("[" + name + "] " + str(len(items)) + " items")
        for it in items:
            if it["id"] in known:
                continue
            known.add(it["id"])
            seen.append(it["id"])
            why = reasons_for(it, cfg)
            if not why:
                continue
            msg = "Possible price error\n\n" + it["title"] + "\n\nWhy: " + "; ".join(why) + "\nFound on: " + name + "\n\n" + it["link"]
            if dry:
                print("ALERT: " + msg)
            elif first and cfg.get("skip_alerts_on_first_run", True):
                print("(first run, not sent) " + it["title"])
            else:
                try:
                    send(msg, token, chat)
                    sent += 1
                    time.sleep(1)
                except Exception as e:
                    print("Could not send: " + str(e))
    if not dry:
        with open(SEEN, "w", encoding="utf-8") as f:
            json.dump({"ids": seen[-3000:]}, f)
    print("Done. Alerts sent: " + str(sent))
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
