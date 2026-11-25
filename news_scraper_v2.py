#!/usr/bin/env python3
"""
Scrape latest news from Indian news sites -> news.json  (v2)

Install:
    pip install curl_cffi feedparser trafilatura beautifulsoup4 lxml requests

Run:
    python news_scraper_v2.py --limit 10 --output news.json --delay 2

What changed vs v1:
  * Fetches with curl_cffi (impersonates a real Chrome TLS fingerprint) to avoid
    "Connection reset by peer" blocks. Falls back to requests if not installed.
  * Feeds are downloaded first, then parsed (feedparser's own fetch is often blocked).
  * Prints HTTP status codes so you can see WHY a source fails.
  * Supports news sitemaps (India TV) as well as RSS.
  * Strips URL fragments like #publisher=newsstand.
  * If an article page is blocked, saves the RSS/sitemap metadata anyway
    (content = RSS summary) instead of dropping the article.
"""
import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urldefrag

import feedparser
import trafilatura
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
    print("Using curl_cffi (browser impersonation).")
except ImportError:
    import requests as http
    IMPERSONATE = {}
    print("curl_cffi not installed; using requests (more sites will block you).")

# type: "rss" or "sitemap". VERIFY URLs in a browser if one returns 0 entries.
SOURCES = [
    {"source": "India TV", "type": "sitemap", "category": "news",
     "url": "https://www.indiatvnews.com/news-sitemap.xml"},
    {"source": "India TV Hindi", "type": "rss", "category": "india",
     "url": "https://www.indiatv.in/rssnews/topstory-india.xml"},
    {"source": "NDTV", "type": "rss", "category": "india",
     "url": "https://feeds.feedburner.com/ndtvnews-india-news"},
    {"source": "NDTV", "type": "rss", "category": "latest",
     "url": "https://feeds.feedburner.com/ndtvnews-latest"},
    {"source": "Times of India", "type": "rss", "category": "top-stories",
     "url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"},
    {"source": "The Hindu", "type": "rss", "category": "national",
     "url": "https://www.thehindu.com/news/national/feeder/default.rss"},
    {"source": "Indian Express", "type": "rss", "category": "latest",
     "url": "https://indianexpress.com/feed/"},
    {"source": "Hindustan Times", "type": "rss", "category": "india-news",
     "url": "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml"},
    {"source": "ABP Live", "type": "rss", "category": "india",
     "url": "https://www.abplive.com/news/india/feed"},
    {"source": "TV9 Hindi", "type": "rss", "category": "general",
     "url": "https://www.tv9hindi.com/feed/gn"},
]

HEADERS = {"Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}

import os
# Set with --proxy or env var, e.g. http://user:pass@host:port
PROXY = os.environ.get("SCRAPER_PROXY")


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


def get(url, retries=2, timeout=25, as_bytes=False):
    for attempt in range(retries + 1):
        try:
            proxies = {"http": PROXY, "https": PROXY} if PROXY else None
            r = http.get(url, headers=HEADERS, timeout=timeout,
                         proxies=proxies, **IMPERSONATE)
            if r.status_code == 200:
                return r.content if as_bytes else r.text
            print(f"  ! HTTP {r.status_code}: {url[:90]}")
            if r.status_code in (403, 404, 410):
                return None
        except Exception as e:
            print(f"  ! error ({type(e).__name__}) attempt {attempt + 1}: {url[:90]}")
        time.sleep(1.5 * (attempt + 1))
    return None


# ---------- listing readers: return list of dicts {url,title,summary,...} ----------
def read_rss(cfg):
    raw = get(cfg["url"], as_bytes=True)
    if not raw:
        return []
    feed = feedparser.parse(raw)
    items = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        image = None
        if e.get("media_content"):
            image = e["media_content"][0].get("url")
        elif e.get("enclosures"):
            image = e["enclosures"][0].get("href")
        items.append({
            "url": clean_url(e["link"]),
            "title": e.get("title"),
            "summary": BeautifulSoup(e.get("summary", ""), "lxml").get_text(" ", strip=True) or None,
            "author": e.get("author"),
            "published": e.get("published"),
            "updated": e.get("updated"),
            "image": image,
        })
    return items


def read_sitemap(cfg):
    raw = get(cfg["url"], as_bytes=True)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ! sitemap parse error: {e}")
        return []

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    items = []
    for url_el in root.iter():
        if local(url_el.tag) != "url":
            continue
        d = {"url": None, "title": None, "published": None, "image": None}
        for el in url_el.iter():
            t = local(el.tag)
            if t == "loc" and not d["url"]:
                d["url"] = (el.text or "").strip()
            elif t == "title":
                d["title"] = (el.text or "").strip()
            elif t == "publication_date":
                d["published"] = (el.text or "").strip()
            elif t == "loc" and d["url"] and not d["image"]:
                d["image"] = (el.text or "").strip()
        if d["url"]:
            d["url"] = clean_url(d["url"])
            items.append(d)
    # newest first
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return items


# ---------- article parsing ----------
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
        tag = (soup.find("meta", attrs={"property": n})
               or soup.find("meta", attrs={"name": n}))
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def detect_language(html_lang, text):
    if text:
        deva = len(re.findall(r"[\u0900-\u097F]", text))
        if deva / max(len(text), 1) > 0.3:
            return "hi"
    if html_lang:
        return html_lang.split("-")[0].lower()
    return "en"


def build_article(item, cfg):
    url = item["url"]
    html = get(url)
    soup = BeautifulSoup(html, "lxml") if html else None
    ld = parse_jsonld(soup) if soup else {}

    content = None
    if html:
        content = trafilatura.extract(html, include_comments=False, include_tables=False)
    content = content or ld.get("articleBody") or item.get("summary")

    category = ld.get("articleSection")
    if isinstance(category, list):
        category = ", ".join(category)
    if soup:
        category = category or meta(soup, "article:section")
    category = category or cfg["category"]

    html_lang = None
    if soup and soup.find("html"):
        html_lang = soup.find("html").get("lang")

    published = ld.get("datePublished") or (meta(soup, "article:published_time") if soup else None) or item.get("published")
    updated = ld.get("dateModified") or (meta(soup, "article:modified_time") if soup else None) or item.get("updated") or published

    return {
        "source": cfg["source"],
        "article_id": make_id(url),
        "title": (ld.get("headline") or (meta(soup, "og:title") if soup else None) or item.get("title") or "").strip() or None,
        "description": ld.get("description") or (meta(soup, "og:description", "description") if soup else None) or item.get("summary"),
        "category": category,
        "author": name_of(ld.get("author")) or (meta(soup, "author", "article:author") if soup else None) or item.get("author"),
        "published_date": to_iso(published),
        "updated_date": to_iso(updated),
        "image_url": image_of(ld.get("image")) or (meta(soup, "og:image", "twitter:image") if soup else None) or item.get("image"),
        "article_url": url,
        "content": content,
        "language": detect_language(html_lang, content),
        "scraped_date": now_iso(),
        "full_text_fetched": bool(html),
    }


def load_existing(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("Existing JSON corrupt, starting fresh.")
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="news.json")
    ap.add_argument("--limit", type=int, default=10, help="articles per source")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--proxy", default=None,
                    help="e.g. http://user:pass@host:port (use an Indian IP)")
    args = ap.parse_args()

    global PROXY
    if args.proxy:
        PROXY = args.proxy
    print(f"Proxy: {'enabled' if PROXY else 'none'}")

    out = Path(args.output)
    articles = load_existing(out)
    seen = {a["article_id"] for a in articles}
    new_count = 0

    for cfg in SOURCES:
        print(f"\n[{cfg['source']}] ({cfg['type']}) {cfg['url']}")
        items = read_sitemap(cfg) if cfg["type"] == "sitemap" else read_rss(cfg)
        if not items:
            print("  ! 0 items from this source")
            continue
        print(f"  found {len(items)} items, taking up to {args.limit} new")
        taken = 0
        for item in items:
            if taken >= args.limit:
                break
            if make_id(item["url"]) in seen:
                continue
            art = build_article(item, cfg)
            time.sleep(args.delay)
            articles.append(art)
            seen.add(art["article_id"])
            new_count += 1
            taken += 1
            flag = "" if art["full_text_fetched"] else " (metadata only)"
            print(f"  + {art['title']}{flag}")

    out.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. {new_count} new, {len(articles)} total -> {out}")


if __name__ == "__main__":
    main()