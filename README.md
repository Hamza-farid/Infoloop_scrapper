# 📞 InfoLookup Scraper

Automated phone number lookup tool for [infolookup.site](https://infolookup.site/) — built with **nodriver** and **Streamlit**.

---

## Project Structure

```
infolookup_scraper/
├── app.py                  ← Streamlit UI (run this)
├── run_cli.py              ← CLI runner for dev/testing
├── requirements.txt
├── scraper/
│   ├── __init__.py
│   ├── config.py           ← All selectors & timing constants
│   └── lookup.py           ← Core async scraper (nodriver)
└── utils/
    ├── __init__.py
    ├── models.py           ← Data classes (LookupResult, PersonRecord…)
    └── phone_generator.py  ← Number range generator
```

---

## Setup

```bash
# 1. Clone / unzip the project
cd infolookup_scraper

# 2. Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run Streamlit UI
streamlit run app.py
```

---

## CLI Testing (no UI)

```bash
# Single number
python run_cli.py 9107852362

# Multiple numbers
python run_cli.py 9107852362 9107852363 9107852364
```

---

## How to Use the UI

1. Enter the **3-digit area code** in the sidebar (e.g. `910`)
2. Set the **middle 3 digits** range (exchange): From → To
3. Set the **last 4 digits** range (subscriber): From → To
4. The preview shows exactly which numbers will be searched + total count
5. Click **🚀 Start Lookup** — live results appear as each number completes
6. Use the tabs to filter: All / Found / No Info / Errors
7. Download all results as **CSV**

---

## Deploying to Streamlit Cloud

1. Push this folder to a **GitHub repository**
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub repo
4. Set **Main file path** to: `app.py`
5. Click **Deploy**

> ⚠️ **Note on headless Chrome:** Streamlit Cloud runs on Linux servers.
> nodriver will auto-detect and run Chrome headlessly. Make sure
> `google-chrome` or `chromium-browser` is available on the deploy target.
> For Streamlit Cloud, add a `packages.txt` file:

```
# packages.txt  (put in project root for Streamlit Cloud)
chromium-browser
chromium-chromedriver
```

---

## Configuration

All scraping settings are in `scraper/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `TIMEOUT_SEC` | 20 | Max wait for any element |
| `POLL_INTERVAL` | 0.5 | Selector polling interval |
| `POST_SEARCH_WAIT` | 3.0 | Extra wait after results appear |
| `INPUT_SELECTORS` | list | Phone input selectors (with fallbacks) |
| `BUTTON_SELECTORS` | list | Search button selectors (with fallbacks) |

---

## Output Fields

| Field | Description |
|-------|-------------|
| Phone | The number searched |
| Found | True if owner info was returned |
| State/Location | State the number is registered in |
| DNC Status | clean / flagged |
| Litigator | clean / flagged |
| Blacklist | clean / flagged |
| Person Name | Owner name (if found) |
| Age/Year | e.g. "71 yrs (1955)" |
| Lives At / City / State / ZIP | Address records |
| Error | Human-readable error message if scrape failed |
