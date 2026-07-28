"""
Core scraper for infolookup.site using nodriver.
All waits use polling loop (timeout=20s, poll=0.5s).
"""

import asyncio
import sys
import time
import traceback
from typing import Optional

# ── Windows fix: must be set BEFORE any event loop is created ──────────────
# nodriver uses create_subprocess_exec which requires SelectorEventLoop on Windows.
# ProactorEventLoop (Windows default in Python 3.8+) raises NotImplementedError.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print("🪟 Windows detected — switched to WindowsSelectorEventLoopPolicy")

import nodriver as uc

from scraper.config import (
    BASE_URL,
    TIMEOUT_SEC,
    POLL_INTERVAL,
    POST_SEARCH_WAIT,
    INPUT_SELECTORS,
    BUTTON_SELECTORS,
    RESULTS_CONTAINER_SELECTORS,
    NO_RESULT_SELECTORS,
    NO_RESULT_TEXT,
    PERSON_SECTION_SELECTORS,
    PERSON_NAME_SELECTORS,
    PERSON_AGE_SELECTORS,
    ADDRESS_TABLE_SELECTORS,
)
from utils.models import (
    LookupResult,
    ComplianceStatus,
    PersonRecord,
    AddressRecord,
)


# ──────────────────────────────────────────────
# Low-level helpers
# ──────────────────────────────────────────────

async def _wait_for_element(tab, selectors: list[str], timeout: float = TIMEOUT_SEC, poll: float = POLL_INTERVAL):
    """
    Try each selector in order every `poll` seconds until one resolves
    or `timeout` is reached.  Returns (element, selector_used) or raises TimeoutError.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                el = await tab.select(sel)
                if el:
                    print(f"  ✅ Found element via selector: {sel}")
                    return el, sel
            except Exception:
                pass
        await asyncio.sleep(poll)
    raise TimeoutError(
        f"⏰ Timed out after {timeout}s waiting for any of: {selectors}"
    )


async def _find_first(tab, selectors: list[str]) -> Optional[object]:
    """Non-blocking: return the first matching element right now, or None."""
    for sel in selectors:
        try:
            el = await tab.select(sel)
            if el:
                return el
        except Exception:
            pass
    return None


async def _inner_text(el) -> str:
    """Safely get inner text from a nodriver element."""
    try:
        return (await el.get_html()).strip()
    except Exception:
        return ""


async def _text_content(tab, selector: str) -> str:
    """JS-based text extraction – more reliable than HTML parse for simple text."""
    try:
        result = await tab.evaluate(
            f"(document.querySelector({repr(selector)}) || {{}}).textContent || ''"
        )
        return str(result).strip()
    except Exception:
        return ""


# ──────────────────────────────────────────────
# Compliance card parser
# ──────────────────────────────────────────────

async def _parse_compliance(tab) -> ComplianceStatus:
    """Extract the green compliance badges from the results card."""
    print("  🔍 Parsing compliance status...")
    status = ComplianceStatus()

    # State / location  – grab text of first badge-like element in the card
    state_js = """
    (() => {
        const card = document.querySelector('#resultsContainer .card');
        if (!card) return '';
        // The NC badge span
        const badge = card.querySelector('.badge, [class*="badge"], span');
        return badge ? badge.textContent.trim() : '';
    })()
    """
    try:
        state_raw = await tab.evaluate(state_js)
        # Also grab the text next to it (e.g. "North Carolina")
        full_loc_js = """
        (() => {
            const card = document.querySelector('#resultsContainer .card');
            if (!card) return '';
            // Grab all text from location row
            const rows = card.querySelectorAll('div, span, p');
            for (const el of rows) {
                const t = el.textContent.trim();
                if (t.length > 2 && t.length < 60 && !t.toLowerCase().includes('status') && !t.toLowerCase().includes('copy')) {
                    return t;
                }
            }
            return '';
        })()
        """
        loc_text = await tab.evaluate(full_loc_js)
        status.state_location = loc_text.strip() if loc_text else str(state_raw).strip()
        print(f"  📍 Location: {status.state_location}")
    except Exception as e:
        print(f"  ⚠️ Could not parse location: {e}")

    # DNC / Litigator / Blacklist – look for "clean" or "flagged" text
    badge_js = """
    (() => {
        const card = document.querySelector('#resultsContainer .card');
        if (!card) return JSON.stringify({dnc:'unknown', litigator:'unknown', blacklist:'unknown'});
        const getText = (label) => {
            const headers = card.querySelectorAll('th, [class*="header"], div');
            for (const h of headers) {
                if (h.textContent.toUpperCase().includes(label.toUpperCase())) {
                    // Find nearest status text
                    const parent = h.closest('td,th') || h.parentElement;
                    if (parent) {
                        const next = parent.nextElementSibling;
                        if (next) return next.textContent.trim().toLowerCase();
                    }
                }
            }
            return 'unknown';
        };
        // Alternative: grep for colored badge spans
        const badges = card.querySelectorAll('[class*="badge"], span, div');
        const results = {};
        let idx = 0;
        const keys = ['dnc','litigator','blacklist'];
        for (const b of badges) {
            const t = b.textContent.trim().toLowerCase();
            if ((t === 'clean' || t === 'flagged' || t === 'listed') && idx < 3) {
                results[keys[idx]] = t;
                idx++;
            }
        }
        return JSON.stringify(results);
    })()
    """
    try:
        import json
        badge_data_raw = await tab.evaluate(badge_js)
        badge_data = json.loads(str(badge_data_raw)) if badge_data_raw else {}
        status.dnc_status  = badge_data.get("dnc",       "unknown")
        status.litigator   = badge_data.get("litigator", "unknown")
        status.blacklist   = badge_data.get("blacklist",  "unknown")
        print(f"  🛡️  DNC={status.dnc_status} | Litigator={status.litigator} | Blacklist={status.blacklist}")
    except Exception as e:
        print(f"  ⚠️ Badge parse error: {e}")

    return status


# ──────────────────────────────────────────────
# Person info parser
# ──────────────────────────────────────────────

async def _parse_persons(tab) -> list[PersonRecord]:
    """Extract all person records from the results container."""
    print("  👤 Parsing person records...")
    persons = []

    extract_js = """
    (() => {
        const container = document.getElementById('personInfoListContainer');
        if (!container) return JSON.stringify([]);

        const sections = container.querySelectorAll('div.section');
        const results = [];

        for (const sec of sections) {
            const rec = { name: '', age_year: '', addresses: [] };

            // Name
            const nameEl = sec.querySelector('.person-name');
            if (nameEl) rec.name = nameEl.textContent.trim();

            // Age/year
            const ageEl = sec.querySelector('.person-age-display');
            if (ageEl) rec.age_year = ageEl.textContent.trim();

            // Addresses from table rows
            const rows = sec.querySelectorAll('table.address-table tbody tr, .tbl-wrap table tbody tr');
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length >= 4) {
                    rec.addresses.push({
                        lives_at: cells[0].textContent.trim(),
                        city:     cells[1].textContent.trim(),
                        state:    cells[2].textContent.trim(),
                        zip_code: cells[3].textContent.trim(),
                    });
                }
            }

            if (rec.name) results.push(rec);
        }

        return JSON.stringify(results);
    })()
    """

    try:
        import json
        raw = await tab.evaluate(extract_js)
        data = json.loads(str(raw)) if raw else []
        for rec in data:
            pr = PersonRecord(
                name=rec.get("name", ""),
                age_year=rec.get("age_year", ""),
            )
            for addr in rec.get("addresses", []):
                pr.addresses.append(AddressRecord(
                    lives_at=addr.get("lives_at", ""),
                    city=addr.get("city", ""),
                    state=addr.get("state", ""),
                    zip_code=addr.get("zip_code", ""),
                ))
            persons.append(pr)
            print(f"  👤 Found person: {pr.name} | {pr.age_year} | {len(pr.addresses)} addr(s)")
    except Exception as e:
        print(f"  ⚠️ Person parse error: {e}")
        traceback.print_exc()

    return persons


# ──────────────────────────────────────────────
# "No result" detector
# ──────────────────────────────────────────────

async def _is_no_result(tab) -> bool:
    """Return True if the page shows 'No result found for this number'."""
    try:
        check_js = """
        (() => {
            const container = document.getElementById('personInfoListContainer');
            if (!container) return false;
            const nameEls = container.querySelectorAll('.person-name');
            for (const el of nameEls) {
                if (el.textContent.toLowerCase().includes('no result')) return true;
            }
            // Also check danger-colored text
            const danger = container.querySelectorAll('[style*="danger"]');
            for (const el of danger) {
                if (el.textContent.toLowerCase().includes('no result')) return true;
            }
            return false;
        })()
        """
        result = await tab.evaluate(check_js)
        return bool(result)
    except Exception:
        return False


# ──────────────────────────────────────────────
# Main lookup function
# ──────────────────────────────────────────────

async def lookup_phone(phone: str, browser=None) -> LookupResult:
    """
    Scrape infolookup.site for a single phone number.

    Args:
        phone:   10-digit string, no formatting, e.g. '9107852362'
        browser: Optional reusable nodriver Browser instance.
                 If None, a new one is launched (and closed after).

    Returns:
        LookupResult dataclass.
    """
    result = LookupResult(phone=phone)
    own_browser = browser is None
    tab = None

    print(f"\n{'='*55}")
    print(f"🔎 Looking up phone: {phone}")
    print(f"{'='*55}")

    try:
        # ── Launch browser ──────────────────────────────────────
        if own_browser:
            print("🌐 Launching browser...")
            browser = await uc.start(headless=True)

        tab = await browser.get(BASE_URL)
        print(f"📄 Loaded: {BASE_URL}")
        await asyncio.sleep(1.5)   # let JS settle

        # ── Find & fill input ───────────────────────────────────
        print("🔤 Locating phone input...")
        inp, inp_sel = await _wait_for_element(tab, INPUT_SELECTORS)

        # Clear existing value then type
        await inp.clear_input()
        await asyncio.sleep(0.3)
        await inp.send_keys(phone)
        print(f"⌨️  Typed: {phone}")
        await asyncio.sleep(0.5)

        # ── Click search button ──────────────────────────────────
        print("🖱️  Locating search button...")
        btn, _ = await _wait_for_element(tab, BUTTON_SELECTORS)
        await btn.click()
        print("✅ Search button clicked")

        # ── Wait for results container ───────────────────────────
        print("⏳ Waiting for results container...")
        await _wait_for_element(tab, RESULTS_CONTAINER_SELECTORS)
        print("📦 Results container appeared")
        await asyncio.sleep(POST_SEARCH_WAIT)

        # ── Parse compliance ─────────────────────────────────────
        result.compliance = await _parse_compliance(tab)

        # ── Check no-result ──────────────────────────────────────
        no_result = await _is_no_result(tab)
        if no_result:
            print("🚫 No owner info found for this number")
            result.found = False
            return result

        # ── Parse persons ────────────────────────────────────────
        persons = await _parse_persons(tab)
        if persons:
            result.found = True
            result.persons = persons
            print(f"🎉 Found {len(persons)} person record(s)!")
        else:
            result.found = False
            print("⚠️  Results container appeared but no person sections found")

    except TimeoutError as e:
        msg = f"Timed out waiting for the page to respond. The site may be slow or down. ({e})"
        print(f"⏰ TimeoutError: {e}")
        result.error = msg

    except Exception as e:
        msg = f"Unexpected error while looking up {phone}: {e}"
        print(f"💥 Exception: {e}")
        traceback.print_exc()
        result.error = msg

    finally:
        if tab:
            try:
                await tab.close()
                print("🗑️  Tab closed")
            except Exception:
                pass
        if own_browser and browser:
            try:
                browser.stop()
                print("🛑 Browser closed")
            except Exception:
                pass

    return result


# ──────────────────────────────────────────────
# Batch runner
# ──────────────────────────────────────────────

async def run_batch(
    phone_numbers: list[str],
    progress_callback=None,
) -> list[LookupResult]:
    """
    Run lookups sequentially (one browser, one tab per number).
    progress_callback(current, total, result) is called after each lookup.
    """
    print(f"\n🚀 Starting batch of {len(phone_numbers)} number(s)")
    browser = None
    results = []

    try:
        print("🌐 Launching shared browser...")
        browser = await uc.start(headless=True)

        for idx, phone in enumerate(phone_numbers, 1):
            print(f"\n📋 [{idx}/{len(phone_numbers)}] Processing {phone}")
            res = await lookup_phone(phone, browser=browser)
            results.append(res)

            if progress_callback:
                try:
                    progress_callback(idx, len(phone_numbers), res)
                except Exception as cb_err:
                    print(f"⚠️  Progress callback error: {cb_err}")

            # Small delay between searches to be polite
            if idx < len(phone_numbers):
                await asyncio.sleep(1.5)

    except Exception as e:
        print(f"💥 Batch runner error: {e}")
        traceback.print_exc()

    finally:
        if browser:
            try:
                browser.stop()
                print("🛑 Shared browser closed")
            except Exception:
                pass

    print(f"\n✅ Batch complete. {len(results)} result(s) collected.")
    return results