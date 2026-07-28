"""
InfoLookup Scraper — Streamlit UI
Run:  streamlit run app.py

Built for large batches. Three things make that possible:
  * The heavy lifting happens in worker.py (a separate process running a pool of
    parallel browser pages). This UI only tails its JSONL output.
  * Results are NEVER all rendered as cards — that's what made big runs freeze
    the browser. We keep running counters + the most recent rows, and build the
    full CSV from disk only when you ask for it.
  * Runs live in ./runs/<id>/ instead of temp files, so a crashed or interrupted
    run can be resumed instead of restarted.
"""

import csv
import json
import os
import subprocess
import sys
import time
from collections import deque
from io import StringIO

import psutil
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper.config import CONCURRENCY
from utils.phone_generator import generate_phone_numbers, count_numbers

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(APP_DIR, "runs")
RECENT_MAX = 200          # rows kept in memory for the live table

# Throughput model for the ETA, fitted to measured runs (dev machine, 4 and 8
# parallel pages: 1.2/s and 1.78/s). Scaling is SUBLINEAR — doubling pages does
# not double speed, because Chromium becomes CPU-bound. Streamlit Cloud's shared
# CPU is slower still, so treat this as an optimistic bound.
def estimate_rate(pages: int) -> float:
    return (pages ** 0.7) / 2.3

# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="InfoLookup Scraper", page_icon="📞", layout="wide")

st.markdown("""
<style>
    .main-title { font-size:2rem; font-weight:800; color:#00d4ff; }
    .subtitle   { color:#888; margin-top:0; }
</style>
""", unsafe_allow_html=True)

DEFAULT_STATE = {
    "running": False,
    "run_complete": False,
    "run_dir": None,
    "_pid": None,
    "_total": 0,
    "_started_at": None,
    "_file_offset": 0,
    "_log_offset": 0,
    "_log_tail": deque(maxlen=40),
    "_recent": deque(maxlen=RECENT_MAX),
    "_done": 0,
    "_found": 0,
    "_no_info": 0,
    "_errors": 0,
    "_worker_error": None,
}
for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Process helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_pid_running(pid):
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def kill_tree(pid):
    """Kill the worker AND its chromium children — orphaned browsers eat the
    container's memory and will break the next run."""
    try:
        parent = psutil.Process(pid)
    except Exception:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Run directory / resume
# ─────────────────────────────────────────────────────────────────────────────

def paths_for(run_dir):
    return (os.path.join(run_dir, "phones.json"),
            os.path.join(run_dir, "results.jsonl"),
            os.path.join(run_dir, "worker.log"),
            os.path.join(run_dir, "meta.json"))


def completed_phones(results_file):
    """Phones already written to the JSONL — used to skip work on resume."""
    done = set()
    try:
        with open(results_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line).get("Phone", ""))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return done


def list_runs():
    if not os.path.isdir(RUNS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        run_dir = os.path.join(RUNS_DIR, name)
        _, results_file, _, meta_file = paths_for(run_dir)
        if not os.path.isfile(meta_file):
            continue
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        meta["run_dir"] = run_dir
        meta["done"] = sum(1 for _ in open(results_file, encoding="utf-8")) \
            if os.path.isfile(results_file) else 0
        out.append(meta)
    return out


def start_worker(run_dir, phones, concurrency, total_target):
    phones_file, results_file, log_file, meta_file = paths_for(run_dir)

    with open(phones_file, "w", encoding="utf-8") as f:
        json.dump(phones, f)
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump({"total": total_target,
                   "concurrency": concurrency,
                   "label": os.path.basename(run_dir)}, f)
    open(results_file, "a", encoding="utf-8").close()

    worker_env = os.environ.copy()
    worker_env["PYTHONUTF8"] = "1"

    with open(log_file, "a", encoding="utf-8") as log_out:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(APP_DIR, "worker.py"),
             phones_file, results_file, str(concurrency)],
            stdout=log_out, stderr=log_out, env=worker_env,
        )
    return proc.pid


def reset_live_state(run_dir, pid, total, keep_offsets=False):
    st.session_state.run_dir      = run_dir
    st.session_state._pid         = pid
    st.session_state._total       = total
    st.session_state.running      = True
    st.session_state.run_complete = False
    st.session_state._started_at  = time.monotonic()
    st.session_state._worker_error = None
    if not keep_offsets:
        st.session_state._file_offset = 0
        st.session_state._log_offset  = 0
        st.session_state._recent   = deque(maxlen=RECENT_MAX)
        st.session_state._log_tail = deque(maxlen=40)
        st.session_state._done = st.session_state._found = 0
        st.session_state._no_info = st.session_state._errors = 0


# ─────────────────────────────────────────────────────────────────────────────
# Result reading (incremental — never re-parses the whole file)
# ─────────────────────────────────────────────────────────────────────────────

def read_new_lines(filepath, offset):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        return [l for l in data.splitlines() if l.strip()], new_offset
    except Exception:
        return [], offset


def summarise(d: dict) -> dict:
    """One compact row per number for the live table."""
    persons = d.get("Persons", []) or []
    comp = d.get("Compliance", {}) or {}
    if d.get("Error"):
        status = "⚠️ error"
    elif d.get("Found"):
        status = "✅ found"
    else:
        status = "🚫 no info"
    return {
        "Phone": d.get("Phone", ""),
        "Status": status,
        "Name": persons[0].get("Name", "") if persons else "",
        "Age/Year": persons[0].get("Age/Year", "") if persons else "",
        "Location": comp.get("State/Location", ""),
        "DNC": comp.get("DNC Status", ""),
        "Litigator": comp.get("Litigator", ""),
        "Blacklist": comp.get("Blacklist", ""),
        "Records": len(persons),
        "Error": (d.get("Error") or "")[:90],
    }


def ingest(results_file):
    """Pull newly written results into counters + the recent-rows buffer."""
    lines, new_offset = read_new_lines(results_file, st.session_state._file_offset)
    st.session_state._file_offset = new_offset
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        st.session_state._done += 1
        if d.get("Error"):
            st.session_state._errors += 1
        elif d.get("Found"):
            st.session_state._found += 1
        else:
            st.session_state._no_info += 1
        st.session_state._recent.append(summarise(d))
    return len(lines)


@st.cache_data(show_spinner="Building CSV…")
def build_csv(results_file, size, mtime):
    """Full CSV straight from disk. Cached on (size, mtime) so it is only
    rebuilt when new results have landed — not on every 3s rerun."""
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Phone", "Found", "State/Location", "DNC Status", "Litigator",
                "Blacklist", "Person Name", "Age/Year", "Lives At", "City",
                "State", "ZIP", "Error"])
    with open(results_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            c = d.get("Compliance", {}) or {}
            base = [d.get("Phone", ""), d.get("Found", False),
                    c.get("State/Location", ""), c.get("DNC Status", ""),
                    c.get("Litigator", ""), c.get("Blacklist", "")]
            persons = d.get("Persons", []) or [None]
            for p in persons:
                addrs = (p.get("Addresses") or [None]) if p else [None]
                for a in addrs:
                    w.writerow(base + [
                        p.get("Name", "") if p else "",
                        p.get("Age/Year", "") if p else "",
                        a.get("Lives At", "") if a else "",
                        a.get("City", "") if a else "",
                        a.get("State", "") if a else "",
                        a.get("ZIP", "") if a else "",
                        d.get("Error") or "",
                    ])
    return out.getvalue()


def csv_download(results_file, label="⬇️ Download CSV"):
    try:
        stat = os.stat(results_file)
    except Exception:
        return
    st.download_button(label, build_csv(results_file, stat.st_size, stat.st_mtime),
                       "infolookup_results.csv", "text/csv", use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📞 Phone Range Builder")
    area_code = st.text_input("Area Code (3 digits)", value="910", max_chars=3)

    st.markdown("**Exchange — middle 3 digits**")
    ec1, ec2 = st.columns(2)
    exchange_start = ec1.number_input("From", 0, 999, 785, key="es")
    exchange_end   = ec2.number_input("To",   0, 999, 785, key="ee")

    st.markdown("**Subscriber — last 4 digits**")
    sc1, sc2 = st.columns(2)
    subscriber_start = sc1.number_input("From", 0, 9999, 2360, key="ss")
    subscriber_end   = sc2.number_input("To",   0, 9999, 2365, key="se")

    valid_area  = len(area_code.strip()) == 3 and area_code.strip().isdigit()
    valid_range = exchange_start <= exchange_end and subscriber_start <= subscriber_end

    total = 0
    if valid_area and valid_range:
        total = count_numbers(exchange_start, exchange_end,
                              subscriber_start, subscriber_end)
        st.success(f"✅ {total:,} numbers")
    else:
        if not valid_area:
            st.error("Area code must be exactly 3 digits")
        if not valid_range:
            st.error("'From' must be ≤ 'To'")

    st.markdown("---")
    st.markdown("### ⚡ Speed")
    concurrency = st.slider(
        "Parallel browser pages", 1, 8, CONCURRENCY,
        help="How many numbers are searched at the same time. Each page costs "
             "~25 MB of RAM on top of ~130 MB for the browser.",
    )
    if concurrency >= 7:
        st.warning("6+ pages can exhaust Streamlit Cloud's ~1 GB RAM and get the "
                   "app killed mid-run. Try 4–5 first.")

    if total:
        rate = estimate_rate(concurrency)
        eta_h = total / rate / 3600
        eta_txt = (f"**{eta_h * 60:,.0f} min**" if eta_h < 1.5
                   else f"**{eta_h:,.1f} hours**")
        st.caption(f"≈ **{rate:.1f}/sec** (~{rate * 3600:,.0f}/hour) → "
                   f"{eta_txt} for {total:,} numbers")
        st.caption("Best case — shared cloud CPU is slower than this.")

    st.markdown("---")
    run_btn = st.button(
        "🚀 Start Lookup", type="primary", use_container_width=True,
        disabled=(not valid_area or not valid_range or total == 0
                  or st.session_state.running),
    )

    if st.session_state.running:
        if st.button("⏹️ Stop Run", use_container_width=True):
            kill_tree(st.session_state._pid)
            st.session_state.running = False
            st.session_state.run_complete = True
            st.rerun()

    # ── Resume ───────────────────────────────────────────────────────────────
    if not st.session_state.running:
        prior = [r for r in list_runs() if r["done"] < r.get("total", 0)]
        if prior:
            st.markdown("---")
            st.markdown("### ♻️ Resume unfinished run")
            pick = st.selectbox(
                "Run", prior,
                format_func=lambda r: f"{r['label']} — {r['done']:,}/{r['total']:,}",
            )
            if st.button("▶️ Resume", use_container_width=True):
                _, results_file, _, _ = paths_for(pick["run_dir"])
                phones_file = paths_for(pick["run_dir"])[0]
                with open(phones_file, "r", encoding="utf-8") as f:
                    original = json.load(f)
                remaining = [p for p in original if p not in completed_phones(results_file)]
                if remaining:
                    pid = start_worker(pick["run_dir"], remaining,
                                       concurrency, pick["total"])
                    reset_live_state(pick["run_dir"], pid, pick["total"])
                    st.rerun()
                else:
                    st.info("That run is already complete.")

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="main-title">📞 InfoLookup Scraper</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Automated phone lookup via infolookup.site</div>',
            unsafe_allow_html=True)
st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Start a new run
# ─────────────────────────────────────────────────────────────────────────────
if run_btn and total and not st.session_state.running:
    phones = list(generate_phone_numbers(area_code.strip(), exchange_start,
                                         exchange_end, subscriber_start,
                                         subscriber_end))
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)
    pid = start_worker(run_dir, phones, concurrency, len(phones))
    reset_live_state(run_dir, pid, len(phones))
    st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Live view
# ─────────────────────────────────────────────────────────────────────────────
if st.session_state.running:
    _, results_file, log_file, _ = paths_for(st.session_state.run_dir)
    ingest(results_file)

    done  = st.session_state._done
    total_n = st.session_state._total
    still_running = is_pid_running(st.session_state._pid)
    elapsed = time.monotonic() - (st.session_state._started_at or time.monotonic())
    rate = done / elapsed if elapsed > 0 and done else 0

    m = st.columns(5)
    m[0].metric("Progress", f"{done:,}/{total_n:,}")
    m[1].metric("✅ Found",  f"{st.session_state._found:,}")
    m[2].metric("🚫 No Info", f"{st.session_state._no_info:,}")
    m[3].metric("⚠️ Errors", f"{st.session_state._errors:,}")
    m[4].metric("Speed", f"{rate:.2f}/s", f"{rate * 3600:,.0f}/hr" if rate else None)

    st.progress(min(done / total_n, 1.0) if total_n else 0.0)
    if rate > 0 and done < total_n:
        st.caption(f"Elapsed {elapsed / 60:.1f} min · "
                   f"ETA ~{(total_n - done) / rate / 60:,.0f} min")

    log_lines, st.session_state._log_offset = read_new_lines(
        log_file, st.session_state._log_offset)
    st.session_state._log_tail.extend(log_lines)
    with st.expander("📋 Worker log (live)"):
        st.code("\n".join(st.session_state._log_tail) or "waiting…", language=None)

    st.markdown(f"#### Latest results (last {RECENT_MAX})")
    if st.session_state._recent:
        st.dataframe(list(st.session_state._recent)[::-1],
                     use_container_width=True, hide_index=True, height=420)
    else:
        st.info("Launching browser…")

    with st.expander("⬇️ Download results so far"):
        csv_download(results_file, "⬇️ Download partial CSV")

    if not still_running:
        ingest(results_file)                      # drain final writes
        if st.session_state._done == 0:
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    st.session_state._worker_error = f.read()[-4000:]
            except Exception:
                st.session_state._worker_error = "Worker exited with no output."
        st.session_state.running = False
        st.session_state.run_complete = True
        st.rerun()
    else:
        time.sleep(3)
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Finished view
# ─────────────────────────────────────────────────────────────────────────────
elif st.session_state.run_complete:
    _, results_file, log_file, _ = paths_for(st.session_state.run_dir)

    if st.session_state._worker_error and st.session_state._done == 0:
        st.error("❌ The scraper crashed before producing any results.")
        st.markdown("**Technical details (share these when reporting):**")
        st.code(st.session_state._worker_error, language=None)
    else:
        done = st.session_state._done
        elapsed = time.monotonic() - (st.session_state._started_at or time.monotonic())
        rate = done / elapsed if elapsed else 0

        m = st.columns(5)
        m[0].metric("🔍 Total",    f"{done:,}")
        m[1].metric("✅ Found",    f"{st.session_state._found:,}")
        m[2].metric("🚫 No Info",  f"{st.session_state._no_info:,}")
        m[3].metric("⚠️ Errors",   f"{st.session_state._errors:,}")
        m[4].metric("Avg speed",  f"{rate:.2f}/s")

        if done < st.session_state._total:
            st.warning(f"Stopped at {done:,} of {st.session_state._total:,}. "
                       "Use **♻️ Resume unfinished run** in the sidebar to "
                       "continue where it left off — completed numbers are kept.")
        st.markdown("---")
        st.markdown(f"#### Last {RECENT_MAX} results")
        if st.session_state._recent:
            st.dataframe(list(st.session_state._recent)[::-1],
                         use_container_width=True, hide_index=True, height=420)
        st.caption("The table shows recent rows only — the CSV below contains "
                   "every result, expanded one row per address.")
        st.markdown("---")
        csv_download(results_file)

        with st.expander("📋 Worker log"):
            lines, _ = read_new_lines(log_file, 0)
            st.code("\n".join(lines[-300:]) or "—", language=None)

else:
    st.markdown("""
    ### 👈 Set your phone range in the sidebar, then hit **Start Lookup**

    **How it works**
    1. Enter the **3-digit area code** (e.g. `910`)
    2. Set the **middle 3 digits** range (exchange)
    3. Set the **last 4 digits** range (subscriber)
    4. Pick how many **parallel browser pages** to use — this is the speed dial
    5. Click **🚀 Start Lookup**; progress, speed and ETA update live
    6. Download everything as CSV when it finishes

    **Running big batches on Streamlit Cloud — please read**
    - Keep this browser tab **open**. Streamlit Cloud sleeps idle apps, and that
      kills the scraper with it.
    - Completed results are saved to disk as they arrive, so if a run does die
      you can **♻️ Resume** it from the sidebar instead of starting over.
    - More pages is faster only up to a point. This container has ~1 GB RAM and
      1–2 shared CPUs; past 5–6 pages Chromium starts thrashing and timing out.
    """)
