#!/usr/bin/env python3
"""Daily personalised news brief, run by GitHub Actions.

For every subscriber (job title + topics) it:
  1. gathers fresh stories (shared RSS feeds + Google News searches for their title and topics)
  2. ranks them against that person's keywords
  3. fact-checks the best ones with verify.py (how many outlets reported it, is the link alive)
  4. asks Gemini to pick 8-10 and write one plain sentence each
  5. emails an HTML brief through Gmail SMTP

Environment variables (GitHub Secrets):
  GEMINI_API_KEY      Google AI Studio key (free tier is enough)
  GMAIL_ADDRESS       sender address, e.g. you@gmail.com
  GMAIL_APP_PASSWORD  Gmail app password (needs 2-step verification)
  SUBSCRIBERS_CSV_URL published-CSV link of the Google Sheet behind the signup form
Optional: GEMINI_MODEL, DRY_RUN=1 (write previews to ./preview instead of sending), ONLY_EMAIL, SITE_URL
Standard library only.
"""
import csv, hashlib, html, io, json, os, re, smtplib, ssl, sys, time, urllib.parse, urllib.request
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import collect
import verify

HERE = os.path.dirname(os.path.abspath(__file__))
SENT_PATH = os.path.join(HERE, "state", "sent.json")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
FALLBACK_MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
SITE_URL = os.environ.get("SITE_URL", "")
DRY = os.environ.get("DRY_RUN") == "1"
MAX_ITEMS = 10

# What each topic checkbox on the signup form means (id -> label, search keywords)
TOPICS = {
    "AI models & research": ["LLM", "GPT", "Gemini", "Claude", "Llama", "DeepSeek", "OpenAI", "Anthropic", "DeepMind", "benchmark", "open-source model"],
    "New AI tools & launches": ["launch", "launches", "introducing", "now available", "beta", "AI agent", "agents", "copilot", "API", "SDK"],
    "AI industry & policy": ["funding", "raises", "acquisition", "regulation", "AI Act", "lawsuit", "pricing", "layoffs", "IPO", "antitrust"],
    "Product management": ["product manager", "product management", "roadmap", "product strategy", "discovery", "prioritization", "user research", "retention", "metrics", "A/B test"],
    "Data & analytics": ["data analyst", "analytics", "SQL", "dashboard", "Power BI", "Tableau", "dbt", "Snowflake", "Databricks", "BigQuery", "pandas"],
    "Software engineering": ["software engineer", "developer", "programming", "open source", "GitHub", "Kubernetes", "TypeScript", "Rust", "Python", "DevOps"],
    "Cybersecurity": ["cybersecurity", "vulnerability", "breach", "ransomware", "zero-day", "CVE", "security patch"],
    "Startups & funding": ["startup", "Series A", "Series B", "seed round", "venture capital", "unicorn", "Y Combinator"],
    "Careers & hiring": ["hiring", "layoffs", "job market", "salary", "remote work", "interview", "career", "recruiting"],
    "Finance & markets": ["stocks", "earnings", "Federal Reserve", "inflation", "interest rates", "fintech", "markets"],
    "Healthcare & biotech": ["healthcare", "biotech", "FDA", "clinical trial", "medical AI", "drug approval"],
    "Design & UX": ["UX", "UI design", "Figma", "design system", "accessibility", "user experience"],
    "Marketing & growth": ["marketing", "SEO", "growth", "advertising", "brand", "social media", "content marketing"],
}
STOP = set("a an and the of for to in at on with senior junior lead principal staff associate jr sr i ii iii".split())


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- subscribers
def parse_subscribers(text):
    """Latest row per email wins. Columns are found by header name so form edits don't break it."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for i, h in enumerate(head):
            if any(n in h for n in names):
                return i
        return None

    ie, ij, it, ia, inm = col("email"), col("job", "title", "role"), col("topic", "interest"), col("action", "subscribe"), col("name")
    if ie is None:
        raise SystemExit("subscriber sheet has no Email column")
    latest = {}
    for r in rows[1:]:
        if len(r) <= ie or not r[ie].strip():
            continue
        g = lambda i: r[i].strip() if i is not None and i < len(r) else ""
        email = g(ie).lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            continue
        latest[email] = {"email": email, "name": g(inm), "job": g(ij), "topics_raw": g(it), "action": g(ia).lower()}
    out = []
    for s in latest.values():
        if "unsub" in s["action"]:
            continue
        s["topics"] = [t.strip() for t in re.split(r"[,;\n]", s["topics_raw"]) if t.strip()]
        out.append(s)
    return out


def load_subscribers():
    url = os.environ.get("SUBSCRIBERS_CSV_URL", "").strip()
    if url:
        text = collect.fetch(url).decode("utf-8-sig", "replace")
    else:
        with open(os.path.join(HERE, "subscribers.example.csv"), encoding="utf-8") as f:
            text = f.read()
    subs = parse_subscribers(text)
    only = os.environ.get("ONLY_EMAIL", "").strip().lower()
    if only:
        subs = [s for s in subs if s["email"] == only]
    return subs


# ---------------------------------------------------------------- stories
def keywords_for(sub):
    """Return list of (keyword, weight)."""
    kws = []
    for w in re.findall(r"[A-Za-z0-9+#.]+", sub["job"]):
        if w.lower() not in STOP and len(w) > 1:
            kws.append((w, 2))
    if sub["job"]:
        kws.append((sub["job"], 4))
    for t in sub["topics"]:
        for k in TOPICS.get(t, [t]):
            kws.append((k, 1))
        if t not in TOPICS:  # free-text topic typed by the user
            kws.append((t, 4))
    return kws


def google_news(query, days=2):
    q = urllib.parse.quote_plus("%s when:%dd" % (query, days))
    url = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % q
    try:
        items = collect.parse_feed(collect.fetch(url))
    except Exception as e:
        log("  google news failed for", query, str(e)[:60])
        return []
    for it in items:
        it["source"] = "Google News"
        it["type"] = "news"
        # Google News titles end with " - Outlet"
        m = re.match(r"^(.*) - ([^-]{2,40})$", it["title"])
        if m:
            it["title"], it["source"] = m.group(1), m.group(2)
    return items


def build_pool(hours):
    sources = collect.load("sources.json", {})
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=hours)
    pool = []

    def get(f):
        try:
            return f, collect.parse_feed(collect.fetch(f["url"])), None
        except Exception as e:
            return f, [], str(e)[:80]

    failed = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for f, items, err in ex.map(get, sources.get("feeds", [])):
            if err:
                failed += 1
                continue
            for it in items:
                it["source"], it["type"] = f["name"], f["type"]
                pool.append(it)
    log("shared feeds: %d ok, %d failed, %d items" % (len(sources.get("feeds", [])) - failed, failed, len(pool)))
    return pool, since, now, sources.get("official_domains", [])


def candidates_for(sub, pool, since, now, official, sent_urls):
    items = list(pool)
    queries = []
    if sub["job"]:
        queries += [sub["job"] + " news", sub["job"] + " hiring OR skills OR salary"]
    queries += [t for t in sub["topics"]][:6]
    for q in queries:
        items += google_news(q)
    kws = [(collect.kw_regex(k), w) for k, w in keywords_for(sub)]
    seen, out = set(), []
    for it in items:
        d = it.get("date")
        if not d or d < since or d > now + dt.timedelta(hours=14):
            continue
        nu = collect.norm_url(it["link"])
        if nu in seen or nu in sent_urls:
            continue
        seen.add(nu)
        text = it["title"] + " " + it.get("summary", "")
        score = 0
        for rx, w in kws:
            if rx.search(it["title"]):
                score += 3 * w
            elif rx.search(text):
                score += w
        if it["source"] == "Google News" or it["type"] == "news":
            score += 1
        if score <= 0:
            continue
        host = urllib.parse.urlsplit(it["link"]).netloc.lower().replace("www.", "")
        is_off = any(host == o or host.endswith("." + o) for o in official)
        if is_off:
            score += 3
        out.append({"title": it["title"], "url": it["link"], "source": it["source"], "official": is_off,
                    "published": d.strftime("%Y-%m-%d"), "summary": it.get("summary", "")[:300], "score": score})
    out.sort(key=lambda c: c["score"], reverse=True)
    merged = []
    for c in out:  # drop near-duplicate headlines
        cw = collect.words(c["title"])
        if any(cw and len(cw & collect.words(m["title"])) / len(cw | collect.words(m["title"])) > 0.5 for m in merged):
            continue
        merged.append(c)
    return merged[:14]


def fact_check(cands):
    def one(c):
        try:
            r = verify.outlets(re.sub(r"[\"']", "", c["title"])[:110], 3)
            c["outlets"] = r["independent_outlets"]
        except Exception:
            c["outlets"] = 0
        return c
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(one, cands))
    for c in cands:
        if c["official"]:
            c["label"] = "Official"
        elif c["outlets"] >= 2:
            c["label"] = "Confirmed (%d outlets)" % c["outlets"]
        else:
            c["label"] = "Single source"
    return cands


# ---------------------------------------------------------------- Gemini
def gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}}).encode()
    for model in [MODEL] + FALLBACK_MODELS:
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key)
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    d = json.loads(r.read())
                return json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
            except urllib.error.HTTPError as e:
                code = e.code
                if code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                if code in (400, 404):
                    break  # unknown model or bad request: try the next model
                time.sleep(5)
            except Exception:
                time.sleep(5)
    return None


def write_brief(sub, cands):
    lines = []
    for i, c in enumerate(cands):
        lines.append("%d. [%s] %s | %s | %s\n   %s" % (i, c["label"], c["title"], c["source"], c["published"], c["summary"]))
    prompt = (
        "You write a short daily news email for one person.\n"
        "Reader: %s. Job title / field: %s. Topics: %s.\n"
        "Below are numbered candidate stories with a fact-check label. Pick the %d most useful for THIS reader "
        "(news about their job title or field first, then their topics). Skip anything off-topic, duplicated or clickbait.\n"
        "For each pick write ONE plain-English sentence (max 30 words) on what happened, and, only if obvious, a short 'why it matters' (max 15 words) for their role. "
        "Do not invent facts beyond the headline and snippet. No em dashes.\n"
        "Return JSON: {\"intro\": \"one friendly sentence\", \"items\": [{\"id\": number, \"what\": string, \"why\": string}]}\n\n"
        "STORIES:\n%s" % (sub["name"] or "a reader", sub["job"] or "general", ", ".join(sub["topics"]) or "general tech",
                          MAX_ITEMS, "\n".join(lines)))
    res = gemini(prompt)
    picks = []
    if res and isinstance(res.get("items"), list):
        for p in res["items"]:
            try:
                c = cands[int(p["id"])]
            except Exception:
                continue
            picks.append((c, str(p.get("what", "")).strip(), str(p.get("why", "")).strip()))
        intro = str(res.get("intro", "")).strip()
    if not picks:  # no AI available: plain list from the snippets
        intro = "Here are today's top stories for you."
        picks = [(c, c["summary"][:200], "") for c in cands[:MAX_ITEMS]]
    return intro, picks[:MAX_ITEMS + 2]


# ---------------------------------------------------------------- email
BADGE = {"Official": "#0b6b3a", "Single": "#8a5a00", "Confirmed": "#1a56b0"}


def render(sub, intro, picks, today):
    e = html.escape
    rows = []
    for c, what, why in picks:
        color = BADGE.get(c["label"].split()[0], "#555")
        rows.append(
            '<tr><td style="padding:14px 0;border-bottom:1px solid #eee">'
            '<a href="%s" style="font-size:16px;font-weight:600;color:#111;text-decoration:none">%s</a><br>'
            '<span style="font-size:12px;color:%s;font-weight:600">%s</span>'
            '<span style="font-size:12px;color:#777"> &middot; %s &middot; %s</span>'
            '<div style="font-size:14px;color:#333;margin-top:4px;line-height:1.45">%s%s</div></td></tr>'
            % (e(c["url"]), e(c["title"]), color, e(c["label"]), e(c["source"]), e(c["published"]), e(what),
               (' <span style="color:#666">Why it matters: %s</span>' % e(why)) if why else ""))
    manage = ('<a href="%s">Change topics or unsubscribe</a>' % e(SITE_URL)) if SITE_URL else "Reply 'unsubscribe' to stop."
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto;color:#222">'
        '<h2 style="margin:0 0 4px">Your morning brief</h2>'
        '<div style="color:#777;font-size:13px;margin-bottom:12px">%s &middot; %s</div>'
        '<p style="font-size:14px">%s</p><table width="100%%" cellspacing="0" cellpadding="0">%s</table>'
        '<p style="font-size:12px;color:#888;margin-top:20px">Labels: Official = from the company itself. Confirmed = 2+ independent outlets in the last 3 days. '
        'Single source = only one outlet found, treat with care.<br>%s</p></div>'
        % (e(today), e(sub["job"] or "General"), e(intro), "".join(rows), manage))


def send(sub, subject, body_html):
    if DRY:
        os.makedirs(os.path.join(HERE, "preview"), exist_ok=True)
        p = os.path.join(HERE, "preview", hashlib.sha1(sub["email"].encode()).hexdigest()[:8] + ".html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body_html)
        log("  preview written:", p)
        return
    user, pw = os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, "Morning Brief <%s>" % user, sub["email"]
    msg.attach(MIMEText(re.sub(r"<[^>]+>", " ", body_html), "plain"))
    msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.sendmail(user, [sub["email"]], msg.as_string())


# ---------------------------------------------------------------- main
def main():
    subs = load_subscribers()
    log("subscribers:", len(subs))
    if not subs:
        return
    try:
        with open(SENT_PATH) as f:
            sent = json.load(f)
    except Exception:
        sent = {}
    pool, since, now, official = build_pool(36)
    today = now.astimezone(dt.timezone(dt.timedelta(hours=-5))).strftime("%A, %B %d")
    stats = {"date": now.strftime("%Y-%m-%d"), "subscribers": len(subs), "sent": 0, "failed": 0, "items": 0}
    for sub in subs:
        h = hashlib.sha256(sub["email"].encode()).hexdigest()[:16]
        sent_urls = set(sent.get(h, []))
        log("-", sub["job"] or "(no title)", "|", ", ".join(sub["topics"]))
        try:
            cands = candidates_for(sub, pool, since, now, official, sent_urls)
            if not cands:
                log("  nothing new, skipped")
                continue
            cands = fact_check(cands)
            intro, picks = write_brief(sub, cands)
            subject = "Morning brief: %s" % (sub["job"] or "your topics")
            send(sub, subject, render(sub, intro, picks, today))
            sent[h] = (list(sent_urls) + [collect.norm_url(c["url"]) for c, _, _ in picks])[-300:]
            stats["sent"] += 1
            stats["items"] += len(picks)
            log("  sent %d items" % len(picks))
        except Exception as e:
            stats["failed"] += 1
            log("  FAILED:", str(e)[:150])
        time.sleep(4)  # stay under free-tier rate limits
    if not DRY:
        os.makedirs(os.path.dirname(SENT_PATH), exist_ok=True)
        with open(SENT_PATH, "w") as f:
            json.dump(sent, f)
        sp = os.path.join(HERE, "state", "stats.json")
        try:
            with open(sp) as f:
                hist = json.load(f)
        except Exception:
            hist = []
        hist = (hist + [stats])[-60:]
        with open(sp, "w") as f:
            json.dump(hist, f, indent=1)
    log("done:", stats)


if __name__ == "__main__":
    main()
