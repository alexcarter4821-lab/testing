#!/usr/bin/env python3
"""
India NATIONAL news scraper.

Discovery per site (first that works wins):  RSS feed(s) -> sitemap -> listing page + URL pattern.
Fetching:  curl_cffi (Chrome TLS fingerprint) first; if blocked or empty, Scrapling's
StealthyFetcher (real stealth browser, solves Cloudflare) as a fallback.

Install:
    pip install curl_cffi feedparser trafilatura beautifulsoup4 lxml "scrapling[fetchers]"
    scrapling install            # one time: downloads the stealth browser

Usage:
    python india_news_scraper.py --check                  # test all sites, write site_report.json
    python india_news_scraper.py                          # scrape (5 new articles per site)
    python india_news_scraper.py --only ndtv,toi --limit 10
    python india_news_scraper.py --proxy http://user:pass@host:port   # only for sites with "proxy": true
"""
import argparse
import hashlib
import html as htmllib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import feedparser
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

SCRAPLING_IMPORT_ERROR = None
try:
    from scrapling.fetchers import StealthyFetcher
    HAVE_SCRAPLING = True
except Exception as _e:  # not installed or browsers missing
    StealthyFetcher = None
    HAVE_SCRAPLING = False
    SCRAPLING_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

HEADERS = {"Accept-Language": "en-IN,en;q=0.9,hi;q=0.8", "Referer": "https://www.google.com/"}
PROXY = os.environ.get("SCRAPER_PROXY")
BLOCK_MARKERS = ("just a moment", "attention required", "access denied",
                 "verify you are human", "enable javascript and cookies", "captcha",
                 "request blocked", "pardon our interruption")

# ------------------------------------------------------------------ helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def to_iso(value):
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).isoformat()
    except (TypeError, ValueError):
        return s


def clean_url(url):
    return urldefrag(url.strip())[0]


def make_id(url):
    return hashlib.sha1(clean_url(url).encode("utf-8")).hexdigest()[:16]


def clean_text(s):
    """Fix double-escaped entities such as &amp;#039; and &#8230;"""
    if not s:
        return s
    for _ in range(3):
        new = htmllib.unescape(s)
        if new == s:
            break
        s = new
    return s.strip()


def as_text(body):
    return body if isinstance(body, str) else (body or b"").decode("utf-8", "ignore")


def looks_blocked(status, body):
    if status != 200 or not body:
        return True
    sample = as_text(body)[:6000].lower()
    return len(sample) < 6000 and any(m in sample for m in BLOCK_MARKERS)


# ------------------------------------------------------------------ national-only filtering
# Sections that are NOT Indian national news. Used for general feeds / homepage fallbacks only.
NON_NATIONAL = {
    "sports", "sport", "cricket", "football", "ipl", "hockey", "tennis", "kabaddi",
    "entertainment", "bollywood", "movies", "movie", "tv", "television", "celebrity",
    "celebrities", "hollywood", "music", "ott", "lifestyle", "health", "fitness", "food",
    "recipes", "travel", "fashion", "beauty", "relationships", "astrology", "horoscope",
    "religion", "tech", "technology", "gadgets", "gaming", "games", "auto", "automobile",
    "cars", "business", "markets", "market", "money", "stocks", "finance", "personal-finance",
    "world", "international", "global", "opinion", "blog", "blogs", "photos", "photo",
    "videos", "video", "web-stories", "webstories", "viral", "trending", "offbeat",
    "education", "jobs", "career", "science", "lounge", "podcast", "shorts",
}
NAV_PATH = re.compile(
    r"/(category|categories|tag|tags|topic|topics|author|authors|page|search|about|about-us|"
    r"contact|contact-us|privacy|privacy-policy|terms|live-tv|epaper|rss|feed|subscribe|"
    r"advertise|careers|sitemap|cookie)(/|$)")


def as_list(x):
    return [x] if isinstance(x, str) else (x or [])


def is_national(item):
    """False if the URL directory or feed tags say sports/entertainment/business/world etc."""
    path = urlparse(item["url"]).path.lower().strip("/")
    for d in path.split("/")[:-1]:
        if d in NON_NATIONAL or d.split("-")[0] in NON_NATIONAL:
            return False
    for t in item.get("tags") or []:
        if t.lower().strip() in NON_NATIONAL:
            return False
    return True


def looks_like_article(path):
    """Heuristic when a site has no URL pattern: long slug or a long numeric id."""
    p = path.lower()
    if p in ("", "/") or NAV_PATH.search(p):
        return False
    last = re.sub(r"\.(html|htm|cms|php|aspx|ece|amp)$", "", p.rstrip("/").rsplit("/", 1)[-1])
    return bool(re.search(r"\d{5,}", p)) or (last.count("-") >= 3 and len(last) >= 20) \
        or ("%" in last and len(last) >= 30)


# ------------------------------------------------------------------ fetching
def fast_get(url, use_proxy=False, timeout=25, raw=False):
    proxies = {"http": PROXY, "https": PROXY} if (PROXY and use_proxy) else None
    try:
        r = cffi.get(url, headers=HEADERS, impersonate="chrome", timeout=timeout,
                     proxies=proxies, allow_redirects=True)
        return r.status_code, (r.content if raw else r.text)
    except Exception as e:
        return type(e).__name__, None


_STEALTH_ERROR_SHOWN = False


def stealth_get(url, use_proxy=False):
    """Scrapling StealthyFetcher: real browser, solves Cloudflare challenges."""
    global _STEALTH_ERROR_SHOWN
    if not HAVE_SCRAPLING:
        return "no-scrapling", None
    attempts = (dict(headless=True, network_idle=True, solve_cloudflare=True),
                dict(headless=True, network_idle=False))
    last_err = None
    for kw in attempts:
        if PROXY and use_proxy:
            kw = dict(kw, proxy=PROXY)
        try:
            page = StealthyFetcher.fetch(url, **kw)
            body = getattr(page, "html_content", None)
            if not body:
                raw = getattr(page, "body", b"")
                body = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
            return page.status, body
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:300]}"
    if not _STEALTH_ERROR_SHOWN:
        _STEALTH_ERROR_SHOWN = True
        print(f"  [stealth browser error] {last_err}")
    return "stealth-error", None


def fetch_page(url, site, stealth_ok=True, raw=False):
    """Returns (status, body, how) where how is 'fast', 'stealth' or 'blocked'."""
    use_proxy = bool(site.get("proxy"))
    st, body = fast_get(url, use_proxy, raw=raw)
    if not looks_blocked(st, body):
        return st, body, "fast"
    if stealth_ok:
        st2, body2 = stealth_get(url, use_proxy)
        if not looks_blocked(st2, body2):
            if raw and isinstance(body2, str):
                body2 = body2.encode("utf-8")
            return st2, body2, "stealth"
        if st2 != "no-scrapling":
            st = f"{st} + stealth:{st2}"
    return st, None, "blocked"


# ------------------------------------------------------------------ discovery
def parse_feed(body):
    items = []
    for e in feedparser.parse(body).entries:
        link = e.get("link")
        if not link:
            continue
        image = None
        if e.get("media_content"):
            image = e["media_content"][0].get("url")
        elif e.get("media_thumbnail"):
            image = e["media_thumbnail"][0].get("url")
        elif e.get("enclosures"):
            image = e["enclosures"][0].get("href")
        feed_content = None
        if e.get("content"):
            feed_content = BeautifulSoup(e["content"][0].get("value", ""), "lxml").get_text(" ", strip=True) or None
        items.append({
            "url": clean_url(link),
            "title": e.get("title"),
            "summary": BeautifulSoup(e.get("summary", ""), "lxml").get_text(" ", strip=True) or None,
            "author": e.get("author"),
            "published": e.get("published"),
            "updated": e.get("updated"),
            "image": image,
            "feed_content": feed_content,
            "tags": [t.get("term") for t in (e.get("tags") or []) if t.get("term")],
        })
    return items


def parse_sitemap(body, pattern=None):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    rx = re.compile(pattern) if pattern else None
    local = lambda t: t.rsplit("}", 1)[-1]
    items = []
    for url_el in root.iter():
        if local(url_el.tag) != "url":
            continue
        d = {"url": None, "title": None, "published": None}
        for el in url_el.iter():
            t = local(el.tag)
            if t == "loc" and not d["url"]:
                d["url"] = (el.text or "").strip()
            elif t == "title":
                d["title"] = (el.text or "").strip()
            elif t == "publication_date":
                d["published"] = (el.text or "").strip()
        if d["url"] and (not rx or rx.search(urlparse(d["url"]).path)):
            d["url"] = clean_url(d["url"])
            items.append(d)
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return items


def root_domain(host):
    parts = host.replace("www.", "").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "nic"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def extract_links(base, body, pattern, limit=60, auto=False):
    soup = BeautifulSoup(as_text(body), "lxml")
    rx = re.compile(pattern) if pattern else None
    base_root = root_domain(urlparse(base).netloc)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = clean_url(urljoin(base, a["href"]))
        p = urlparse(href)
        if p.scheme not in ("http", "https") or root_domain(p.netloc) != base_root:
            continue
        if rx and not rx.search(p.path):
            continue
        if not rx and auto and not looks_like_article(p.path):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"url": href, "title": a.get_text(" ", strip=True) or None})
        if len(out) >= limit:
            break
    return out


def short(u):
    return u if len(u) <= 70 else u[:67] + "..."


def try_feeds(urls, site, stealth_ok, notes, label):
    for u in urls:
        st, body, how = fetch_page(u, site, stealth_ok, raw=True)
        if how == "blocked":
            notes.append(f"{label} blocked (HTTP {st}): {short(u)}")
            continue
        items = parse_feed(body)
        if items:
            return items, how
        notes.append(f"{label} returned no entries (HTTP {st}): {short(u)}")
    return [], None


def try_pages(urls, site, stealth_ok, notes, label, use_pattern=True):
    pattern = site.get("pattern") if use_pattern else None
    for u in urls:
        st, body, how = fetch_page(u, site, stealth_ok)
        if how == "blocked":
            notes.append(f"{label} blocked (HTTP {st}): {short(u)}")
            continue
        items = extract_links(u, body, pattern, auto=not pattern)
        if not items and stealth_ok and how == "fast":  # maybe a JS-rendered listing
            st2, body2 = stealth_get(u, bool(site.get("proxy")))
            if not looks_blocked(st2, body2):
                items = extract_links(u, body2, pattern, auto=not pattern)
                how = "stealth"
        if items:
            return items, how
        notes.append(f"{label} loaded but 0 article links found: {short(u)}")
    return [], None


def discover(site, stealth_ok):
    """Order: national feed -> sitemap -> section page -> general feed (filtered)
    -> latest-news page (filtered). First one that yields articles wins."""
    notes = []
    apply_filter = site.get("filter", True)

    def finish(items, via, general):
        if general and apply_filter:
            kept = [i for i in items if is_national(i)]
            if not kept:
                notes.append(f"{via}: all {len(items)} items were sports/entertainment/business/world (filtered out)")
                return None
            items = kept
        return {"items": items, "via": via, "notes": notes}

    items, how = try_feeds(as_list(site.get("feed")), site, stealth_ok, notes, "feed")
    if items:
        return finish(items, f"feed/{how}", False)

    if site.get("sitemap"):
        st, body, how = fetch_page(site["sitemap"], site, stealth_ok, raw=True)
        if how == "blocked":
            notes.append(f"sitemap blocked (HTTP {st})")
        else:
            items = parse_sitemap(body, site.get("pattern"))
            if items:
                return finish(items, f"sitemap/{how}", False)
            notes.append("sitemap had no matching articles")

    pages = as_list(site.get("page"))
    if pages:
        items, how = try_pages(pages, site, stealth_ok, notes, "page")
        if items:
            r = finish(items, f"page/{how}", not site.get("pattern"))
            if r:
                return r

    items, how = try_feeds(as_list(site.get("general_feed")), site, stealth_ok, notes, "general feed")
    if items:
        r = finish(items, f"general-feed/{how}", True)
        if r:
            return r

    items, how = try_pages(as_list(site.get("latest_page")), site, stealth_ok, notes,
                           "latest page", use_pattern=False)
    if items:
        r = finish(items, f"latest-page/{how}", True)
        if r:
            return r

    if not any(site.get(k) for k in ("feed", "general_feed", "sitemap", "page", "latest_page")):
        notes.append("no feed/page configured: needs a national-news URL")
    return {"items": [], "via": None, "notes": notes}


# ------------------------------------------------------------------ article parsing
def parse_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = []
        for it in (data if isinstance(data, list) else [data]):
            stack.append(it)
            if isinstance(it, dict) and "@graph" in it:
                stack.extend(it["@graph"])
        for it in stack:
            if isinstance(it, dict):
                t = it.get("@type")
                t = " ".join(t) if isinstance(t, list) else str(t)
                if "Article" in t:
                    return it
    return {}


def name_of(x):
    if isinstance(x, list):
        return ", ".join(filter(None, (name_of(i) for i in x))) or None
    if isinstance(x, dict):
        return x.get("name")
    return x if isinstance(x, str) else None


def image_of(x):
    if isinstance(x, list) and x:
        x = x[0]
    if isinstance(x, dict):
        return x.get("url")
    return x if isinstance(x, str) else None


def meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


SCRIPTS = [("bn", r"[\u0980-\u09FF]"), ("pa", r"[\u0A00-\u0A7F]"), ("gu", r"[\u0A80-\u0AFF]"),
           ("or", r"[\u0B00-\u0B7F]"), ("ta", r"[\u0B80-\u0BFF]"), ("te", r"[\u0C00-\u0C7F]"),
           ("kn", r"[\u0C80-\u0CFF]"), ("ml", r"[\u0D00-\u0D7F]"), ("hi", r"[\u0900-\u097F]")]
INDIAN_LANGS = {"as", "mr", "ne", "hi", "bn", "ta", "te", "kn", "ml", "gu", "pa", "or", "ur"}


def detect_language(forced, html_lang, text):
    if forced:
        return forced
    hl = (html_lang or "").split("-")[0].lower()
    sample = (text or "")[:3000]
    for code, rx in SCRIPTS:
        if sample and len(re.findall(rx, sample)) / len(sample) > 0.25:
            return hl if hl in INDIAN_LANGS else code
    return hl or "en"


def build_article(item, site, budget):
    url = item["url"]
    st, html, how = fetch_page(url, site, stealth_ok=budget["left"] > 0)
    if budget["left"] > 0 and how != "fast":
        budget["left"] -= 1
    soup = BeautifulSoup(html, "lxml") if html else None
    ld = parse_jsonld(soup) if soup else {}

    content = trafilatura.extract(html, include_comments=False, include_tables=False) if html else None
    content = content or ld.get("articleBody") or item.get("feed_content") or item.get("summary")

    category = ld.get("articleSection")
    if isinstance(category, list):
        category = ", ".join(category)
    category = category or (meta(soup, "article:section") if soup else None) or "national"

    html_lang = soup.find("html").get("lang") if (soup and soup.find("html")) else None
    published = ld.get("datePublished") or (meta(soup, "article:published_time") if soup else None) or item.get("published")
    updated = ld.get("dateModified") or (meta(soup, "article:modified_time") if soup else None) or item.get("updated") or published

    art = {
        "source": site["name"],
        "article_id": make_id(url),
        "title": ld.get("headline") or (meta(soup, "og:title") if soup else None) or item.get("title"),
        "description": ld.get("description") or (meta(soup, "og:description", "description") if soup else None) or item.get("summary"),
        "category": category,
        "author": name_of(ld.get("author")) or (meta(soup, "author", "article:author") if soup else None) or item.get("author"),
        "published_date": to_iso(published),
        "updated_date": to_iso(updated),
        "image_url": image_of(ld.get("image")) or (meta(soup, "og:image", "twitter:image") if soup else None) or item.get("image"),
        "article_url": url,
        "content": content,
        "language": detect_language(site.get("language"), html_lang, content or item.get("title")),
        "scraped_date": now_iso(),
        "full_text_fetched": bool(html),
        "fetched_via": how,
    }
    for k in ("title", "description", "author"):
        art[k] = clean_text(art[k])
    return art


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sites.json")
    ap.add_argument("--output", default="news.json")
    ap.add_argument("--report", default="site_report.json")
    ap.add_argument("--limit", type=int, default=5, help="new articles per site per run")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=8, help="parallel discovery threads")
    ap.add_argument("--stealth-limit", type=int, default=3,
                    help="max stealth-browser article fetches per site per run")
    ap.add_argument("--only", default=None, help="comma-separated site keys")
    ap.add_argument("--check", action="store_true", help="only test sites, don't fetch articles")
    ap.add_argument("--no-stealth", action="store_true")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--test-stealth", default=None, metavar="URL",
                    help="try the Scrapling stealth browser on one URL and print any error")
    args = ap.parse_args()

    if args.test_stealth:
        import traceback
        print("Scrapling importable:", HAVE_SCRAPLING, SCRAPLING_IMPORT_ERROR or "")
        if HAVE_SCRAPLING:
            try:
                page = StealthyFetcher.fetch(args.test_stealth, headless=True, network_idle=False)
                print("stealth OK. status:", page.status,
                      "| html chars:", len(getattr(page, "html_content", "") or ""))
            except Exception:
                traceback.print_exc()
        return

    global PROXY
    if args.proxy:
        PROXY = args.proxy
    stealth_available = HAVE_SCRAPLING and not args.no_stealth
    print(f"Scrapling stealth fallback: {'ON' if stealth_available else 'OFF'} | proxy: {'on' if PROXY else 'none'}"
          + (f" | import error: {SCRAPLING_IMPORT_ERROR}" if SCRAPLING_IMPORT_ERROR else ""))

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sites = cfg.get("sites", cfg)
    keys = [k.strip() for k in args.only.split(",")] if args.only else list(sites)
    keys = [k for k in keys if k in sites]

    # Phase 1: fast discovery, in parallel
    print(f"\nPhase 1: testing {len(keys)} sites with fast requests...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = dict(zip(keys, ex.map(lambda k: discover(sites[k], False), keys)))

    # Phase 2: stealth browser for the ones that failed
    failed = [k for k in keys if not results[k]["items"]]
    if stealth_available and failed:
        print(f"Phase 2: retrying {len(failed)} failed sites with Scrapling stealth browser...")
        for k in failed:
            results[k] = discover(sites[k], True)
            print(f"  {sites[k]['name']}: {'OK via ' + results[k]['via'] if results[k]['items'] else 'still failing'}")

    # Report
    report, ok = [], []
    for k in keys:
        r = results[k]
        if r["items"]:
            ok.append(k)
        report.append({"key": k, "name": sites[k]["name"], "status": "ok" if r["items"] else "fail",
                       "via": r["via"], "items_found": len(r["items"]), "notes": r["notes"]})
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    via = Counter((r["via"] or "").split("/")[0] for r in report if r["status"] == "ok")
    stealth_n = sum(1 for r in report if r["status"] == "ok" and (r["via"] or "").endswith("stealth"))
    print(f"\n=== SITE REPORT: {len(ok)} working / {len(keys) - len(ok)} failing (of {len(keys)}) ===")
    print(f"working via national feed: {via['feed']}, sitemap: {via['sitemap']}, section page: {via['page']}, "
          f"general feed: {via['general-feed']}, latest-news page: {via['latest-page']} | needed stealth browser: {stealth_n}")
    print("FAILING:")
    for r in report:
        if r["status"] == "fail":
            print(f"  - {r['name']}: {'; '.join(r['notes']) or 'unknown'}")
    if args.check:
        print(f"\nReport written to {args.report}")
        return

    # Scrape articles
    out = Path(args.output)
    articles = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    seen = {a["article_id"] for a in articles}
    new_count = 0
    for k in ok:
        site, budget, taken = sites[k], {"left": args.stealth_limit if stealth_available else 0}, 0
        print(f"\n[{site['name']}] via {results[k]['via']}")
        for item in results[k]["items"]:
            if taken >= args.limit:
                break
            if make_id(item["url"]) in seen:
                continue
            art = build_article(item, site, budget)
            time.sleep(args.delay)
            articles.append(art)
            seen.add(art["article_id"])
            taken += 1
            new_count += 1
            print(f"  + {art['title']}{'' if art['full_text_fetched'] else ' (metadata only)'}")
        out.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nDone. {new_count} new, {len(articles)} total -> {out}")


if __name__ == "__main__":
    main()
