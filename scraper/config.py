"""
Configuration constants for InfoLookup scraper.
"""

# --- Site ---
BASE_URL = "https://infolookup.site/"

# --- Timing (legacy: used by scraper/lookup.py + run_cli.py) ---
TIMEOUT_SEC = 20          # Max wait for any element
POLL_INTERVAL = 0.5       # Polling frequency (seconds)
POST_SEARCH_WAIT = 3.0    # Extra settle time after results appear

# ─────────────────────────────────────────────────────────────────────────────
# Fast-path settings — used by worker.py (the pooled scraper)
# ─────────────────────────────────────────────────────────────────────────────

# How many pages search in parallel inside ONE browser.
# Streamlit Community Cloud gives ~1 GB RAM / 1-2 shared vCPU.
# Chromium base ~130 MB + ~25 MB per page. 4-6 is the safe zone; 8+ will OOM.
CONCURRENCY = 4

# Max wait for a number's results to render. Replaces the old fixed 3s sleep —
# we wait for the ACTUAL content instead, so most numbers finish in ~1-2.5s.
RESULT_TIMEOUT_SEC = 20.0

# ── Settle detection (do not lower these without re-testing) ────────────────
# The site renders person records PROGRESSIVELY — it paints the first record,
# then keeps appending. Extracting as soon as content appears produces
# nondeterministic results: the same number returns 5 owners on one run and 1
# (or 0) on the next. Measured on a 40-number sample: 9/40 rows disagreed
# between two identical runs before this was added.
#
# So we fingerprint the person list (section count + text length) and only
# extract once it has stopped changing for SETTLE_STABLE_CHECKS consecutive
# polls. Costs ~0.5s per number and makes the output reproducible.
SETTLE_POLL = 0.12
SETTLE_STABLE_CHECKS = 5

# The site is a single-page app, so we search over and over WITHOUT reloading
# (this alone removes ~2s per number). Reload every N searches anyway, to
# clear any JS state the site may accumulate.
RELOAD_EVERY = 40

# Politeness delay between searches on the same page. 0.0 = flat out.
# Raise to ~0.5 if the site starts rate-limiting / returning errors.
INTER_SEARCH_DELAY = 0.0

# Retries per number before recording an error.
MAX_RETRIES = 2

# Navigation timeout for the initial page load.
NAV_TIMEOUT_SEC = 30.0

# Don't download things we never read. Big bandwidth + CPU win.
BLOCK_RESOURCE_TYPES = {"image", "font", "media"}

# Third-party junk the page pulls in: ad network (Ezoic), analytics, consent
# manager, icon/webfont CDNs. None of it affects the data we parse, and it's
# the majority of the page's requests. Blocking it is a large speedup.
# NOTE: infolookup.site's own scripts (bootstrap.php, backend.js, test2.js,
# theme.js) are essential — never add that domain here.
BLOCK_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "ezojs.com",
    "ezoic.net",
    "ezoic.com",
    "gatekeeperconsent.com",
    "doubleclick.net",
    "cdnjs.cloudflare.com",      # font-awesome icons only
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)

# Blocking CSS is faster still, but the site may use CSS to toggle result
# visibility — which could corrupt a 50k run. Left OFF on purpose.
# Flip to True only after spot-checking that results still parse correctly.
BLOCK_CSS = False

# --- Input selectors (primary → fallbacks) ---
INPUT_SELECTORS = [
    "#phoneInput",                            # id (most reliable)
    "input[type='tel']",                      # type attr
    "input[placeholder*='555']",              # placeholder partial
    "input[aria-label='Phone number']",       # aria label
    "input[inputmode='tel']",                 # inputmode
]

# --- Search button selectors (primary → fallbacks) ---
BUTTON_SELECTORS = [
    "#searchButton",                          # id
    "button.flex",                            # class fragment
    "button[id='searchButton']",              # explicit attr
    "button span:contains('Search')",         # text child (limited support)
]

# --- Result container ---
RESULTS_CONTAINER_SELECTORS = [
    "#resultsContainer",
    "div[id='resultsContainer']",
]

# --- Compliance card ---
COMPLIANCE_CARD_SELECTORS = [
    "#resultsContainer .card",
    "#resultsContainer > div.card",
]

# --- DNC / Litigator / Blacklist status cells ---
DNC_SELECTORS        = ["[data-label='DNC']", ".dnc-status", "th:contains('DNC') + td"]
LITIGATOR_SELECTORS  = [".litigator-status", "[data-label='Litigator']"]
BLACKLIST_SELECTORS  = [".blacklist-status", "[data-label='Blacklist']"]

# --- "No result" indicator ---
NO_RESULT_SELECTORS = [
    "div.person-name[style*='color:var(--danger)']",
    ".person-name[style*='danger']",
    "#personInfoListContainer .person-name",
]
NO_RESULT_TEXT = "no result found"

# --- Person info container ---
PERSON_INFO_CONTAINER_SELECTORS = [
    "#personInfoListContainer",
    "div[id='personInfoListContainer']",
]

# --- Person section (each record) ---
PERSON_SECTION_SELECTORS = [
    "#personInfoListContainer div.section",
    "#personInfoListContainer .section",
]

# Person name
PERSON_NAME_SELECTORS = [
    ".person-name",
    "div.person-name",
]

# Person age/year
PERSON_AGE_SELECTORS = [
    ".person-age-display",
    "div.person-age-display",
]

# Address table
ADDRESS_TABLE_SELECTORS = [
    ".address-table",
    "table.address-table",
    ".tbl-wrap table",
]

# State / Location badge
STATE_SELECTORS = [
    ".card .badge",
    ".compliance-card .state-badge",
    "#resultsContainer .card span",
]
