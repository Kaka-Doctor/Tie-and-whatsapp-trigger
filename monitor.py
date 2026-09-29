#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TIE e-Library Monitor
====================
Watches https://ol.tie.go.tz (Tanzania Institute of Education online library)
for new / changed books — with special attention to the Divinity Form 6 book —
and sends WhatsApp alerts through CallMeBot (free for personal use).

Why multiple signal paths?
  ol.tie.go.tz is frequently unreachable from outside Tanzania (connection
  timeouts from US/EU datacenters). The monitor therefore combines:

    1. DIRECT HTTPS probes        — used whenever the site happens to accept us
    2. Wayback "Save Page Now"    — archive.org fetches the page for us and
                                    records a fresh snapshot + content digest
    3. Wayback CDX index          — passive: snapshots archived by ANYONE
    4. www.tie.go.tz/news         — always reachable; TIE announces new books

Configuration (GitHub repository secrets):
    WHATSAPP_PHONE     e.g. +255765793764
    CALLMEBOT_APIKEY   one-time free registration, see README.md

State is kept in state.json (committed back to the repo after every run).
"""

import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------- configuration

OL = "https://ol.tie.go.tz"
WWW_NEWS = "https://www.tie.go.tz/news"

# The book we care about most (exact URL given by the user).
WATCH_URL_PATH = "/uploaded_files/books/adv_secondary/frmvi/Divinity/Divinity_Form_6.html"

# The real library uses different folder patterns for different classes
# (e.g. Form 5 = /adv_secondary/frmv/Stud_Book/Divinity/Divinity_F5.html),
# so we probe several plausible locations for the Form 6 Divinity book too.
CANDIDATE_PATHS = [
    WATCH_URL_PATH,
    "/uploaded_files/books/adv_secondary/frmvi/Stud_Book/Divinity/Divinity_F6.html",
    "/uploaded_files/books/adv_secondary/frmvi/Divinity/Divinity_F6.html",
    "/uploaded_files/books/adv_secondary/frmvi/Divinity/Divinity_Form_F6.html",
]

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
SPN_SLEEP = 14               # seconds between Save-Page-Now calls (anonymous rate limit)

OUTAGE_ALERT_DAYS = 7        # alert once if the library is unreachable this long

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


def http_get(url, timeout=30):
    """GET a URL. Never raises — network failures return R(status=None)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return R(resp.status, resp.geturl(), resp.read(), None)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return R(e.code, url, body, f"HTTP {e.code}")
    except Exception as e:  # timeouts, DNS, refused, TLS...
        return R(None, url, b"", f"{type(e).__name__}: {e}"[:160])


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
    if parts and parts[-1].lower().endswith((".html", ".htm")):
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

    # -------------------------------------------------- phase 1: direct probes
    log("── Phase 1: direct probes to ol.tie.go.tz")
    cand_urls = [OL + p for p in CANDIDATE_PATHS]
    home_url = OL + "/"
    direct = parallel_get(cand_urls + [home_url], DIRECT_TIMEOUT)
    home = direct.get(home_url)
    ol_reachable = home is not None and home.status is not None

    cat_urls = {}
    if ol_reachable:
        log(f"  ✅ ol.tie.go.tz IS reachable directly right now (status={home.status})")
        state["unreachable_since"] = None
        state["outage_alerted"] = False
        api = parallel_get([OL + p for p in API_PROBES], API_TIMEOUT)
        for u, r in sorted(api.items()):
            if r.status is not None:
                log(f"  probe {urllib.parse.urlsplit(u).path} -> {r.status} ({len(r.body)}B)")
            if r.status == 200 and r.body[:1] in (b"{", b"["):
                try:
                    data = json.loads(r.body.decode("utf-8", "replace"))
                    found = extract_book_urls(data)
                    if found:
                        cat_urls.update(found)
                except Exception:
                    pass
        if cat_urls:
            log(f"  catalogue API exposed {len(cat_urls)} book URLs")
    else:
        log(f"  ❌ ol.tie.go.tz unreachable directly ({(home.err if home else 'no result')})")
        if not state.get("unreachable_since"):
            state["unreachable_since"] = now.isoformat()

    cand_status = {u: (direct.get(u).status if direct.get(u) else None) for u in cand_urls}
    any_200 = [u for u, s in cand_status.items() if s == 200]
    any_404 = [u for u, s in cand_status.items() if s == 404]
    for u, s in cand_status.items():
        log(f"  candidate {urllib.parse.urlsplit(u).path[-45:]} -> {s}")

    # -------------------------------------------------- phase 2: Wayback SPN
    log("── Phase 2: Wayback Save-Page-Now (active probe)")
    watch_saved, watch_ts, watch_note = spn(OL + WATCH_URL_PATH)
    log(f"  SPN watch-url: saved={watch_saved} ts={watch_ts or '-'} ({watch_note[:90]})")
    home_saved = False
    if not ol_reachable:
        time.sleep(SPN_SLEEP)
        home_saved, home_ts, home_note = spn(home_url)
        log(f"  SPN homepage:  saved={home_saved} ts={home_ts or '-'} ({home_note[:90]})")

    # -------------------------------------------------- phase 3: Wayback CDX
    log("── Phase 3: Wayback CDX index (passive signal)")
    all_rows = cdx(BOOK_CDX_PREFIX, collapse=True)
    log(f"  unique archived book URLs: {len(all_rows)}")
    watch_rows = cdx("ol.tie.go.tz" + WATCH_URL_PATH, collapse=False, limit=50)
    latest_watch = watch_rows[-1] if watch_rows else None
    if latest_watch:
        log(f"  latest archived snapshot of watch url: ts={latest_watch.get('timestamp')} "
            f"status={latest_watch.get('statuscode')} digest={latest_watch.get('digest', '')[:12]}")

    html_entries = [row for row in all_rows
                    if (row.get("original") or "").lower().endswith((".html", ".htm"))]
    known_urls = set(state.get("known_book_urls", []))
    new_entries = [row for row in html_entries if row["original"] not in known_urls]
    roots = {book_root(row["original"]) for row in html_entries}
    known_roots = set(state.get("known_book_roots", []))
    new_roots = roots - known_roots

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
    watch_state = state.get("watch", {}) or {}
    prev_exists = watch_state.get("exists")       # None / True / False
    prev_digest = watch_state.get("digest")
    prev_found = watch_state.get("found_url")

    exists_now = None
    digest_now = None
    found_url = None

    if any_200:
        u = any_200[0]
        exists_now = True
        digest_now = "sha256:" + sha256_hex(direct[u].body)
        found_url = u
    elif ol_reachable and len(any_404) == len(cand_urls):
        exists_now = False                       # site up, all candidates 404
    elif watch_saved:
        exists_now = True                        # archive.org just saved it (2xx)
        digest_now = f"spn:{watch_ts}"
        found_url = OL + WATCH_URL_PATH
    elif home_saved and not watch_saved:
        exists_now = False                       # site reachable via archive, page missing
    if exists_now is None and latest_watch and str(latest_watch.get("statuscode")) in ("200", "-2xx"):
        exists_now = True                        # CDX has a 200 snapshot
        digest_now = f"cdx:{latest_watch.get('digest')}"
        found_url = latest_watch.get("original")

    verdict = {True: "AVAILABLE ✅", False: "not found ❌", None: "unknown ❓"}[exists_now]
    log(f"  Divinity Form 6 verdict: {verdict} (previous: {prev_exists})")

    # -------------------------------------------------- build alerts
    if not first_run:
        if exists_now is True and prev_exists is not True:
            messages.append(
                "🎯 *Divinity Form 6 is on the TIE e-Library!*\n"
                f"{found_url or prev_found or OL + WATCH_URL_PATH}")
        elif (exists_now is True and prev_exists is True
              and digest_now and prev_digest and digest_now != prev_digest):
            messages.append(
                "📝 *Divinity Form 6 book was updated/changed.*\n"
                f"{found_url or OL + WATCH_URL_PATH}")
        elif exists_now is False and prev_exists is True:
            messages.append(
                "⚠️ Divinity Form 6 is no longer accessible at its previous URL.\n"
                "(It may have been moved — check ol.tie.go.tz)")

        if new_roots:
            pretty = sorted(pretty_book_name(r) for r in new_roots)
            extra = "" if len(new_roots) <= 5 else f"\n…and {len(new_roots) - 5} more"
            messages.append(
                "📚 *New book(s) detected in the TIE library* (via Wayback index):\n• "
                + "\n• ".join(pretty[:5]) + extra)

        if cat_urls:
            known_cat = set(state.get("known_catalog_urls", []))
            fresh_cat = [u for u in cat_urls if u not in known_cat]
            if fresh_cat:
                titles = sorted({cat_urls[u] for u in fresh_cat})
                extra = "" if len(titles) <= 5 else f"\n…and {len(titles) - 5} more"
                messages.append(
                    "📚 *New book(s) on the TIE e-Library catalogue:*\n• "
                    + "\n• ".join(titles[:5]) + extra)
            state["known_catalog_urls"] = sorted(
                set(state.get("known_catalog_urls", [])) | set(cat_urls))

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
                    f"⚠️ Heads-up: the TIE e-Library has been unreachable from my monitoring "
                    f"vantage points for {days} days.\nI keep watching the Wayback index and "
                    "TIE news in the meantime.")
                state["outage_alerted"] = True
    else:
        log("  first run — building baseline, no alerts for existing content")

    # -------------------------------------------------- send / save state
    if messages:
        text = "🔔 *TIE Library update*\n\n" + "\n\n".join(messages)
        send_whatsapp(text[:1600])
    elif not first_run:
        log("no changes detected — no notification sent")

    if first_run:
        status_txt = ("already online 📖\n" + (found_url or OL + WATCH_URL_PATH)
                      if exists_now is True else
                      "not detected yet — you'll get a 🎯 message when it appears")
        send_whatsapp("✅ *TIE Library Monitor is now ACTIVE.*\n"
                      f"Watched book: Divinity Form 6 — {status_txt}.\n"
                      "I also watch for new TIE books and book-related TIE news.\n"
                      "— your free GitHub Actions monitor 🤖")

    state.update({
        "first_run_done": True,
        "last_run": now.isoformat(),
        "last_direct_reachable": ol_reachable,
        "last_spn_watch_saved": watch_saved,
        "last_spn_home_saved": home_saved,
        "known_book_urls": sorted(known_urls | {row["original"] for row in html_entries}),
        "known_book_roots": sorted(known_roots | roots),
        "known_news": sorted(set(state.get("known_news", []))
                             | {i["url"] for i in (items or [])}),
        "watch": {
            "url": WATCH_URL_PATH,
            "exists": exists_now,
            "digest": digest_now,
            "found_url": found_url,
            "checked_at": now.isoformat(),
            "direct_candidate_status": {urllib.parse.urlsplit(u).path: s
                                        for u, s in cand_status.items()},
        },
    })
    save_state(state)
    log("state.json saved — done ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
