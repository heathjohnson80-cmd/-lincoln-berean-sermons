#!/usr/bin/env python3
"""
Build a private Apple Podcasts-compatible RSS feed from Lincoln Berean's public
sermon archive. Audio remains hosted by Lincoln Berean Church.
"""
import re
import time
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse
from xml.etree.ElementTree import Element, SubElement, ElementTree, register_namespace

import requests
from bs4 import BeautifulSoup

BASE = "https://www.lincolnberean.org"
SERIES_INDEX = BASE + "/past-sermons/series/"
OUTPUT = "lincoln-berean-archive.xml"
REPORT = "feed-build-report.json"
ART_URL = "https://heathjohnson80-cmd.github.io/-lincoln-berean-sermons/lincoln-berean-archive-cover.png"
UA = "LincolnBereanPersonalArchive/1.0 (+personal podcast index)"

session = requests.Session()
session.headers.update({"User-Agent": UA})

def get(url, timeout=30):
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def unique(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def canonical(url):
    return url.split("#")[0].rstrip("/")

def collect_series():
    soup = BeautifulSoup(get(SERIES_INDEX).text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE, a["href"])
        if "/sermon-serie/" in href:
            title = clean(a.get_text(" ", strip=True))
            if title:
                links.append((canonical(href), title))
    # de-dupe URL
    d = {}
    for u,t in links:
        d[u] = t
    return list(d.items())

DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")

def collect_sermons_from_series(series_url, series_title):
    soup = BeautifulSoup(get(series_url).text, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE, a["href"])
        if "/sermon/" not in href:
            continue
        title = clean(a.get_text(" ", strip=True))
        if not title:
            continue

        # Try to recover date/scripture from the surrounding row/line/container.
        container = a.find_parent(["tr","li","p","div"])
        text = clean(container.get_text(" | ", strip=True)) if container else clean(a.parent.get_text(" | ", strip=True))
        m = DATE_RE.search(text)
        date = m.group(1) if m else ""
        scripture = ""
        if "|" in text:
            parts = [clean(x) for x in text.split("|") if clean(x)]
            # usually date | title | scripture
            if len(parts) >= 3:
                scripture = parts[-1]
                if scripture == title or DATE_RE.fullmatch(scripture or ""):
                    scripture = ""
        results.append({
            "url": canonical(href),
            "title": title,
            "series": series_title,
            "date_hint": date,
            "scripture_hint": scripture,
        })
    # de-dupe by sermon URL
    d = {}
    for x in results:
        d[x["url"]] = x
    return list(d.values())

def find_mp3(soup, html):
    candidates = []
    for tag in soup.find_all(["a","audio","source"], href=True):
        candidates.append(tag.get("href"))
    for tag in soup.find_all(["audio","source"], src=True):
        candidates.append(tag.get("src"))
    # Search raw HTML too because some players embed URLs in JS/shortcodes.
    candidates += re.findall(r'https?://[^"\'<>\s]+?\.mp3(?:\?[^"\'<>\s]*)?', html, flags=re.I)
    candidates += re.findall(r'[^"\'<>\s]+?\.mp3(?:\?[^"\'<>\s]*)?', html, flags=re.I)

    for c in candidates:
        if not c:
            continue
        c = c.replace("&amp;", "&")
        u = urljoin(BASE, c)
        if ".mp3" in u.lower():
            return u
    return ""

def parse_date(text, hint=""):
    vals = []
    if hint:
        vals.append(hint)
    vals += re.findall(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b", text)
    vals += re.findall(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
    for v in vals:
        for fmt in ("%B %d, %Y","%m/%d/%Y","%m/%d/%y"):
            try:
                return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None

def parse_sermon(item):
    r = get(item["url"])
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    all_text = clean(soup.get_text(" ", strip=True))

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else item["title"]

    speaker = ""
    # Common site format: BY NAME
    m = re.search(r"\bBY\s+([A-Z][A-Za-z .'\-]+?)(?=\s+(?:[A-Z][a-z]+\s+\d{1,2},\s+\d{4}|Transcript|Audio|$))", all_text)
    if m:
        speaker = clean(m.group(1))
    if not speaker:
        bynode = soup.find(string=re.compile(r"^\s*BY\s+", re.I))
        if bynode:
            speaker = clean(re.sub(r"^\s*BY\s+", "", bynode, flags=re.I))

    mp3 = find_mp3(soup, html)
    dt = parse_date(all_text, item.get("date_hint",""))

    scripture = item.get("scripture_hint","")
    # If series row parsing accidentally captured junk, keep it modest.
    if scripture and len(scripture) > 180:
        scripture = ""

    length = 1
    if mp3:
        try:
            hr = session.head(mp3, allow_redirects=True, timeout=15)
            if hr.ok:
                length = int(hr.headers.get("content-length") or 1)
        except Exception:
            pass

    return {
        "title": title or item["title"],
        "series": item["series"],
        "speaker": speaker or "Lincoln Berean Church",
        "date": dt,
        "scripture": scripture,
        "mp3": mp3,
        "page": item["url"],
        "length": max(length, 1),
    }

def rfc822(dt):
    if dt is None:
        dt = datetime(2000,1,1,tzinfo=timezone.utc)
    return format_datetime(dt)

def build_xml(episodes):
    register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    itunes = "http://www.itunes.com/dtds/podcast-1.0.dtd"

    rss = Element("rss", {"version":"2.0"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Lincoln Berean — Complete Sermon Archive"
    SubElement(channel, "link").text = BASE + "/past-sermons/"
    SubElement(channel, "description").text = (
        "Personal convenience feed indexing publicly hosted Lincoln Berean Church sermon audio "
        "from the church archive. Audio remains hosted by Lincoln Berean Church."
    )
    SubElement(channel, "language").text = "en-us"
    SubElement(channel, f"{{{itunes}}}author").text = "Lincoln Berean Church"
    SubElement(channel, f"{{{itunes}}}summary").text = "Complete Lincoln Berean Church sermon archive for convenient personal listening."
    SubElement(channel, f"{{{itunes}}}explicit").text = "false"
    SubElement(channel, f"{{{itunes}}}image", {"href":ART_URL})
    cat = SubElement(channel, f"{{{itunes}}}category", {"text":"Religion & Spirituality"})
    SubElement(cat, f"{{{itunes}}}category", {"text":"Christianity"})

    # newest first
    episodes = sorted(episodes, key=lambda e: e["date"] or datetime(1900,1,1,tzinfo=timezone.utc), reverse=True)
    for e in episodes:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = f'{e["series"]} // {e["title"]}'
        SubElement(item, "link").text = e["page"]
        SubElement(item, "guid", {"isPermaLink":"false"}).text = e["mp3"]
        SubElement(item, "pubDate").text = rfc822(e["date"])
        descbits = [e["series"], e["speaker"]]
        if e["scripture"]:
            descbits.append(e["scripture"])
        desc = " • ".join(descbits)
        SubElement(item, "description").text = desc
        SubElement(item, f"{{{itunes}}}author").text = e["speaker"]
        SubElement(item, f"{{{itunes}}}summary").text = desc
        SubElement(item, f"{{{itunes}}}explicit").text = "false"
        SubElement(item, f"{{{itunes}}}image", {"href":ART_URL})
        SubElement(item, "enclosure", {
            "url": e["mp3"],
            "length": str(e["length"]),
            "type": "audio/mpeg"
        })

    tree = ElementTree(rss)
    try:
        import xml.etree.ElementTree as ET
        ET.indent(tree, space="  ")
    except Exception:
        pass
    tree.write(OUTPUT, encoding="utf-8", xml_declaration=True)

def main():
    series = collect_series()
    print(f"Found {len(series)} sermon series.")
    sermon_map = {}
    failed_series = []

    for idx, (url, title) in enumerate(series, 1):
        try:
            items = collect_sermons_from_series(url, title)
            print(f"[{idx}/{len(series)}] {title}: {len(items)} sermon links")
            for x in items:
                # One sermon can appear in more than one series (e.g. Easter).
                # Prefer the first listing from the site's main series index.
                sermon_map.setdefault(x["url"], x)
        except Exception as e:
            failed_series.append({"url":url,"title":title,"error":str(e)})
        time.sleep(0.15)

    episodes = []
    failed_sermons = []
    no_audio = []

    items = list(sermon_map.values())
    print(f"Found {len(items)} unique sermon pages.")
    for idx, item in enumerate(items, 1):
        try:
            e = parse_sermon(item)
            if e["mp3"]:
                episodes.append(e)
            else:
                no_audio.append(item)
            if idx % 25 == 0:
                print(f"Parsed {idx}/{len(items)} sermons; {len(episodes)} with audio.")
        except Exception as ex:
            failed_sermons.append({"url":item["url"],"title":item["title"],"error":str(ex)})
        time.sleep(0.15)

    build_xml(episodes)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series_found": len(series),
        "unique_sermon_pages": len(items),
        "episodes_with_audio": len(episodes),
        "sermons_without_audio": len(no_audio),
        "failed_series": failed_series,
        "failed_sermons": failed_sermons,
        "no_audio": no_audio,
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps({k:v for k,v in report.items() if not isinstance(v,list)}, indent=2))
    if len(episodes) < 100:
        raise SystemExit("Safety stop: fewer than 100 playable sermons found; not committing a suspiciously incomplete feed.")

if __name__ == "__main__":
    main()
