#!/usr/bin/env python3
"""Collect fresh candidate stories for the daily AI + PM brief.

Usage: python3 collect.py [--hours N] [--out PATH]
Reads sources.json, interests.json and memory/sent_log.json from this folder.
Writes candidates JSON (default /home/user/brief/candidates.json) and prints a compact list.
Standard library only.
"""
import json, os, re, sys, html, time, argparse, datetime as dt
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
TYPE_WEIGHT = {"official": 3.0, "news": 2.0, "pm": 2.5, "data": 1.5, "launches": 1.0, "community": 1.5, "analysis": 2.0}


def load(name, default):
    p = os.path.join(HERE, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(s, n=320):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def strip_ns(tag):
    return tag.split("}", 1)[-1].lower()


def parse_feed(raw):
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raw = re.sub(rb"&(?!amp;|lt;|gt;|quot;|apos;|#)", b"&amp;", raw)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return items
    for el in root.iter():
        t = strip_ns(el.tag)
        if t not in ("item", "entry"):
            continue
        rec = {"title": "", "link": "", "date": None, "summary": ""}
        for c in el:
            ct = strip_ns(c.tag)
            if ct == "title":
                rec["title"] = clean("".join(c.itertext()), 200)
            elif ct == "link":
                href = c.attrib.get("href")
                rel = c.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    rec["link"] = href
                elif not href and (c.text or "").strip():
                    rec["link"] = c.text.strip()
            elif ct in ("pubdate", "published", "updated", "date") and not rec["date"]:
                rec["date"] = parse_date(c.text)
            elif ct in ("description", "summary", "content", "encoded") and not rec["summary"]:
                rec["summary"] = clean("".join(c.itertext()))
        if rec["title"] and rec["link"]:
            items.append(rec)
    return items


def norm_url(u):
    try:
        p = urllib.parse.urlsplit(u)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((p.scheme, p.netloc.lower().replace("www.", ""), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))
    except Exception:
        return u


def words(s):
    return set(w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2)


def kw_regex(kw):
    k = re.escape(kw)
    return re.compile(r"(?<![A-Za-z0-9])" + k + r"(?![A-Za-z0-9])", 0 if kw.isupper() or len(kw) <= 3 else re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--out", default="/home/user/brief/candidates.json")
    a = ap.parse_args()

    sources = load("sources.json", {})
    interests = load("interests.json", {"topics": []})
    state = load("memory/state.json", {})
    sent = load("memory/sent_log.json", [])

    now = dt.datetime.now(dt.timezone.utc)
    hours = a.hours
    if hours is None:
        hours = 30.0
        last = parse_date(state.get("last_run_utc")) if state.get("last_run_utc") else None
        if last:
            hours = min(72.0, max(24.0, (now - last).total_seconds() / 3600 + 2))
    since = now - dt.timedelta(hours=hours)

    sent_urls = set()
    sent_titles = []
    for day in sent:
        for it in day.get("items", []):
            sent_urls.add(norm_url(it.get("url", "")))
            sent_titles.append(words(it.get("title", "")))

    topics = [(t["id"], t["name"], [kw_regex(k) for k in t["keywords"]]) for t in interests.get("topics", [])]

    feeds = sources.get("feeds", [])
    stats = {"ok": [], "failed": []}

    def get_feed(f):
        try:
            return f, parse_feed(fetch(f["url"])), None
        except Exception as e:
            return f, [], str(e)[:80]

    def get_hn(q):
        url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode({
            "tags": "story", "query": q,
            "numericFilters": "points>%d,created_at_i>%d" % (sources.get("hn_min_points", 80), int(since.timestamp()))})
        try:
            d = json.loads(fetch(url))
            out = []
            for h in d.get("hits", []):
                if not h.get("url"):
                    continue
                out.append({"title": h.get("title", ""), "link": h["url"],
                            "date": dt.datetime.fromtimestamp(h["created_at_i"], dt.timezone.utc),
                            "summary": "Hacker News: %d points, %d comments (https://news.ycombinator.com/item?id=%s)" % (h.get("points", 0), h.get("num_comments", 0) or 0, h.get("objectID")),
                            "points": h.get("points", 0)})
            return q, out, None
        except Exception as e:
            return q, [], str(e)[:80]

    raw = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for f, items, err in ex.map(get_feed, feeds):
            if err:
                stats["failed"].append(f["name"] + ": " + err)
                continue
            stats["ok"].append(f["name"])
            for it in items:
                it["source"] = f["name"]; it["type"] = f["type"]
                raw.append(it)
        for q, items, err in ex.map(get_hn, sources.get("hn_queries", [])):
            if err:
                stats["failed"].append("HN '" + q + "': " + err)
                continue
            for it in items:
                it["source"] = "Hacker News"; it["type"] = "community"
                raw.append(it)

    official = sources.get("official_domains", [])
    seen, cands = set(), []
    for it in raw:
        d = it.get("date")
        if not d or d < since or d > now + dt.timedelta(hours=14):
            continue
        nu = norm_url(it["link"])
        if nu in seen or nu in sent_urls:
            continue
        tw = words(it["title"])
        if tw and any(len(tw & s) / max(1, len(tw | s)) > 0.6 for s in sent_titles):
            continue
        seen.add(nu)
        text = it["title"] + " " + it.get("summary", "")
        hits = {}
        for tid, tname, regs in topics:
            sc = 0
            for r in regs:
                if r.search(it["title"]):
                    sc += 3
                elif r.search(text):
                    sc += 1
            if sc:
                hits[tname] = sc
        rel = sum(hits.values())
        score = rel * TYPE_WEIGHT.get(it["type"], 1.0)
        if it.get("points"):
            score += min(it["points"], 1000) / 60.0
        if it["type"] in ("pm", "analysis", "official"):
            score += 2
        host = urllib.parse.urlsplit(it["link"]).netloc.lower().replace("www.", "")
        is_official = any(host == od or host.endswith("." + od) for od in official)
        cands.append({
            "title": it["title"], "url": it["link"], "source": it["source"], "type": it["type"],
            "published_utc": d.strftime("%Y-%m-%d %H:%M"), "summary": it.get("summary", ""),
            "topics": sorted(hits, key=hits.get, reverse=True), "score": round(score, 1),
            "official_domain": is_official,
        })

    # merge near-duplicate titles (same story from several feeds): keep best, record other sources
    cands.sort(key=lambda c: c["score"], reverse=True)
    merged = []
    for c in cands:
        cw = words(c["title"])
        dup = None
        for m in merged:
            mw = words(m["title"])
            if cw and mw and len(cw & mw) / len(cw | mw) > 0.5:
                dup = m; break
        if dup:
            dup.setdefault("also_seen_in", []).append(c["source"] + " | " + c["url"])
        else:
            merged.append(c)

    out = {"generated_utc": now.strftime("%Y-%m-%d %H:%M"), "window_hours": round(hours, 1),
           "sources_ok": len(stats["ok"]), "sources_failed": stats["failed"],
           "scanned": len(raw), "candidates": merged[:120]}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("Window: last %.0fh | feeds ok: %d | failed: %d | items scanned: %d | candidates: %d" % (
        hours, len(stats["ok"]), len(stats["failed"]), len(raw), len(merged)))
    for s in stats["failed"]:
        print("  FAILED:", s)
    def show(lst, start=1):
        for i, c in enumerate(lst, start):
            flag = " [OFFICIAL]" if c["official_domain"] else ""
            also = " (+%d more sources)" % len(c.get("also_seen_in", [])) if c.get("also_seen_in") else ""
            print("%2d. [%s] %s | %s%s%s\n    %s | %s" % (i, c["score"], c["title"], c["source"], flag, also,
                  ", ".join(c["topics"]) or "-", c["url"]))

    print("\n=== TOP OVERALL ===")
    top = merged[:45]
    show(top)
    shown = set(id(c) for c in top)
    for label, types in (("PRODUCT MANAGEMENT / CAREER", ("pm",)), ("NEW PRODUCT LAUNCHES (Product Hunt)", ("launches",)),
                         ("DATA & ANALYTICS", ("data",)), ("ANALYSIS NEWSLETTERS", ("analysis",))):
        extra = [c for c in merged if c["type"] in types and id(c) not in shown][:12]
        if extra:
            print("\n=== MORE: %s ===" % label)
            show(extra)
    print("\nFull details (summaries, also_seen_in):", a.out)


if __name__ == "__main__":
    main()
