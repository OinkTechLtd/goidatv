#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎯 IPTV Mega Aggregator — Unified Entry Point for GitVerse CI
Запуск: 
  python main.py              # ВСЕ джобы (для CI)
  python main.py 1|2|3        # Конкретный джоб
"""
import os
import sys
import re
import json
import time
import html as html_module
import logging
import threading
import requests
import concurrent.futures
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from typing import List, Dict, Set, Tuple, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ═══════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════
OUTPUT_FOLDER = "output"
RAW_FILE = os.path.join(OUTPUT_FOLDER, "raw.json")
WEBTV_FILE = os.path.join(OUTPUT_FOLDER, "webtv.json")
CHECKED_M3U = os.path.join(OUTPUT_FOLDER, "CHECKED.m3u")
RAW_M3U = os.path.join(OUTPUT_FOLDER, "RAW.m3u")
REPORT_HTML = os.path.join(OUTPUT_FOLDER, "report.html")
README_MD = os.path.join(OUTPUT_FOLDER, "README.md")

FETCH_TIMEOUT = 20
CHECK_TIMEOUT = 7
CHECK_WORKERS = 150
MAX_CHANNELS = 30000

GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", os.environ.get("GITVERSE_REPOSITORY", ""))
GITHUB_BRANCH = os.environ.get("GITHUB_REF_NAME", os.environ.get("GITVERSE_BRANCH", "main"))
RAW_BASE_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{OUTPUT_FOLDER}" if GITHUB_REPO else f"./{OUTPUT_FOLDER}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def make_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler()]
    )
    return logging.getLogger(name)


class HTTPClient:
    def __init__(self, retries: int = 1, backoff: float = 0.5):
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=200,
            pool_maxsize=200,
            max_retries=Retry(
                total=retries,
                backoff_factor=backoff,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["HEAD", "GET"]
            )
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": UA})

    def fetch(self, url: str, timeout: int = FETCH_TIMEOUT, method: str = "GET", stream: bool = False):
        try:
            if method.upper() == "HEAD":
                return self.session.head(url, timeout=timeout, allow_redirects=True)
            return self.session.get(url, timeout=timeout, allow_redirects=True, stream=stream)
        except Exception:
            return None

    def fetch_text(self, url: str, timeout: int = FETCH_TIMEOUT) -> Optional[str]:
        r = self.fetch(url, timeout=timeout)
        if r is None or r.status_code >= 500:
            return None
        try:
            t = r.text
            r.close()
            return t
        except Exception:
            return None

    def close(self):
        self.session.close()


def parse_m3u(content: str, group: str) -> List[Dict]:
    channels = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            extinf = line
            name_m = re.search(r",(.+)$", line)
            ch_name = name_m.group(1).strip() if name_m else "Unknown"

            grp_m = re.search(r'group-title="([^"]*)"', line, re.IGNORECASE)
            final_group = group
            if grp_m:
                orig = grp_m.group(1)
                final_group = f"{group} | {orig}" if orig.lower() not in group.lower() else group
                extinf = extinf.replace(grp_m.group(0), f'group-title="{final_group}"')
            else:
                extinf = re.sub(r"(#EXTINF:[^,]*)", rf'\1 group-title="{final_group}"', extinf, count=1)

            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    if nxt.startswith(("http", "rtmp")):
                        channels.append({
                            "name": ch_name,
                            "url": nxt,
                            "group": final_group,
                            "extinf": extinf
                        })
                    break
                j += 1
            i = j + 1
        else:
            i += 1
    return channels


def write_m3u(channels: List[Dict], filepath: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in channels:
            f.write(f"{ch['extinf']}\n{ch['url']}\n")


def save_json(data: list, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_json(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════
# JOB 1 — СБОР
# ═══════════════════════════════════════════════════════
SOURCES = [
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/index.m3u", "group": "IPTV-ORG"},
    {"type": "direct", "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", "group": "Free-TV"},
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/countries/ru.m3u", "group": "Russia"},
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/countries/ua.m3u", "group": "Ukraine"},
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/countries/by.m3u", "group": "Belarus"},
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/countries/kz.m3u", "group": "Kazakhstan"},
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/categories/news.m3u", "group": "News"},
    {"type": "direct", "url": "https://iptv-org.github.io/iptv/categories/sports.m3u", "group": "Sports"},
    {"type": "paged", "url": "https://m3u.su/page/{page}", "pages": 6, "group": "M3U.SU"},
    {"type": "paged", "url": "https://iptv.axenov.dev/page/{page}", "pages": 6, "group": "Axenov"},
    {"type": "aggregator", "url": "http://rafail1982.uz/playlists/", "group": "Rafail1982", "max_depth": 2},
    {"type": "aggregator", "url": "https://www.iptvlist.ru/", "group": "IPTVList", "max_depth": 2},
    {"type": "github", "url": "https://github.com/iptv-org/iptv", "group": "iptv-org"},
    {"type": "github", "url": "https://github.com/Free-TV/IPTV", "group": "Free-TV"},
    {"type": "github", "url": "https://github.com/smolnp/IPTVru", "group": "IPTVru"},
    {"type": "github", "url": "https://github.com/anthonyaxenov/iptv", "group": "Axenov"},
    {"type": "github", "url": "https://github.com/devsground/IPTV", "group": "Devsground"},
]


class HTMLCrawler:
    M3U_PATS = [
        r'href=["\']([^"\']*?\.m3u8?)["\']',
        r'(https?://[^\s<>"\'&]+?\.m3u8?)(?=[\s<>"\'&]|$)'
    ]

    def __init__(self, base_url: str, max_depth: int, client: HTTPClient):
        self.base = base_url
        self.max_depth = max_depth
        self.client = client
        self.visited: Set[str] = set()
        self.found: List[str] = []

    def run(self) -> List[str]:
        content = self.client.fetch_text(self.base)
        if not content:
            return []
        if "#EXTM3U" in content[:3000]:
            return [self.base]
        self._crawl(self.base, content, 0)
        return list(dict.fromkeys(self.found))

    def _crawl(self, url: str, content: str, depth: int):
        if depth > self.max_depth or len(self.visited) >= 60:
            return
        self.visited.add(url)
        for pat in self.M3U_PATS:
            for m in re.findall(pat, content, re.IGNORECASE):
                u = urljoin(url, m).strip("'\"")
                if u not in self.found:
                    self.found.append(u)
        if depth < self.max_depth:
            domain = urlparse(url).netloc
            for m in re.findall(r'href=["\'](/[^"\'#?]{1,80})["\']', content):
                nxt = f"https://{domain}{m}"
                if nxt not in self.visited:
                    time.sleep(0.05)
                    sub = self.client.fetch_text(nxt)
                    if sub:
                        self._crawl(nxt, sub, depth + 1)


def job1_collect(client: HTTPClient) -> List[Dict]:
    log = make_logger("JOB1")
    log.info("🔍 JOB 1 — СБОР ПЛЕЙЛИСТОВ")
    all_channels = []
    seen = set()
    stats = {"direct": 0, "aggregator": 0, "github": 0, "failed": 0}

    def add(channels):
        for ch in channels:
            if ch["url"] not in seen:
                seen.add(ch["url"])
                all_channels.append(ch)

    for src in SOURCES:
        stype = src["type"]
        group = src.get("group", "Other")
        label = src.get("url", "?")[:45]
        channels = []

        try:
            if stype == "direct":
                text = client.fetch_text(src["url"])
                if text and "#EXTM3U" in text[:200]:
                    channels = parse_m3u(text, group)
                    stats["direct"] += len(channels)
                    log.info("  ✓ %-45s +%d", label, len(channels))

            elif stype == "paged":
                m3u_urls = []
                for page in range(1, src.get("pages", 6) + 1):
                    page_url = src["url"].replace("{page}", str(page))
                    html = client.fetch_text(page_url, timeout=15)
                    if not html:
                        continue
                    for pat in [r'href=["\']([^"\']*?\.m3u8?)["\']', r'(https?://[^\s<>"&]+?\.m3u8?)(?=[\s<>"&]|$)']:
                        for m in re.findall(pat, html, re.IGNORECASE):
                            u = urljoin(page_url, m)
                            if u not in m3u_urls:
                                m3u_urls.append(u)
                for mu in m3u_urls:
                    text = client.fetch_text(mu, timeout=12)
                    if text and "#EXTM3U" in text[:200]:
                        channels.extend(parse_m3u(text, group))
                stats["aggregator"] += len(channels)
                log.info("  ✓ %-45s +%d (из %d файлов)", label, len(channels), len(m3u_urls))

            elif stype == "aggregator":
                crawler = HTMLCrawler(src["url"], src.get("max_depth", 2), client)
                m3u_urls = crawler.run()
                for mu in m3u_urls:
                    text = client.fetch_text(mu, timeout=12)
                    if text and "#EXTM3U" in text[:200]:
                        channels.extend(parse_m3u(text, group))
                stats["aggregator"] += len(channels)
                log.info("  ✓ %-45s +%d (из %d файлов)", label, len(channels), len(m3u_urls))

            elif stype == "github":
                m = re.search(r"github\.com/([^/]+)/([^/]+)", src["url"])
                if m:
                    user, repo = m.groups()
                    repo = repo.replace(".git", "")
                    for branch in ("master", "main"):
                        api = f"https://api.github.com/repos/{user}/{repo}/git/trees/{branch}?recursive=1"
                        resp = client.fetch(api, timeout=20)
                        if resp and resp.status_code == 200:
                            tree = resp.json().get("tree", [])
                            for item in tree:
                                path = item.get("path", "")
                                if path.lower().endswith((".m3u", ".m3u8")):
                                    raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
                                    text = client.fetch_text(raw_url, timeout=15)
                                    if text and "#EXTM3U" in text[:200]:
                                        channels.extend(parse_m3u(text, group))
                            if channels:
                                break
                stats["github"] += len(channels)
                log.info("  ✓ %-45s +%d", label, len(channels))

        except Exception as e:
            stats["failed"] += 1
            log.warning("  ✗ %-45s %s", label, str(e)[:60])

        add(channels)
        if len(all_channels) >= MAX_CHANNELS:
            log.info("  ⚠ Лимит %d каналов", MAX_CHANNELS)
            break

    log.info("📊 ИТОГО: прямые=%d, агрегаторы=%d, github=%d, уникальных=%d",
             stats["direct"], stats["aggregator"], stats["github"], len(all_channels))
    return all_channels


# ═══════════════════════════════════════════════════════
# JOB 2 — WEBTV
# ═══════════════════════════════════════════════════════
WEBTV_SOURCES = [
    {"name": "smotret.tv", "group": "RU | smotret.tv", "base_url": "https://smotret.tv",
     "paths": ["/1-kanal","/rossiya-1","/tnt","/ntv","/sts","/ren-tv","/zvezda","/5-kanal","/kultura","/mir","/tv-3","/pyatnica","/che","/domashniy","/match-tv","/russia-24"]},
    {"name": "telik.live", "group": "RU | telik.live", "base_url": "https://telik.live",
     "paths": ["/pervyj-kanal.html","/rossiya-1.html","/ntv.html","/tnt.html","/sts.html","/tv-3.html","/pyatnitsa.html","/match-tv.html","/pervyj-kanal-hd.html","/rossiya-hd.html"]},
    {"name": "tv2free.ru", "group": "RU | tv2free.ru", "base_url": "https://tv2free.ru",
     "paths": ["/tv/rossiya-1","/tv/otr","/tv/rossiya-24","/tv/rbk","/tv/spas","/tv/yu","/tv/muz-tv","/tv/europa-plus-tv","/tv/1-kanal","/tv/ntv","/tv/tnt","/tv/sts"]},
    {"name": "onlinetv.ru", "group": "RU | onlinetv.ru", "base_url": "https://onlinetv.ru",
     "paths": ["/1-kanal/","/rossiya-1/","/ntv/","/tnt/","/sts/","/ren-tv/","/zvezda/","/kultura/","/russia-24/"]},
]

HLS_PATTERNS = [
    r'file\s*:\s*["\']([^"\']+?\.m3u8[^"\']*)["\']',
    r'src\s*:\s*["\']([^"\']+?\.m3u8[^"\']*)["\']',
    r'source\s*:\s*["\']([^"\']+?\.m3u8[^"\']*)["\']',
    r'hls(?:Url|_url|URL|Source)?\s*[=:]\s*["\']([^"\']+?\.m3u8[^"\']*)["\']',
    r'(?:stream|video|player)(?:Url|URL|_url|Src|SRC)\s*[=:]\s*["\']([^"\']+?\.m3u8[^"\']*)["\']',
    r'["\']([https?://[^"\']+?\.m3u8[^"\']*)["\']',
    r'src=["\'](https?://[^"\']+?\.m3u8[^"\']*)["\']'
]


def extract_hls(html: str) -> Optional[str]:
    for pat in HLS_PATTERNS:
        for m in re.finditer(pat, html, re.IGNORECASE):
            url = m.group(1).strip()
            if url.startswith("http") and ".m3u8" in url.lower():
                if any(bad in url.lower() for bad in ("demo", "test", "example", "sample")):
                    continue
                return url
    return None


def job2_scrape_source(src: Dict, client: HTTPClient, log) -> List[Dict]:
    base = src["base_url"].rstrip("/")
    group = src["group"]
    results = []

    def scrape_path(path: str) -> Optional[Dict]:
        html = client.fetch_text(base + path, timeout=15)
        if not html:
            return None
        hls = extract_hls(html)
        if not hls:
            return None
        name = path.rstrip("/").split("/")[-1].replace(".html", "").replace("-", " ").title()
        return {
            "name": name,
            "url": hls,
            "group": group,
            "extinf": f'#EXTINF:-1 group-title="{group}",{name}'
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(scrape_path, p): p for p in src["paths"]}
        for ft in concurrent.futures.as_completed(futures):
            try:
                r = ft.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    log.info("  ✓ %-30s %d каналов", src["name"], len(results))
    return results


def job2_collect(client: HTTPClient) -> List[Dict]:
    log = make_logger("JOB2")
    log.info("📡 JOB 2 — WEBTV СКРАПЕР")
    all_channels = []
    seen = set()

    for src in WEBTV_SOURCES:
        channels = job2_scrape_source(src, client, log)
        for ch in channels:
            if ch["url"] not in seen:
                seen.add(ch["url"])
                all_channels.append(ch)

    log.info("📊 WebTV итого: %d уникальных каналов", len(all_channels))
    return all_channels


# ═══════════════════════════════════════════════════════
# JOB 3 — ПРОВЕРКА + ОТЧЁТ
# ═══════════════════════════════════════════════════════
def job3_check_stream(ch: Dict, client: HTTPClient) -> Tuple[Dict, bool]:
    url = ch["url"]
    try:
        r = client.fetch(url, timeout=5, method="HEAD")
        if r:
            code = r.status_code
            ct = r.headers.get("content-type", "").lower()
            r.close()
            if code in (301, 302, 307, 308):
                return ch, True
            if code >= 400:
                return ch, False
            if code in (200, 204, 206):
                if any(t in ct for t in ("video", "mpeg", "octet-stream", "mpegurl", "audio")):
                    return ch, True
                if code == 200:
                    return ch, True

        r = client.fetch(url, timeout=CHECK_TIMEOUT, stream=True)
        if r is None or r.status_code not in (200, 206):
            if r:
                r.close()
            return ch, False

        chunk = b""
        for data in r.iter_content(512):
            chunk += data
            if len(chunk) >= 512:
                break
        ct = r.headers.get("content-type", "").lower()
        r.close()

        text = chunk.decode("utf-8", errors="ignore")
        if any(s in text for s in ("#EXTM3U", "#EXT-X", "#EXTINF")):
            return ch, True
        if chunk[:1] == b"\x47":
            return ch, True
        if b"FLV" in chunk[:4]:
            return ch, True
        if any(t in ct for t in ("video", "mpeg", "octet-stream", "mpegurl", "audio")):
            return ch, True

        return ch, len(chunk) > 100
    except Exception:
        return ch, False


def job3_check_all(channels: List[Dict], client: HTTPClient) -> List[Dict]:
    log = make_logger("JOB3")
    total = len(channels)
    working = []
    done = 0
    alive = 0

    log.info("⚡ JOB 3 — ПРОВЕРКА %d КАНАЛОВ (%d потоков)", total, CHECK_WORKERS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=CHECK_WORKERS) as ex:
        futs = {ex.submit(job3_check_stream, ch, client): ch for ch in channels}
        for fut in concurrent.futures.as_completed(futs):
            ch, ok = fut.result()
            done += 1
            if ok:
                alive += 1
                working.append(ch)
            if done % 1000 == 0 or done == total:
                pct = alive / done * 100
                log.info("  [%d/%d] живых: %d (%.1f%%)", done, total, alive, pct)
                for h in log.handlers:
                    h.flush()
                sys.stdout.flush()

    log.info("📊 РЕЗУЛЬТАТ: всего=%d, рабочих=%d ✅, мёртвых=%d ❌, живых=%.1f%%",
             total, alive, total - alive, alive / total * 100 if total else 0)
    return working


def job3_build_report(raw: List[Dict], checked: List[Dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(raw)
    working = len(checked)
    dead = total - working
    pct = round(working / total * 100, 1) if total else 0
    bar_w = min(int(pct), 100)

    checked_url = f"{RAW_BASE_URL}/CHECKED.m3u"
    raw_url = f"{RAW_BASE_URL}/RAW.m3u"

    groups = Counter(ch.get("group", "Other") for ch in checked)
    groups_rows = "\n".join(
        f'<tr><td>{html_module.escape(g)}</td><td class="num">{c}</td></tr>'
        for g, c in sorted(groups.items(), key=lambda x: -x[1])[:25]
    )

    ch_rows = ""
    for ch in checked[:300]:
        name = html_module.escape(ch.get("name", "?"))
        grp = html_module.escape(ch.get("group", "Other"))
        url = ch.get("url", "")
        short = (url[:55] + "…") if len(url) > 55 else url
        ch_rows += f'<tr><td>{name}</td><td><span class="badge">{grp}</span></td><td><a href="{html_module.escape(url)}" target="_blank" class="lnk">{html_module.escape(short)}</a></td></tr>\n'

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>📺 IPTV Report</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d0d1a;color:#c9d1d9;min-height:100vh;padding:16px}}
.app-icon-wrap{{display:flex;justify-content:center;margin-bottom:20px}}.app-icon{{width:96px;height:96px;border-radius:22px;background:linear-gradient(135deg,#1a1aff 0%,#9b30ff 50%,#ff3080 100%);display:flex;align-items:center;justify-content:center;font-size:2.8rem;box-shadow:0 8px 32px rgba(100,60,255,.45)}}
h1{{text-align:center;font-size:1.35rem;color:#fff;margin-bottom:4px}}.sub{{text-align:center;color:#555;font-size:.8rem;margin-bottom:22px}}
.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:18px}}@media(min-width:550px){{.cards{{grid-template-columns:repeat(4,1fr)}}}}
.card{{background:#161625;border:1px solid #222240;border-radius:14px;padding:16px 12px;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.4)}}
.card .ico{{font-size:1.9rem;margin-bottom:6px}}.card .val{{font-size:1.75rem;font-weight:700;color:#fff;line-height:1}}
.card .lbl{{font-size:.7rem;color:#666;margin-top:5px;text-transform:uppercase;letter-spacing:.05em}}
.card.green .val{{color:#3ddc84}}.card.red .val{{color:#ff5757}}.card.blue .val{{color:#4da6ff}}.card.gold .val{{color:#ffd060}}
.prog-wrap{{margin-bottom:18px}}.prog-label{{display:flex;justify-content:space-between;font-size:.8rem;color:#666;margin-bottom:6px}}
.prog{{height:10px;border-radius:6px;background:#1c1c30;overflow:hidden}}.prog-bar{{height:100%;border-radius:6px;background:linear-gradient(90deg,#3ddc84,#00c8ff);width:{bar_w}%;transition:width 1s ease}}
.block{{background:#161625;border:1px solid #222240;border-radius:14px;padding:16px;margin-bottom:14px}}
.block h2{{font-size:.9rem;color:#888;margin-bottom:12px;text-transform:uppercase;letter-spacing:.08em}}
.btn{{display:block;width:100%;padding:13px;border-radius:10px;text-align:center;font-weight:700;font-size:.9rem;text-decoration:none;margin-bottom:9px;transition:opacity .15s}}
.btn:active{{opacity:.75}}.btn.green{{background:#3ddc84;color:#000}}.btn.blue{{background:#4da6ff;color:#000}}.btn small{{display:block;font-weight:400;font-size:.75rem;opacity:.7;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}th{{padding:8px 6px;text-align:left;color:#555;border-bottom:1px solid #1f1f35;font-weight:500}}
td{{padding:7px 6px;border-bottom:1px solid #1a1a2e;vertical-align:middle}}tr:hover td{{background:#1a1a2e}}
.num{{text-align:right;color:#4da6ff;font-weight:600}}.badge{{background:#1f1f35;padding:2px 8px;border-radius:20px;font-size:.7rem;color:#888;white-space:nowrap}}
.lnk{{color:#4da6ff;text-decoration:none;font-size:.75rem;word-break:break-all}}.lnk:hover{{text-decoration:underline}}.note{{color:#444;font-size:.72rem;margin-top:8px;text-align:center}}
</style></head><body>
<div class="app-icon-wrap"><div class="app-icon">📺</div></div><h1>IPTV Mega Aggregator</h1><p class="sub">Обновлено: {now}</p>
<div class="cards"><div class="card blue"><div class="ico">📡</div><div class="val">{total:,}</div><div class="lbl">Сканировано</div></div><div class="card green"><div class="ico">✅</div><div class="val">{working:,}</div><div class="lbl">Рабочих</div></div>
<div class="card red"><div class="ico">❌</div><div class="val">{dead:,}</div><div class="lbl">Нерабочих</div></div><div class="card gold"><div class="ico">📊</div><div class="val">{pct}%</div><div class="lbl">Живых</div></div></div>
<div class="prog-wrap"><div class="prog-label"><span>Живых потоков</span><span>{pct}%</span></div><div class="prog"><div class="prog-bar"></div></div></div>
<div class="block"><h2>📥 Плейлисты</h2>
<a href="{checked_url}" class="btn green">🟢 CHECKED.m3u — рабочие каналы <small>{working:,} каналов · рекомендуется</small></a>
<a href="{raw_url}" class="btn blue">🔵 RAW.m3u — все собранные <small>{total:,} каналов · без проверки</small></a></div>
<div class="block"><h2>🗂 Топ групп</h2><table><tr><th>Группа</th><th style="text-align:right">Каналов</th></tr>{groups_rows}</table></div>
<div class="block"><h2>📋 Рабочие каналы (первые 300)</h2><table><tr><th>Название</th><th>Группа</th><th>URL</th></tr>{ch_rows}</table><p class="note">Показано 300 из {working:,}. Полный список в CHECKED.m3u</p></div></body></html>"""


def job3_run():
    log = make_logger("JOB3")
    raw_channels = load_json(RAW_FILE)
    webtv_channels = load_json(WEBTV_FILE)
    log.info("📂 Загружено: raw=%d, webtv=%d", len(raw_channels), len(webtv_channels))

    seen = set()
    merged = []
    for ch in raw_channels + webtv_channels:
        if ch["url"] not in seen:
            seen.add(ch["url"])
            merged.append(ch)

    log.info("🔀 После мёрджа: %d уникальных каналов", len(merged))
    if not merged:
        log.error("❌ Нет каналов для проверки!")
        return False

    client = HTTPClient(retries=0)
    checked = job3_check_all(merged, client)
    client.close()

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    write_m3u(merged, RAW_M3U)
    write_m3u(checked, CHECKED_M3U)

    report = job3_build_report(merged, checked)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(report)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    readme = f"# 📺 IPTV Mega Aggregator\n\n**Обновлено:** `{now}`\n\n| | |\n|---|---|\n| 📡 Сканировано | **{len(merged):,}** |\n| ✅ Рабочих | **{len(checked):,}** |\n| ❌ Нерабочих | {len(merged)-len(checked):,} |\n\n[📊 Отчёт](report.html) · [🟢 CHECKED.m3u](CHECKED.m3u) · [🔵 RAW.m3u](RAW.m3u)\n"
    with open(README_MD, "w", encoding="utf-8") as f:
        f.write(readme)

    log.info("✅ Сохранено: %s (%d), %s, %s", CHECKED_M3U, len(checked), REPORT_HTML, README_MD)
    return True


def start_heartbeat(interval: int = 120):
    log = make_logger("HEARTBEAT")
    def beat():
        while True:
            log.info("💓 heartbeat — pipeline still running...")
            time.sleep(interval)
    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return t


def main():
    job_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JOB_ID", "all").strip()
    log = make_logger("MAIN")
    mode_label = "ALL JOBS" if job_id == "all" else f"JOB {job_id}"
    log.info("🚀 IPTV Pipeline — режим: %s", mode_label)

    if os.environ.get("CI") == "true":
        global CHECK_WORKERS, FETCH_TIMEOUT
        CHECK_WORKERS = int(os.environ.get("CHECK_WORKERS", "40"))
        FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "15"))
        log.info("🔧 CI mode: workers=%d, timeout=%ds", CHECK_WORKERS, FETCH_TIMEOUT)

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    hb_thread = start_heartbeat(90) if job_id in ("3", "all") else None
    if hb_thread:
        log.info("💓 Heartbeat started (90s interval)")

    client = HTTPClient(retries=1)
    try:
        if job_id in ("1", "all"):
            log.info("▶️ Запуск JOB 1 — Сбор плейлистов")
            channels = job1_collect(client)
            if not channels and job_id == "1":
                log.error("❌ Каналы не найдены!")
                sys.exit(1)
            if channels:
                save_json(channels, RAW_FILE)
                write_m3u(channels, RAW_M3U)
                log.info("💾 JOB 1: %s (%d каналов)", RAW_FILE, len(channels))

        if job_id in ("2", "all"):
            log.info("▶️ Запуск JOB 2 — WebTV Scraper")
            channels = job2_collect(client)
            webtv_m3u = os.path.join(OUTPUT_FOLDER, "webtv.m3u")
            write_m3u(channels, webtv_m3u)
            save_json(channels, WEBTV_FILE)
            log.info("💾 JOB 2: %s (%d каналов)", WEBTV_FILE, len(channels))

        if job_id in ("3", "all"):
            log.info("▶️ Запуск JOB 3 — Проверка + Отчёт")
            success = job3_run()
            if not success and job_id == "3":
                sys.exit(1)

    except KeyboardInterrupt:
        log.warning("⚠️ Прервано пользователем")
        sys.exit(130)
    except Exception as e:
        log.error("❌ Ошибка: %s", str(e))
        import traceback
        log.debug(traceback.format_exc())
        sys.exit(1)
    finally:
        client.close()

    log.info("✅ 🎉 Все джобы завершены успешно!" if job_id == "all" else f"✅ JOB {job_id} завершён")


if __name__ == "__main__":
    main()
