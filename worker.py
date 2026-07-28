"""
worker.py — runs as a SEPARATE PROCESS launched by app.py.
Uses playwright sync API — zero asyncio, zero event loop issues on Windows.
Writes results as JSONL to the output file so app.py can read live progress.
"""

import sys
import os
import json
import traceback
import time

# Force UTF-8 output before any print
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper.config import (
    BASE_URL, TIMEOUT_SEC, POST_SEARCH_WAIT,
)
from utils.models import LookupResult, ComplianceStatus, PersonRecord, AddressRecord

TIMEOUT_MS = int(TIMEOUT_SEC * 1000)  # playwright uses milliseconds


# ─────────────────────────────────────────────────────────────────────────────
# Element wait helper
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_any(page, selectors: list, timeout_ms=TIMEOUT_MS):
    """Wait until any selector in the list is visible. Returns (locator, selector)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=500):
                    print(f"  [OK] Found: {sel}", flush=True)
                    return loc, sel
            except Exception:
                pass
        time.sleep(0.5)
    raise TimeoutError(f"Timeout after {TIMEOUT_SEC}s waiting for: {selectors}")


# ─────────────────────────────────────────────────────────────────────────────
# Compliance parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_compliance(page) -> ComplianceStatus:
    status = ComplianceStatus()
    try:
        loc_js = """
        (() => {
            const card = document.querySelector('#resultsContainer .card');
            if (!card) return '';
            const rows = card.querySelectorAll('div, span, p');
            for (const el of rows) {
                const t = el.textContent.trim();
                if (t.length > 2 && t.length < 60 &&
                    !t.toLowerCase().includes('status') &&
                    !t.toLowerCase().includes('copy')) return t;
            }
            return '';
        })()
        """
        status.state_location = str(page.evaluate(loc_js)).strip()
        print(f"  [LOC] {status.state_location}", flush=True)
    except Exception as e:
        print(f"  [WARN] Location: {e}", flush=True)

    try:
        badge_js = """
        (() => {
            const card = document.querySelector('#resultsContainer .card');
            if (!card) return '{}';
            const keys = ['dnc','litigator','blacklist'];
            const results = {};
            let idx = 0;
            for (const b of card.querySelectorAll('[class*="badge"], span, div')) {
                const t = b.textContent.trim().toLowerCase();
                if ((t === 'clean' || t === 'flagged' || t === 'listed') && idx < 3) {
                    results[keys[idx++]] = t;
                }
            }
            return JSON.stringify(results);
        })()
        """
        data = json.loads(page.evaluate(badge_js) or "{}")
        status.dnc_status = data.get("dnc", "unknown")
        status.litigator  = data.get("litigator", "unknown")
        status.blacklist  = data.get("blacklist", "unknown")
        print(f"  [DNC] {status.dnc_status} [LIT] {status.litigator} [BL] {status.blacklist}", flush=True)
    except Exception as e:
        print(f"  [WARN] Badges: {e}", flush=True)

    return status


# ─────────────────────────────────────────────────────────────────────────────
# No-result detector
# ─────────────────────────────────────────────────────────────────────────────

def is_no_result(page) -> bool:
    try:
        return page.evaluate("""
        (() => {
            const c = document.getElementById('personInfoListContainer');
            if (!c) return false;
            for (const el of c.querySelectorAll('.person-name, [style*="danger"]')) {
                if (el.textContent.toLowerCase().includes('no result')) return true;
            }
            return false;
        })()
        """)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Person parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_persons(page) -> list:
    persons = []
    try:
        data = json.loads(page.evaluate("""
        (() => {
            const c = document.getElementById('personInfoListContainer');
            if (!c) return '[]';
            const results = [];
            for (const sec of c.querySelectorAll('div.section')) {
                const rec = {name:'', age_year:'', addresses:[]};
                const n = sec.querySelector('.person-name');
                if (n) rec.name = n.textContent.trim();
                const a = sec.querySelector('.person-age-display');
                if (a) rec.age_year = a.textContent.trim();
                for (const row of sec.querySelectorAll('.tbl-wrap table tbody tr, table.address-table tbody tr')) {
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
        """) or "[]")

        for rec in data:
            pr = PersonRecord(name=rec.get("name",""), age_year=rec.get("age_year",""))
            for addr in rec.get("addresses", []):
                pr.addresses.append(AddressRecord(
                    lives_at=addr.get("lives_at",""), city=addr.get("city",""),
                    state=addr.get("state",""),       zip_code=addr.get("zip_code",""),
                ))
            persons.append(pr)
            print(f"  [PERSON] {pr.name} | {pr.age_year} | {len(pr.addresses)} addr(s)", flush=True)
    except Exception as e:
        print(f"  [WARN] Person parse: {e}", flush=True)
        traceback.print_exc()
    return persons


# ─────────────────────────────────────────────────────────────────────────────
# Single phone lookup
# ─────────────────────────────────────────────────────────────────────────────

def lookup_one(page, phone: str) -> LookupResult:
    result = LookupResult(phone=phone)
    print(f"\n{'='*50}", flush=True)
    print(f"[SEARCH] {phone}", flush=True)

    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        time.sleep(1.5)

        # Fill input — try selectors in order
        inp = None
        for sel in ["#phoneInput", "input[type='tel']", "input[placeholder*='555']",
                    "input[aria-label='Phone number']"]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    inp = loc
                    print(f"  [INPUT] {sel}", flush=True)
                    break
            except Exception:
                pass

        if not inp:
            raise TimeoutError("Could not find phone input field")

        inp.click()
        inp.fill("")
        inp.type(phone, delay=50)
        print(f"  [TYPED] {phone}", flush=True)
        time.sleep(0.5)

        # Click search button
        btn = None
        for sel in ["#searchButton", "button:has-text('Search')", "button.flex"]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    btn = loc
                    print(f"  [BTN] {sel}", flush=True)
                    break
            except Exception:
                pass

        if not btn:
            raise TimeoutError("Could not find search button")

        btn.click()
        print("  [CLICK] Search clicked", flush=True)

        # Wait for results
        page.wait_for_selector("#resultsContainer", timeout=TIMEOUT_MS)
        print("  [RESULTS] Container appeared", flush=True)
        time.sleep(POST_SEARCH_WAIT)

        result.compliance = parse_compliance(page)

        if is_no_result(page):
            print("  [NO-OWNER] No owner info", flush=True)
            result.found = False
        else:
            persons = parse_persons(page)
            result.found = bool(persons)
            result.persons = persons
            if result.found:
                print(f"  [FOUND] {len(persons)} person(s)", flush=True)

    except TimeoutError as e:
        result.error = f"The site took too long to respond. Please try again. ({e})"
        print(f"  [TIMEOUT] {e}", flush=True)
    except Exception as e:
        result.error = f"Unexpected error for {phone}: {e}"
        print(f"  [ERROR] {e}", flush=True)
        traceback.print_exc()

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main — sync, no asyncio at all
# ─────────────────────────────────────────────────────────────────────────────

def main(phones, out_file):
    from playwright.sync_api import sync_playwright

    print(f"[START] Worker starting - {len(phones)} numbers", flush=True)

    with sync_playwright() as p:
        print("[BROWSER] Launching Chromium...", flush=True)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        print("[BROWSER] Launched OK!", flush=True)

        try:
            with open(out_file, "w", encoding="utf-8") as f:
                for idx, phone in enumerate(phones, 1):
                    print(f"\n[{idx}/{len(phones)}] {phone}", flush=True)
                    res = lookup_one(page, phone)

                    line = json.dumps(res.to_dict())
                    f.write(line + "\n")
                    f.flush()
                    print(f"  [SAVED] {phone}", flush=True)

                    if idx < len(phones):
                        time.sleep(1.0)

        except Exception as e:
            print(f"[FATAL] {e}", flush=True)
            traceback.print_exc()
        finally:
            context.close()
            browser.close()
            print("[DONE] Worker finished.", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python worker.py <phones_json> <results_jsonl>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        phones = json.load(f)

    main(phones, sys.argv[2])