"""RSS থেকে খবর আনে, Gemini (ফ্রি) দিয়ে বিসিএস পড়ার উপকরণ বানায়, ৪৮ ঘণ্টার পুরনো খবর মুছে দেয়।"""
import hashlib, json, os, re, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import zip_longest

KEY = os.environ.get("GEMINI_API_KEY", "")
MODELS = []  # খালি থাকলে চালুর সময় API থেকে খুঁজে নেওয়া হয়
dead = set()  # এই রানে যে মডেলের কোটা শেষ
MAX_NEW = int(os.environ.get("MAX_NEW", "6"))  # প্রতিবার সর্বোচ্চ নতুন খবর
MAX_AGE = timedelta(hours=48)
NOW = datetime.now(timezone.utc)

PROMPT = """তুমি বিসিএস প্রিলি ও লিখিত পরীক্ষার অভিজ্ঞ প্রশ্নকারী। নিচের খবর থেকে পড়ার উপকরণ বানাও।
শুধু খবরে থাকা তথ্য ব্যবহার করো, অনুমানে কিছু বানাবে না। সব বাংলায় লেখো (প্রয়োজনীয় ইংরেজি নাম বাদে)।
খবরটি বিসিএসের জন্য অপ্রাসঙ্গিক (খেলা, বিনোদন, অপরাধ, ব্যক্তিগত ঘটনা) হলে শুধু {"relevant": false} দাও।
নইলে শুধু এই JSON দাও:
{"relevant": true, "title": "বাংলা শিরোনাম", "summary": "২-৩ বাক্যে নিজের ভাষায়",
 "key_facts": ["৬-১০টি পরীক্ষাযোগ্য তথ্য: নাম, সংখ্যা, তারিখ, স্থান, সংস্থা"],
 "mcqs": [{"q": "", "options": ["", "", "", ""], "answer": 0, "explain": ""}],
 "written": [{"q": "বিসিএস লিখিত পরীক্ষার সম্ভাব্য প্রশ্ন", "a": "৫-৮ বাক্যের উত্তর"}],
 "creative": [{"q": "বিশ্লেষণ বা মতামতভিত্তিক সৃজনশীল প্রশ্ন", "a": "উত্তর"}]}
mcqs: ১০টি (খবরে তথ্য বেশি থাকলে ১৫টি); answer হলো সঠিক অপশনের সূচক (০ থেকে ৩)। written: ৩টি। creative: ৩টি।

শিরোনাম: <<TITLE>>
পত্রিকা: <<SOURCE>>
খবর: <<TEXT>>"""


class Stop(Exception):
    pass


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal BCS study bot)"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def clean(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or "")).strip()


def when(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def first(e, *names):
    for n in names:  # (Element-এর "or" ব্যবহার করা যায় না: ফাঁকা Element মিথ্যা ধরা হয়)
        for c in e:
            if c.tag.split("}")[-1] == n:
                return c
    return None


def read_feed(f):
    out = []
    for e in ET.fromstring(get(f["url"])).iter():
        if e.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t, l, d, s = first(e, "title"), first(e, "link"), first(e, "pubDate", "published", "updated"), first(e, "description", "summary")
        link = ((l.get("href") or l.text or "").strip()) if l is not None else ""
        title = clean(t.text) if t is not None else ""
        if title and link:
            out.append({"title": title, "link": link, "source": f["name"], "category": f["category"],
                        "published": (when(d.text) if d is not None else None) or NOW,
                        "desc": clean(s.text)[:600] if s is not None else ""})
    return out


def article_text(url, fallback):
    try:  # শুধু AI-কে পড়ানোর জন্য; সংরক্ষণ বা প্রকাশ করা হয় না
        h = get(url, 20).decode("utf-8", "ignore")
        t = clean(re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", h))
        return t[:6000] if len(t) > 500 else fallback
    except Exception:
        return fallback


def pick_models():
    """API থেকে চালু Flash মডেল খোঁজে: Lite আগে (ফ্রি সীমা বেশি), তারপর বাকিগুলো; নতুন মডেল আগে।"""
    names = []
    try:
        r = json.load(urllib.request.urlopen(f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={KEY}", timeout=30))
        for m in r.get("models", []):
            n = m["name"].split("/")[-1]
            if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in n \
                    and not re.search(r"image|tts|live|audio|embed|thinking|exp|8b|latest", n):
                names.append(n)
    except Exception as e:
        print("model list failed", e)
    names.sort(key=lambda n: ("lite" not in n, "preview" in n, [-int(x) for x in re.findall(r"\d+", n)]))
    pin = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else []
    return pin + names + ["gemini-flash-lite-latest", "gemini-flash-latest"]


def ask(prompt):
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}).encode()
    for m in MODELS:
        if m in dead:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={KEY}"
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
                t = json.load(urllib.request.urlopen(req, timeout=120))["candidates"][0]["content"]["parts"][0]["text"]
                print("used", m)
                return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip()))
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")
                print("Gemini", m, e.code, re.sub(r"\s+", " ", msg)[:300])  # আসল কারণ লগে দেখা যাবে
                if e.code in (401, 403) or "API key" in msg:
                    raise Stop("API কী সমস্যা")
                wait = re.search(r'"retryDelay":\s*"(\d+)', msg)
                if e.code == 429 and "PerDay" not in msg and wait and int(wait.group(1)) <= 90 and attempt < 2:
                    time.sleep(int(wait.group(1)) + 2)  # মিনিটের সীমা: একটু অপেক্ষা
                    continue
                dead.add(m)  # দিনের কোটা শেষ বা মডেল চলছে না: পরের মডেল
                break
            except Exception as e:
                print("error", e)
                time.sleep(5)
    if all(m in dead for m in MODELS):
        raise Stop("সব মডেলের ফ্রি কোটা শেষ (কোটা বাংলাদেশ সময় প্রায় দুপুর ২টায় নতুন হয়)")
    return None


def valid(d):
    try:
        ok = []
        for m in d["mcqs"]:
            m["answer"] = int(m["answer"])
            if len(m["options"]) == 4 and 0 <= m["answer"] <= 3:
                ok.append(m)
        d["mcqs"] = ok
        return len(ok) >= 8 and len(d["key_facts"]) >= 3
    except Exception:
        return False


def main():
    db = json.load(open("news.json", encoding="utf-8")) if os.path.exists("news.json") else {}
    items = [i for i in db.get("items", []) if NOW - when(i["published"]) < MAX_AGE]  # ২ দিনের পুরনো খবর বাদ
    seen = {k: v for k, v in db.get("seen", {}).items() if NOW - when(v) < MAX_AGE + timedelta(days=1)}
    pool = []
    for f in json.load(open("feeds.json", encoding="utf-8")):
        try:
            got = sorted((x for x in read_feed(f) if NOW - x["published"] < MAX_AGE), key=lambda x: x["published"], reverse=True)
            pool.append(got[:5])
            print("ok", f["name"], len(got))
        except Exception as e:
            print("feed failed", f["name"], e)
    note, done, tried = "", 0, 0
    for x in (y for row in zip_longest(*pool) for y in row if y):  # সব পত্রিকা পালা করে
        if done >= MAX_NEW or tried >= MAX_NEW * 2:
            break
        i = hashlib.md5(x["link"].encode()).hexdigest()[:12]
        if i in seen or any(k["id"] == i for k in items):
            continue
        if not KEY:
            note = "GEMINI_API_KEY দেওয়া হয়নি"
            break
        if not MODELS:
            MODELS[:] = pick_models()
            print("models:", MODELS)
        tried += 1
        try:
            d = ask(PROMPT.replace("<<TITLE>>", x["title"]).replace("<<SOURCE>>", x["source"])
                    .replace("<<TEXT>>", article_text(x["link"], x["title"] + ". " + x["desc"])))
        except Stop as e:
            note = f"নতুন খবর যোগ থেমে আছে: {e}"
            break
        if isinstance(d, dict) and d.get("relevant") is False:
            seen[i] = NOW.isoformat()
            continue
        if not isinstance(d, dict) or not valid(d):
            continue
        items.append({"id": i, "title": d.get("title") or x["title"], "source": x["source"], "category": x["category"],
                      "link": x["link"], "published": x["published"].isoformat(), "summary": d.get("summary", ""),
                      "key_facts": d["key_facts"], "mcqs": d["mcqs"], "written": d.get("written", []),
                      "creative": d.get("creative", [])})
        seen[i] = NOW.isoformat()
        done += 1
        time.sleep(8)  # ফ্রি সীমার মধ্যে থাকতে
    items.sort(key=lambda k: k["published"], reverse=True)
    json.dump({"updated": NOW.isoformat(), "note": note, "items": items, "seen": seen},
              open("news.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"added {done}, total {len(items)} {note}")


if __name__ == "__main__":
    main()
