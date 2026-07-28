"""
worker.py — runs as a SEPARATE PROCESS launched by app.py.

Pooled async Playwright scraper. Key differences from the old sequential version:

  1. N pages search CONCURRENTLY, pulling from one shared queue (self-balancing —
     a slow number never blocks the others).
  2. The page is loaded ONCE per worker, not once per number. The site is a
     single-page app, so we clear the result containers and search again.
     Saves ~2s/number.
  3. Zero fixed sleeps. We wait for the actual result element to appear instead
     of blindly sleeping 3s + 1.5s + 1s. Saves ~5s/number.
  4. The search is fired via one JS call (set value -> dispatch input -> click)
     instead of several Playwright round-trips, with a Playwright fill/click
     fallback if that doesn't take.
  5. Images / fonts / media are blocked — never downloaded.

Results stream to a JSONL file (one line per number) which app.py tails, so
progress is live and a crash never loses completed work.

Usage:  python worker.py <phones.json> <results.jsonl> [concurrency]
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

# Force UTF-8 output before any print (Windows console defaults to cp1252)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper.config import (
    BASE_URL,
    CONCURRENCY,
    RESULT_TIMEOUT_SEC,
    SETTLE_POLL,
    SETTLE_STABLE_CHECKS,
    RELOAD_EVERY,
    INTER_SEARCH_DELAY,
    MAX_RETRIES,
    NAV_TIMEOUT_SEC,
    BLOCK_RESOURCE_TYPES,
    BLOCK_HOSTS,
    BLOCK_CSS,
)
from utils.models import LookupResult, ComplianceStatus, PersonRecord, AddressRecord


def log(msg):
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# JS payloads — kept as module constants so they're compiled once
# ─────────────────────────────────────────────────────────────────────────────

# Clear stale results, then fire the search. Returns a status string.
#
# Clearing is ESSENTIAL: because we no longer reload the page, the previous
# number's results are still in the DOM. Without it, the readiness check would
# pass instantly against stale data and every number after the first would
# report the previous number's owner.
#
# But clear CHILDREN ONLY. #personInfoListContainer sits INSIDE
# #resultsContainer, so `resultsContainer.innerHTML = ''` deletes the element
# the site is about to populate and the lookup then hangs forever.
JS_SEARCH = """
(phone) => {
    const inp = document.querySelector('#phoneInput')
             || document.querySelector("input[type='tel']")
             || document.querySelector("input[inputmode='tel']");
    const btn = document.querySelector('#searchButton')
             || document.querySelector('button.flex');
    if (!inp || !btn) return 'no-elements';

    const pc = document.getElementById('personInfoListContainer');
    if (pc) pc.innerHTML = '';
    for (const id of ['state', 'dncStatus', 'litigator', 'blacklist']) {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '';
    }

    inp.focus();
    inp.value = phone;
    inp.dispatchEvent(new Event('input',  { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    btn.click();
    return 'ok';
}
"""

# Readiness probe. Returns the state AND a fingerprint of the person list.
#
# Two traps here, both of which silently corrupt output:
#
#  1. The site paints PLACEHOLDERS the instant you click ("loading…" in the stat
#     cells, "Loading person information…" as a .person-name). Presence is not
#     readiness — the text must also not be a placeholder.
#  2. Person records render PROGRESSIVELY. The first record appearing does not
#     mean the list is complete, so we return a fingerprint (`sections`, `len`)
#     and the caller waits for it to stop changing before extracting.
JS_READY_STATE = """
() => {
    const norm = (s) => (s || '').trim().toLowerCase();

    const d = document.getElementById('dncStatus');
    const dt = d ? norm(d.textContent) : '';
    const dncReady = dt.length > 0 && !dt.includes('loading');

    let sections = 0, len = 0, real = 0, loading = false, noResult = false;
    const pc = document.getElementById('personInfoListContainer');
    if (pc) {
        sections = pc.querySelectorAll('div.section').length;
        len = pc.textContent.length;
        for (const n of pc.querySelectorAll('.person-name')) {
            const t = norm(n.textContent);
            if (t.includes('loading')) { loading = true; }
            else if (t.length > 0) {
                real++;
                if (t.includes('no result')) noResult = true;
            }
        }
    }
    return { dncReady, loading, real, sections, len, noResult };
}
"""

# One evaluate() returning compliance + persons + no-result together — three
# separate round-trips per number was pure latency.
#
# Reads the site's real element ids (#state / #dncStatus / #litigator /
# #blacklist). The old version walked every span in the card and assigned the
# first three "clean"/"flagged" strings POSITIONALLY, which mislabels the moment
# the site reorders its DOM.
JS_EXTRACT = """
() => {
    const txt = (id) => {
        const el = document.getElementById(id);
        if (!el) return 'unknown';
        const t = el.textContent.trim().toLowerCase();
        return (!t || t.includes('loading')) ? 'unknown' : t;
    };

    const out = {
        location: '', dnc: txt('dncStatus'), litigator: txt('litigator'),
        blacklist: txt('blacklist'), no_result: false, persons: []
    };

    const st = document.getElementById('state');
    if (st) {
        const full = st.querySelector('.state-full-text');
        const abbr = st.querySelector('.state-abbr-badge');
        out.location = full ? full.textContent.trim()
                     : (abbr ? abbr.textContent.trim() : st.textContent.trim());
        if (out.location.toLowerCase().includes('loading')) out.location = '';
    }

    const c = document.getElementById('personInfoListContainer');
    if (c) {
        for (const el of c.querySelectorAll('.person-name, [style*="danger"]')) {
            if (el.textContent.toLowerCase().includes('no result')) { out.no_result = true; break; }
        }
        if (!out.no_result) {
            for (const sec of c.querySelectorAll('div.section')) {
                const rec = { name: '', age_year: '', addresses: [] };
                const n = sec.querySelector('.person-name');
                if (n) rec.name = n.textContent.trim();
                if (!rec.name || rec.name.toLowerCase().includes('loading')) continue;
                const a = sec.querySelector('.person-age-display');
                if (a) rec.age_year = a.textContent.trim();
                const rows = sec.querySelectorAll(
                    '.tbl-wrap table tbody tr, table.address-table tbody tr');
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
                out.persons.push(rec);
            }
        }
    }
    return JSON.stringify(out);
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Result assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_result(phone: str, data: dict) -> LookupResult:
    res = LookupResult(phone=phone)
    res.compliance = ComplianceStatus(
        state_location=data.get("location", ""),
        dnc_status=data.get("dnc", "unknown"),
        litigator=data.get("litigator", "unknown"),
        blacklist=data.get("blacklist", "unknown"),
    )
    if data.get("no_result"):
        res.found = False
        return res
    for rec in data.get("persons", []):
        pr = PersonRecord(name=rec.get("name", ""), age_year=rec.get("age_year", ""))
        for addr in rec.get("addresses", []):
            pr.addresses.append(AddressRecord(
                lives_at=addr.get("lives_at", ""),
                city=addr.get("city", ""),
                state=addr.get("state", ""),
                zip_code=addr.get("zip_code", ""),
            ))
        res.persons.append(pr)
    res.found = bool(res.persons)
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Single lookup on an already-loaded page
# ─────────────────────────────────────────────────────────────────────────────

async def wait_for_results(page) -> str:
    """
    Wait until the result has SETTLED, i.e.:
      * the compliance side resolved (dncStatus is real text, not "loading…"),
      * no "Loading person information…" placeholder remains, and
      * the person list fingerprint stopped changing for SETTLE_STABLE_CHECKS
        consecutive polls.

    Returns "settled", or "unsettled" if we ran out of time with partial content
    (caller keeps it but logs it), and raises TimeoutError if nothing arrived.
    """
    deadline = time.monotonic() + RESULT_TIMEOUT_SEC
    last_fp = None
    stable = 0
    saw_content = False

    while time.monotonic() < deadline:
        try:
            s = await page.evaluate(JS_READY_STATE)
        except Exception:
            await asyncio.sleep(SETTLE_POLL)
            continue

        has_content = s["real"] > 0 or s["noResult"]
        saw_content = saw_content or has_content

        if s["dncReady"] and not s["loading"] and has_content:
            fp = (s["sections"], s["len"], s["real"])
            if fp == last_fp:
                stable += 1
                if stable >= SETTLE_STABLE_CHECKS:
                    return "settled"
            else:
                last_fp = fp
                stable = 0
        else:
            # Still painting — reset, the list is not final yet.
            last_fp = None
            stable = 0

        await asyncio.sleep(SETTLE_POLL)

    if saw_content:
        return "unsettled"
    raise TimeoutError(
        f"No results rendered within {RESULT_TIMEOUT_SEC:.0f}s "
        f"(site slow, rate-limiting, or layout changed)"
    )


async def lookup_one(page, phone: str) -> LookupResult:
    """Search one number on a page that is already sitting on BASE_URL."""
    status = await page.evaluate(JS_SEARCH, phone)

    if status != "ok":
        # JS couldn't find the controls — fall back to real Playwright actions.
        # Also covers the (unlikely) case of a framework that ignores .value.
        await page.fill("#phoneInput", phone, timeout=5000)
        await page.click("#searchButton", timeout=5000)

    state = await wait_for_results(page)          # raises TimeoutError if empty
    if state == "unsettled":
        log(f"  [WARN] {phone}: list still changing at timeout — "
            f"record may be incomplete")

    raw = await page.evaluate(JS_EXTRACT)
    return build_result(phone, json.loads(raw or "{}"))


# ─────────────────────────────────────────────────────────────────────────────
# Pool worker — owns one page, drains the shared queue
# ─────────────────────────────────────────────────────────────────────────────

class Writer:
    """Serialises JSONL appends from all workers onto one file handle."""

    def __init__(self, path):
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = asyncio.Lock()
        self.count = 0

    async def write(self, result: LookupResult):
        line = json.dumps(result.to_dict())
        async with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            self.count += 1
            return self.count

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def ensure_playwright_browser():
    """
    Download Playwright's own Chromium if it isn't present.

    Streamlit Cloud does NOT execute setup.sh, so the `playwright install
    chromium` in it never runs there. Normally apt-installed chromium (from
    packages.txt) is used instead and this is skipped entirely — this is the
    last-resort net so a missing browser doesn't kill the whole run.
    """
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(cache) and any(n.startswith("chromium")
                                    for n in os.listdir(cache)):
        return
    log("[BROWSER] no system chromium found; running 'playwright install chromium'…")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, timeout=900,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        log("[BROWSER] playwright chromium installed")
    except Exception as e:
        log(f"[BROWSER] 'playwright install chromium' failed: {e}")


def resolve_chromium():
    """
    Return a path to a system Chromium, or None to let Playwright use its own.

    On Linux (Streamlit Cloud) we prefer the apt build from packages.txt — it is
    already on disk, so there's nothing to download at startup. On Windows we
    always use the playwright-managed build.
    """
    if sys.platform == "win32":
        return None
    for name in ("chromium", "chromium-browser",
                 "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    ensure_playwright_browser()
    return None


async def setup_page(context):
    page = await context.new_page()
    page.set_default_timeout(int(RESULT_TIMEOUT_SEC * 1000))

    blocked = set(BLOCK_RESOURCE_TYPES)
    if BLOCK_CSS:
        blocked.add("stylesheet")

    async def router(route):
        req = route.request
        if req.resource_type in blocked:
            return await route.abort()
        if any(h in req.url for h in BLOCK_HOSTS):
            return await route.abort()
        await route.continue_()

    await page.route("**/*", router)
    await page.goto(BASE_URL, wait_until="domcontentloaded",
                    timeout=int(NAV_TIMEOUT_SEC * 1000))
    # One settle pass so the site's own JS wires up its handlers.
    await page.wait_for_selector("#phoneInput", timeout=int(NAV_TIMEOUT_SEC * 1000))
    return page


async def pool_worker(wid: int, context, queue: asyncio.Queue, writer: Writer, total: int):
    page = None
    since_reload = 0

    try:
        page = await setup_page(context)
        log(f"[W{wid}] page ready")
    except Exception as e:
        log(f"[W{wid}] FAILED to open page: {e}")
        return

    while True:
        try:
            phone = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        result = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if since_reload >= RELOAD_EVERY:
                    await page.goto(BASE_URL, wait_until="domcontentloaded",
                                    timeout=int(NAV_TIMEOUT_SEC * 1000))
                    await page.wait_for_selector("#phoneInput")
                    since_reload = 0

                result = await lookup_one(page, phone)
                since_reload += 1
                break

            except Exception as e:
                if attempt >= MAX_RETRIES:
                    result = LookupResult(
                        phone=phone,
                        error=f"The site did not respond correctly after "
                              f"{MAX_RETRIES} attempts. ({e})",
                    )
                else:
                    # Reload before retrying — the page may be in a bad state.
                    try:
                        await page.goto(BASE_URL, wait_until="domcontentloaded",
                                        timeout=int(NAV_TIMEOUT_SEC * 1000))
                        await page.wait_for_selector("#phoneInput")
                        since_reload = 0
                    except Exception:
                        pass

        done = await writer.write(result)
        tag = "ERR " if result.error else ("HIT " if result.found else "none")
        log(f"[{done}/{total}] {phone} {tag} (W{wid})")

        queue.task_done()
        if INTER_SEARCH_DELAY > 0:
            await asyncio.sleep(INTER_SEARCH_DELAY)

    try:
        await page.close()
    except Exception:
        pass
    log(f"[W{wid}] finished")


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat — throughput + ETA in the live log
# ─────────────────────────────────────────────────────────────────────────────

async def heartbeat(writer: Writer, total: int, started: float):
    while True:
        await asyncio.sleep(15)
        done = writer.count
        if done == 0:
            continue
        elapsed = time.monotonic() - started
        rate = done / elapsed
        remaining = (total - done) / rate if rate > 0 else 0
        log(f"[STATS] {done}/{total} | {rate:.2f}/sec | "
            f"{rate * 3600:,.0f}/hour | ETA {remaining / 60:.0f} min")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run(phones, out_file, concurrency):
    from playwright.async_api import async_playwright

    total = len(phones)
    concurrency = max(1, min(concurrency, total))
    log(f"[START] {total} numbers | {concurrency} parallel pages")

    queue = asyncio.Queue()
    for p in phones:
        queue.put_nowait(p)

    writer = Writer(out_file)
    started = time.monotonic()

    async with async_playwright() as pw:
        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",       # required in small containers
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--blink-settings=imagesEnabled=false",
            ],
        }
        exe = resolve_chromium()
        if exe:
            launch_args["executable_path"] = exe
            log(f"[BROWSER] using system chromium: {exe}")
        else:
            log("[BROWSER] using playwright-managed chromium")

        try:
            browser = await pw.chromium.launch(**launch_args)
        except Exception as e:
            log(f"[FATAL] Could not launch Chromium: {e}")
            log("[FATAL] No usable browser. On Streamlit Cloud, packages.txt must "
                "contain 'chromium' (NOT 'chromium-browser' — that package does "
                "not exist on Debian). Locally, run: playwright install chromium")
            writer.close()
            return
        # ONE context shared by all pages — far cheaper than a context each.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        log("[BROWSER] launched")

        hb = asyncio.create_task(heartbeat(writer, total, started))
        try:
            await asyncio.gather(*[
                pool_worker(i + 1, context, queue, writer, total)
                for i in range(concurrency)
            ])
        except Exception as e:
            log(f"[FATAL] {e}")
            traceback.print_exc()
        finally:
            hb.cancel()
            writer.close()
            try:
                await context.close()
                await browser.close()
            except Exception:
                pass

    elapsed = time.monotonic() - started
    rate = writer.count / elapsed if elapsed else 0
    log(f"[DONE] {writer.count}/{total} in {elapsed / 60:.1f} min "
        f"| {rate:.2f}/sec | {rate * 3600:,.0f}/hour")


def main():
    if len(sys.argv) < 3:
        print("Usage: python worker.py <phones_json> <results_jsonl> [concurrency]")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        phones = json.load(f)

    concurrency = int(sys.argv[3]) if len(sys.argv) > 3 else CONCURRENCY

    # Playwright's async API needs the default ProactorEventLoop on Windows,
    # so unlike scraper/lookup.py we must NOT switch to a selector loop here.
    asyncio.run(run(phones, sys.argv[2], concurrency))


if __name__ == "__main__":
    main()
