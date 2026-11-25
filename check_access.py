#!/usr/bin/env python3
"""
Check which Indian news sites are reachable from your IP (or through a proxy).

pip install curl_cffi
python check_access.py                                   # direct
python check_access.py --proxy socks5h://127.0.0.1:1080  # via proxy / SSH tunnel
"""
import argparse
from curl_cffi import requests

SITES = {
    "India TV": "https://www.indiatvnews.com/news-sitemap.xml",
    "India TV Hindi": "https://www.indiatv.in/rssnews/topstory-india.xml",
    "NDTV": "https://feeds.feedburner.com/ndtvnews-india-news",
    "NDTV article page": "https://www.ndtv.com/india-news",
    "Times of India": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "The Hindu": "https://www.thehindu.com/news/national/feeder/default.rss",
    "Indian Express": "https://indianexpress.com/feed/",
    "Hindustan Times": "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml",
    "ABP Live": "https://www.abplive.com/news/india/feed",
    "TV9 Hindi": "https://www.tv9hindi.com/feed/gn",
}

ap = argparse.ArgumentParser()
ap.add_argument("--proxy", default=None)
args = ap.parse_args()
proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None

try:
    r = requests.get("https://ipinfo.io/json", proxies=proxies, timeout=20)
    d = r.json()
    print(f"Exit IP: {d.get('ip')}  country: {d.get('country')}  org: {d.get('org')}\n")
except Exception as e:
    print(f"Could not check IP ({type(e).__name__}). Proxy likely wrong.\n")

for name, url in SITES.items():
    try:
        r = requests.get(url, proxies=proxies, impersonate="chrome", timeout=25)
        note = {200: "OK", 403: "BLOCKED (geo/bot)", 404: "feed URL wrong",
                429: "rate limited"}.get(r.status_code, "")
        print(f"{name:20} HTTP {r.status_code}  {len(r.content):>8} bytes  {note}")
    except Exception as e:
        print(f"{name:20} ERROR {type(e).__name__}")