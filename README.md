# 📚 TIE e-Library → WhatsApp Monitor

A tiny **free** agent that watches the [TIE Online Library](https://ol.tie.go.tz)
(Tanzania Institute of Education) and **sends you a WhatsApp message** whenever
something changes about the books — with a special eye on the
**Divinity Form 6** book:

> `https://ol.tie.go.tz/uploaded_files/books/adv_secondary/frmvi/Divinity/Divinity_Form_6.html`

It runs on **GitHub Actions** (free, no server needed) and sends WhatsApp
messages through **CallMeBot** (free for personal use).

---

## 🔔 What triggers a WhatsApp message

| Event | Message |
|---|---|
| Divinity Form 6 becomes available on the library | 🎯 *Divinity Form 6 is on the TIE e-Library!* |
| Divinity Form 6 content changes/updated | 📝 *book was updated* |
| Divinity Form 6 disappears from its URL | ⚠️ warning |
| Any new book detected in the library | 📚 *New book(s) detected* |
| New book-related TIE announcement (news page) | 📰 *TIE news* |
| Library unreachable for 7+ days straight | ⚠️ heads-up (once) |

## 🛠 How it works (why it looks a bit clever)

`ol.tie.go.tz` is frequently **unreachable from outside Tanzania** — its server
drops connections from US/EU datacenters (where GitHub Actions runs). One
plain "check the website" script would therefore be blind most of the time.
This monitor instead combines **four redundant signal paths**, every run:

```
 GitHub Actions (every 4h, free)
        │
        ├─ 1) DIRECT https probes ────────── ol.tie.go.tz  (used whenever reachable)
        │       the Divinity F6 URL + 3 alternative URL patterns it might use,
        │       plus the library's JSON catalogue endpoints
        │
        ├─ 2) Wayback "Save Page Now" ───── web.archive.org (active probe)
        │       archive.org fetches the page FOR us and archives it fresh;
        │       homepage-vs-book comparison tells "404" apart from "site down"
        │
        ├─ 3) Wayback CDX index ─────────── (passive probe)
        │       any newly archived book URL = a new book appeared
        │
        └─ 4) www.tie.go.tz/news ────────── always reachable
                TIE announces new books in its news (Swahili keyword filter)
        │
        ▼
   state.json (diffed against previous run, committed to the repo)
        │
        ▼
   WhatsApp via CallMeBot → your phone 📲
```

Everything the monitor learns is stored in **`state.json`** and committed back
to this repo after every run, so the next run can diff against it.

---

## ✅ One-time setup (5 minutes)

**Step 1 — activate CallMeBot for your WhatsApp (from the phone that should
receive the alerts):**

1. Add this number to your phone contacts: **+34 684 783 347** (name it anything, e.g. *CallMeBot*)
2. Using WhatsApp, send it this exact message:
   ```
   I allow callmebot to send me messages
   ```
3. Wait ~1 minute. The bot replies:
   ```
   API Activated for your phone number. Your APIKEY is 123456
   ```
   *(if nothing arrives within 2 minutes, retry after 24h — it's their free-tier quirk)*

**Step 2 — add the API key as a GitHub secret:**

1. Open this repo → **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `CALLMEBOT_APIKEY`  Value: *(the key the bot sent you)*

> `WHATSAPP_PHONE` is already configured (`+255765793764`).
> To change the receiving number: edit/update that same secret.

**Step 3 — test it:**

- Repo → **Actions → TIE Library Monitor → Run workflow →**
  tick **"Send a test WhatsApp message"** → **Run workflow**
- You should receive a ✅ test message on WhatsApp within ~2 minutes.

---

## 🧪 Manual run / checking the monitor

- **Actions → TIE Library Monitor → Run workflow** (leave the test box unticked)
  runs one full monitoring cycle immediately — the log shows exactly what each
  signal path found.
- Each scheduled run's log doubles as a health record: direct reachability,
  Wayback save results, CDX index size, news parsing, etc.

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
| `monitor.py` | The whole agent (~450 lines, stdlib only, no dependencies) |
| `state.json` | Memory of what has been seen (auto-updated each run) |

## 🔧 Fine-tuning

All knobs are constants at the top of `monitor.py`:

- `CANDIDATE_PATHS` — extra URL patterns probed for the Divinity F6 book
- `NEWS_KEYWORDS` — which news titles count as "book-related"
- `OUTAGE_ALERT_DAYS` — when to warn about prolonged unreachability
- `API_PROBES` — library API endpoints tried when the site is reachable

## ⚠️ Known limitations (honesty section)

- Because the TIE library blocks most foreign IPs, the *fastest* signals come
  from the Wayback Machine and TIE's news page; a brand-new book might be
  noticed with a delay of a few hours to a day rather than within minutes.
- When the library *is* directly reachable (it happens intermittently), the
  monitor also checks the live catalogue — those runs are exact.
- Wayback's Save-Page-Now has a rate limit (the script already paces itself),
  and the CDX index can lag a few minutes behind a save.

## 🔒 Security notes

- No credentials live in the code — the phone number and API key are GitHub
- **Actions secrets**, and state.json contains only public information.
- If you shared your GitHub PAT with anyone (e.g. while setting this up),
  **revoke/regenerate it** afterwards: GitHub → Settings → Developer settings →
  Fine-grained tokens.
