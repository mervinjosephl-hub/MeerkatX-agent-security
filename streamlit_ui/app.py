"""MeerkatX — Streamlit UI for running scans and viewing results.

Run with:
    uv run --with streamlit --with certifi streamlit run streamlit_ui/app.py

Requires the Strix repo's own environment to already be set up (uv sync)
and a .env file at the repo root with STRIX_LLM / LLM_API_KEY / LLM_API_BASE.

Login state lives in Streamlit's session_state, which is per-browser-tab and
reset on a full page reload — this is a lightweight session-based auth
system, not a hardened production one.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import certifi
import streamlit as st


_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")

# Brand palette — near-black ground with a blue/green accent gradient.
# Status colors are reserved and never reused as a decorative series hue.
INK = "#0d0d0d"  # primary dark background
INK_RAISED = "#161616"  # card surface on ink
MIST = "#ffffff"  # text-on-dark, headlines
STEEL = "#c3c2b7"  # secondary text
FAINT = "#898781"  # captions / muted labels
BLUE = "#2a78d6"  # primary accent
BLUE_BRIGHT = "#3987e5"  # accent on dark, higher-contrast variant
GREEN = "#1baf7a"  # secondary accent — pairs with blue in gradients
GREEN_DEEP = "#16966b"  # darker green, gradient endpoints / CTA fills
RED = "#d03b3b"  # alert / critical
AMBER = "#fab219"  # in progress

_STATUS_COLORS = {
    "completed": "#0ca30c",
    "running": AMBER,
    "stopped": RED,
    "failed": RED,
    "unknown": FAINT,
}


def dedupe_repeated_headings(markdown: str) -> str:
    """Collapse a heading immediately followed by an identical heading.

    Strix's report writer occasionally emits the same section title twice
    in a row (e.g. two consecutive '# Executive Summary' lines) — drop the
    first, keep the second (which is the one directly followed by the
    section body).
    """
    lines = markdown.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _HEADING_RE.match(lines[i])
        if match:
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                next_match = _HEADING_RE.match(lines[j])
                if (
                    next_match
                    and next_match.group(1) == match.group(1)
                    and next_match.group(2).strip() == match.group(2).strip()
                ):
                    i = j
                    continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# --------------------------------------------------------------------------
# Global brand system — fonts, tokens, and the shared "watch line" signature.
# Big Shoulders Condensed (signage/alarm character) for display type, IBM
# Plex Sans for body text, IBM Plex Mono for anything that's a reading —
# costs, counts, run names. Injected once, ahead of any page-specific CSS.
# --------------------------------------------------------------------------

_GLOBAL_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Big+Shoulders+Condensed:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

:root {{
  --ink: {INK};
  --ink-raised: {INK_RAISED};
  --mist: {MIST};
  --steel: {STEEL};
  --faint: {FAINT};
  --blue: {BLUE};
  --blue-bright: {BLUE_BRIGHT};
  --green: {GREEN};
  --green-deep: {GREEN_DEEP};
  --red: {RED};
  --amber: {AMBER};
  --hairline: rgba(255,255,255,0.10);
}}

html, body, .stApp {{ background: var(--ink) !important; }}
[data-testid="stHeader"] {{ background: transparent !important; }}
section[data-testid="stSidebar"] {{ background: var(--ink-raised) !important; border-right: 1px solid var(--hairline); }}
hr {{ border-color: var(--hairline) !important; }}

/* Body font on real text elements only — deliberately scoped so it never
   touches Streamlit's material-icon ligature spans (buttons, inputs,
   expanders), which would render as literal words instead of glyphs. */
.stApp, .stApp p, .stApp li, .stApp label,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp button, .stApp input, .stApp textarea, .stApp a,
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] *:not([class*="mx-"]),
[data-testid="stMetricLabel"], [data-testid="stMetricDelta"], [data-testid="stWidgetLabel"] {{
  font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
}}
[data-testid="stMetricValue"], .stApp code, .stApp pre {{
  font-family: 'IBM Plex Mono', ui-monospace, monospace !important;
}}

.stButton button, [data-testid="stFormSubmitButton"] button {{ border-radius: 8px !important; }}
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input {{ border-radius: 6px !important; }}
[data-testid="stDataFrame"] {{ border: 1px solid var(--hairline) !important; border-radius: 8px; overflow: hidden; }}

/* Shared brand primitives, reused across landing / auth / dashboard. */
.mx-eyebrow {{
  font-family: 'IBM Plex Mono', monospace !important; font-size: 11px; font-weight: 600;
  letter-spacing: 0.16em; text-transform: uppercase; color: var(--blue-bright);
}}
.mx-display {{
  font-family: 'Big Shoulders Condensed', sans-serif !important; font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.01em; color: var(--mist); line-height: 0.98;
}}
.mx-tag {{
  display: inline-block; font-family: 'IBM Plex Mono', monospace !important; font-size: 11px;
  font-weight: 600; letter-spacing: 0.04em; padding: 3px 8px; border-radius: 4px;
  border: 1px solid var(--hairline); color: var(--steel);
}}

/* The watch line: a horizon rule where a few sentries periscope up in
   staggered rotation while the rest hold low — meerkats take turns on
   guard duty rather than all watching at once. Idle marks sit in green,
   the sentry currently up glows blue — the brand's two accents in one motif. */
.mx-watch-line {{
  display: flex; align-items: flex-end; justify-content: center; gap: 9px;
  height: 30px; margin: 0 auto; padding-top: 10px; width: min(560px, 92%);
  border-top: 1px solid var(--hairline);
}}
.mx-watch-mark {{
  flex: 0 0 3px; width: 3px; height: 8px; border-radius: 2px 2px 0 0;
  background: var(--green); opacity: 0.45;
}}
.mx-watch-mark.is-up {{
  background: var(--blue-bright); opacity: 0.95;
  animation: mx-periscope 5.2s ease-in-out infinite;
  box-shadow: 0 0 9px rgba(57,135,229,0.55);
}}
@keyframes mx-periscope {{
  0%, 32% {{ height: 8px; opacity: 0.5; }}
  50%, 70% {{ height: 24px; opacity: 1; }}
  88%, 100% {{ height: 8px; opacity: 0.5; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .mx-watch-mark.is-up {{ animation: none; height: 18px; opacity: 0.85; }}
}}
</style>
"""


def render_global_css() -> None:
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


def render_watch_line(count: int = 27, up_every: int = 6) -> str:
    """A row of sentinel marks on a horizon rule; every `up_every`-th one
    periscopes up on a staggered delay, evoking a rotating guard."""
    marks: list[str] = []
    for i in range(count):
        if i % up_every == 3:
            delay = f"{(i * 0.61) % 5.2:.2f}s"
            marks.append(f'<span class="mx-watch-mark is-up" style="animation-delay:{delay};"></span>')
        else:
            marks.append('<span class="mx-watch-mark"></span>')
    return f'<div class="mx-watch-line">{"".join(marks)}</div>'


REPO_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_DIR / "strix_runs"
ENV_FILE = REPO_DIR / ".env"
LOG_DIR = REPO_DIR / "streamlit_ui" / "logs"
DB_PATH = REPO_DIR / "streamlit_ui" / "users.db"
LOGO_PATH = REPO_DIR / "streamlit_ui" / "assets" / "logo.png"
ICON_PATH = REPO_DIR / "streamlit_ui" / "assets" / "logo_icon.png"


@st.cache_data
def _logo_data_uri() -> str | None:
    """Icon-only mark on a transparent ground — the sentinel meerkats
    without the baked-in wordmark, for use at nav/sidebar/favicon scale
    where the wordmark would be illegible and the source PNG's opaque
    white background would show as a box against the dark UI."""
    path = ICON_PATH if ICON_PATH.exists() else LOGO_PATH
    if not path.exists():
        return None
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/png;base64,{encoded}"


st.set_page_config(
    page_title="MeerkatX",
    page_icon=str(ICON_PATH) if ICON_PATH.exists() else (str(LOGO_PATH) if LOGO_PATH.exists() else "🛡️"),
    layout="wide",
)


# --------------------------------------------------------------------------
# Auth (SQLite-backed: unique username + auto-increment id, PBKDF2 password
# hashing with a per-user random salt — no plaintext passwords stored)
# --------------------------------------------------------------------------


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                run_name TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT,
                cost REAL,
                vulnerability_count INTEGER,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        # Forward-compatible with DBs created before these columns existed.
        for column, coltype in (
            ("status", "TEXT"),
            ("cost", "REAL"),
            ("vulnerability_count", "INTEGER"),
            ("updated_at", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE scan_runs ADD COLUMN {column} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def record_scan_ownership(run_name: str, user_id: int) -> None:
    """Associate a run directory with the user who launched it. Idempotent."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO scan_runs (run_name, user_id, created_at) VALUES (?, ?, ?)",
            (run_name, user_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def sync_run_status(run_dir: Path, run_json: dict) -> None:
    """Write this run's current status/cost/vuln count into the DB.

    Called every time run.json is read so the database stays a live mirror
    of on-disk outcomes, not just a record of who launched what. run.json
    has no vulnerability list of its own — the real count is the number of
    per-finding files Strix writes under <run_dir>/vulnerabilities/, same
    source render_results() uses to display them.
    """
    usage = run_json.get("llm_usage", {})
    vuln_dir = run_dir / "vulnerabilities"
    # Strix only creates this directory once it has ≥1 finding to write, so
    # "directory doesn't exist" reliably means 0 findings so far — not unknown.
    vuln_count = len(list(vuln_dir.glob("*.md"))) if vuln_dir.exists() else 0
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            UPDATE scan_runs
            SET status = ?, cost = ?, vulnerability_count = ?, updated_at = ?
            WHERE run_name = ?
            """,
            (
                run_json.get("status"),
                usage.get("cost"),
                vuln_count,
                datetime.now(UTC).isoformat(),
                run_dir.name,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_runs_metadata(user_id: int) -> dict[str, dict]:
    """Return {run_name: {status, cost, vulnerability_count}} for this user's runs."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT run_name, status, cost, vulnerability_count FROM scan_runs WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        run_name: {"status": status, "cost": cost, "vulnerability_count": vuln_count}
        for run_name, status, cost, vuln_count in rows
    }


def get_user_run_names(user_id: int) -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT run_name FROM scan_runs WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def get_user_runs_list(user_id: int) -> list[dict]:
    """All of a user's runs as ordered rows, for building the overview charts."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT run_name, created_at, status, cost, vulnerability_count
            FROM scan_runs WHERE user_id = ? ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "run_name": r[0],
            "created_at": r[1],
            "status": r[2] or "unknown",
            "cost": r[3] or 0.0,
            "vulnerability_count": r[4] or 0,
        }
        for r in rows
    ]


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def create_user(username: str, password: str) -> tuple[bool, str]:
    username = username.strip()
    if not username or not password:
        return False, "Username and password are required."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, pw_hash, salt, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        return True, "Account created — you can now log in."
    except sqlite3.IntegrityError:
        return False, "That username is already taken."
    finally:
        conn.close()


def authenticate(username: str, password: str) -> dict[str, object] | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, salt FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    user_id, uname, pw_hash, salt = row
    if secrets.compare_digest(_hash_password(password, salt), pw_hash):
        return {"id": user_id, "username": uname}
    return None


# --------------------------------------------------------------------------
# Landing page — top nav + hero + capability sections, shown before login.
# Deliberately describes what each engine proves, not which engine does
# it — the brand is MeerkatX, the underlying tools stay unnamed.
# --------------------------------------------------------------------------

_LANDING_CSS = """
<style>
.mx-hero {
  position: relative; overflow: hidden; border-radius: 18px;
  background:
    radial-gradient(ellipse 55% 50% at 16% 0%, rgba(42,120,214,0.28), transparent 58%),
    radial-gradient(ellipse 50% 45% at 88% 18%, rgba(27,175,122,0.16), transparent 55%),
    linear-gradient(180deg, #101010 0%, #161616 100%);
  border: 1px solid var(--hairline);
  padding: 64px 28px 40px 28px; text-align: center; margin-bottom: 30px;
}
.mx-headline { font-size: clamp(32px, 5.6vw, 58px); margin: 16px 0 18px 0; }
.mx-headline .mx-accent {
  background: linear-gradient(90deg, var(--blue-bright), var(--green));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.mx-subhead {
  font-family: 'IBM Plex Sans', sans-serif !important; font-size: 16px; color: var(--steel);
  max-width: 620px; margin: 0 auto 30px auto; line-height: 1.6;
}
.mx-log-row { display: flex; align-items: baseline; gap: 14px; padding: 14px 4px; border-bottom: 1px solid var(--hairline); flex-wrap: wrap; }
.mx-log-row:last-child { border-bottom: none; }
.mx-log-label { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 14px; font-weight: 600; color: var(--mist); }
.mx-log-body { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 13px; color: var(--steel); }
.mx-call-card { background: var(--ink-raised); border: 1px solid var(--hairline); border-radius: 14px; padding: 26px; height: 100%; }
.mx-call-cap { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 12px; font-weight: 600; color: var(--steel); margin: 8px 0 14px 0; text-transform: uppercase; letter-spacing: 0.04em; }
.mx-call-q { font-family: 'Big Shoulders Condensed', sans-serif !important; font-weight: 700; font-size: 22px; color: var(--mist); margin-bottom: 14px; }
.mx-call-li { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 13px; color: var(--steel); margin: 7px 0; padding-left: 16px; position: relative; }
.mx-call-li::before { content: "\\2014"; color: var(--blue); position: absolute; left: 0; }
.mx-trust-block { text-align: center; padding: 6px 12px; }
.mx-trust-label { font-family: 'Big Shoulders Condensed', sans-serif !important; font-weight: 700; font-size: 20px; color: var(--blue-bright); border-top: 2px solid var(--blue); padding-top: 10px; display: inline-block; text-transform: uppercase; }
.mx-trust-body { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 13px; color: var(--steel); margin-top: 8px; }
.mx-section-title { font-size: clamp(22px, 3.6vw, 30px); text-align: center; margin: 6px 0 4px 0; }
.mx-section-sub { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 14px; color: var(--steel); text-align: center; margin-bottom: 30px; }
.mx-cta { text-align: center; padding: 40px 20px 6px 20px; border-top: 1px solid var(--hairline); margin-top: 10px; }
.mx-cta-line { font-size: clamp(20px, 3.2vw, 27px); color: var(--mist); margin-bottom: 6px; }
.mx-cta-sub { font-family: 'IBM Plex Sans', sans-serif !important; font-size: 13px; color: var(--steel); margin-bottom: 20px; }
</style>
"""


def render_top_nav() -> None:
    st.markdown(_LANDING_CSS, unsafe_allow_html=True)
    st.markdown(
        """
        <style>
        .st-key-nav_signup_btn button {
          background: linear-gradient(135deg, var(--blue-bright), var(--green)) !important;
          color: #062e22 !important; border: none !important;
          font-weight: 700 !important; border-radius: 6px !important; letter-spacing: 0.04em;
          text-transform: uppercase; font-size: 13px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    col_brand, col_spacer, col_signup = st.columns([4, 3, 1])
    with col_brand:
        logo_uri = _logo_data_uri()
        icon_html = (
            f'<img src="{logo_uri}" style="height:34px; width:34px; object-fit:contain; '
            'border-radius:6px;" />'
            if logo_uri
            else "🛡️"
        )
        st.markdown(
            _flatten_html(
                f'<div style="display:flex; align-items:center; gap:10px; padding-top:2px;">'
                f"{icon_html}"
                '<span class="mx-display" style="font-size:24px;">Meerkat'
                '<span style="color:var(--blue-bright);">X</span></span></div>'
            ),
            unsafe_allow_html=True,
        )
    with col_signup:
        if st.button("Sign up", key="nav_signup_btn", use_container_width=True):
            st.session_state.show_login = True
            st.rerun()


def render_landing_page() -> None:
    render_top_nav()

    st.markdown(
        _flatten_html(
            f"""
        <div class="mx-hero">
          <div class="mx-eyebrow">On watch</div>
          <div class="mx-display mx-headline">Ship AI apps that<br>
            <span class="mx-accent">survive contact</span></div>
          <div class="mx-subhead">AI-built apps can look finished and still ship with holes a
            read-through misses. MeerkatX runs three independent engines against yours, then
            merges what they find into one call: ship, or don't.</div>
          {render_watch_line()}
        </div>
        """
        ),
        unsafe_allow_html=True,
    )
    _, cta_col, _ = st.columns([2, 1, 2])
    with cta_col:
        if st.button("Get started", use_container_width=True, key="hero_cta"):
            st.session_state.show_login = True
            st.rerun()

    st.markdown("<div style='height:44px'></div>", unsafe_allow_html=True)
    st.markdown(
        '<div class="mx-eyebrow" style="text-align:center;">What gets missed</div>'
        '<div class="mx-display mx-section-title">Finished-looking still means unfinished</div>'
        "<div class=\"mx-section-sub\">The gap AI speed creates — problems a working demo won't show you.</div>",
        unsafe_allow_html=True,
    )
    problems = [
        ("AUTH", "Missing authentication", "Functions lack the right checks."),
        ("SECRETS", "Hardcoded secrets", "Keys stay inside the code."),
        ("INJECT", "Injection flaws", "Input alters code or AI behavior."),
        ("EXPOSURE", "Data exposure", "Restricted data reaches the wrong user."),
    ]
    rows_html = "".join(
        f"""
        <div class="mx-log-row">
          <span class="mx-tag" style="color:var(--red); border-color:rgba(208,59,59,0.35);">{_esc(tag)}</span>
          <span class="mx-log-label">{_esc(title)}</span>
          <span class="mx-log-body">{_esc(body)}</span>
        </div>
        """
        for tag, title, body in problems
    )
    st.markdown(
        _flatten_html(f'<div style="max-width:760px; margin:0 auto;">{rows_html}</div>'),
        unsafe_allow_html=True,
    )

    st.markdown("<div style='height:46px'></div>", unsafe_allow_html=True)
    st.markdown(
        '<div class="mx-eyebrow" style="text-align:center;">Three calls, one verdict</div>'
        '<div class="mx-display mx-section-title">Every threat gets its own alarm</div>'
        '<div class="mx-section-sub">Each engine proves something different — together '
        "they cover what a single scan misses.</div>",
        unsafe_allow_html=True,
    )
    capabilities = [
        (
            "Ground call",
            "Application Security",
            "Can attackers break the app?",
            ["Authentication", "Secrets", "SQL / command injection", "Misconfiguration"],
        ),
        (
            "Aerial call",
            "AI Manipulation Testing",
            "Can users manipulate the AI?",
            [
                "Indirect prompt injection",
                "Unsafe tool invocation",
                "Data theft actions",
                "Physical / financial harm",
            ],
        ),
        (
            "All-clear check",
            "Behavioral Validation",
            "Can the AI stay useful and secure?",
            [
                "Stateful tool use",
                "Legitimate task completion",
                "Attacker-goal detection",
                "Deterministic scoring",
            ],
        ),
    ]
    cols = st.columns(3)
    for col, (call, capability, question, bullets) in zip(cols, capabilities, strict=False):
        with col:
            bullet_html = "".join(f'<div class="mx-call-li">{_esc(b)}</div>' for b in bullets)
            st.markdown(
                _flatten_html(
                    f"""
                <div class="mx-call-card">
                  <div class="mx-tag">{_esc(call)}</div>
                  <div class="mx-call-cap">{_esc(capability)}</div>
                  <div class="mx-call-q">{_esc(question)}</div>
                  {bullet_html}
                </div>
                """
                ),
                unsafe_allow_html=True,
            )

    st.markdown("<div style='height:44px'></div>", unsafe_allow_html=True)
    st.markdown(
        '<div class="mx-display mx-section-title">Why teams trust the result</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    usp_cols = st.columns(3)
    usps = [
        ("Breadth", "Comprehensive, autonomous discovery across the app."),
        ("Repeatability", "Dozens of concrete attack categories, scored pass or fail."),
        ("Objectivity", "Findings come from environment state, not opinion."),
    ]
    for col, (title, body) in zip(usp_cols, usps, strict=False):
        with col:
            st.markdown(
                _flatten_html(
                    f"""
                <div class="mx-trust-block">
                  <span class="mx-trust-label">{_esc(title)}</span>
                  <div class="mx-trust-body">{_esc(body)}</div>
                </div>
                """
                ),
                unsafe_allow_html=True,
            )

    st.markdown(
        _flatten_html(
            """
        <div class="mx-cta">
          <div class="mx-display mx-cta-line">One target. Three independent checks. One clear decision.</div>
          <div class="mx-cta-sub">Sign in to run your first assessment.</div>
        </div>
        """
        ),
        unsafe_allow_html=True,
    )
    _, cta_col2, _ = st.columns([2, 1, 2])
    with cta_col2:
        if st.button("Log in or sign up", use_container_width=True, key="footer_cta"):
            st.session_state.show_login = True
            st.rerun()


_AUTH_CSS = """
<style>
.stApp {
  background:
    radial-gradient(ellipse 50% 40% at 18% 8%, rgba(42,120,214,0.18), transparent 60%),
    radial-gradient(ellipse 45% 40% at 85% 92%, rgba(27,175,122,0.14), transparent 55%),
    linear-gradient(165deg, var(--ink) 0%, var(--ink-raised) 60%, var(--ink) 100%) !important;
  background-attachment: fixed !important;
}
div[data-testid="stLayoutWrapper"]:has(> div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] div.mx-auth-marker) {
  max-width: 420px; margin: 22px auto 0 auto; border-radius: 16px !important;
  border: 1px solid var(--hairline) !important;
  background: var(--ink-raised) !important;
  box-shadow: 0 30px 70px rgba(0,0,0,0.45);
  padding: 0 0 20px 0 !important;
  overflow: hidden;
}
.mx-auth-header { padding: 28px 24px 16px 24px; text-align: center; }
.mx-auth-header-title { font-size: 26px; }
.mx-auth-logo { height: 46px; width: 46px; object-fit: contain; margin-bottom: 10px; border-radius: 8px; }
.mx-auth-sub { text-align: center; font-size: 13px; font-family: 'IBM Plex Sans', sans-serif !important; color: var(--steel); margin: 4px 0 4px 0; }
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker)
  > div[data-testid="stElementContainer"]:nth-child(n+2) {
  padding: 0 24px;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) [data-testid="stForm"] {
  border: none !important; padding: 0 !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) [data-testid="stTextInput"] label p {
  color: var(--steel) !important; font-size: 13px;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-baseweb="input"] {
  background: var(--ink) !important; border-radius: 6px !important;
  border: 1px solid var(--hairline) !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-baseweb="input"]:focus-within {
  border-color: var(--blue) !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) input {
  background: transparent !important; color: var(--mist) !important; box-shadow: none !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-testid="stFormSubmitButton"] button {
  width: 100%; border-radius: 8px; margin-top: 8px;
  background: linear-gradient(135deg, var(--blue-bright), var(--green)) !important;
  border: none !important; color: #062e22 !important; font-weight: 700 !important;
  text-transform: uppercase; letter-spacing: 0.06em; padding: 11px 0 !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-testid="stFormSubmitButton"] button p {
  color: #062e22 !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) [role="tablist"] {
  display: flex !important; width: 100% !important; justify-content: center !important; gap: 40px;
  background: transparent !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-testid="stTab"] {
  background: transparent !important; border: none !important; box-shadow: none !important;
  padding: 0 0 8px 0 !important; margin: 0 !important; min-width: 0 !important; cursor: pointer;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-testid="stTab"] p {
  font-size: 14px !important; font-weight: 600 !important; color: var(--steel) !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-testid="stTab"]:hover p {
  color: var(--blue-bright) !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) div[data-testid="stTab"][aria-selected="true"] p {
  color: var(--blue-bright) !important;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] div.mx-auth-marker) .react-aria-SelectionIndicator {
  background-color: var(--blue) !important; height: 2px !important;
}
</style>
"""


def render_auth_gate() -> None:
    st.markdown(_AUTH_CSS, unsafe_allow_html=True)
    if st.button("← Back", key="auth_back"):
        st.session_state.show_login = False
        st.rerun()

    _, col_center, _ = st.columns([1, 1.3, 1])
    with col_center, st.container(border=True):
        logo_uri = _logo_data_uri()
        logo_html = f'<img class="mx-auth-logo" src="{logo_uri}" />' if logo_uri else ""
        st.markdown(
            '<div class="mx-auth-marker"></div>'
            '<div class="mx-auth-header">'
            f"{logo_html}"
            '<div class="mx-display mx-auth-header-title">MeerkatX</div>'
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(render_watch_line(count=17, up_every=5), unsafe_allow_html=True)
        st.markdown('<div class="mx-auth-sub">Sign in to view your dashboard</div>', unsafe_allow_html=True)

        tab_login, tab_signup = st.tabs(["Log in", "Sign up"])

        with tab_login, st.form("login_form", clear_on_submit=True):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", use_container_width=True)
            if submitted:
                user = authenticate(username, password)
                if user is None:
                    st.error("Wrong username or password. Try again.")
                else:
                    st.session_state.user = user
                    st.rerun()

        with tab_signup, st.form("signup_form", clear_on_submit=True):
            new_username = st.text_input("Choose a username")
            new_password = st.text_input("Choose a password", type="password")
            confirm_password = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create account", use_container_width=True)
            if submitted:
                if new_password != confirm_password:
                    st.error("Those passwords don't match.")
                else:
                    ok, message = create_user(new_username, new_password)
                    (st.success if ok else st.error)(message)


# --------------------------------------------------------------------------
# Chatbot — answers questions about a scan's report, grounded in that run's
# actual report + vulnerability content, using the same LLM configured for
# Strix itself (STRIX_LLM / LLM_API_KEY / LLM_API_BASE from .env).
# --------------------------------------------------------------------------


def call_llm(messages: list[dict[str, str]], env: dict[str, str]) -> str:
    base = env.get("LLM_API_BASE", "").rstrip("/") or "https://api.openai.com/v1"
    api_key = env.get("LLM_API_KEY", "")
    model = env.get("STRIX_LLM", "gpt-4o-mini")
    if "/" in model:
        model = model.split("/", 1)[1]

    payload = json.dumps({"model": model, "messages": messages}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl_context) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"LLM request failed ({e.code}): {detail}") from e
    return str(data["choices"][0]["message"]["content"])


def _build_report_context(run_dir: Path) -> str:
    parts: list[str] = []
    report_path = run_dir / "penetration_test_report.md"
    if report_path.exists():
        parts.append("FULL REPORT:\n" + report_path.read_text(errors="replace"))
    vuln_dir = run_dir / "vulnerabilities"
    if vuln_dir.exists():
        for vf in sorted(vuln_dir.glob("*.md")):
            parts.append(f"FINDING ({vf.name}):\n" + vf.read_text(errors="replace"))
    return "\n\n---\n\n".join(parts) or "No report content is available for this run."


def render_chat(run_dir: Path, env: dict[str, str]) -> None:
    st.divider()
    st.subheader("Ask about this scan")

    history_key = f"chat_history_{run_dir.name}"
    pending_key = f"chat_pending_{run_dir.name}"
    st.session_state.setdefault(history_key, [])

    for msg in st.session_state[history_key]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    prefill = st.session_state.pop(pending_key, None)
    user_input = st.chat_input("Ask a question about these results...") or prefill

    if user_input:
        st.session_state[history_key].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        system_prompt = (
            "You are a security assistant helping a user understand a penetration test "
            "report. Answer ONLY using the report content provided below — if the answer "
            "isn't in it, say so. Be concise and clear.\n\n"
            "After your answer, on a new line write exactly '---FOLLOWUPS---' then list "
            "2-3 short suggested follow-up questions the user might ask next, one per "
            "line, no numbering or dashes.\n\n"
            f"REPORT CONTENT:\n{_build_report_context(run_dir)}"
        )
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state[history_key][-6:]
        )

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    raw = call_llm(messages, env)
                except Exception as e:  # noqa: BLE001
                    raw = f"Sorry, I couldn't reach the LLM: {e}"
            answer, _, followups_raw = raw.partition("---FOLLOWUPS---")
            answer = answer.strip()
            st.markdown(answer)
            st.session_state[history_key].append({"role": "assistant", "content": answer})

        followups = [
            line.strip("-* ").strip() for line in followups_raw.strip().splitlines() if line.strip()
        ]
        if followups:
            st.session_state[f"followups_{run_dir.name}"] = followups[:3]
            st.rerun()

    followups = st.session_state.get(f"followups_{run_dir.name}", [])
    if followups:
        st.caption("Suggested follow-ups:")
        cols = st.columns(len(followups))
        for col, question in zip(cols, followups, strict=False):
            if col.button(question, key=f"followup_{run_dir.name}_{hash(question)}"):
                st.session_state[pending_key] = question
                st.session_state[f"followups_{run_dir.name}"] = []
                st.rerun()


# --------------------------------------------------------------------------
# Scan runner + results dashboard
# --------------------------------------------------------------------------


def load_env_file() -> dict[str, str]:
    env = os.environ.copy()
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def find_run_dir(started_after: float) -> Path | None:
    if not RUNS_DIR.exists():
        return None
    candidates = [d for d in RUNS_DIR.iterdir() if d.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    newest = candidates[0]
    return newest if newest.stat().st_mtime >= started_after - 5 else None


def read_run_json(run_dir: Path) -> dict:
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return {}
    try:
        data = json.loads(run_json.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    sync_run_status(run_dir, data)
    return data


def render_results(run_dir: Path) -> None:
    data = read_run_json(run_dir)
    usage = data.get("llm_usage", {})

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Status", data.get("status", "unknown"))
    col2.metric("Cost", f"${usage.get('cost', 0):.4f}")
    col3.metric("LLM Requests", usage.get("requests", 0))
    vulns = data.get("vulnerabilities") or []
    col4.metric("Vulnerabilities", len(vulns) if isinstance(vulns, list) else "?")

    vuln_dir = run_dir / "vulnerabilities"
    vuln_files = sorted(vuln_dir.glob("*.md")) if vuln_dir.exists() else []

    st.divider()

    if vuln_files:
        st.subheader(f"{len(vuln_files)} vulnerability report(s)")
        for vf in vuln_files:
            content = dedupe_repeated_headings(vf.read_text(errors="replace"))
            first_line = content.splitlines()[0] if content else vf.stem
            title = first_line.lstrip("#").strip() or vf.stem
            with st.expander(title, expanded=True):
                st.markdown(content)
    else:
        st.success("No vulnerabilities reported")

    report_path = run_dir / "penetration_test_report.md"
    if report_path.exists():
        st.divider()
        st.subheader("Full penetration test report")
        st.markdown(dedupe_repeated_headings(report_path.read_text(errors="replace")))

    sarif_path = run_dir / "findings.sarif"
    if sarif_path.exists():
        st.download_button(
            "Download SARIF",
            data=sarif_path.read_bytes(),
            file_name="findings.sarif",
            mime="application/json",
            key=f"sarif_{run_dir.name}",
        )

    st.info(f"Ask questions about this run in the **Chat** tab — it's preselected for `{run_dir.name}`.")
    st.session_state.chat_run_choice = run_dir.name


def _esc(value: object) -> str:
    return html.escape(str(value))


def _flatten_html(raw: str) -> str:
    """Collapse a multi-line HTML template to a single line with no leading
    whitespace on any line. Streamlit's markdown renderer treats 4+ leading
    spaces as a literal code block, so pretty-printed multi-line HTML gets
    displayed as raw text instead of rendered — flattening avoids that."""
    return " ".join(line.strip() for line in raw.strip().splitlines())


def render_stat_cards(cards: list[tuple[str, str, str]]) -> None:
    """Stat-card row: (LABEL, big value, small caption) per card. The value
    reads as an instrument-panel number, so it takes the mono utility face."""
    cells = "".join(
        f"""
        <div style="flex:1; min-width:160px; background:var(--ink-raised); border:1px solid
             var(--hairline); border-radius:10px; padding:16px 18px;">
          <div class="mx-eyebrow">{_esc(label)}</div>
          <div style="font-family:'IBM Plex Mono',monospace; font-size:28px; font-weight:600;
               color:var(--mist); margin-top:8px; line-height:1.1;">{_esc(value)}</div>
          <div style="font-family:'IBM Plex Sans',sans-serif; font-size:12px; color:var(--steel);
               margin-top:4px;">{_esc(caption)}</div>
        </div>
        """
        for label, value, caption in cards
    )
    st.markdown(
        _flatten_html(
            f'<div style="display:flex; gap:14px; flex-wrap:wrap; margin-bottom:18px;">{cells}</div>'
        ),
        unsafe_allow_html=True,
    )


def render_gradient_bars(
    title: str,
    rows: list[tuple[str, float, str]],
    *,
    color_for: dict[str, str] | None = None,
    value_fmt: str = "{:.0f}",
) -> None:
    """Horizontal gradient bar list — (label, value, tooltip-status) per row.

    Matches the 'Value on Hand by Manufacturer' reference style: label left,
    value right, a single gradient-filled bar scaled to the row's share of
    the max value in the list.
    """
    if not rows:
        return
    max_value = max((v for _, v, _ in rows), default=0) or 1
    bar_rows = "".join(
        f"""
        <div style="margin-bottom:10px;">
          <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
            <span style="color:var(--mist); font-size:13px; font-family:'IBM Plex Sans',sans-serif;">{_esc(label)}</span>
            <span style="color:var(--blue-bright); font-weight:600; font-size:13px; font-family:'IBM Plex Mono',monospace;">
                {_esc(value_fmt.format(value))}</span>
          </div>
          <div style="background:var(--ink); border-radius:4px; height:10px; overflow:hidden;">
            <div style="background:{(color_for or {}).get(status, 'linear-gradient(90deg,var(--green-deep),var(--blue))')};
                 width:{max(value, 0) / max_value * 100:.1f}%; height:100%; border-radius:4px;"></div>
          </div>
        </div>
        """
        for label, value, status in rows
    )
    st.markdown(
        _flatten_html(
            f"""
        <div style="background:var(--ink-raised); border:1px solid var(--hairline);
             border-radius:10px; padding:16px 18px;">
          <div class="mx-eyebrow" style="margin-bottom:14px;">{_esc(title)}</div>
          {bar_rows}
        </div>
        """
        ),
        unsafe_allow_html=True,
    )


def render_overview(user_id: int) -> None:
    """Stat cards + gradient-bar visuals across all of this user's scans."""
    resync_user_runs(user_id)
    runs = get_user_runs_list(user_id)
    if not runs:
        st.info("No scans yet — run your first one in the **New Scan** tab.")
        return

    total_scans = len(runs)
    total_vulns = sum(r["vulnerability_count"] for r in runs)
    total_cost = sum(r["cost"] for r in runs)
    completed = sum(1 for r in runs if r["status"] == "completed")

    render_stat_cards(
        [
            ("TOTAL SCANS RUN", str(total_scans), f"{completed} completed"),
            ("TOTAL VULNERABILITIES", str(total_vulns), "found across all scans"),
            ("TOTAL SPEND", f"${total_cost:.4f}", "across all scans"),
            (
                "AVG VULNS / SCAN",
                f"{(total_vulns / total_scans):.1f}",
                "per completed scan",
            ),
        ]
    )

    bar_col, status_col = st.columns([3, 2])

    with bar_col:
        vuln_rows = sorted(
            (
                (r["run_name"][:32], float(r["vulnerability_count"]), r["status"])
                for r in runs
            ),
            key=lambda row: row[1],
            reverse=True,
        )
        render_gradient_bars(
            "Vulnerabilities by scan",
            vuln_rows,
            value_fmt="{:.0f}",
        )

    with status_col:
        status_totals: dict[str, int] = {}
        for r in runs:
            status_totals[r["status"]] = status_totals.get(r["status"], 0) + 1
        status_rows = sorted(status_totals.items(), key=lambda kv: kv[1], reverse=True)
        gradient_for_status = {
            name: f"linear-gradient(90deg, {hexcolor}99, {hexcolor})"
            for name, hexcolor in _STATUS_COLORS.items()
        }
        render_gradient_bars(
            "Scans by status",
            [(name, float(count), name) for name, count in status_rows],
            color_for=gradient_for_status,
            value_fmt="{:.0f}",
        )

    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)


def render_new_scan_tab() -> None:
    for key, default in (
        ("process", None),
        ("run_dir", None),
        ("start_time", None),
        ("log_path", None),
    ):
        st.session_state.setdefault(key, default)

    scanning = st.session_state.process is not None

    with st.form("scan_form"):
        target_url = st.text_input(
            "Application URL",
            placeholder="https://your-app.example.com",
            disabled=scanning,
        )
        col1, col2 = st.columns(2)
        with col1:
            scan_mode = st.selectbox(
                "Scan mode", ["quick", "standard", "deep"], index=0, disabled=scanning
            )
        with col2:
            max_budget = st.number_input(
                "Max budget (USD)",
                min_value=0.5,
                max_value=50.0,
                value=2.0,
                step=0.5,
                disabled=scanning,
            )
        authorized = st.checkbox(
            "I own this application or have explicit authorization to test it "
            "(findings are actively exploited, not just flagged)",
            disabled=scanning,
        )
        submitted = st.form_submit_button("Run scan", disabled=scanning)

    if submitted:
        if not target_url.strip():
            st.error("Enter a target URL first.")
        elif not authorized:
            st.error("Confirm authorization before scanning a target.")
        else:
            env = load_env_file()
            if not env.get("LLM_API_KEY"):
                st.error("No LLM API key configured. Set it up before scanning.")
            else:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_file = LOG_DIR / f"scan-{int(time.time())}-{os.getpid()}.log"
                log_handle = open(log_file, "w")
                cmd = [
                    "uv",
                    "run",
                    "strix",
                    "--target",
                    target_url.strip(),
                    "-n",
                    "-m",
                    scan_mode,
                    "--max-budget",
                    str(max_budget),
                    # Bound wall-clock time independent of budget — a cheap model can
                    # buy far more than this many turns within max_budget alone.
                    "--max-turns",
                    "20",
                ]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(REPO_DIR),
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                st.session_state.process = proc
                st.session_state.start_time = time.time()
                st.session_state.log_path = str(log_file)
                st.session_state.run_dir = None
                st.rerun()

    if st.session_state.process is not None:
        proc: subprocess.Popen = st.session_state.process
        poll = proc.poll()

        run_dir = find_run_dir(st.session_state.start_time)
        if run_dir is not None:
            st.session_state.run_dir = str(run_dir)
            record_scan_ownership(run_dir.name, st.session_state.user["id"])

        st.info("Scan running…" if poll is None else "Finishing up…")

        if st.session_state.run_dir:
            data = read_run_json(Path(st.session_state.run_dir))
            usage = data.get("llm_usage", {})
            col1, col2, col3 = st.columns(3)
            col1.metric("Status", data.get("status", "starting"))
            col2.metric("Cost", f"${usage.get('cost', 0):.4f} / ${max_budget:.2f}")
            col3.metric("Requests", usage.get("requests", 0))

        with st.expander("Live log tail", expanded=False):
            log_path = Path(st.session_state.log_path)
            if log_path.exists():
                content = log_path.read_text(errors="replace")
                st.code(content[-4000:] or "(no output yet)", language=None)

        if st.button("Cancel scan"):
            proc.terminate()
            st.session_state.process = None
            st.rerun()

        if poll is None:
            time.sleep(2)
            st.rerun()
        else:
            st.session_state.process = None
            st.rerun()

    elif st.session_state.run_dir:
        run_dir = Path(st.session_state.run_dir)
        st.success(f"Scan finished: `{run_dir.name}`")
        render_results(run_dir)
        if st.button("Run another scan"):
            st.session_state.run_dir = None
            st.rerun()

    else:
        st.caption("Past scans live in the **History** tab.")


def _user_run_dirs(user_id: int) -> list[Path]:
    owned_names = get_user_run_names(user_id)
    if not RUNS_DIR.exists():
        return []
    return sorted(
        (d for d in RUNS_DIR.iterdir() if d.is_dir() and d.name in owned_names),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )


def resync_user_runs(user_id: int) -> list[Path]:
    """Re-read every one of this user's run.json files, syncing the DB cache.

    The DB only gets updated when a *live* session's polling loop happens to
    call read_run_json() for a run — if that session's tab is switched, the
    page reloaded, or the scan simply outlives the polling loop, the cached
    status/cost/vuln count goes stale even though the scan kept running and
    finished on disk. Call this before displaying any dashboard view built
    from the DB cache so it never shows an outdated snapshot.
    """
    run_dirs = _user_run_dirs(user_id)
    for run_dir in run_dirs:
        read_run_json(run_dir)
    return run_dirs


def render_history_tab(user_id: int) -> None:
    past_runs = resync_user_runs(user_id)
    if not past_runs:
        st.info("No completed scans yet — run one in the **New Scan** tab.")
        return

    st.subheader("Past runs")
    metadata = get_user_runs_metadata(user_id)
    st.dataframe(
        [
            {
                "Run": d.name,
                "Status": metadata.get(d.name, {}).get("status") or "unknown",
                "Cost": f"${(metadata.get(d.name, {}).get('cost') or 0):.4f}",
                "Vulnerabilities": metadata.get(d.name, {}).get("vulnerability_count"),
            }
            for d in past_runs
        ],
        hide_index=True,
        use_container_width=True,
    )
    choice = st.selectbox(
        "View a previous scan",
        [d.name for d in past_runs],
        index=None,
        placeholder="Select a run...",
    )
    if choice:
        render_results(RUNS_DIR / choice)


def render_chat_tab(user_id: int) -> None:
    """Always-visible chat panel — pick any of your scans and ask about it."""
    past_runs = _user_run_dirs(user_id)
    if not past_runs:
        st.info(
            "The chatbot answers questions about a scan's results. "
            "Run a scan first (in **New Scan**), then come back here."
        )
        return

    run_names = [d.name for d in past_runs]
    default_choice = st.session_state.get("chat_run_choice")
    default_index = run_names.index(default_choice) if default_choice in run_names else 0

    choice = st.selectbox("Which scan do you want to ask about?", run_names, index=default_index)
    st.session_state.chat_run_choice = choice

    if choice:
        render_chat(RUNS_DIR / choice, load_env_file())


def render_dashboard() -> None:
    with st.sidebar:
        st.markdown('<div class="mx-eyebrow">Signed in</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div style="font-family:\'IBM Plex Mono\',monospace; font-size:15px; '
            f'color:var(--mist); margin-top:2px;">{_esc(st.session_state.user["username"])}</div>',
            unsafe_allow_html=True,
        )
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        if st.button("Log out", use_container_width=True):
            st.session_state.user = None
            st.rerun()

    logo_uri = _logo_data_uri()
    if logo_uri:
        st.markdown(
            f'<div style="display:flex; align-items:center; gap:12px; margin-bottom:6px;">'
            f'<img src="{logo_uri}" style="height:38px; width:38px; object-fit:contain; '
            'border-radius:8px;" />'
            '<span class="mx-display" style="font-size:30px;">Meerkat'
            '<span style="color:var(--blue-bright);">X</span></span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<span class="mx-display" style="font-size:30px;">MeerkatX</span>', unsafe_allow_html=True)

    render_overview(st.session_state.user["id"])

    tab_scan, tab_chat, tab_history = st.tabs(["New scan", "Chat", "History"])
    with tab_scan:
        render_new_scan_tab()
    with tab_chat:
        render_chat_tab(st.session_state.user["id"])
    with tab_history:
        render_history_tab(st.session_state.user["id"])


def main() -> None:
    init_db()
    render_global_css()
    st.session_state.setdefault("user", None)
    st.session_state.setdefault("show_login", False)

    if st.session_state.user is not None:
        render_dashboard()
        return

    if st.session_state.show_login:
        render_auth_gate()
        return

    render_landing_page()


if __name__ == "__main__":
    main()
