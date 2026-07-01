"""ANTON — Agentic OS dashboard (Streamlit MVP).

⚠️  DEPRECATED 2026-05-14 — superseded by the React + FastAPI dashboard at
    ``<repo>\\dashboard\\``. That stack runs on the same routines
    backend via ``routines/api/`` (FastAPI bridge on :8765) and ships with
    autostart, Cmd-K, live OpenBB ticker, chunked-embedding recall, and the
    full set of vault / daily / runs / drafts tabs.

    This file stays in tree as historical reference — it documents the
    original Streamlit MVP shape and the subprocess wrappers around the
    five routines. To run it anyway:

        streamlit run routines/routines/dashboard/app.py

    But all new workflow wiring belongs in the React app + bridge.

Original purpose:
  - Top: ANTON wordmark + nav (Knowledge Vault, Plan, Approvals, IDLE pill)
  - Markets banner — scrolling right-to-left
  - Project selector + Create Project + General Chat buttons
  - Tabs: Agent Mode | Knowledge Vault | Daily Notes | Run History | Drafts
  - Centre: prompt + Run + skill grid
  - Right rail: Priority tasks · Latest intelligence · Vault activity · Forecast
  - Footer KPI: 5-hour cap · Weekly cap · Routines
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.profile import load as load_operator_profile
from routines.shared.vault_writer import VaultPaths


# ============================================================ config

DEFAULT_VAULT = Path(os.environ.get("AGENTIC_VAULT", "/mnt/x/OS AI Vault"))
ROUTINES_REPO = Path(__file__).resolve().parent.parent.parent
RUNS_DIR = ROUTINES_REPO / "runs"
RECALL_INDEX = DEFAULT_VAULT / ".recall-index" / "index.db"


st.set_page_config(
    page_title="Anton",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ============================================================ theme

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

/* Designer brief palette */
html, body, [class*="css"], .stApp {
  background-color: #0A1016 !important;
  color: #E8EEF6 !important;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif !important;
}
.stApp > header { background-color: #0A1016 !important; }
code, pre, .mono { font-family: 'JetBrains Mono', Menlo, Consolas, monospace !important; }
h1, h2, h3, h4 { color: #E8EEF6 !important; font-weight: 600 !important; }

/* Nav */
.anton-nav {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 0 18px 0; border-bottom: 1px solid #233041;
}
.anton-logo { font-size: 30px; font-weight: 700; letter-spacing: 0.02em; color: #E8EEF6; }
.anton-nav-links { color: #A9B4C0; font-size: 14px; }
.anton-nav-links span { margin: 0 14px; }
.anton-nav-links .active { color: #2F8CFF; }
.anton-status-pill {
  display: inline-block; padding: 3px 12px; border-radius: 999px;
  background: rgba(107, 203, 119, 0.10);
  border: 1px solid rgba(107, 203, 119, 0.35);
  color: #6BCB77; font-size: 12px; font-weight: 500;
}

/* ----------------------------------------------------------------------
   Typographic scale (consistent across the app):
     text-xs:    12px — chips, small captions
     text-sm:    13px — secondary/sub text, KPI label, rail-sub
     text-base:  14px — default body, buttons, rail items, section labels, tabs, nav
     text-lg:    16px — markets ticker, slight emphasis
     text-xl:    22px — KPI metrics (display)
     text-2xl:   30px — logo
   Use these consistently; deviations only when there's a clear reason.
   ---------------------------------------------------------------------- */

/* Markets ticker — static, no scrolling. Items laid out horizontally with
   subtle vertical separators between them. */
.marquee-container {
  width: 100%; background: #0F1720;
  border: 1px solid #233041; border-radius: 8px;
  padding: 14px 20px; margin: 10px 0 18px 0;
  font-size: 16px;
}
.marquee {
  display: flex; flex-wrap: wrap; gap: 0;
  align-items: center; justify-content: space-between;
  width: 100%;
}
.ticker-item {
  color: #A9B4C0;
  padding: 0 18px;
  border-right: 1px solid #233041;
  display: flex; align-items: baseline; gap: 8px;
  white-space: nowrap;
}
.ticker-item:first-child { padding-left: 0; }
.ticker-item:last-child { border-right: none; padding-right: 0; }
.ticker-item b { color: #E8EEF6; font-weight: 600; }
.ticker-up { color: #2F8CFF; font-weight: 500; }
.ticker-down { color: #7E8A97; font-weight: 500; }

/* Section labels (skill groups, etc.) — base body size, semi-bold */
.anton-section, .skill-section {
  color: #A9B4C0; font-size: 14px; font-weight: 500;
  margin: 22px 0 10px 0;
}

/* KPI footer cards */
.kpi {
  background: #0F1720; border: 1px solid #233041; border-radius: 10px;
  padding: 16px 20px;
}
.kpi-label { color: #A9B4C0; font-size: 13px; }
.kpi-metric {
  font-size: 22px; color: #E8EEF6; margin-top: 4px; font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.kpi-metric .accent { color: #2F8CFF; }
.kpi-bar {
  height: 4px; background: #233041; border-radius: 2px;
  margin-top: 10px; overflow: hidden;
}
.kpi-bar-fill { height: 4px; background: #2F8CFF; border-radius: 2px; }
.kpi-sub { color: #7E8A97; font-size: 13px; margin-top: 6px; }

/* Right-rail cards */
.rail-card {
  background: #0F1720; border: 1px solid #233041; border-radius: 10px;
  padding: 16px 18px; margin-bottom: 14px;
}
.rail-title {
  color: #E8EEF6; font-size: 14px; font-weight: 600; margin-bottom: 12px;
}
.rail-sub { color: #7E8A97; font-size: 13px; margin-bottom: 6px; }
.rail-item {
  font-size: 14px; color: #A9B4C0; margin-bottom: 10px; line-height: 1.55;
}
.rail-item .badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 12px; margin-left: 8px; font-weight: 500;
}
.badge-overdue {
  background: rgba(255, 122, 69, 0.10); color: #FF7A45;
  border: 1px solid rgba(255, 122, 69, 0.30);
}
.badge-due-today {
  background: rgba(47, 140, 255, 0.10); color: #2F8CFF;
  border: 1px solid rgba(47, 140, 255, 0.30);
}
.badge-open {
  background: transparent; color: #7E8A97; border: 1px solid #334155;
}
.badge-created {
  background: rgba(107, 203, 119, 0.10); color: #6BCB77;
  border: 1px solid rgba(107, 203, 119, 0.30);
}
.badge-updated {
  background: transparent; color: #A9B4C0; border: 1px solid #334155;
}

/* Buttons — base body size for label legibility */
.stButton > button {
  background-color: #0F1720 !important;
  border: 1px solid #233041 !important;
  color: #E8EEF6 !important;
  font-family: inherit !important;
  font-size: 14px !important;
  padding: 10px 14px !important;
  width: 100% !important;
  font-weight: 400 !important;
  border-radius: 8px !important;
  transition: all 120ms ease;
}
.stButton > button:hover {
  border-color: #2F8CFF !important;
  background-color: #131C28 !important;
}
.stButton > button[kind="primary"] {
  background-color: #2F8CFF !important;
  color: #FFFFFF !important;
  border: 1px solid #2F8CFF !important;
  font-weight: 500 !important;
}
.stButton > button[kind="primary"]:hover {
  background-color: #1D6FE0 !important;
}
.stButton > button:disabled {
  color: #475569 !important; border-color: #1B2433 !important;
  background-color: #0B121A !important;
}

/* Inputs */
.stTextInput > div > div > input,
.stTextArea textarea,
.stSelectbox > div > div,
.stNumberInput input {
  background-color: #0F1720 !important;
  border: 1px solid #233041 !important;
  color: #E8EEF6 !important;
  border-radius: 8px !important;
  font-family: inherit !important;
}
.stTextArea textarea::placeholder,
.stTextInput input::placeholder { color: #7E8A97 !important; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] {
  border-bottom: 1px solid #233041; gap: 4px;
}
.stTabs [data-baseweb="tab"] {
  color: #A9B4C0 !important;
  background: transparent !important;
  font-size: 14px !important;
  font-weight: 400 !important;
  padding: 10px 16px !important;
}
.stTabs [aria-selected="true"] {
  color: #2F8CFF !important;
  border-bottom: 2px solid #2F8CFF !important;
}

a { color: #2F8CFF !important; }

#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; }
</style>
"""

# Use st.html (Streamlit ≥1.32) — st.markdown(unsafe_allow_html=True) sanitizes
# <style> tags into text in Streamlit 1.40+. st.html injects raw HTML/CSS
# without sanitization.
st.html(CSS)


# ============================================================ helpers

@st.cache_data(ttl=10)
def list_active_projects(vault_root: str) -> list[str]:
    return VaultPaths(Path(vault_root)).list_projects()


@st.cache_data(ttl=30)
def operator(vault_root: str) -> dict:
    p = load_operator_profile(Path(vault_root))
    return {
        "operator": p.operator,
        "current_role": p.raw.get("current_role", {}),
        "active_sectors": p.active_sectors,
        "plan_tier": p.plan_tier,
    }


@st.cache_data(ttl=10)
def read_audit_log(routine: str, limit: int = 25) -> list[dict]:
    path = RUNS_DIR / f"{routine}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


@st.cache_data(ttl=15)
def vault_pulse(vault_root: str, hours: int = 24, limit: int = 10) -> list[dict]:
    root = Path(vault_root)
    cutoff = datetime.now().timestamp() - (hours * 3600)
    skip_dirs = {".git", ".obsidian", ".smart-env", ".recall-index"}
    hits = []
    for path in root.rglob("*.md"):
        if any(d in path.parts for d in skip_dirs):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            hits.append((mtime, path))
    hits.sort(reverse=True)
    out = []
    for t, p in hits[:limit]:
        ago = (datetime.now().timestamp() - t)
        ago_label = f"{int(ago/60)}m ago" if ago < 3600 else (
            f"{int(ago/3600)}h ago" if ago < 86400 else f"{int(ago/86400)}d ago"
        )
        is_new = (datetime.now().timestamp() - t) < 60 * 5  # heuristic: created if very recent
        out.append({
            "path": str(p.relative_to(root).as_posix()),
            "ago": ago_label,
            "kind": "CREATED" if is_new else "UPDATED",
        })
    return out


@st.cache_data(ttl=30)
def ollama_status(url: str = "http://localhost:11434") -> dict:
    try:
        return {"ok": True, **OllamaClient(base_url=url).health()}
    except OllamaError as e:
        return {"ok": False, "error": str(e)}


@st.cache_data(ttl=10)
def recall_index_status() -> dict:
    if not RECALL_INDEX.exists():
        return {"exists": False}
    try:
        import sqlite3
        conn = sqlite3.connect(str(RECALL_INDEX))
        n = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        latest = conn.execute("SELECT MAX(indexed_at) FROM notes").fetchone()[0]
        conn.close()
        return {"exists": True, "count": n, "latest_indexed_at": latest}
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}


@st.cache_data(ttl=300)
def latest_newsletter() -> dict:
    """Find the most-recent newsletter in Resources/Newsletters/."""
    folder = DEFAULT_VAULT / "Resources" / "Newsletters"
    if not folder.exists():
        return {"exists": False}
    files = sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"exists": False}
    latest = files[0]
    text = latest.read_text(encoding="utf-8", errors="replace")
    # Pull out top 3 bullet headlines
    headlines = []
    for line in text.splitlines():
        ls = line.strip()
        if ls.startswith("- ") or ls.startswith("* "):
            headlines.append(ls[2:].strip())
        if len(headlines) >= 3:
            break
    return {
        "exists": True,
        "path": str(latest.relative_to(DEFAULT_VAULT).as_posix()),
        "headlines": headlines,
        "name": latest.stem,
    }


def fire_subprocess(cmd: list[str], cwd: Path | None = None) -> str:
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(p.pid)


# ============================================================ markets banner (static for now)

MARKETS = [
    ("Gold",       "£2,341.20", +0.48),
    ("Brent",      "$82.15",    -0.71),
    ("S&P 500",    "5,304",     +0.35),
    ("FTSE 100",   "8,172",     -0.12),
    ("NASDAQ",     "16,795",    +0.62),
    ("SONIA 3M",   "4.81%",     +0.00),
    ("UK 10Y",     "4.27%",     -0.02),
]


def markets_marquee_html() -> str:
    """Static markets ticker — items laid out horizontally with subtle
    vertical separators between them. No scrolling animation."""
    pieces = []
    for name, price, pct in MARKETS:
        cls = "ticker-up" if pct >= 0 else "ticker-down"
        sign = "+" if pct >= 0 else ""
        pct_str = f"{sign}{pct:.2f}%" if pct != 0 else "flat"
        pieces.append(
            f'<span class="ticker-item">'
            f'<span style="color:#7E8A97;">{name}</span> '
            f'<b>{price}</b> '
            f'<span class="{cls}">{pct_str}</span>'
            f'</span>'
        )
    return f'<div class="marquee-container"><div class="marquee">{"".join(pieces)}</div></div>'


# ============================================================ NAV STRIP

prof = operator(str(DEFAULT_VAULT))

st.markdown(
    f"""
    <div class="anton-nav">
      <div class="anton-logo">ANTON</div>
      <div class="anton-nav-links">
        <span>Knowledge vault</span>
        <span>Plan</span>
        <span>Approvals</span>
        <span>Admin override</span>
        <span class="anton-status-pill">● idle</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(markets_marquee_html(), unsafe_allow_html=True)


# ============================================================ project selector strip

projects = list_active_projects(str(DEFAULT_VAULT))
project_options = ["General Chat (no project)"] + projects

c1, c2, c3, _spacer = st.columns([2, 1, 1, 1])
with c1:
    project_choice = st.selectbox(
        "Project", options=project_options, index=0, label_visibility="collapsed",
        placeholder="Select project…",
    )
with c2:
    create_clicked = st.button("＋ Create project", use_container_width=True)
with c3:
    chat_clicked = st.button("💬 General chat", use_container_width=True)

if create_clicked:
    st.session_state["show_create_modal"] = True
if chat_clicked:
    st.session_state["project_choice"] = "General Chat (no project)"

active_project = None if project_choice.startswith("General Chat") else project_choice
default_sensitivity = "confidential" if active_project else "internal"


# ============================================================ create-project modal

if st.session_state.get("show_create_modal"):
    with st.expander("Create project", expanded=True):
        cn1, cn2 = st.columns(2)
        with cn1:
            new_name = st.text_input("Project name (used as folder name)",
                                       placeholder="HB-Leisure")
            new_side = st.selectbox("Side", ["advisory", "buy", "sell", "minority"])
        with cn2:
            new_sector = st.text_input("Sector",
                                         placeholder="Travel / Leisure / Hospitality")
            new_sensitivity = st.selectbox("Sensitivity",
                                              ["confidential", "internal", "public", "MNPI"],
                                              index=0)
        cb1, cb2 = st.columns(2)
        with cb1:
            create_now = st.button("Create", type="primary", use_container_width=True)
        with cb2:
            cancel = st.button("Cancel", use_container_width=True)
        if cancel:
            st.session_state["show_create_modal"] = False
            st.rerun()
        if create_now and new_name:
            template = DEFAULT_VAULT / "Projects" / "_template"
            target = DEFAULT_VAULT / "Projects" / new_name
            if target.exists():
                st.error(f"`Projects/{new_name}` already exists")
            else:
                shutil.copytree(template, target)
                # Naive frontmatter prefill on 00 Brief.md — operator can refine
                brief_path = target / "00 Brief.md"
                if brief_path.exists():
                    txt = brief_path.read_text(encoding="utf-8")
                    txt = txt.replace("project:", f"project: {new_name}", 1)
                    txt = txt.replace("status: live | paused | won | lost | archived", "status: live")
                    txt = txt.replace("sensitivity: confidential", f"sensitivity: {new_sensitivity}", 1)
                    txt = txt.replace("side: buy | sell | advisory", f"side: {new_side}")
                    txt = txt.replace("sector: \"[[]]\"", f"sector: \"[[Sectors/{new_sector}]]\"" if new_sector else "sector:")
                    brief_path.write_text(txt, encoding="utf-8")
                st.session_state["show_create_modal"] = False
                list_active_projects.clear()
                st.success(f"Created `Projects/{new_name}` from template.")
                st.rerun()


# ============================================================ tabs

tab_agent, tab_vault, tab_daily, tab_runs, tab_drafts = st.tabs([
    "Agent mode", "Knowledge vault", "Daily notes", "Run history", "Draft materials"
])


# =============================================================================
# AGENT MODE TAB — main view
# =============================================================================

with tab_agent:
    main_col, rail_col = st.columns([2.4, 1.0])

    # ---- LEFT / CENTRE STAGE ----
    with main_col:
        st.markdown("### What do you want Anton to do?")
        proj_label = active_project if active_project else "no project"
        sens_label = default_sensitivity
        st.markdown(
            f"<div style='color:#94A3B8;font-size:13px;margin-top:-4px;margin-bottom:8px;'>"
            f"Context · project: <b style='color:#3B82F6;'>{html.escape(str(proj_label))}</b> "
            f"· sensitivity: <b style='color:#3B82F6;'>{html.escape(str(sens_label))}</b> "
            f"· operator: <b style='color:#E5E7EB;'>{html.escape(str(prof.get('operator', '')))}</b>"
            f"</div>",
            unsafe_allow_html=True,
        )

        prompt_text = st.text_area(
            "Prompt", label_visibility="collapsed",
            placeholder="Ask Anton to research a company, draft materials, prepare a meeting pack, or search the vault…",
            height=110,
        )
        rb1, rb2, _ = st.columns([1, 1, 4])
        with rb1:
            run_now = st.button("▶ Run workflow", type="primary", use_container_width=True,
                                 disabled=not prompt_text.strip())
        with rb2:
            clear = st.button("Clear", use_container_width=True)

        if clear:
            st.rerun()
        if run_now:
            project_arg = "" if not active_project else f"{active_project} "
            full_prompt = (f"Project: {active_project or 'none'}. "
                           f"Sensitivity: {default_sensitivity}. "
                           f"Request: {prompt_text}")
            cmd = ["claude", "-p", full_prompt]
            pid = fire_subprocess(cmd)
            st.success(f"Anton fired (pid {pid}). Output writes to vault.")

        # ---- skill grid ----

        st.markdown("<div class='skill-section'>Research</div>", unsafe_allow_html=True)
        r1, r2, r3 = st.columns(3)
        with r1:
            st.button("Company profile", use_container_width=True)
            st.button("Comps pull", use_container_width=True)
        with r2:
            st.button("Market snapshot", use_container_width=True)
            st.button("Precedents pull", use_container_width=True)
        with r3:
            st.button("Sector read", use_container_width=True)

        st.markdown("<div class='skill-section'>Transaction materials</div>",
                      unsafe_allow_html=True)
        t1, t2, t3, t4 = st.columns(4)
        with t1:
            st.button("Pitch", use_container_width=True)
            st.button("Process letter", use_container_width=True)
        with t2:
            st.button("Teaser", use_container_width=True)
            st.button("Buyer list", use_container_width=True)
        with t3:
            st.button("NDAs", use_container_width=True)
            st.button("Investment proposal", use_container_width=True)
        with t4:
            st.button("CIM draft", use_container_width=True)
            st.button("IC memo", use_container_width=True)

        st.markdown("<div class='skill-section'>Meetings</div>", unsafe_allow_html=True)
        m1, m2, m3 = st.columns(3)
        with m1:
            st.button("Build agenda", use_container_width=True)
        with m2:
            st.button("Pre-read pack", use_container_width=True)
        with m3:
            st.button("Post-call cleanup", use_container_width=True)

        st.markdown("<div class='skill-section'>Valuation (engine pending)</div>",
                      unsafe_allow_html=True)
        v1, v2, v3 = st.columns(3)
        with v1:
            st.button("DCF run", use_container_width=True, disabled=True,
                       help="Engine wiring pending")
            st.button("3-statement", use_container_width=True, disabled=True,
                       help="Engine wiring pending")
        with v2:
            st.button("LBO run", use_container_width=True, disabled=True,
                       help="Engine wiring pending")
            st.button("Football field", use_container_width=True, disabled=True,
                       help="Engine wiring pending")
        with v3:
            st.button("Sensitivity", use_container_width=True, disabled=True,
                       help="Engine wiring pending")
            st.button("Audit model", use_container_width=True, disabled=True,
                       help="Engine wiring pending")

        st.markdown("<div class='skill-section'>Vault & ops</div>", unsafe_allow_html=True)
        o1, o2, o3, o4 = st.columns(4)
        with o1:
            if st.button("Recall query", use_container_width=True):
                st.session_state["_show_recall"] = True
        with o2:
            if st.button("Promote memory", use_container_width=True):
                cmd = [sys.executable, "-m", "routines.promotion.cli", "run-all"]
                pid = fire_subprocess(cmd, cwd=ROUTINES_REPO)
                st.success(f"Memory promote started (pid {pid}). Proposals will land in `Routines/memory-promotion/`.")
        with o3:
            if st.button("Reindex", use_container_width=True):
                cmd = [sys.executable, "-m", "routines.recall.cli", "index"]
                pid = fire_subprocess(cmd, cwd=ROUTINES_REPO)
                st.success(f"Reindex started (pid {pid})")
        with o4:
            if st.button("Newsletter run", use_container_width=True):
                if not active_project:
                    sectors_pick = prof.get("active_sectors", []) or []
                    if sectors_pick:
                        cmd = [sys.executable, "-m", "routines.sectornews.cli", "run-all"]
                        pid = fire_subprocess(cmd, cwd=ROUTINES_REPO)
                        st.success(f"Newsletter run-all started (pid {pid})")
                    else:
                        st.warning("No active sectors in profile.md")

        # ---- inline recall panel ----
        if st.session_state.get("_show_recall"):
            with st.expander("🔎 Recall query", expanded=True):
                rq = st.text_input("Query", placeholder="e.g. 'what do I know about DemoTelco synergies?'")
                rc1, rc2 = st.columns(2)
                with rc1:
                    rsynth = st.checkbox("Synthesise (slower; cited answer)", value=False)
                with rc2:
                    rlimit = st.number_input("Limit", min_value=3, max_value=30, value=10)
                if st.button("Run query", type="primary", disabled=not rq):
                    cmd = [sys.executable, "-m", "routines.recall.cli", "query", rq,
                           "--limit", str(rlimit)]
                    if rsynth:
                        cmd.append("--synthesise")
                    with st.spinner("Querying…"):
                        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                        st.markdown(proc.stdout)

    # ---- RIGHT RAIL ----
    with rail_col:
        # Priority tasks
        st.markdown(
            """
            <div class="rail-card">
              <div class="rail-title">Priority tasks</div>
              <div class="rail-item">
                <span class="badge badge-overdue">Overdue</span>
                Send NDA to Heartwood Collection
                <div class="rail-sub">[ HiNotes 2026-04-12 ]</div>
              </div>
              <div class="rail-item">
                <span class="badge badge-due-today">Due today</span>
                Draft IC memo for Project Falcon
                <div class="rail-sub">[ HiNotes 2026-05-07 ]</div>
              </div>
              <div class="rail-item">
                <span class="badge badge-open">Open</span>
                Review FY25 audit comments
                <div class="rail-sub">[ Email · Stephen ]</div>
              </div>
              <div class="rail-item">
                <span class="badge badge-open">Open</span>
                Update buyer universe for Project Sage
                <div class="rail-sub">[ Manual ]</div>
              </div>
              <div class="rail-sub" style="margin-top:8px;">
                Auto-fed from HiNotes / Outlook (W5+) / manual entry. Routine pending.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Latest intelligence (newsletter)
        nl = latest_newsletter()
        if nl.get("exists"):
            bullets = "".join(
                f'<div class="rail-item">• {html.escape(str(h)[:90])}</div>' for h in nl["headlines"]
            ) or '<div class="rail-sub">No headlines parsed.</div>'
            st.markdown(
                f"""
                <div class="rail-card">
                  <div class="rail-title">Latest intelligence</div>
                  <div class="rail-sub">{html.escape(str(nl["name"]))}</div>
                  {bullets}
                  <div class="rail-sub" style="margin-top:10px;">
                    <a href="obsidian://open?path={html.escape(urllib.parse.quote(str(nl['path']), safe='/:'), quote=True)}">▸ open in vault</a>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                """
                <div class="rail-card">
                  <div class="rail-title">Latest intelligence</div>
                  <div class="rail-sub">No newsletter yet. Run "Newsletter run" to generate one.</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # Vault activity
        pulse = vault_pulse(str(DEFAULT_VAULT), hours=24, limit=6)
        pulse_items = ""
        for p in pulse:
            badge_class = "badge-created" if p["kind"] == "CREATED" else "badge-updated"
            pulse_items += (
                f'<div class="rail-item">'
                f'<span class="badge {badge_class}">{html.escape(str(p["kind"]))}</span>'
                f'<code style="color:#C8CDD2;font-size:11px;">{html.escape(str(p["path"])[:50])}</code>'
                f'<span style="color:#7A8189;float:right;">{html.escape(str(p["ago"]))}</span>'
                f'</div>'
            )
        if not pulse_items:
            pulse_items = '<div class="rail-sub">Nothing touched in the last 24h.</div>'
        st.markdown(
            f"""
            <div class="rail-card">
              <div class="rail-title">Vault activity</div>
              {pulse_items}
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Forecast
        ostat = ollama_status()
        ridx = recall_index_status()
        ollama_line = (
            f"<div class='rail-sub'>Ollama · v{ostat['version']} · {len(ostat['models'])} models</div>"
            if ostat.get("ok")
            else f"<div class='rail-sub' style='color:#EF4444;'>Ollama down</div>"
        )
        index_line = (
            f"<div class='rail-sub'>Recall index · {ridx.get('count', '?')} notes</div>"
            if ridx.get("exists")
            else "<div class='rail-sub' style='color:#F59E0B;'>Recall index not built</div>"
        )
        st.markdown(
            f"""
            <div class="rail-card">
              <div class="rail-title">Forecast</div>
              {ollama_line}
              {index_line}
              <div class="rail-sub" style="margin-top:8px;">
                Vault compact in 44m · Morning brief in 11h 44m
                <span style="color:#3B82F6;">(stub — routines pending)</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# =============================================================================
# KNOWLEDGE VAULT TAB
# =============================================================================

with tab_vault:
    st.markdown("### Knowledge vault")
    st.markdown(
        f"<div style='color:#7A8189;font-size:12px;'>"
        f"Vault root: <code>{DEFAULT_VAULT}</code> · "
        f"Recall index: {recall_index_status().get('count', 'not built')} notes"
        f"</div>",
        unsafe_allow_html=True,
    )
    sub_tabs = st.tabs(["Recall", "Vault pulse"])
    with sub_tabs[0]:
        kq = st.text_input("Query", placeholder="What do you want to recall?")
        synth = st.checkbox("Synthesise", value=False, key="vault_synth")
        if st.button("Run", type="primary", disabled=not kq, key="vault_run"):
            cmd = [sys.executable, "-m", "routines.recall.cli", "query", kq]
            if synth:
                cmd.append("--synthesise")
            with st.spinner("Querying…"):
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                st.markdown(proc.stdout)
    with sub_tabs[1]:
        hours = st.slider("Look-back (hours)", 1, 168, 24, 1)
        full = vault_pulse(str(DEFAULT_VAULT), hours=hours, limit=50)
        if full:
            st.dataframe(full, use_container_width=True, hide_index=True)
        else:
            st.info(f"Nothing in last {hours}h.")


# =============================================================================
# DAILY NOTES TAB
# =============================================================================

with tab_daily:
    st.markdown("### Daily notes")
    today = datetime.now().date()
    daily_path = DEFAULT_VAULT / "Daily" / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.isoformat()}.md"
    if daily_path.exists():
        st.code(daily_path.read_text(encoding="utf-8"), language="markdown")
    else:
        st.info(f"No daily note for {today.isoformat()} yet — open Obsidian to create one.")


# =============================================================================
# RUN HISTORY TAB
# =============================================================================

with tab_runs:
    st.markdown("### Run history")
    routine = st.radio(
        "Routine",
        ["hinotes", "sectornews", "memory-promote", "dealtracker"],
        horizontal=True,
    )
    runs = read_audit_log(routine, limit=30)
    if not runs:
        st.info(f"No runs in `runs/{routine}.jsonl` yet.")
    else:
        for r in runs:
            emoji = {"ok": "✅", "skipped": "⏭", "error": "❌"}.get(r.get("status"), "❓")
            label = f"{emoji} {r.get('ts','?')} · `{r.get('run_id','?')}` · {r.get('duration_ms', 0)}ms"
            with st.expander(label):
                st.json(r)


# =============================================================================
# DRAFT MATERIALS TAB
# =============================================================================

with tab_drafts:
    st.markdown("### Draft materials")
    if active_project:
        outputs_dir = DEFAULT_VAULT / "Projects" / active_project / "12 Outputs"
        if outputs_dir.exists():
            files = sorted(outputs_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                for f in files:
                    st.write(f"📄 `{f.relative_to(DEFAULT_VAULT).as_posix()}`")
            else:
                st.info("No drafts yet for this project.")
        else:
            st.info(f"No `12 Outputs/` folder in `{active_project}` yet.")
    else:
        st.info("Select a project to view its draft materials.")


# =============================================================================
# FOOTER KPI ROW (5h cap · Weekly · Routines)
# =============================================================================

st.markdown("---")
k1, k2, k3 = st.columns(3)
# Stub values — real values would come from `claude /status` or audit log aggregation
WINDOWS = [
    ("5-hour cap · resets · now", "3.1M", "5.0M", 0.62, "7 sessions"),
    ("Weekly cap · resets · Mon", "41.0M", "60.0M", 0.68, "43 sessions"),
    ("Routines · Max · resets midnight", "9", "15", 0.60, "$14.30 today"),
]
for col, (label, used, cap, frac, sub) in zip([k1, k2, k3], WINDOWS):
    with col:
        st.markdown(
            f"""
            <div class="kpi">
              <div class="kpi-label">{label}</div>
              <div class="kpi-metric"><span class="accent">{used}</span> / {cap}</div>
              <div class="kpi-bar"><div class="kpi-bar-fill" style="width:{int(frac*100)}%;"></div></div>
              <div class="kpi-sub">{sub}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
