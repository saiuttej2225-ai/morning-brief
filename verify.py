#!/usr/bin/env python3
"""Cross-check a story: how many independent outlets reported it recently, and is the link alive.

Usage:
  python3 verify.py "Gemini 4 Carbon"            # outlets covering it in the last 3 days (Google News)
  python3 verify.py "Gemini 4 Carbon" --days 7
  python3 verify.py --url https://example.com/post # does the link load? final URL + HTTP status
Standard library only. Prints JSON.
"""
import json, re, sys, html, argparse, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read()


def outlets(query, days):
    q = urllib.parse.quote_plus("%s when:%dd" % (query, days))
    url = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % q
    _, _, raw = fetch(url)
    root = ET.fromstring(raw)
    rows, names = [], {}
    for it in root.iter("item"):
        src = it.find("source")
        name = (src.text or "").strip() if src is not None else ""
        site = src.attrib.get("url", "") if src is not None else ""
        title = html.unescape(it.findtext("title") or "")
        rows.append({"outlet": name, "site": site, "title": title, "date": it.findtext("pubDate")})
        if name:
            names.setdefault(name, site)
    return {"query": query, "days": days, "independent_outlets": len(names),
            "outlets": list(names)[:25], "headlines": rows[:12], "all_headlines": rows}


STOP = set("the and for with from that this are was were has have had its into over after about will new says say how why what who your you our out not but can could would more than just they their them been being also amid per via".split())
RUMOR = re.compile(r"\b(rumou?rs?|leak(s|ed)?|reportedly|allegedly|unconfirmed|said to|sources? say|insiders?|speculat\w*)\b", re.I)


def _toks(s):
    return set(w for w in re.findall(r"[a-z0-9][a-z0-9.+-]*", s.lower()) if len(w) > 2 and w not in STOP)


def same_story(query, headlines, own_outlet="", min_shared=3, min_ratio=0.5):
    """Count outlets whose headline covers the SAME story as `query`.

    A headline counts only if it shares at least `min_shared` distinctive words with the query
    and at least `min_ratio` of the shorter headline's words. Outlets are de-duplicated by name.
    Returns matching outlet names, and whether any headline uses rumor wording."""
    q = _toks(re.sub(r" - [^-]{2,40}$", "", query))
    matched, rumor = {}, bool(RUMOR.search(query))
    for h in headlines:
        title = re.sub(r" - [^-]{2,40}$", "", h["title"])
        t = _toks(title)
        if not q or not t:
            continue
        shared = len(q & t)
        if shared >= min(min_shared, len(q)) and shared / min(len(q), len(t)) >= min_ratio:
            name = (h.get("outlet") or "").strip().lower()
            if name and name not in matched:
                matched[name] = h["outlet"]
            if RUMOR.search(title):
                rumor = True
    names = list(matched.values())
    if own_outlet and own_outlet.strip().lower() not in matched:
        names.append(own_outlet)  # the original source counts as one outlet
    return {"outlets": len(names), "names": names[:15], "rumor_wording": rumor}


def check_url(u):
    try:
        status, final, body = fetch(u)
        t = re.search(rb"<title[^>]*>(.*?)</title>", body[:200000], re.S | re.I)
        return {"url": u, "status": status, "final_url": final,
                "page_title": html.unescape(t.group(1).decode("utf-8", "ignore").strip())[:200] if t else ""}
    except Exception as e:
        return {"url": u, "status": "error", "error": str(e)[:160]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--url")
    a = ap.parse_args()
    if a.url:
        print(json.dumps(check_url(a.url), indent=1, ensure_ascii=False))
    elif a.query:
        try:
            print(json.dumps(outlets(a.query, a.days), indent=1, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"query": a.query, "error": str(e)[:200]}))
    else:
        ap.print_help()
