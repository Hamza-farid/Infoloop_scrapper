# 📞 InfoLookup Scraper

Bulk phone number lookup against [infolookup.site](https://infolookup.site/) —
Playwright worker pool + Streamlit dashboard.

---

## Project Structure

```
infolookup_scraper/
├── app.py                  ← Streamlit dashboard (run this)
├── worker.py               ← Pooled async Playwright scraper (the engine)
├── run_cli.py              ← CLI runner for dev/testing (uses scraper/lookup.py)
├── requirements.txt
├── packages.txt            ← Streamlit Cloud system packages (chromium)
├── setup.sh                ← Streamlit Cloud: playwright install chromium
├── runs/                   ← Output per run (gitignored, resumable)
├── scraper/
│   ├── config.py           ← Selectors, timing, concurrency, block lists
│   └── lookup.py           ← Legacy single-number nodriver scraper (CLI only)
└── utils/
    ├── models.py           ← LookupResult / PersonRecord / AddressRecord
    └── phone_generator.py  ← Number range generator
```

`app.py` never scrapes. It launches `worker.py` as a separate process and tails
its JSONL output, so the UI stays responsive and a crash never loses results.

---

## How it works

1. Set the **area code**, **exchange** (middle 3) and **subscriber** (last 4) ranges
2. Pick **parallel browser pages** — this is the speed dial
3. **🚀 Start Lookup** → live progress, throughput and ETA
4. **⬇️ Download CSV** (one row per address; also available mid-run)

Results stream to `runs/<timestamp>/results.jsonl` as they complete. If a run
dies, **♻️ Resume unfinished run** in the sidebar picks up where it stopped —
already-completed numbers are skipped, not re-scraped.

---

## Performance

The scraper was ~8.5s per number. Where that went, and what changed:

| Change | Saving |
|---|---|
| Was reloading the whole page for every number — the site is a single-page app, so we now search repeatedly on one loaded page | ~2s |
| Removed hardcoded `sleep(1.5)` + `sleep(0.5)` + `sleep(3.0)` + `sleep(1.0)`; we wait for actual content instead | ~5s |
| `fill()` instead of `type(delay=50)`, search fired in a single JS call | ~0.5s |
| Blocked images/fonts/media + Ezoic ads, GTM, analytics, consent manager, icon CDNs | page work 2–4× lighter |
| N pages searching concurrently off a shared queue | ×N (sublinear) |

**Measured** (dev machine, zero errors): 4 pages ≈ **1.2/sec (~4,300/hr)**,
8 pages ≈ **1.78/sec (~6,400/hr)**. Scaling is sublinear — Chromium goes
CPU-bound, so 2× the pages is not 2× the speed.

### Correctness: two bugs worth knowing about

**Progressive rendering.** The site paints the first person record and then keeps
appending. Extracting as soon as content appeared made results
*nondeterministic* — on a 40-number sample, **9 of 40 rows disagreed between two
identical runs**, and 5 numbers falsely reported "no owner" when they had 5, 4,
2, 2 and 1 owners. `worker.py` now fingerprints the person list and only reads it
once it has stopped changing (`SETTLE_*` in `config.py`). Two identical runs now
diff to **zero disagreements**, matching fresh-page ground truth.

**Compliance parsing.** The old parser scanned every `<span>` in the card and
assigned the first three `clean`/`flagged`/`listed` strings *positionally*. But
DNC-registered numbers render a third value the list never had —
`federal & state dnc` (class `registered`) — so those rows silently shifted the
litigator value into DNC and lost blacklist entirely. We now read the site's real
element ids: `#state`, `#dncStatus`, `#litigator`, `#blacklist`.

---

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

streamlit run app.py
```

CLI testing (single-number, legacy nodriver path):

```bash
python run_cli.py 9107852362
```

Direct worker run (bypasses the UI):

```bash
python worker.py phones.json out.jsonl 4      # 4 parallel pages
```

---

## Deploying to Streamlit Cloud

`packages.txt` must contain exactly:

```
chromium
```

Common deploy failure — these names **do not exist** on Debian and will abort the
build with `Package 'chromium-browser' has no installation candidate`:

- ❌ `chromium-browser`, `chromium-chromedriver` — those are *Ubuntu* names
- ❌ `chromium-driver` — that's chromedriver, only needed for Selenium.
  Playwright talks CDP directly and doesn't use it.

Streamlit Cloud also **does not run `setup.sh`** — only `packages.txt` and
`requirements.txt`. That's fine: `worker.py` auto-detects the apt chromium, and
falls back to `playwright install chromium` if it's ever absent.

---

## Running large batches on Streamlit Cloud — read this

Streamlit Community Cloud gives roughly **1 GB RAM and 1–2 shared vCPUs**.
Chromium costs ~130 MB, plus ~25 MB per page.

- **4–5 pages is the safe ceiling.** 7+ risks the container being OOM-killed
  mid-run.
- **Keep the browser tab open.** Streamlit Cloud sleeps idle apps and that kills
  the scraper with it. Results already written survive — use **♻️ Resume**.
- `runs/` is on ephemeral disk. It survives reruns and app sleep, but **not** a
  redeploy or container restart. Download the CSV when a run finishes.
- At ~4,300/hr, **50,000 numbers is roughly 10–14 hours** on cloud hardware, in
  one continuously-open session. Plan on splitting it across sessions and
  resuming, or move the worker to an always-on host where it can run headless
  and unattended.

## Tuning

All in `scraper/config.py`:

| Setting | Default | Notes |
|---|---|---|
| `CONCURRENCY` | 4 | Default parallel pages (UI slider overrides) |
| `RESULT_TIMEOUT_SEC` | 20 | Max wait per number |
| `SETTLE_POLL` / `SETTLE_STABLE_CHECKS` | 0.12 / 5 | Progressive-render guard. **Don't lower without re-testing determinism** |
| `RELOAD_EVERY` | 40 | Reload the page every N searches |
| `INTER_SEARCH_DELAY` | 0.0 | Raise to ~0.5 if the site starts erroring |
| `MAX_RETRIES` | 2 | Retries before recording an error |
| `BLOCK_HOSTS` | ads/analytics | Never add `infolookup.site` here |
| `BLOCK_CSS` | False | Faster, but may affect result visibility — off deliberately |

---

## Output Fields

`Phone`, `Found`, `State/Location`, `DNC Status`, `Litigator`, `Blacklist`,
`Person Name`, `Age/Year`, `Lives At`, `City`, `State`, `ZIP`, `Error` —
one CSV row per address.
