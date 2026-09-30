#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TIE e-Library Monitor — whole-library edition
============================================
Watches https://ol.tie.go.tz (Tanzania Institute of Education online library)
for ANY new book upload — every subject, level and form — and sends WhatsApp
alerts through CallMeBot (free for personal use).

Why multiple signal paths?
  ol.tie.go.tz is frequently unreachable from outside Tanzania (connection
  timeouts from US/EU datacenters). The monitor therefore combines:

    1. DIRECT HTTPS enumeration — full crawl of the /uploaded_files/books/
       tree (directory listings), sitemap.xml and catalogue API probes,
       whenever the site accepts us. These runs are exact.
    2. Wayback "Save Page Now" — archive.org fetches the library homepage
       for us; the fresh snapshot is parsed for book links.
    3. Wayback CDX index — passive: book pages archived by anyone;
       new URLs and changed content digests.
    4. www.tie.go.tz/news — always reachable; TIE announces new books.

Configuration (GitHub repository secrets):
    WHATSAPP_PHONE     e.g. 255765793764
    CALLMEBOT_APIKEY   one-time free registration, see README.md

State is kept in state.json (committed back to the repo after every run).
"""

import concurrent.futures
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------- configuration

OL = os.environ.get("TIE_OL_BASE", "https://ol.tie.go.tz")
WWW_NEWS = "https://www.tie.go.tz/news"

BOOKS_PREFIX = "/uploaded_files/books/"        # the whole library lives here

# Possible public catalogue endpoints of the library API (probed when reachable).
API_PROBES = [
    "/api/",
    "/api/books",
    "/api/v1/books",
    "/api/public/books",
    "/api/books/public",
    "/api/catalog",
    "/api/library",
]

# Wayback CDX prefix covering every book file in the library.
BOOK_CDX_PREFIX = "ol.tie.go.tz/uploaded_files/books*"

# News items are only reported when the title/slug mentions one of these.
NEWS_KEYWORDS = [
    "vitabu", "kitabu", "book", "library", "maktaba", "divinity", "dini",
]

DIRECT_TIMEOUT = 25          # per-request timeout for direct probes (connect hangs!)
API_TIMEOUT = 25
CDX_TIMEOUT = 75
SPN_TIMEOUT = 80
NEWS_TIMEOUT = 40

CRAWL_MAX_PAGES = 200        # safety cap for the tree crawl
CRAWL_MAX_DEPTH = 6          # books/<level>/<form>/<subject>/<book>/<file>.html
CRAWL_DELAY = 0.35           # polite delay between listing fetches (seconds)
CRAWL_TIME_BUDGET = 330      # hard wall-clock budget for the crawl (seconds)

OUTAGE_ALERT_DAYS = 7        # alert once if the library is unreachable this long
BACK_ONLINE_HOURS = 12       # only announce recovery after this long down

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ------------------------------------------------------------------- utilities

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


class R:
    """Tiny HTTP result object."""
    __slots__ = ("status", "final_url", "body", "err")

    def __init__(self, status, final_url, body, err):
        self.status, self.final_url, self.body, self.err = status, final_url, body, err

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 400


_SSL_STRICT = ssl.create_default_context()
_SSL_LOOSE = ssl.create_default_context()
_SSL_LOOSE.check_hostname = False
_SSL_LOOSE.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=30):
    """GET a URL. Never raises — network failures return R(status=None).
    Retries once without certificate verification on TLS problems (some
    TIE hosts have historically served mismatched certificates)."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for attempt, ctx in enumerate((_SSL_STRICT, _SSL_LOOSE)):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return R(resp.status, resp.geturl(), resp.read(), None)
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            return R(e.code, url, body, f"HTTP {e.code}")
        except Exception as e:  # timeouts, DNS, refused, TLS...
            if attempt == 0 and re.search(r"cert|ssl|tls", str(e), re.I):
                continue      # retry without verification
            return R(None, url, b"", f"{type(e).__name__}: {e}"[:160])
    return R(None, url, b"", "unreachable")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def parallel_get(urls, timeout, workers=8):
    """Fetch several URLs concurrently. Returns {url: R}."""
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(http_get, u, timeout): u for u in urls}
        for f in concurrent.futures.as_completed(futs):
            out[futs[f]] = f.result()
    return out


# ------------------------------------------------------------------ Wayback

def cdx(pattern, collapse=False, limit=5000, timeout=CDX_TIMEOUT):
    """Query the Wayback CDX index. Returns list of row dicts (may be empty)."""
    q = ("https://web.archive.org/cdx/search/cdx?url="
         + urllib.parse.quote(pattern, safe="")
         + "&output=json&fl=timestamp,original,statuscode,digest&limit=" + str(limit))
    if collapse:
        q += "&collapse=urlkey"
    r = http_get(q, timeout=timeout)
    if not r.body:
        return []
    try:
        data = json.loads(r.body.decode("utf-8", "replace"))
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    hdr = data[0]
    return [dict(zip(hdr, row)) for row in data[1:]]


def spn(url, timeout=SPN_TIMEOUT):
    """Trigger Wayback 'Save Page Now'. Returns (saved, snapshot_ts, note)."""
    r = http_get("https://web.archive.org/save/" + url, timeout=timeout)
    m = re.search(r"/web/(\d{12,14})/", r.final_url or "")
    if m and r.ok:
        return True, m.group(1), r.final_url
    return False, None, (r.err or f"status={r.status} final={r.final_url}")


# Book links as they appear inside an archived (or live) library page.
SNAP_BOOK_RE = re.compile(
    r"(?:https?://ol\.tie\.go\.tz)?(/uploaded_files/books/"
    r"[A-Za-z0-9\-_.%!/()+,' ]+?\.(?:html|htm|pdf|epub))", re.I)


def spn_snapshot_book_links(ts, original_url):
    """Fetch the snapshot that Save-Page-Now just created and pull book links."""
    snap = f"https://web.archive.org/web/{ts}id_/{original_url}"
    r = http_get(snap, timeout=SPN_TIMEOUT)
    if not r.ok or not r.body:
        return []
    text = r.body.decode("utf-8", "replace")
    links = set()
    for m in SNAP_BOOK_RE.finditer(text):
        path = m.group(1).split("?")[0].split("#")[0]
        links.add(OL + path)
    return sorted(links)


# ------------------------------------------------------------------ news page

NEWS_ITEM_RE = re.compile(
    r'<a\s+href="(https://www\.tie\.go\.tz/news/[a-z0-9-]+)"[^>]*>(.*?)</a>', re.S)


def news_items():
    """Parse the (always-reachable) TIE news listing. Returns (items, error)."""
    r = http_get(WWW_NEWS, timeout=NEWS_TIMEOUT)
    if not r.ok:
        return None, (r.err or f"status={r.status}")
    items, seen = [], set()
    for m in NEWS_ITEM_RE.finditer(r.body.decode("utf-8", "replace")):
        url, inner = m.group(1), m.group(2)
        date_m = re.search(r"(\d{1,2}\s+\w+,\s+\d{4})", inner)
        title_m = re.search(r'text-bold">([^<]+)<', inner)
        alt_m = re.search(r'alt="([^"]+)"', inner)
        title = (title_m.group(1) if title_m else (alt_m.group(1) if alt_m else "")).strip()
        if title and url not in seen:
            seen.add(url)
            items.append({"url": url, "title": title,
                          "date": date_m.group(1) if date_m else ""})
    return items, None


# ------------------------------------------------------------------ helpers

def book_root(url):
    """Collapse a book file URL to its 'book' folder for change detection."""
    p = urllib.parse.urlparse(url).path
    if "/files/" in p:
        p = p.split("/files/")[0]
    parts = [x for x in p.split("/") if x]
    if parts and parts[-1].lower().endswith((".html", ".htm", ".pdf", ".epub")):
        parts.pop()                      # entry file -> book folder
    while parts and parts[-1].lower() in ("flip", "files", "thumb"):
        parts.pop()                      # flipbook wrapper folders
    return "/" + "/".join(parts)


def pretty_book_name(root):
    return root.rstrip("/").rsplit("/", 1)[-1].replace("_", " ").replace("%20", " ").strip()


def extract_book_urls(obj, out=None):
    """Recursively pull uploaded_files/books/*.html URLs (and titles) from API JSON."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("name") or obj.get("book_title")
        for v in obj.values():
            if isinstance(v, str) and "uploaded_files/books/" in v and v.lower().endswith((".html", ".htm")):
                u = v.split("?")[0].split("#")[0]
                if u.startswith("/"):
                    u = OL + u           # absolutize relative API paths
                out[u] = str(title or v.rsplit("/", 1)[-1])
            else:
                extract_book_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_book_urls(v, out)
    return out


def sitemap_book_urls():
    """Fetch sitemap.xml (following one level of sitemap index). Book URLs only."""
    found, queue, seen = [], [OL + "/sitemap.xml"], set()
    while queue and len(seen) < 5:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = http_get(sm, timeout=DIRECT_TIMEOUT)
        if not r.ok or not r.body:
            continue
        text = r.body.decode("utf-8", "replace")
        locs = [m.group(1).strip()
                for m in re.finditer(r"<loc>\s*([^<]+?)\s*</loc>", text)]
        for loc in locs:
            path = urllib.parse.urlsplit(urllib.parse.urljoin(OL, loc)).path
            if BOOKS_PREFIX in path:
                found.append(urllib.parse.urljoin(OL, loc))
        for loc in locs:                 # nested sitemap indexes, same host only
            if (loc.lower().endswith(".xml")
                    and urllib.parse.urlsplit(loc).netloc == urllib.parse.urlsplit(OL).netloc):
                queue.append(loc)
    return sorted(set(found))


def crawl_books_tree():
    """BFS through the directory listings under /uploaded_files/books/.

    Returns (dirs, files, root_ok):
      dirs   {listing_url: {"ok": bool, "hash": str, "children": [names]}}
      files  {book_file_url: listing_url}
      root_ok  True when the root listing answered HTTP 200
    """
    root = OL + BOOKS_PREFIX
    root_netloc = urllib.parse.urlsplit(root).netloc
    dirs, files = {}, {}
    root_ok = False
    queue = [(root, 0)]
    fetched = 0
    deadline = time.monotonic() + CRAWL_TIME_BUDGET
    while queue and fetched < CRAWL_MAX_PAGES and time.monotonic() < deadline:
        url, depth = queue.pop(0)
        if url in dirs:
            continue
        r = http_get(url, timeout=DIRECT_TIMEOUT)
        fetched += 1
        if r.status != 200 or not r.body:
            dirs[url] = {"ok": False, "hash": None, "children": []}
            if fetched <= 3 or r.status not in (None,):
                log(f"    {urllib.parse.urlsplit(url).path} -> {r.status}"
                    + (f" ({(r.err or '')[:60]})" if r.status is None else ""))
            continue
        body = r.body
        text = body.decode("utf-8", "replace")
        here = urllib.parse.urlsplit(url).path
        children = []
        for href in re.findall(r'href=[\'"]([^\'"#]+)[\'"]', text):
            if href.startswith(("javascript:", "mailto:", "tel:", "?")):
                continue
            absu = urllib.parse.urljoin(url, href).split("#")[0]
            u = urllib.parse.urlsplit(absu)
            if (u.scheme not in ("http", "https")
                    or u.netloc != root_netloc
                    or not u.path.startswith(BOOKS_PREFIX)
                    or u.query):
                continue
            name = u.path.rstrip("/").rsplit("/", 1)[-1]
            if u.path.endswith("/"):
                children.append(name + "/")
                if depth + 1 <= CRAWL_MAX_DEPTH and absu not in dirs:
                    queue.append((absu.rstrip("/") + "/", depth + 1))
            elif u.path != here:
                children.append(name)
                files[absu] = url
        dirs[url] = {"ok": True, "hash": sha256_hex(body), "children": sorted(children)}
        if url == root:
            root_ok = True
        time.sleep(CRAWL_DELAY)
    return dirs, files, root_ok


# ------------------------------------------------------------------ state

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️  state.json unreadable ({e}); starting fresh")
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# ------------------------------------------------------------------ WhatsApp

def send_whatsapp(text):
    phone = os.environ.get("WHATSAPP_PHONE", "").strip()
    key = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not key:
        log("⚠️  WhatsApp NOT configured — set the repository secrets "
            "WHATSAPP_PHONE and CALLMEBOT_APIKEY (see README.md)")
        return False
    api = ("https://api.callmebot.com/whatsapp.php?"
           + urllib.parse.urlencode({"phone": phone, "apikey": key, "text": text}))
    r = http_get(api, timeout=40)
    body = (r.body or b"").decode("utf-8", "replace").replace("\n", " ")[:220]
    lowered = body.lower()
    failed = (not r.ok) or any(k in lowered for k in
                               ("apikey is invalid", "api key", "error", "not registered"))
    if failed:
        log(f"❌ WhatsApp send failed (status={r.status}) {body}")
    else:
        log(f"✅ WhatsApp message sent (status={r.status})")
    return not failed


# ------------------------------------------------------------------ main

def main():
    test_mode = os.environ.get("TEST_MODE") == "1"
    state = load_state()
    now = datetime.now(timezone.utc)

    if test_mode:
        log("TEST MODE — sending a test WhatsApp message and exiting")
        send_whatsapp("✅ TIE Library Monitor — test message.\n"
                      "If you can read this, WhatsApp alerts are working! 🤖")
        return 0

    first_run = not state.get("first_run_done")
    messages = []          # alert texts for this run
    prev_unreachable_since = state.get("unreachable_since")
    prev_reachable = state.get("last_direct_reachable")

    # -------------------------------------------------- phase 1: direct contact
    log("── Phase 1: direct contact with ol.tie.go.tz")
    home = http_get(OL + "/", timeout=DIRECT_TIMEOUT)
    ol_reachable = home.status is not None
    enumerated = set()     # every book URL seen directly this run
    crawl_dirs, crawl_root_ok = {}, False

    if ol_reachable:
        log(f"  ✅ ol.tie.go.tz reachable (status={home.status})")
        state["unreachable_since"] = None
        state["outage_alerted"] = False

        log("  crawling the /uploaded_files/books/ tree ...")
        crawl_dirs, crawl_files, crawl_root_ok = crawl_books_tree()
        enumerated |= set(crawl_files)
        log(f"  crawl: {len(crawl_dirs)} listing pages, {len(crawl_files)} "
            f"book files, root_ok={crawl_root_ok}")

        sm = sitemap_book_urls()
        if sm:
            enumerated |= set(sm)
            log(f"  sitemap.xml: {len(sm)} book URLs")

        cat_urls = {}
        api = parallel_get([OL + p for p in API_PROBES], API_TIMEOUT)
        for u, r in sorted(api.items()):
            if r.status == 200 and r.body[:1] in (b"{", b"["):
                try:
                    data = json.loads(r.body.decode("utf-8", "replace"))
                    found = extract_book_urls(data)
                    if found:
                        cat_urls.update(found)
                except Exception:
                    pass
        if cat_urls:
            enumerated |= set(cat_urls)
            log(f"  catalogue API: {len(cat_urls)} book URLs")
    else:
        log(f"  ❌ ol.tie.go.tz unreachable ({(home.err if home else '?')})")
        if not state.get("unreachable_since"):
            state["unreachable_since"] = now.isoformat()

    # --- direct-set diffing (own baseline; first success only records it) ---
    new_direct, updated_direct, removed_files, removed_dirs = [], [], [], []
    prev_dirs = state.get("direct_dirs", {}) or {}
    direct_ok = bool(enumerated) or crawl_root_ok
    if direct_ok:
        baseline = set(state.get("direct_books", []))
        if not state.get("direct_ever_ok"):
            log("  direct enumeration first success — recording baseline "
                f"({len(enumerated)} book files seen)")
            state["direct_books"] = sorted(baseline | enumerated
                                           | set(state.get("known_book_urls", [])))
        else:
            new_direct = sorted(enumerated - baseline)
            for durl, info in crawl_dirs.items():
                prev = prev_dirs.get(durl)
                if not (prev and prev.get("ok") and info.get("ok")):
                    continue
                gone = set(prev.get("children", [])) - set(info.get("children", []))
                for child in gone:
                    if child.endswith("/"):
                        removed_dirs.append((durl, child))
                    else:
                        removed_files.append(durl + child)
                if (not gone
                        and prev.get("hash") != info.get("hash")
                        and prev.get("children") == info.get("children")):
                    updated_direct.append(durl)
            removed_urls = {u for u in removed_files}
            if len(removed_urls) > max(10, len(baseline) // 2):
                log(f"  crawl looks partial ({len(removed_urls)} removals) — "
                    "suppressing removal alerts this run")
                removed_files, removed_dirs = [], []
            kept = (set(state.get("direct_books", [])) | enumerated) - removed_urls
            for durl, dirname in removed_dirs:
                prefix = durl + dirname
                kept = {u for u in kept if not u.startswith(prefix)}
            state["direct_books"] = sorted(kept)
        state["direct_ever_ok"] = True
        state["direct_dirs"] = {u: {"ok": i.get("ok"), "hash": i.get("hash"),
                                    "children": i.get("children")}
                                for u, i in crawl_dirs.items()}

    # -------------------------------------------------- phase 2: Wayback SPN
    spn_books = []
    if not ol_reachable:
        log("── Phase 2: Wayback Save-Page-Now (active probe)")
        saved, ts, note = spn(OL + "/")
        state["last_spn_home_saved"] = saved
        log(f"  SPN homepage: saved={saved} ts={ts or '-'} ({(note or '')[:90]})")
        if saved and ts:
            spn_books = spn_snapshot_book_links(ts, OL + "/")
            log(f"  homepage snapshot exposes {len(spn_books)} book links")
            prev_spn = set(state.get("spn_books", []))
            if not state.get("spn_ever_ok"):
                state["spn_books"] = sorted(prev_spn | set(spn_books)
                                           | set(state.get("known_book_urls", [])))
                state["spn_ever_ok"] = True
                log("  snapshot source first success — recording baseline")
            else:
                new_books_spn = sorted(set(spn_books) - prev_spn)
                if new_books_spn:
                    spn_new = sorted({pretty_book_name(book_root(u))
                                      for u in new_books_spn})
                    extra = "" if len(spn_new) <= 6 else f"\n…and {len(spn_new) - 6} more"
                    messages.append(
                        "📚 *New book(s) visible on the TIE e-Library homepage* "
                        "(via web archive):\n• " + "\n• ".join(spn_new[:6]) + extra)
                state["spn_books"] = sorted(prev_spn | set(spn_books))
    else:
        state["last_spn_home_saved"] = False

    # -------------------------------------------------- phase 3: Wayback CDX
    log("── Phase 3: Wayback CDX index (passive signal)")
    all_rows = cdx(BOOK_CDX_PREFIX, collapse=True)
    ok_rows = [row for row in all_rows if str(row.get("statuscode")) == "200"]
    html_entries = [row for row in ok_rows
                    if (row.get("original") or "").lower()
                    .endswith((".html", ".htm", ".pdf"))]
    log(f"  unique archived book URLs: {len(html_entries)}")

    known_urls = set(state.get("known_book_urls", []))
    new_entries = [row for row in html_entries if row["original"] not in known_urls]
    roots = {book_root(row["original"]) for row in html_entries}
    known_roots = set(state.get("known_book_roots", []))
    new_roots = roots - known_roots

    prev_digests = state.get("cdx_digests", {}) or {}
    updated_cdx = []
    if state.get("cdx_seen_once") and html_entries:
        updated_cdx = [row["original"] for row in html_entries
                       if row.get("digest") and prev_digests.get(row["original"])
                       and prev_digests[row["original"]] != row["digest"]]
    if html_entries:      # keep old digests when the CDX query itself failed
        state["cdx_digests"] = {row["original"]: row.get("digest")
                                for row in html_entries if row.get("digest")}
        state["cdx_seen_once"] = True

    # -------------------------------------------------- phase 4: TIE news
    log("── Phase 4: TIE news page (www.tie.go.tz)")
    items, news_err = news_items()
    news_new = []
    if items is None:
        log(f"  ❌ news fetch failed: {news_err}")
    else:
        known_news = set(state.get("known_news", []))
        log(f"  news items on page: {len(items)}")
        for it in items:
            hay = (it["title"] + " " + it["url"]).lower()
            if it["url"] not in known_news and any(k in hay for k in NEWS_KEYWORDS):
                news_new.append(it)
        if news_new:
            for it in news_new:
                log(f"  📰 new book-related news: {it['title']}")

    # -------------------------------------------------- phase 5: analysis
    log("── Phase 5: analysis")
    if not first_run:
        # new books — live catalogue / sitemap (exact)
        if new_direct:
            pretty = sorted({pretty_book_name(book_root(u)) for u in new_direct})
            extra = "" if len(pretty) <= 6 else f"\n…and {len(pretty) - 6} more"
            messages.append(
                "📚 *New book(s) uploaded to the TIE e-Library:*\n• "
                + "\n• ".join(pretty[:6]) + extra)

        # new books — Wayback index (passive)
        if new_roots:
            pretty = sorted(pretty_book_name(r) for r in new_roots)
            extra = "" if len(pretty) <= 5 else f"\n…and {len(pretty) - 5} more"
            messages.append(
                "📚 *New book(s) detected in the TIE library* (via Wayback "
                "index):\n• " + "\n• ".join(pretty[:5]) + extra)

        # updated book files (same folder, changed content/listing)
        updated_names = sorted({pretty_book_name(d.rstrip("/")) for d in updated_direct})
        updated_names += sorted({pretty_book_name(book_root(u)) for u in updated_cdx})
        if updated_names:
            extra = "" if len(updated_names) <= 5 else f"\n…and {len(updated_names) - 5} more"
            messages.append(
                "📝 *Book file(s) updated on the TIE e-Library:*\n• "
                + "\n• ".join(sorted(set(updated_names))[:5]) + extra)

        # removed / renamed books
        if removed_files or removed_dirs:
            names = sorted({pretty_book_name(book_root(u)) for u in removed_files})
            names += sorted(d.rstrip("/") for _, d in removed_dirs)
            extra = "" if len(names) <= 5 else f"\n…and {len(names) - 5} more"
            messages.append(
                "⚠️ Book(s) no longer listed on the TIE e-Library (removed or "
                "renamed):\n• " + "\n• ".join(names[:5]) + extra)

        for it in news_new[:3]:
            messages.append(f"📰 *TIE news* (may relate to books):\n{it['title']}\n{it['url']}")

        uns = state.get("unreachable_since")
        if uns and not state.get("outage_alerted"):
            try:
                days = (now - datetime.fromisoformat(uns)).days
            except Exception:
                days = 0
            if days >= OUTAGE_ALERT_DAYS:
                messages.append(
                    f"⚠️ Heads-up: the TIE e-Library has been unreachable from my "
                    f"monitoring vantage points for {days} days.\nI keep watching "
                    "the Wayback index and TIE news in the meantime.")
                state["outage_alerted"] = True
    else:
        log("  first run — building baseline, no alerts for existing content")

    # library came back online after a longer outage
    if (ol_reachable and prev_unreachable_since
            and not first_run and prev_reachable is False):
        down_hours = 0.0
        try:
            down_hours = ((now - datetime.fromisoformat(prev_unreachable_since))
                          .total_seconds() / 3600)
        except Exception:
            pass
        if down_hours >= BACK_ONLINE_HOURS:
            messages.append(
                "ℹ️ The TIE e-Library is reachable again — live catalogue checks "
                "resumed. "
                + (f"{len(enumerated)} book files currently catalogued."
                   if enumerated else ""))

    # -------------------------------------------------- send / save state
    if messages:
        text = "🔔 *TIE Library update*\n\n" + "\n\n".join(messages)
        send_whatsapp(text[:1600])
    elif not first_run:
        log("no changes detected — no notification sent")

    if first_run:
        send_whatsapp("✅ *TIE Library Monitor is now ACTIVE.*\n"
                      "I watch the whole TIE e-Library for ANY new book upload "
                      "(every subject, level and form), plus book-related TIE "
                      "news.\n— your free GitHub Actions monitor 🤖")

    state.update({
        "first_run_done": True,
        "last_run": now.isoformat(),
        "last_direct_reachable": ol_reachable,
        "known_book_urls": sorted(known_urls | {row["original"] for row in html_entries}),
        "known_book_roots": sorted(known_roots | roots),
        "known_news": sorted(set(state.get("known_news", []))
                             | {i["url"] for i in (items or [])}),
    })
    state.pop("watch", None)          # legacy Divinity-watch state, no longer used
    save_state(state)
    log("state.json saved — done ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
