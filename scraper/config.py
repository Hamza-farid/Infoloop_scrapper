"""
Configuration constants for InfoLookup scraper.
"""

# --- Site ---
BASE_URL = "https://infolookup.site/"

# --- Timing ---
TIMEOUT_SEC = 20          # Max wait for any element
POLL_INTERVAL = 0.5       # Polling frequency (seconds)
POST_SEARCH_WAIT = 3.0    # Extra settle time after results appear

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
