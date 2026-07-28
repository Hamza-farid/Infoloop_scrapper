"""
InfoLookup Scraper — Streamlit UI
Run:  streamlit run app.py
"""

import sys
import os
import json
import subprocess
import tempfile
import time
import psutil
from io import StringIO
import csv

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.phone_generator import generate_phone_numbers, count_numbers
from utils.models import LookupResult, ComplianceStatus, PersonRecord, AddressRecord

# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="InfoLookup Scraper", page_icon="📞", layout="wide")

st.markdown("""
<style>
    .main-title { font-size:2rem; font-weight:800; color:#00d4ff; }
    .subtitle   { color:#888; margin-top:0; }
    .result-card {
        background:#1a1d2e; border:1px solid #2a2d3e;
        border-radius:12px; padding:1.2rem; margin-bottom:1rem;
    }
    .status-clean   { color:#00e676; font-weight:700; }
    .status-flagged { color:#ff5252; font-weight:700; }
    .status-unknown { color:#ffd740; font-weight:700; }
    .found-chip    { background:#1b5e20; color:#69f0ae; padding:3px 10px; border-radius:20px; font-size:0.78rem; font-weight:700; }
    .nofound-chip  { background:#3e2723; color:#ff8a65; padding:3px 10px; border-radius:20px; font-size:0.78rem; font-weight:700; }
    .error-chip    { background:#4a1942; color:#f48fb1; padding:3px 10px; border-radius:20px; font-size:0.78rem; font-weight:700; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
for k, v in {
    "results": [], "running": False, "run_complete": False,
    "phones_to_run": [],
    "_pid": None,           # worker process PID (serializable)
    "_log_file": None,      # worker stdout log file path
    "_out_file": None,      # JSONL results file path
    "_phones_file": None,
    "_file_offset": 0,
    "_log_offset": 0,
    "_worker_error": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


def _is_pid_running(pid):
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────
def _status_html(val):
    v = val.lower()
    if v == "clean":              return f'<span class="status-clean">✅ {val}</span>'
    if v in ("flagged","listed"): return f'<span class="status-flagged">🚨 {val}</span>'
    return f'<span class="status-unknown">❓ {val}</span>'


def _dict_to_result(d: dict) -> LookupResult:
    r = LookupResult(phone=d.get("Phone",""), found=d.get("Found", False), error=d.get("Error"))
    c = d.get("Compliance", {})
    if c:
        r.compliance = ComplianceStatus(
            state_location=c.get("State/Location",""),
            dnc_status=c.get("DNC Status","unknown"),
            litigator=c.get("Litigator","unknown"),
            blacklist=c.get("Blacklist","unknown"),
        )
    for p in d.get("Persons", []):
        pr = PersonRecord(name=p.get("Name",""), age_year=p.get("Age/Year",""))
        for a in p.get("Addresses", []):
            pr.addresses.append(AddressRecord(
                lives_at=a.get("Lives At",""), city=a.get("City",""),
                state=a.get("State",""),       zip_code=a.get("ZIP",""),
            ))
        r.persons.append(pr)
    return r


def _render_result(res: LookupResult):
    if res.error:    chip = '<span class="error-chip">⚠️ ERROR</span>'
    elif res.found:  chip = '<span class="found-chip">✅ OWNER FOUND</span>'
    else:            chip = '<span class="nofound-chip">🚫 NO OWNER INFO</span>'

    st.markdown(f"""
    <div class="result-card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem">
            <span style="font-size:1.1rem;font-weight:700;color:#00d4ff">📞 {res.phone}</span>
            {chip}
        </div>
    """, unsafe_allow_html=True)

    if res.error:
        st.error(f"Something went wrong: {res.error}")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    if res.compliance:
        c = res.compliance
        c1,c2,c3,c4 = st.columns(4)
        c1.markdown(f"**📍 Location**<br>{c.state_location or '—'}", unsafe_allow_html=True)
        c2.markdown(f"**DNC**<br>{_status_html(c.dnc_status)}",       unsafe_allow_html=True)
        c3.markdown(f"**Litigator**<br>{_status_html(c.litigator)}",  unsafe_allow_html=True)
        c4.markdown(f"**Blacklist**<br>{_status_html(c.blacklist)}",  unsafe_allow_html=True)

    if res.found and res.persons:
        st.markdown("<hr style='margin:.8rem 0'>", unsafe_allow_html=True)
        for p in res.persons:
            st.markdown(f"**👤 {p.name}**  {p.age_year}", unsafe_allow_html=True)
            if p.addresses:
                import pandas as pd
                st.dataframe(pd.DataFrame([a.to_dict() for a in p.addresses]),
                             use_container_width=True, hide_index=True)
    elif not res.found:
        st.markdown("*No owner information available.*")

    st.markdown("</div>", unsafe_allow_html=True)


def _results_to_csv(results):
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Phone","Found","State/Location","DNC Status","Litigator","Blacklist",
                "Person Name","Age/Year","Lives At","City","State","ZIP","Error"])
    for r in results:
        co = r.compliance
        persons = r.persons if r.found and r.persons else [None]
        for p in persons:
            addrs = p.addresses if p and p.addresses else [None]
            for a in addrs:
                w.writerow([
                    r.phone, r.found,
                    co.state_location if co else "", co.dnc_status if co else "",
                    co.litigator if co else "",      co.blacklist  if co else "",
                    p.name if p else "",   p.age_year if p else "",
                    a.lives_at if a else "", a.city  if a else "",
                    a.state    if a else "", a.zip_code if a else "",
                    r.error or ""
                ])
    return out.getvalue()


def _read_new_lines(filepath, offset):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        lines = [l.strip() for l in data.splitlines() if l.strip()]
        return lines, new_offset
    except Exception:
        return [], offset


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📞 Phone Range Builder")
    st.markdown("### Area Code (3 digits)")
    area_code = st.text_input("Area Code", value="910", max_chars=3, label_visibility="collapsed")

    st.markdown("### Exchange — middle 3 digits")
    ec1, ec2 = st.columns(2)
    exchange_start = ec1.number_input("From", 0, 999,  785, key="es")
    exchange_end   = ec2.number_input("To",   0, 999,  785, key="ee")

    st.markdown("### Subscriber — last 4 digits")
    sc1, sc2 = st.columns(2)
    subscriber_start = sc1.number_input("From", 0, 9999, 2360, key="ss")
    subscriber_end   = sc2.number_input("To",   0, 9999, 2365, key="se")

    valid_area  = len(area_code.strip()) == 3 and area_code.strip().isdigit()
    valid_range = exchange_start <= exchange_end and subscriber_start <= subscriber_end

    if valid_area and valid_range:
        total = count_numbers(exchange_start, exchange_end, subscriber_start, subscriber_end)
        st.success(f"✅ {total:,} numbers")
        preview_phones = list(generate_phone_numbers(area_code.strip(),
                              exchange_start, exchange_end, subscriber_start, subscriber_end))
        snippet = "\n".join(preview_phones[:5]) + (f"\n... +{total-5} more" if total > 5 else "")
        st.code(snippet, language=None)
    else:
        if not valid_area:  st.error("Area code must be exactly 3 digits")
        if not valid_range: st.error("'From' must be ≤ 'To'")
        preview_phones = []
        total = 0

    st.markdown("---")
    run_btn = st.button("🚀 Start Lookup",
                        disabled=(not valid_area or not valid_range or total == 0
                                  or st.session_state.running),
                        use_container_width=True, type="primary")
    if st.session_state.results:
        if st.button("🗑️ Clear Results", use_container_width=True):
            st.session_state.results      = []
            st.session_state.run_complete = False
            st.rerun()

# ── Main header ───────────────────────────────────────────────────────────────
st.markdown('<div class="main-title">📞 InfoLookup Scraper</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Automated phone lookup via infolookup.site</div>', unsafe_allow_html=True)
st.markdown("---")

# ── Start batch ───────────────────────────────────────────────────────────────
if run_btn and preview_phones and not st.session_state.running:
    # Temp file for phones list
    pf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(preview_phones, pf)
    pf.close()

    # Temp file for JSONL results (worker appends here)
    rf = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    rf.close()

    # Temp file to capture worker stdout (for error display)
    lf = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8")
    lf.close()

    worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")

    # PYTHONUTF8=1 forces UTF-8 stdout/stderr in the child process on Windows
    worker_env = os.environ.copy()
    worker_env["PYTHONUTF8"] = "1"

    with open(lf.name, "w", encoding="utf-8") as log_out:
        proc = subprocess.Popen(
            [sys.executable, "-u", worker_path, pf.name, rf.name],
            stdout=log_out,
            stderr=log_out,
            env=worker_env,
        )

    st.session_state._pid          = proc.pid
    st.session_state._phones_file  = pf.name
    st.session_state._out_file     = rf.name
    st.session_state._log_file     = lf.name
    st.session_state.phones_to_run = preview_phones
    st.session_state.results       = []
    st.session_state.run_complete  = False
    st.session_state.running       = True
    st.session_state._file_offset  = 0
    st.session_state._log_offset   = 0
    st.session_state._worker_error = None
    st.rerun()

# ── Running: poll for results ─────────────────────────────────────────────────
if st.session_state.running:
    pid      = st.session_state._pid
    out_file = st.session_state._out_file
    log_file = st.session_state._log_file
    phones   = st.session_state.phones_to_run
    total_n  = len(phones)
    offset   = st.session_state._file_offset

    # Read new result lines from JSONL file
    new_lines, new_offset = _read_new_lines(out_file, offset)
    st.session_state._file_offset = new_offset
    for line in new_lines:
        try:
            st.session_state.results.append(_dict_to_result(json.loads(line)))
        except Exception:
            pass

    done_count = len(st.session_state.results)
    pct = done_count / total_n if total_n else 1

    still_running = _is_pid_running(pid)

    st.info(f"🔄 Running... **{done_count}/{total_n}** complete")
    st.progress(pct)

    # Show live terminal log in expander
    log_lines, new_log_offset = _read_new_lines(log_file, st.session_state._log_offset)
    st.session_state._log_offset = new_log_offset
    if log_lines:
        with st.expander("📋 Worker log (live)", expanded=False):
            st.code("\n".join(log_lines), language=None)

    for r in st.session_state.results:
        _render_result(r)

    if not still_running:
        # Process ended — drain remaining lines
        final_lines, _ = _read_new_lines(out_file, new_offset)
        for line in final_lines:
            try:
                st.session_state.results.append(_dict_to_result(json.loads(line)))
            except Exception:
                pass

        # Check if worker crashed (no results written but process ended)
        if not st.session_state.results:
            try:
                with open(log_file, "r", encoding="utf-8") as lf:
                    crash_log = lf.read()
                st.session_state._worker_error = crash_log
            except Exception:
                st.session_state._worker_error = "Worker process exited with no output."

        # Cleanup
        for f in [st.session_state._phones_file, out_file, log_file]:
            try: os.unlink(f)
            except Exception: pass

        st.session_state.running      = False
        st.session_state.run_complete = True
        st.rerun()
    else:
        time.sleep(2)
        st.rerun()

# ── Final results ─────────────────────────────────────────────────────────────
elif st.session_state.run_complete:
    # Show crash log if worker died with no results
    if st.session_state._worker_error and not st.session_state.results:
        st.error("❌ The scraper process crashed before producing any results.")
        st.markdown("**Technical details (share these when reporting the issue):**")
        st.code(st.session_state._worker_error, language=None)

    elif st.session_state.results:
        results = st.session_state.results
        found   = [r for r in results if r.found]
        no_info = [r for r in results if not r.found and not r.error]
        errors  = [r for r in results if r.error]

        m1,m2,m3,m4 = st.columns(4)
        m1.metric("🔍 Total",       len(results))
        m2.metric("✅ Owner Found", len(found))
        m3.metric("🚫 No Info",     len(no_info))
        m4.metric("⚠️ Errors",      len(errors))
        st.markdown("---")

        t1,t2,t3,t4 = st.tabs([f"All ({len(results)})", f"✅ Found ({len(found)})",
                                f"🚫 No Info ({len(no_info)})", f"⚠️ Errors ({len(errors)})"])
        with t1:
            for r in results: _render_result(r)
        with t2:
            [_render_result(r) for r in found]   if found   else st.info("None found.")
        with t3:
            [_render_result(r) for r in no_info] if no_info else st.info("None.")
        with t4:
            [_render_result(r) for r in errors]  if errors  else st.info("No errors.")

        st.markdown("---")
        st.download_button("⬇️ Download CSV", _results_to_csv(results),
                           "infolookup_results.csv", "text/csv", use_container_width=True)

else:
    st.markdown("""
    ### 👈 Set your phone range in the sidebar, then hit **Start Lookup**

    **How it works:**
    1. Enter the **3-digit area code** (e.g. `910`)
    2. Set **middle 3 digits** range (exchange)
    3. Set **last 4 digits** range (subscriber)
    4. Click **🚀 Start Lookup** — a background process searches each number
    5. Results appear live as each number completes
    6. Download all results as CSV when done

    ---
    **Example:** Area `910`, Exchange `785–785`, Subscriber `2360–2365` → 6 numbers
    """)