# 📚 TIE e-Library → WhatsApp Monitor

A tiny **free** agent that watches the whole [TIE Online Library](https://ol.tie.go.tz)
(Tanzania Institute of Education) and **sends you a WhatsApp message whenever
any book is uploaded** — every subject, level and form — plus book-related TIE
news.

It runs on **GitHub Actions** (free, no server needed) and sends WhatsApp
messages through **CallMeBot** (free for personal use).

---

## 🔔 What triggers a WhatsApp message

| Event | Message |
|---|---|
| **Any new book uploaded** to the library (exact, when the site is directly reachable) | 📚 *New book(s) uploaded to the TIE e-Library* |
| New book visible on the library homepage (via web archive) | 📚 *New book(s) visible on the TIE e-Library homepage* |
| New book detected in the Wayback index | 📚 *New book(s) detected (via Wayback index)* |
| An existing book file is **updated / re-uploaded** | 📝 *Book file(s) updated* |
| A book is **removed or renamed** | ⚠️ *Book(s) no longer listed* |
| New book-related TIE announcement (news page) | 📰 *TIE news* |
| The library comes back online after 12+ h unreachable | ℹ️ heads-up |
| Library unreachable for 7+ days straight | ⚠️ heads-up (once) |

## 🛠 How it works (why it looks a bit clever)

`ol.tie.go.tz` is frequently **unreachable from outside Tanzania** — its server
drops connections from US/EU datacenters (where GitHub Actions runs). One
plain "check the website" script would therefore be blind most of the time.
This monitor instead combines **four redundant signal paths**, every run:

```
 GitHub Actions (every 4h, free)
        │
        ├─ 1) DIRECT https ─────────────── ol.tie.go.tz (used whenever reachable)
        │       full crawl of the /uploaded_files/books/ tree,
        │       sitemap.xml, and JSON catalogue API probes.
        │       These runs are exact: every book file is compared
        │       against the previous run (new / updated / removed).
        │
        ├─ 2) Wayback "Save Page Now" ──── web.archive.org (active probe)
        │       archive.org fetches the library homepage FOR us;
        │       the fresh snapshot is parsed for book links
        │
        ├─ 3) Wayback CDX index ────────── (passive probe)
        │       any newly archived book URL = a new book appeared;
        │       changed content digests = a book was updated
        │
        └─ 4) www.tie.go.tz/news ──────── always reachable
                TIE announces new books in its news (Swahili keyword filter)
        │
        ▼
   state.json (diffed against previous run, committed to the repo)
        │
        ▼
   WhatsApp via CallMeBot → your phone 📲
```

Everything the monitor learns is stored in **`state.json`** and committed back
to this repo after every run, so the next run can diff against it. Each signal
path keeps its **own baseline**, so a path coming back after a failure never
floods you with everything it sees.

---

## ✅ Setup (already done — for reference)

**CallMeBot (WhatsApp gateway):**

1. From the phone that should receive alerts, WhatsApp this exact message to
   the CallMeBot number (**+34 684 783 347**):
   ```
   I allow callmebot to send me messages
   ```
2. The bot replies with your API key.

**GitHub secrets** (repo → **Settings → Secrets and variables → Actions**):

| Secret | Value |
|---|---|
| `WHATSAPP_PHONE` | ✅ configured (`255765793764`) |
| `CALLMEBOT_APIKEY` | ✅ configured |

**Test it anytime:** repo → **Actions → TIE Library Monitor → Run workflow →**
tick **"Send a test WhatsApp message"** → **Run workflow**. A ✅ test message
should arrive within ~2 minutes.

---

## 🧪 Manual run / checking the monitor

- **Actions → TIE Library Monitor → Run workflow** (leave the test box unticked)
  runs one full monitoring cycle immediately — the log shows exactly what each
  signal path found.
- Each scheduled run's log doubles as a health record: direct reachability,
  crawl size, Wayback save results, CDX index size, news parsing, etc.
- `state.json` shows the current baseline (book counts, last runs, outage status).

## ⏱ Changing the check frequency

Edit the `cron` line in `.github/workflows/monitor.yml`
(`"0 */4 * * *"` = every 4 hours). GitHub Actions may delay scheduled runs a few
minutes during peak load — that's normal.

> **Cost note:** this repo is private, so free Actions minutes are capped at
> 2,000/month. At every-4-hours and ~2–4 min/run you'll use ~500–700 — safe.
> Making the repo **public** removes the limit entirely if you ever want
> hourly checks.

## 📁 Files

| File | Purpose |
|---|---|
| `.github/workflows/monitor.yml` | The schedule + runner (GitHub Actions) |
| `monitor.py` | The whole agent (~660 lines, stdlib only, no dependencies) |
| `state.json` | Memory of what has been seen (auto-updated each run) |

## 🔧 Fine-tuning

All knobs are constants at the top of `monitor.py`:

- `CRAWL_MAX_PAGES` / `CRAWL_MAX_DEPTH` / `CRAWL_TIME_BUDGET` — size of the
  live book-tree crawl
- `API_PROBES` — library API endpoints tried when the site is reachable
- `NEWS_KEYWORDS` — which news titles count as "book-related"
- `OUTAGE_ALERT_DAYS` — when to warn about prolonged unreachability
- `BACK_ONLINE_HOURS` — minimum outage length before a "back online" note

## ⚠️ Known limitations (honesty section)

- Because the TIE library blocks most foreign IPs, the *fastest* signals come
  from the Wayback Machine and TIE's news page; a brand-new book might be
  noticed with a delay of a few hours to a day rather than within minutes —
  **when the site accepts direct connections, detection is exact and complete**.
- The very first successful direct crawl only records a baseline (no alert for
  the whole catalogue). Books uploaded *during* the current outage may be
  caught by the news/Wayback paths; once the baseline exists, every later
  upload (even after outages) is detected by diffing against the saved state.
- Wayback's Save-Page-Now has a rate limit (the script already paces itself),
  and the CDX index can lag a few minutes behind a save.

## 🔒 Security notes

- No credentials live in the code — the phone number and API key are GitHub
  **Actions secrets**, and state.json contains only public information.
- If you shared your GitHub PAT with anyone (e.g. while setting this up),
  **revoke/regenerate it** afterwards: GitHub → Settings → Developer settings →
  Fine-grained tokens.
