"""
app.py — BoatAI 競艇予想 Webアプリ  (Design C: Simple Modern)
"""
import subprocess
import sys
import json
import html
from contextlib import contextmanager
import streamlit as st
import sqlite3
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta, timezone

# Streamlit Cloud は UTC で動くため JST (+9) を明示
_JST = timezone(timedelta(hours=9))
from pathlib import Path
import importlib
import predict as _predict_module
from predict import DB_PATH
import analysis as _analysis
import ml_predict as _ml_predict_module

def _predict(date, venue_code, race_no):
    mode = st.session_state.get("model_mode", "XGBoost ML")
    if mode == "XGBoost ML":
        return _ml_predict_module.predict_ml(date, venue_code, race_no)
    importlib.reload(_predict_module)
    return _predict_module.predict(date, venue_code, race_no)
from db_lock import acquire_write_lock, release_write_lock

st.set_page_config(
    page_title="BoatAI 競艇予想",
    page_icon="⛵",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── パスワード認証 ────────────────────────────────────────────────────────────
import hashlib, hmac as _hmac

def _make_auth_token(pw: str) -> str:
    """パスワードから決定論的なURLトークンを生成（クッキー代わり）"""
    key = (pw + "_boatai_v1").encode()
    return _hmac.new(key, pw.encode(), hashlib.sha256).hexdigest()[:32]

def _check_auth():
    """
    AUTH_PASSWORD 環境変数が設定されていればログイン画面を表示。
    ログイン後はURLに ?t=トークン を付与し、次回アクセス時は自動認証。
    （URLをブックマークすれば毎回パスワード入力不要）
    """
    import os
    _pw = os.environ.get("AUTH_PASSWORD", "")
    if not _pw:
        return  # 認証なし

    expected = _make_auth_token(_pw)

    # URLトークンチェック（リロード後も有効）
    if st.query_params.get("t") == expected:
        st.session_state["_authenticated"] = True
        return

    if st.session_state.get("_authenticated"):
        return

    # ── ログイン画面 ──
    st.markdown("""
    <style>
    [data-testid="stSidebar"] { display: none; }
    </style>
    """, unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("## ⛵ BoatAI")
        st.markdown("---")
        with st.form("_login_form"):
            entered = st.text_input("パスワード", type="password", placeholder="パスワードを入力")
            ok = st.form_submit_button("ログイン", use_container_width=True)
            if ok:
                if entered == _pw:
                    st.session_state["_authenticated"] = True
                    st.query_params["t"] = expected  # URLにトークンを埋め込む
                    st.rerun()
                else:
                    st.error("パスワードが違います")
    st.stop()

_check_auth()

# ─── Global CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
:root {
    --ba-bg: #edf3f7;
    --ba-surface: #ffffff;
    --ba-surface-2: #f6f9fc;
    --ba-line: #d7e1ec;
    --ba-line-strong: #aebed1;
    --ba-text: #111827;
    --ba-muted: #607086;
    --ba-blue: #0f68d9;
    --ba-blue-2: #e7f1ff;
    --ba-green: #078760;
    --ba-red: #d33f49;
    --ba-amber: #c77a05;
    --ba-accent: #0d9488;
    --ba-radius: 8px;
    --ba-shadow: 0 1px 2px rgba(17, 31, 51, 0.05), 0 8px 18px rgba(17, 31, 51, 0.035);
    --ba-shadow-strong: 0 14px 34px rgba(17, 31, 51, 0.10);
    --ba-navy: #0b1b2f;
    --ba-focus: 0 0 0 3px rgba(15, 104, 217, 0.16);
}

.stApp {
    background:
        linear-gradient(180deg, #f5f9fd 0%, var(--ba-bg) 260px),
        linear-gradient(90deg, rgba(15,104,217,0.04), rgba(13,148,136,0.035)),
        var(--ba-bg);
    color: var(--ba-text);
}

.main .block-container {
    max-width: 1420px;
    padding-top: 1.15rem;
    padding-bottom: 2.5rem;
}

.block-container,
[data-testid="stSidebar"] {
    font-feature-settings: "tnum" 1;
}

[data-testid="stSidebar"] {
    background:
        linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0)),
        linear-gradient(180deg, #08192c 0%, #102943 100%);
    border-right: 1px solid rgba(255,255,255,0.08);
}
[data-testid="stSidebar"] * {
    color: rgba(255,255,255,0.90);
}
[data-testid="stSidebar"] .stCaption,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
    color: rgba(255,255,255,0.66);
}
[data-testid="stSidebar"] hr {
    border-color: rgba(255,255,255,0.22) !important;
}

h1, h2, h3 {
    color: var(--ba-text);
    letter-spacing: 0;
}
h1 {
    font-size: 1.72rem !important;
    font-weight: 800 !important;
    margin-bottom: 0.15rem !important;
}
h2, h3 {
    font-weight: 750 !important;
}

[data-testid="stCaptionContainer"] {
    color: var(--ba-muted);
}

hr {
    margin: 0.95rem 0 1.1rem !important;
    border-color: var(--ba-line) !important;
}

[data-testid="stVerticalBlockBorderWrapper"] {
    border: 1px solid var(--ba-line) !important;
    border-radius: var(--ba-radius) !important;
    background: var(--ba-surface) !important;
    box-shadow: var(--ba-shadow);
}

[data-testid="stExpander"] {
    border: 1px solid var(--ba-line) !important;
    border-radius: var(--ba-radius) !important;
    background: var(--ba-surface) !important;
    box-shadow: var(--ba-shadow);
    overflow: hidden;
}
[data-testid="stExpander"] details summary {
    background: linear-gradient(180deg, #fff 0%, #f8fbff 100%);
    border-radius: var(--ba-radius) var(--ba-radius) 0 0;
    min-height: 42px;
}

.stButton > button,
[data-testid="stFormSubmitButton"] button {
    min-height: 2.15rem;
    border-radius: 7px;
    border: 1px solid var(--ba-line-strong);
    background: #fff;
    color: var(--ba-text);
    font-weight: 650;
    box-shadow: 0 1px 2px rgba(17, 31, 51, 0.05);
    transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease, color 120ms ease;
}
.stButton > button:hover,
[data-testid="stFormSubmitButton"] button:hover {
    border-color: var(--ba-blue);
    color: var(--ba-blue);
    background: var(--ba-blue-2);
    box-shadow: 0 3px 10px rgba(23, 104, 201, 0.12);
}
.stButton > button:focus,
[data-testid="stFormSubmitButton"] button:focus {
    box-shadow: var(--ba-focus) !important;
}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] button[kind="primary"] {
    border-color: var(--ba-blue);
    background: var(--ba-blue);
    color: #fff;
}
[data-testid="stFormSubmitButton"] button[kind="primary"] p,
.stButton > button[kind="primary"] p {
    color: #fff;
}
[data-testid="stSidebar"] .stButton > button {
    background: rgba(255,255,255,0.07);
    border-color: rgba(255,255,255,0.18);
    color: rgba(255,255,255,0.96);
    min-height: 2.45rem;
    box-shadow: none;
}
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span {
    color: rgba(255,255,255,0.96);
    font-weight: 760;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(255,255,255,0.95);
    border-color: rgba(255,255,255,0.95);
    transform: translateY(-1px);
}
[data-testid="stSidebar"] .stButton > button:hover p,
[data-testid="stSidebar"] .stButton > button:hover span {
    color: #0b4fae;
}
/* セクションラベル */
[data-testid="stSidebar"] .sidebar-section-label {
    font-size: 10px; font-weight: 500; color: rgba(255,255,255,0.35);
    letter-spacing: 0.08em; text-transform: uppercase;
    padding: 12px 8px 4px; display: block;
}
/* アクティブページのボタン */
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: rgba(56,139,245,0.18) !important;
    border-color: rgba(96,165,250,0.4) !important;
    color: #fff !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"] p,
[data-testid="stSidebar"] .stButton > button[kind="primary"] span {
    color: #fff !important;
}

.stTabs [data-baseweb="tab-list"] {
    gap: 4px; border-bottom: none; padding-bottom: 12px;
}
.stTabs [data-baseweb="tab"] {
    height: 2.1rem; padding: 0 14px;
    border-radius: 999px !important;
    color: var(--ba-muted); font-weight: 500; border: 0.5px solid transparent;
}
.stTabs [aria-selected="true"] {
    background: #fff !important; color: var(--ba-blue) !important;
    border: 0.5px solid #b5d4f4 !important; border-bottom-color: #b5d4f4 !important;
}
.stTabs [data-baseweb="tab-highlight"] { display: none; }

[data-testid="stMetric"] {
    background: #fff;
    border: 1px solid var(--ba-line);
    border-radius: var(--ba-radius);
    padding: 12px 14px;
    box-shadow: var(--ba-shadow);
}

[data-testid="stDataFrame"] {
    border: 1px solid var(--ba-line);
    border-radius: var(--ba-radius);
    overflow: hidden;
    background: #fff;
    box-shadow: var(--ba-shadow);
}

[data-testid="stPlotlyChart"] {
    border: 1px solid var(--ba-line);
    border-radius: var(--ba-radius);
    background: #fff;
    padding: 8px;
    box-shadow: var(--ba-shadow);
}

[data-testid="stAlert"] {
    border-radius: var(--ba-radius);
    border: 1px solid var(--ba-line);
    box-shadow: var(--ba-shadow);
}

[data-baseweb="select"] > div,
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input {
    border-color: var(--ba-line-strong) !important;
    border-radius: 6px !important;
    background: #fff !important;
}

[data-testid="stRadio"] label {
    color: var(--ba-text);
    font-weight: 650;
}

.ba-page-head {
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(174,190,209,0.80);
    border-radius: 8px;
    background:
        linear-gradient(135deg, rgba(255,255,255,0.98) 0%, rgba(247,251,255,0.98) 58%, rgba(231,241,255,0.95) 100%);
    padding: 17px 19px;
    margin-bottom: 16px;
    box-shadow: var(--ba-shadow-strong);
}
.ba-page-head::before {
    content: "";
    position: absolute;
    inset: 0 auto 0 0;
    width: 5px;
    background: linear-gradient(180deg, var(--ba-blue), var(--ba-accent));
}
.ba-page-kicker {
    color: var(--ba-blue);
    font-size: 11px;
    font-weight: 850;
    letter-spacing: .09em;
    text-transform: uppercase;
    margin-bottom: 4px;
}
.ba-page-title {
    color: var(--ba-text);
    font-size: 25px;
    font-weight: 900;
    line-height: 1.18;
}
.ba-page-subtitle {
    color: var(--ba-muted);
    font-size: 13px;
    font-weight: 650;
    margin-top: 6px;
}
.ba-page-meta {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 10px;
}
.ba-pill {
    display: inline-flex;
    align-items: center;
    min-height: 24px;
    padding: 3px 8px;
    border-radius: 999px;
    border: 1px solid #cfdbea;
    background: rgba(255,255,255,0.78);
    color: var(--ba-muted);
    font-size: 12px;
    font-weight: 750;
    white-space: nowrap;
}
.ba-pill-strong {
    border-color: #8dbdf1;
    background: var(--ba-blue-2);
    color: var(--ba-blue);
}
.ba-action-row {
    padding: 8px 0 2px;
}

/* 艇番カラーサークル */
.waku {
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; border-radius: 50%;
    font-size: 13px; font-weight: 800; flex-shrink: 0; line-height: 1;
    box-shadow: inset 0 0 0 1px rgba(0,0,0,0.14), 0 1px 2px rgba(17,31,51,0.10);
}
.w1 { background: #f7f7f7; color: #252525; }
.w2 { background: #202633; color: #fff; }
.w3 { background: #cf2e36; color: #fff; }
.w4 { background: #1465bd; color: #fff; }
.w5 { background: #f2b51d; color: #312400; }
.w6 { background: #168449; color: #fff; }

/* 払戻表示 */
.payout-card { margin-bottom: 10px; }
.payout-type { font-size: 12px; color: #888; margin-bottom: 2px; }
.payout-line { display: flex; align-items: baseline; gap: 10px; }
.payout-combo { font-size: 17px; font-weight: 500; letter-spacing: 2px; }
.payout-amount { font-size: 22px; font-weight: 500; color: var(--ba-blue); }
.payout-amount-sub { font-size: 18px; font-weight: 500; }
.payout-pop { font-size: 12px; color: #888; }

/* 着順表示 */
.result-row { display: flex; align-items: center; gap: 10px; padding: 8px 0;
              border-bottom: 1px solid rgba(128,128,128,0.15); }
.result-row:last-child { border-bottom: none; }
.result-rank { font-size: 16px; font-weight: 500; min-width: 32px; color: #555; }
.result-name { font-size: 15px; }
.result-st   { font-size: 12px; color: #888; }

/* 勝率バー */
.prob-wrap { display: flex; align-items: center; gap: 10px; margin: 4px 0; }
.prob-num  { font-size: 14px; font-weight: 800; min-width: 50px; color: var(--ba-blue); font-variant-numeric: tabular-nums; }
.prob-bar-bg { flex: 1; height: 8px; background: #e3e9f2; border-radius: 4px; overflow: hidden; }
.prob-bar-fill { height: 8px; border-radius: 4px; }

/* 推奨買い目コンボ */
.rec-card {
    background: #fff; border-radius: 10px;
    border: 0.5px solid #d8e3ed;
    padding: 12px 14px; margin-bottom: 8px;
    transition: box-shadow 120ms ease, border-color 120ms ease;
}
.rec-card:hover {
    border-color: #b5d4f4;
    box-shadow: 0 4px 12px rgba(17,31,51,0.07);
}
.rec-head { display:flex; align-items:center; gap:8px; min-width:0; }
.rec-rank {
    width: 22px; height: 22px; display:inline-flex; align-items:center; justify-content:center;
    border-radius: 6px; background: #f1f5f9;
    border: 0.5px solid #d8e3ed;
    color: #9ca3af; font-size: 11px; font-weight: 700; flex-shrink:0;
}
.rec-combo { font-size: 20px; font-weight: 700; letter-spacing: 3px; font-variant-numeric: tabular-nums; }
.rec-badge {
    padding: 2px 7px; border-radius: 999px;
    font-size: 10px; color: #fff; font-weight: 600; white-space: nowrap;
}
.rec-live {
    background: #e8f1fb; color: #0f68d9;
    padding: 1px 6px; border-radius: 999px; font-size: 10px; font-weight: 600;
}
.rec-stats {
    display:flex; gap:10px; flex-wrap:wrap;
    color: #9ca3af; font-size: 11px; padding: 6px 0 0;
}
.rec-stats b { color: #374151; font-variant-numeric: tabular-nums; }
.rec-ev-good { color: #078760 !important; }
.rec-ev-bad { color: #d33f49 !important; }

/* 会場カード内テキスト */
.venue-name { font-size: 18px; font-weight: 500; margin: 0 0 4px; }

/* セクション見出し */
.sec-label {
    font-size: 11px; color: var(--ba-muted); text-transform: uppercase;
    letter-spacing: 0.09em; margin: 0 0 7px;
    font-weight: 800;
}

.race-card {
    background: #fff; border-radius: 10px;
    border: 0.5px solid #d8e3ed;
    padding: 12px 14px; min-height: 110px;
    transition: transform 120ms ease, box-shadow 120ms ease, border-color 120ms ease;
}
.race-card:hover {
    transform: translateY(-1px); border-color: #93c5fd;
    box-shadow: 0 6px 16px rgba(17,31,51,0.08);
}
.race-card-top {
    display:flex; align-items:flex-start; justify-content:space-between; gap:8px;
}
.race-no { font-size: 28px; font-weight: 500; color: var(--ba-text); line-height: 1; }
.race-time { font-size: 12px; color: var(--ba-muted); margin-top: 4px; font-weight: 500; }
.race-payout { margin-top: 4px; font-size: 13px; font-weight: 700; color: var(--ba-accent); }
.race-title {
    margin-top: 6px; min-height: 32px;
    color: var(--ba-muted); font-size: 12px; line-height: 1.4;
}
.status-badge {
    display: inline-flex; align-items: center;
    padding: 2px 8px; border-radius: 999px;
    font-size: 10px; font-weight: 500;
}
.status-done  { background: #dcfce7; color: #15803d; }
.status-live  { background: #dbeafe; color: #1d4ed8; }
.status-ready { background: #f1f5f9; color: #475569; }

/* 会場カード（新デザイン） */
.vc-card {
    background: #fff; border-radius: 10px;
    border: 0.5px solid #d8e3ed; overflow: hidden;
    transition: transform 120ms ease, box-shadow 120ms ease;
}
.vc-card:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(17,31,51,0.08); }
.vc-card.vc-active  { border-color: #3b82f6; }
.vc-card.vc-night   { border-color: #7c3aed; }
.vc-card.vc-morning { border-color: #d97706; }
.vc-card.vc-ended   { opacity: 0.55; }
.vc-card.vc-none    { background: #f4f6f8; border: 0.5px dashed #c8d3de; }
.vc-head {
    padding: 9px 12px 7px; background: #f8fbff;
    border-bottom: 0.5px solid #e5e9f0;
}
.vc-head.vc-night-head  { background: #f5f1ff; }
.vc-head.vc-morning-head { background: #fffbeb; }
.vc-head.vc-ended-head  { background: #9ea6b1; border-bottom-color: #8e97a3; }
.vc-head.vc-none-head   { background: transparent; border-bottom: none; }
.vc-name { font-size: 15px; font-weight: 500; display: block; }
.vc-head.vc-ended-head .vc-name { color: #fff; }
.vc-card.vc-none .vc-name { color: #9ca3af; }
.vc-name-row { display: flex; align-items: center; justify-content: space-between; }
.vc-status-badge {
    font-size: 9px; padding: 2px 6px; border-radius: 999px; font-weight: 500;
}
.vc-badge-live    { background: #e8f2ff; color: #155fae; }
.vc-badge-night   { background: #ede9fe; color: #5b21b6; }
.vc-badge-morning { background: #fef3c7; color: #92400e; }
.vc-badge-done    { background: #f1f5f9; color: #6b7280; }
.vc-badge-none    { background: #f3f4f6; color: #9ca3af; }
.vc-prog { padding: 5px 12px 2px; }
.vc-prog-bar { height: 3px; background: #e5e7eb; border-radius: 2px; overflow: hidden; margin-bottom: 2px; }
.vc-prog-fill { height: 100%; background: #3b82f6; border-radius: 2px; }
.vc-prog-fill.night   { background: #7c3aed; }
.vc-prog-fill.morning { background: #d97706; }
.vc-prog-text { font-size: 10px; color: #9ca3af; }
.vc-none-dash { padding: 10px 0 8px; text-align: center; font-size: 10px; color: #d1d5db; }
.grade-sg    { background:#e53935; color:#fff; font-size:11px; padding:1px 5px;
               border-radius:3px; font-weight:700; margin-right:4px; }
.grade-g1    { background:#c62828; color:#fff; font-size:11px; padding:1px 5px;
               border-radius:3px; font-weight:700; margin-right:4px; }
.grade-g2    { background:#1565c0; color:#fff; font-size:11px; padding:1px 5px;
               border-radius:3px; font-weight:700; margin-right:4px; }
.grade-g3    { background:#1565c0; color:#fff; font-size:11px; padding:1px 5px;
               border-radius:3px; font-weight:700; margin-right:4px; }

/* 予想精度・履歴の追加表示 */
.hit-summary {
    border: 0.5px solid #d8e3ed;
    border-radius: 10px;
    background: #fff;
    padding: 13px 15px;
}
.acc-metric {
    background: #fff; border-radius: 10px;
    border: 0.5px solid #d8e3ed;
    padding: 14px 16px; min-height: 96px;
    display: flex; flex-direction: column; justify-content: space-between;
}
.acc-metric.acc-blue   { border-top: 3px solid #0f68d9; }
.acc-metric.acc-green  { border-top: 3px solid #078760; }
.acc-metric.acc-amber  { border-top: 3px solid #c77a05; }
.acc-metric.acc-red    { border-top: 3px solid #d33f49; }
.acc-metric.acc-slate  { border-top: 3px solid #64748b; }
.acc-label { color: #6b7280; font-size: 12px; font-weight: 500; }
.acc-value { color: #111; font-size: 28px; font-weight: 500; line-height: 1.1; font-variant-numeric: tabular-nums; }
.acc-sub   { color: #9ca3af; font-size: 11px; }
.acc-pattern {
    background: #fff; border-radius: 10px;
    border: 0.5px solid #d8e3ed;
    padding: 14px 16px; min-height: 96px;
}
.acc-pattern.acc-blue   { border-top: 3px solid #0f68d9; }
.acc-pattern.acc-amber  { border-top: 3px solid #c77a05; }
.acc-pattern.acc-red    { border-top: 3px solid #d33f49; }
.pattern-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
}
.pattern-title {
    color: var(--ba-text);
    font-size: 14px;
    font-weight: 850;
}
.pattern-count {
    color: var(--ba-muted);
    font-size: 12px;
    font-weight: 750;
    font-variant-numeric: tabular-nums;
}
.pattern-rate {
    color: var(--ba-text);
    font-size: 24px;
    font-weight: 850;
    line-height: 1;
    font-variant-numeric: tabular-nums;
}
.combo-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 6px;
}
.combo-chip {
    min-height: 48px;
    border: 1px solid var(--ba-line);
    border-radius: 7px;
    background: var(--ba-surface-2);
    padding: 6px 4px;
    text-align: center;
    box-sizing: border-box;
}
.combo-chip-hit {
    background: #dcfce7;
    border: 2px solid var(--ba-green);
}
.combo-rank {
    display: block;
    color: var(--ba-muted);
    font-size: 10px;
    font-weight: 800;
    line-height: 1.1;
    margin-bottom: 4px;
}
.combo-value {
    display: block;
    color: var(--ba-text);
    font-size: 13px;
    font-weight: 850;
    line-height: 1.1;
    letter-spacing: 1px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.combo-chip-hit .combo-value,
.combo-chip-hit .combo-rank {
    color: #087249;
}
.hit-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 10px;
}
.hit-title {
    color: var(--ba-text);
    font-size: 15px;
    font-weight: 850;
}
.hit-meta {
    color: var(--ba-muted);
    font-size: 12px;
    font-weight: 650;
}
.hit-payout {
    color: var(--ba-blue);
    font-size: 18px;
    font-weight: 850;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
}

.deadline-row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; margin-bottom: 5px;
    background: #fff; border-radius: 8px; border: 0.5px solid #d8e3ed;
}
.deadline-row-soon { border-color: #f97316; background: #fff7ed; }
.deadline-row-now  { border-color: #dc2626; background: #fff1f2; }
.deadline-venue { min-width: 44px; font-size: 13px; font-weight: 500; color: #111; }
.deadline-race  { flex: 1; font-size: 12px; color: #6b7280; }
.deadline-combo { font-size: 13px; font-weight: 500; color: #185fa5; min-width: 60px; letter-spacing:2px; }
.deadline-cat   { font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; white-space: nowrap; }
.deadline-cat-honmei { background: #e8f1fb; color: #185fa5; }
.deadline-cat-chuana { background: #fef3c7; color: #b45309; }
.deadline-cat-ana    { background: #fee2e2; color: #b91c1c; }
.deadline-time { font-size: 12px; font-weight: 500; color: #374151; white-space: nowrap; }
.deadline-row-soon .deadline-time { color: #c2410c; }
.deadline-row-now  .deadline-time { color: #dc2626; }
.deadline-title { color: var(--ba-muted); font-weight: 500; }

.payout-panel {
    border: 1px solid var(--ba-line);
    border-radius: var(--ba-radius);
    background: #fff;
    overflow: hidden;
    margin-bottom: 15px;
    box-shadow: var(--ba-shadow);
}
.payout-head {
    background: linear-gradient(135deg, #0f68d9 0%, #0b4fae 100%);
    color: #fff;
    padding: 8px 12px;
    font-size: 15px;
    font-weight: 850;
}
.payout-head span {
    color: rgba(255,255,255,0.82);
    font-size: 11px;
    font-weight: 750;
}
.payout-line-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 37px;
    padding: 7px 10px;
    border-bottom: 1px solid #eef3f8;
}
.payout-line-row:nth-child(even) {
    background: #fbfdff;
}
.payout-line-row:last-child {
    border-bottom: none;
}
.payout-race-no {
    min-width: 28px;
    color: var(--ba-muted);
    font-size: 12px;
    font-weight: 800;
}
.payout-boats {
    display: flex;
    gap: 2px;
    flex: 1;
}
.payout-empty,
.payout-time {
    color: var(--ba-muted);
    font-size: 13px;
    font-weight: 650;
}

.recommend-card {
    background: #fff; border-radius: 10px;
    border: 0.5px solid #d8e3ed; overflow: hidden;
    margin-bottom: 10px;
    transition: transform 120ms ease, box-shadow 120ms ease;
}
.recommend-card:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(17,31,51,0.08); }
.recommend-card-head {
    padding: 10px 14px; border-bottom: 0.5px solid #e5e7eb;
    display: flex; align-items: center; justify-content: space-between;
}
.recommend-cat-badge {
    font-size: 11px; font-weight: 500; padding: 3px 8px;
    border-radius: 999px; white-space: nowrap;
}
.recommend-cat-honmei { background: #e8f1fb; color: #185fa5; }
.recommend-cat-chuana { background: #fef3c7; color: #b45309; }
.recommend-cat-ana    { background: #fee2e2; color: #b91c1c; }
.recommend-venue-race { font-size: 11px; color: #6b7280; margin-top: 2px; }
.recommend-card-body { padding: 12px 14px; }
.recommend-combo-big { font-size: 24px; font-weight: 500; letter-spacing: 4px; color: #111; }
.recommend-stats-row { display: flex; gap: 12px; margin-top: 6px; flex-wrap: wrap; }
.recommend-stat-item { font-size: 11px; color: #6b7280; }
.recommend-stat-item b { color: #111; font-weight: 500; }
.recommend-card-footer {
    padding: 8px 14px; background: #f8fafc;
    border-top: 0.5px solid #e5e7eb;
}
.recommend-category {
    color: #fff;
    padding: 9px 12px;
    border-radius: var(--ba-radius);
    font-weight: 850;
    font-size: 16px;
    margin-bottom: 10px;
    box-shadow: var(--ba-shadow);
}

.boat-row {
    display: grid;
    grid-template-columns: auto minmax(86px, 1fr) minmax(150px, 2.2fr);
    align-items: center;
    gap: 9px;
    padding: 8px 9px;
    border: 1px solid transparent;
    border-radius: 6px;
}
.boat-row:nth-child(even) {
    background: var(--ba-surface-2);
}
.boat-row:hover {
    background: #edf6ff;
    border-color: #c9def5;
}
.boat-row-name {
    color: var(--ba-text);
    font-size: 13px;
    font-weight: 800;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.boat-row-meta {
    color: var(--ba-muted);
    font-size: 12px;
    font-weight: 700;
}
.result-combo-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-top: 12px;
    padding: 12px 14px;
    border: 1px solid var(--ba-line);
    border-radius: var(--ba-radius);
    background: linear-gradient(135deg, #fff 0%, #f8fbff 100%);
    box-shadow: var(--ba-shadow);
}
.result-combo-label {
    color: var(--ba-muted);
    font-size: 12px;
    font-weight: 800;
}
.result-combo-value {
    color: var(--ba-text);
    font-size: 26px;
    font-weight: 900;
    letter-spacing: 4px;
    font-variant-numeric: tabular-nums;
}
.result-combo-payout {
    color: var(--ba-amber);
    font-size: 18px;
    font-weight: 900;
    white-space: nowrap;
}
.odds-shell {
    overflow-x: auto;
    margin-top: 10px;
    border: 1px solid var(--ba-line);
    border-radius: var(--ba-radius);
    background: #fff;
    box-shadow: var(--ba-shadow-strong);
}
.odds-table {
    border-collapse: collapse;
    width: 100%;
    table-layout: fixed;
}
.odds-table th {
    padding: 8px 4px;
    text-align: center;
    color: #fff;
    font-size: 11px;
    border: 1px solid rgba(255,255,255,0.2);
    vertical-align: bottom;
}
.odds-table td {
    padding: 5px 4px;
    font-size: 12px;
    border: 1px solid #e5ebf2;
    vertical-align: middle;
    white-space: nowrap;
}
.odds-table tbody tr:nth-child(even) td {
    background-image: linear-gradient(rgba(247,250,253,0.58), rgba(247,250,253,0.58));
}
.odds-table tbody tr:hover td {
    background-image: linear-gradient(rgba(231,241,255,0.88), rgba(231,241,255,0.88));
}
.odds-cell {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 3px;
}
.odds-value {
    font-weight: 850;
    min-width: 40px;
    text-align: right;
    font-variant-numeric: tabular-nums;
}
.odds-group-label {
    text-align: center;
    background: #f3f6fa;
    padding: 0 !important;
}

@media (max-width: 900px) {
    .main .block-container {
        padding-left: 0.8rem;
        padding-right: 0.8rem;
    }
    .combo-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .deadline-row,
    .hit-summary {
        align-items: flex-start;
        flex-direction: column;
    }
    .recommend-line {
        grid-template-columns: 18px minmax(70px, auto) 1fr 1fr;
    }
    .boat-row {
        grid-template-columns: auto minmax(82px, 1fr);
    }
    .boat-row .prob-wrap {
        grid-column: 1 / -1;
    }
    .ba-page-title {
        font-size: 21px;
    }
}
</style>
""", unsafe_allow_html=True)

# ─── Session state ─────────────────────────────────────────────────────────────
for _k, _v in [("page", "home"), ("venue_code", None), ("venue_name", None),
               ("race_no", None), ("hist_vc", None)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


def nav(page, **kw):
    st.session_state.page = page
    for k, v in kw.items():
        st.session_state[k] = v
    st.rerun()


# ─── DB helpers ───────────────────────────────────────────────────────────────
def _conn():
    from db_connect import open_db
    return open_db(row_factory=sqlite3.Row)


@st.cache_data(ttl=120)
def latest_date():
    with _conn() as c:
        return c.execute("SELECT MAX(date) FROM races").fetchone()[0]


@st.cache_data(ttl=300)
def get_meet_day(date: str, venue_code: str) -> int:
    """今日を含む連続開催日数（1始まり）を返す"""
    with _conn() as c:
        past = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM races WHERE venue_code=? AND date<=? ORDER BY date DESC",
            (venue_code, date)
        ).fetchall()]
    day_num = 1
    for i in range(1, len(past)):
        try:
            d0 = datetime.strptime(past[i - 1], "%Y%m%d")
            d1 = datetime.strptime(past[i], "%Y%m%d")
            if (d0 - d1).days == 1:
                day_num += 1
            else:
                break
        except Exception:
            break
    return day_num


@st.cache_data(ttl=60)
def get_venues(date):
    """
    指定日の開催会場一覧を返す。
    1コネクション・5クエリに統合（旧: 2コネクション + N会場×1クエリ）。
    """
    with _conn() as c:
        race_rows = c.execute(
            "SELECT id, venue_code FROM races WHERE date=?", (date,)
        ).fetchall()
        if not race_rows:
            return []

        venue_races: dict = {}
        for r in race_rows:
            venue_races.setdefault(r["venue_code"], []).append(r["id"])

        result_ids = {r[0] for r in c.execute(
            "SELECT DISTINCT rre.race_id FROM race_result_entries rre "
            "JOIN races r ON r.id = rre.race_id WHERE r.date=?", (date,)
        ).fetchall()}
        bi_ids = {r[0] for r in c.execute(
            "SELECT DISTINCT bi.race_id FROM before_info bi "
            "JOIN races r ON r.id = bi.race_id "
            "WHERE bi.exhibition_time IS NOT NULL AND r.date=?", (date,)
        ).fetchall()}
        venue_map = {r[0]: r[1] for r in c.execute(
            "SELECT venue_code, venue_name FROM venues"
        ).fetchall()}

        # 開催何日目: 全会場まとめて1クエリ取得 → Python側でカウント
        vcodes = list(venue_races.keys())
        placeholders = ",".join("?" * len(vcodes))
        past_rows = c.execute(
            f"SELECT venue_code, date FROM races "
            f"WHERE venue_code IN ({placeholders}) AND date<=? "
            f"ORDER BY venue_code, date DESC",
            vcodes + [date]
        ).fetchall()

    # Python側で開催連続日数を計算（DB往復なし）
    from collections import defaultdict
    past_by_vc: dict = defaultdict(list)
    for r in past_rows:
        past_by_vc[r[0]].append(r[1])
    meet_days: dict = {}
    for vc, past_dates in past_by_vc.items():
        day_num = 1
        for i in range(1, len(past_dates)):
            try:
                d0 = datetime.strptime(past_dates[i - 1], "%Y%m%d")
                d1 = datetime.strptime(past_dates[i], "%Y%m%d")
                if (d0 - d1).days == 1:
                    day_num += 1
                else:
                    break
            except Exception:
                break
        meet_days[vc] = day_num

    return [
        {"venue_code": vc, "venue_name": venue_map.get(vc, vc),
         "race_count": len(rids),
         "result_count": sum(1 for rid in rids if rid in result_ids),
         "bi_count": sum(1 for rid in rids if rid in bi_ids),
         "meet_day": meet_days.get(vc, 1)}
        for vc, rids in sorted(venue_races.items())
    ]


@st.cache_data(ttl=30)
def get_races(date, venue_code):
    """
    指定日・会場のレース一覧を返す。
    1コネクション・4クエリに統合（旧: 2コネクション）。
    """
    with _conn() as c:
        races = c.execute(
            "SELECT id, race_no, race_title, scheduled_time FROM races "
            "WHERE date=? AND venue_code=? ORDER BY race_no",
            (date, venue_code)
        ).fetchall()
        if not races:
            return []
        result_ids = {r[0] for r in c.execute(
            "SELECT DISTINCT rre.race_id FROM race_result_entries rre "
            "JOIN races r ON r.id = rre.race_id WHERE r.date=? AND r.venue_code=?",
            (date, venue_code)
        ).fetchall()}
        bi_ids = {r[0] for r in c.execute(
            "SELECT DISTINCT bi.race_id FROM before_info bi "
            "JOIN races r ON r.id = bi.race_id "
            "WHERE bi.exhibition_time IS NOT NULL AND r.date=? AND r.venue_code=?",
            (date, venue_code)
        ).fetchall()}
        sanrentan_map = {r[0]: r[1] for r in c.execute(
            "SELECT p.race_id, p.payout FROM payouts p "
            "JOIN races r ON r.id = p.race_id "
            "WHERE p.bet_type='3連単' AND r.date=? AND r.venue_code=?",
            (date, venue_code)
        ).fetchall()}
    return [
        {"race_no": r["race_no"], "race_title": r["race_title"] or f"{r['race_no']}R",
         "scheduled_time": r["scheduled_time"],
         "has_result": r["id"] in result_ids, "has_bi": r["id"] in bi_ids,
         "sanrentan": sanrentan_map.get(r["id"])}
        for r in races
    ]


@st.cache_data(ttl=3600)
def get_prediction(date, venue_code, race_no, model_mode="XGBoost ML"):
    try:
        return _predict(date, venue_code, race_no), None
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=60)
def get_result(date, venue_code, race_no):
    with _conn() as c:
        rows = c.execute("""
            SELECT rre.rank, rre.boat_no, rre.player_name, rre.start_timing
            FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=? ORDER BY rre.rank
        """, (date, venue_code, race_no)).fetchall()
        payout_row = c.execute("""
            SELECT p.payout FROM payouts p
            JOIN races r ON r.id = p.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=? AND p.bet_type='3連単'
        """, (date, venue_code, race_no)).fetchone()
    if not rows:
        return None
    return {
        "entries": [dict(r) for r in rows],
        "sanrentan": payout_row[0] if payout_row else None,
    }


# ─── HTML helpers ─────────────────────────────────────────────────────────────
def _h(value):
    return html.escape("" if value is None else str(value), quote=True)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@contextmanager
def _db_write_lock(label="DB書き込み", timeout=180):
    try:
        acquire_write_lock(wait=True, timeout=timeout)
    except SystemExit as exc:
        raise RuntimeError(f"{label}のロックを取得できませんでした。別の更新処理が実行中です。") from exc
    try:
        yield
    finally:
        release_write_lock()


def _run_db_writer(args, label, timeout=180):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path(DB_PATH).parent),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{label}に失敗しました: {detail or f'終了コード {result.returncode}'}")
    return result


def _waku(n):
    """艇番カラーサークルHTML"""
    boat_no = _safe_int(n)
    return f"<span class='waku w{boat_no}'>{boat_no}</span>"


def _page_header(title, subtitle="", kicker="BoatAI", pills=None):
    pills = pills or []
    pill_html = "".join(
        f"<span style='display:inline-flex;align-items:center;padding:3px 8px;"
        f"border-radius:999px;border:0.5px solid {'#b5d4f4' if i==0 else '#d8e3ed'};"
        f"background:{'#e8f1fb' if i==0 else '#fff'};"
        f"color:{'#185fa5' if i==0 else '#6b7280'};"
        f"font-size:11px;font-weight:500;white-space:nowrap'>{_h(p)}</span>"
        for i, p in enumerate(pills)
    )
    badges_html = f"<div style='display:flex;gap:6px;flex-wrap:wrap;margin-top:10px'>{pill_html}</div>" if pill_html else ""
    sub_html = (
        f"<div style='font-size:12px;color:#6b7280;margin-top:3px'>{_h(subtitle)}</div>"
        if subtitle else ""
    )
    st.markdown(
        f"<div style='background:#fff;border:0.5px solid #d8e3ed;border-radius:10px;"
        f"padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;"
        f"justify-content:space-between;gap:12px'>"
        f"<div>"
        f"<div style='font-size:10px;font-weight:500;color:#6b7280;letter-spacing:0.08em;"
        f"text-transform:uppercase;margin-bottom:3px'>{_h(kicker)}</div>"
        f"<div style='font-size:18px;font-weight:500;color:#111'>{_h(title)}</div>"
        f"{sub_html}"
        f"</div>"
        f"{badges_html}"
        f"</div>",
        unsafe_allow_html=True
    )


def _prob_bar(prob, color="#1768c9"):
    """勝率バーHTML"""
    w = min(int(prob), 100)
    return (
        f"<div class='prob-wrap'>"
        f"<span class='prob-num'>{prob:.1f}%</span>"
        f"<div class='prob-bar-bg'>"
        f"<div class='prob-bar-fill' style='width:{w}%;background:{color}'></div>"
        f"</div></div>"
    )


def _boat_prob_row(b, color="#1768c9", meta=""):
    meta_html = f"<div class='boat-row-meta'>{_h(meta)}</div>" if meta else ""
    return (
        f"<div class='boat-row'>"
        f"{_waku(b['boat_no'])}"
        f"<div><div class='boat-row-name'>{_h(b['player_name'])}</div>{meta_html}</div>"
        f"{_prob_bar(b['win_prob'], color)}"
        f"</div>"
    )


def _boat_top3_row(b, color="#2e7d32"):
    """3連対率表示行"""
    top3 = b.get("top3_prob", 0.0)
    return (
        f"<div class='boat-row'>"
        f"{_waku(b['boat_no'])}"
        f"<div><div class='boat-row-name'>{_h(b['player_name'])}</div></div>"
        f"{_prob_bar(top3, color)}"
        f"</div>"
    )


def _result_combo_card(actual, payout=None):
    payout_html = f"<div class='result-combo-payout'>¥{payout:,}</div>" if payout else ""
    return (
        f"<div class='result-combo-card'>"
        f"<div>"
        f"<div class='result-combo-label'>3連単 確定</div>"
        f"<div class='result-combo-value'>{_h(actual)}</div>"
        f"</div>"
        f"{payout_html}"
        f"</div>"
    )


def _payout_html(payouts):
    """払戻結果をテーブル形式で表示するHTML"""
    ORDER = {"3連単": 1, "3連複": 2, "2連単": 3, "2連複": 4, "単勝": 5, "複勝": 6}
    sorted_pays = sorted(payouts, key=lambda p: ORDER.get(p["bet_type"], 9))
    rows = ""
    for p in sorted_pays:
        is_main = p["bet_type"] in ("3連単", "3連複")
        amt_size = "18px" if is_main else "15px"
        combo_size = "16px" if is_main else "14px"
        amt_color = "#1768c9" if is_main else "inherit"
        rows += (
            f"<tr style='border-bottom:0.5px solid rgba(128,128,128,0.15)'>"
            f"<td style='padding:7px 10px;font-size:12px;color:#888;white-space:nowrap'>{_h(p['bet_type'])}</td>"
            f"<td style='padding:7px 10px;font-size:{combo_size};font-weight:500;letter-spacing:2px'>{_h(p['combination'])}</td>"
            f"<td style='padding:7px 10px;font-size:{amt_size};font-weight:500;color:{amt_color};text-align:right;white-space:nowrap'>{p['payout']:,}円</td>"
            f"<td style='padding:7px 10px;font-size:12px;color:#888;text-align:right;white-space:nowrap'>{_h(p['popularity'])}番人気</td>"
            f"</tr>"
        )
    return (
        f"<table style='width:100%;border-collapse:collapse'>"
        f"<thead><tr style='border-bottom:1px solid rgba(128,128,128,0.25)'>"
        f"<th style='padding:4px 10px;font-size:11px;color:#888;font-weight:500;text-align:left'>賭け式</th>"
        f"<th style='padding:4px 10px;font-size:11px;color:#888;font-weight:500;text-align:left'>組合せ</th>"
        f"<th style='padding:4px 10px;font-size:11px;color:#888;font-weight:500;text-align:right'>払戻</th>"
        f"<th style='padding:4px 10px;font-size:11px;color:#888;font-weight:500;text-align:right'>人気</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _result_html(results):
    """着順表示HTML（艇番カラー付き）"""
    html = ""
    for r in results:
        if r["rank"] and r["rank"] <= 6:
            st_str = f"ST: {r['start_timing']}" if r["start_timing"] else ""
            html += (
                f"<div class='result-row'>"
                f"<span class='result-rank'>{r['rank']}着</span>"
                f"{_waku(r['boat_no'])}"
                f"<span class='result-name'>{_h(r['player_name'])}</span>"
                f"<span class='result-st'>{_h(st_str)}</span>"
                f"</div>"
            )
    return html


def _acc_metric_card(label, value, sub="", tone="blue"):
    return (
        f"<div class='acc-metric acc-{tone}'>"
        f"<div class='acc-label'>{label}</div>"
        f"<div class='acc-value'>{value}</div>"
        f"<div class='acc-sub'>{sub}</div>"
        f"</div>"
    )


def _pattern_card(label, rate, count_text, tone="blue"):
    return (
        f"<div class='acc-pattern acc-{tone}'>"
        f"<div class='pattern-head'>"
        f"<span class='pattern-title'>{label}</span>"
        f"<span class='pattern-count'>{count_text}</span>"
        f"</div>"
        f"<div class='pattern-rate'>{rate}</div>"
        f"</div>"
    )


def _combo_chip_html(combo, rank, is_hit=False):
    cls = "combo-chip combo-chip-hit" if is_hit else "combo-chip"
    mark = "的中" if is_hit else f"#{rank}"
    return (
        f"<div class='{cls}'>"
        f"<span class='combo-rank'>{mark}</span>"
        f"<span class='combo-value'>{_h(combo)}</span>"
        f"</div>"
    )


def _combo_grid_html(combos, actual=None):
    chips = "".join(
        _combo_chip_html(combo, idx + 1, combo == actual)
        for idx, combo in enumerate(combos[:5])
    )
    return f"<div class='combo-grid'>{chips}</div>"


def _hit_summary_html(badge, date_disp, venue_name, race_no, actual, payout=None):
    payout_html = (
        f"<div style='font-size:17px;font-weight:500;color:#0f68d9;white-space:nowrap'>{payout:,}円</div>"
        if payout else ""
    )
    return (
        f"<div style='display:flex;align-items:center;justify-content:space-between;gap:12px;"
        f"padding:12px 14px;background:#fff;border-radius:10px;border:0.5px solid #d8e3ed;margin-bottom:6px'>"
        f"<div>"
        f"<div style='font-size:14px;font-weight:500;color:#111'>{_h(badge)} {_h(venue_name)} {_h(race_no)}R　"
        f"<span style='letter-spacing:3px'>{_h(actual)}</span></div>"
        f"<div style='font-size:11px;color:#9ca3af;margin-top:2px'>{_h(date_disp)}</div>"
        f"</div>"
        f"{payout_html}"
        f"</div>"
    )


def _deadline_row_html(venue_name, race_no, title, scheduled_time, urgency="normal", rec_combo=None, rec_cat=None):
    cls = {
        "now":  "deadline-row deadline-row-now",
        "soon": "deadline-row deadline-row-soon",
    }.get(urgency, "deadline-row")
    title_html = f" {_h(title)}" if title else ""
    combo_html = f"<span class='deadline-combo'>{_h(rec_combo)}</span>" if rec_combo else "<span style='min-width:60px'></span>"
    if rec_cat:
        cat_map = {"honmei": ("◎本命", "deadline-cat deadline-cat-honmei"),
                   "chuana": ("△中穴", "deadline-cat deadline-cat-chuana"),
                   "ana":    ("☆穴",   "deadline-cat deadline-cat-ana")}
        cat_label, cat_cls = cat_map.get(rec_cat, (rec_cat, "deadline-cat"))
        cat_html = f"<span class='{cat_cls}'>{cat_label}</span>"
    else:
        cat_html = ""
    return (
        f"<div class='{cls}'>"
        f"<span class='deadline-time'>{_h(scheduled_time)}</span>"
        f"<span class='deadline-venue'>{_h(venue_name)}</span>"
        f"<span class='deadline-race'>{_h(race_no)}R{title_html}</span>"
        f"{combo_html}"
        f"{cat_html}"
        f"</div>"
    )


def _combo_rank_stars(actual: str, combos: list) -> tuple[str, int, int]:
    """combosリスト内のactualの順位から (★文字列, 順位, 総数) を返す"""
    if not combos or actual not in combos:
        return "", 0, 0
    pos = combos.index(actual)
    total = len(combos)
    ratio = 1.0 - pos / total
    if   ratio >= 0.9: n = 5
    elif ratio >= 0.7: n = 4
    elif ratio >= 0.5: n = 3
    elif ratio >= 0.3: n = 2
    else:              n = 1
    stars = "★" * n + "☆" * (5 - n)
    return stars, pos + 1, total


def _stars_html(confidence, category: str) -> str:
    """自信度 float → ★1〜5 HTML"""
    if confidence is None or confidence <= 0:
        n = 1
    elif category == "honmei":
        # honmei は prob（0〜100%）で評価
        if   confidence >= 20: n = 5
        elif confidence >= 15: n = 4
        elif confidence >= 10: n = 3
        elif confidence >=  6: n = 2
        else:                   n = 1
    else:
        # chuana / ana は EV で評価
        if   confidence >= 2.5: n = 5
        elif confidence >= 1.8: n = 4
        elif confidence >= 1.3: n = 3
        elif confidence >= 1.0: n = 2
        else:                   n = 1
    filled  = "<span style='color:#f5a623'>★</span>" * n
    empty   = "<span style='color:#ccc'>★</span>" * (5 - n)
    return f"<span style='font-size:13px;letter-spacing:1px'>{filled}{empty}</span>"


def _recommend_card_html(rank, venue_name, race_no, combo_rows, actual=None, hit=None, scheduled_time=None, confidence=None, category=None):
    CAT_LABEL = {"honmei": "◎ 本命", "chuana": "△ 中穴", "ana": "☆ 穴"}
    cat_cls_map = {
        "honmei": "recommend-cat-badge recommend-cat-honmei",
        "chuana": "recommend-cat-badge recommend-cat-chuana",
        "ana":    "recommend-cat-badge recommend-cat-ana",
    }
    cat_label = CAT_LABEL.get(category, category or "")
    cat_cls   = cat_cls_map.get(category, "recommend-cat-badge")
    stars_html = _stars_html(confidence, category) if category else ""
    time_html  = f"<span style='font-size:10px;color:#9ca3af'>　🕐{_h(scheduled_time)}</span>" if scheduled_time else ""

    # 1番目の買い目を大きく表示
    top = combo_rows[0] if combo_rows else {}
    top_combo = top.get("combo", "")
    top_prob  = f"{top['prob']:.1f}%" if top.get("prob") else "─"
    top_ev    = top.get("ev") or top.get("prob")
    ev_str    = f"{top_ev:.2f}" if top_ev else "─"
    top_odds  = f"{top.get('expected_odds'):.0f}倍" if top.get("expected_odds") else "─"
    ev_color  = "#0f6e56" if (top_ev or 0) >= 1.0 else "#d33f49"

    is_hit_top = actual is not None and top_combo == actual
    combo_style = "color:#078760" if is_hit_top else ""

    if hit is None:
        result_html = ""
    elif hit == 1:
        result_html = f"<div style='font-size:12px;font-weight:500;color:#078760;margin-top:4px'>✓ 的中（{_h(actual)}）</div>"
    else:
        result_html = f"<div style='font-size:12px;font-weight:500;color:#d33f49;margin-top:4px'>✗ ハズレ（実際: {_h(actual)}）</div>"

    # 残りの買い目（小さく）
    sub_rows = ""
    for idx, combo in enumerate(combo_rows[1:], 2):
        cb  = combo.get("combo", "")
        p_s = f"{combo['prob']:.1f}%" if combo.get("prob") else "─"
        o_s = f"{combo.get('expected_odds'):.0f}倍" if combo.get("expected_odds") else "─"
        is_h = actual is not None and cb == actual
        cc   = "color:#078760;font-weight:500" if is_h else "color:#6b7280"
        sub_rows += (
            f"<div style='display:flex;align-items:center;gap:8px;margin-top:3px;font-size:11px'>"
            f"<span style='color:#9ca3af;min-width:14px'>{idx}</span>"
            f"<span style='letter-spacing:2px;{cc}'>{_h(cb)}</span>"
            f"<span style='color:#9ca3af'>{p_s}</span>"
            f"<span style='color:#9ca3af'>{o_s}</span>"
            f"</div>"
        )

    return (
        f"<div class='recommend-card'>"
        f"<div class='recommend-card-head'>"
        f"<div>"
        f"<div style='display:flex;align-items:center;gap:6px;margin-bottom:3px'>"
        f"<span class='{cat_cls}'>{cat_label}</span>"
        f"{stars_html}"
        f"</div>"
        f"<div class='recommend-venue-race'>{_h(rank)}位　{_h(venue_name)} {_h(race_no)}R{time_html}</div>"
        f"</div>"
        f"</div>"
        f"<div class='recommend-card-body'>"
        f"<div class='recommend-combo-big' style='{combo_style}'>{_h(top_combo)}</div>"
        f"<div class='recommend-stats-row'>"
        f"<div class='recommend-stat-item'>確率 <b>{top_prob}</b></div>"
        f"<div class='recommend-stat-item'>EV <b style='color:{ev_color}'>{ev_str}</b></div>"
        f"<div class='recommend-stat-item'>期待オッズ <b>{top_odds}</b></div>"
        f"</div>"
        f"{sub_rows}"
        f"{result_html}"
        f"</div>"
        f"</div>"
    )


# ─── Bets ─────────────────────────────────────────────────────────────────────
def ensure_bets():
    from db_connect import open_db as _open_db
    c = _open_db()
    c.execute("""CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
        venue_code TEXT NOT NULL, race_no INTEGER NOT NULL,
        combination TEXT NOT NULL, bet_type TEXT NOT NULL DEFAULT '3連単',
        amount INTEGER NOT NULL, result TEXT NOT NULL DEFAULT 'unknown',
        payout INTEGER NOT NULL DEFAULT 0, profit INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")
    c.commit(); c.close()


def check_bet_result(date, venue_code, race_no, combination, bet_type):
    with _conn() as c:
        n = c.execute("""SELECT COUNT(*) FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=?""",
            (date, venue_code, race_no)).fetchone()[0]
        if not n: return "unknown", 0
        pay = c.execute("""SELECT p.payout FROM payouts p
            JOIN races r ON r.id = p.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=? AND p.bet_type=? AND p.combination=?""",
            (date, venue_code, race_no, bet_type, combination)).fetchone()
    return ("win", pay["payout"]) if pay else ("lose", 0)


def add_bet(date, venue_code, race_no, combination, bet_type, amount):
    ensure_bets()
    result, payout = check_bet_result(date, venue_code, race_no, combination, bet_type)
    profit = (payout * amount // 100 - amount) if result == "win" else (-amount if result == "lose" else 0)
    with _db_write_lock("投票登録"):
        from db_connect import open_db as _open_db
        c = _open_db()
        c.execute("INSERT INTO bets (date,venue_code,race_no,combination,bet_type,amount,result,payout,profit,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (date, venue_code, race_no, combination, bet_type, amount, result, payout, profit,
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.commit(); c.close()


def refresh_bets():
    ensure_bets()
    with _db_write_lock("投票結果更新"):
        from db_connect import open_db as _open_db
        c = _open_db()
        rows = c.execute("SELECT id,date,venue_code,race_no,combination,bet_type,amount FROM bets WHERE result='unknown'").fetchall()
        n = 0
        for r in rows:
            res, payout = check_bet_result(r[1], r[2], r[3], r[4], r[5])
            if res != "unknown":
                profit = (payout * r[6] // 100 - r[6]) if res == "win" else -r[6]
                c.execute("UPDATE bets SET result=?,payout=?,profit=? WHERE id=?", (res, payout, profit, r[0]))
                n += 1
        c.commit(); c.close()
    return n


def get_bets():
    ensure_bets()
    with _conn() as c:
        rows = c.execute("""SELECT b.id, b.date, COALESCE(v.venue_name, b.venue_code) AS venue_name,
               b.race_no, b.combination, b.bet_type, b.amount, b.result, b.payout, b.profit, b.created_at
            FROM bets b LEFT JOIN venues v ON v.venue_code = b.venue_code
            ORDER BY b.date DESC, b.created_at DESC""").fetchall()
    cols = ["id","date","venue_name","race_no","combination","bet_type","amount","result","payout","profit","created_at"]
    return pd.DataFrame([dict(r) for r in rows], columns=cols) if rows else pd.DataFrame(columns=cols)


def delete_bet(bid):
    with _db_write_lock("投票削除"):
        from db_connect import open_db as _open_db
        c = _open_db()
        c.execute("DELETE FROM bets WHERE id=?", (bid,)); c.commit(); c.close()


# ─── Chart helpers ────────────────────────────────────────────────────────────
COURSE_COLORS = {1:"#64748b",2:"#111827",3:"#c5363d",4:"#1768c9",5:"#d78a00",6:"#089468"}
CATEGORY_COLORS = {
    "◎ 本命":"#d78a00","○ 準本命":"#1768c9","△ 中穴":"#089468",
    "☆ 大穴":"#7c3aed","▲ 参考":"#64748b","✕ 割高":"#374151","─":"#374151"
}


def score_chart(boats):
    boats_asc = sorted(boats, key=lambda b: b["score"])
    fig = go.Figure(go.Bar(
        x=[b["score"] for b in boats_asc],
        y=[f"{b['boat_no']}号  {b['player_name']}" for b in boats_asc],
        orientation="h",
        text=[f"{b['score']:.0f}pt" for b in boats_asc], textposition="outside",
        marker_color=[COURSE_COLORS.get(b["boat_no"], "#9e9e9e") for b in boats_asc],
        hovertemplate="<b>%{y}</b><br>スコア: %{x:.1f}<extra></extra>",
    ))
    fig.update_layout(
        xaxis=dict(range=[0, 135], title="スコア", gridcolor="rgba(128,128,128,0.15)"),
        yaxis=dict(title=""),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=13), height=260,
        margin=dict(l=0, r=10, t=10, b=10), showlegend=False,
    )
    return fig


def pnl_chart(df):
    df = df[df["result"] != "unknown"].sort_values(["date", "created_at"]).copy()
    if df.empty: return None
    df["cumulative"] = df["profit"].cumsum()
    fig = go.Figure()
    fig.add_trace(go.Bar(x=list(range(len(df))), y=df["profit"],
        marker_color=["#34d399" if p >= 0 else "#f87171" for p in df["profit"]],
        name="単発損益", hovertemplate="損益: %{y:+,}円<extra></extra>"))
    fig.add_trace(go.Scatter(x=list(range(len(df))), y=df["cumulative"],
        mode="lines+markers", line=dict(color="#1768c9", width=2),
        marker=dict(size=7, color="#1768c9"), name="累積損益",
        hovertemplate="累積: %{y:+,}円<extra></extra>"))
    fig.add_hline(y=0, line_dash="dash", line_color="rgba(128,128,128,0.4)", line_width=1)
    fig.update_layout(
        xaxis=dict(title="", showticklabels=False, gridcolor="rgba(128,128,128,0.15)"),
        yaxis=dict(title="損益（円）", gridcolor="rgba(128,128,128,0.15)"),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        height=300, margin=dict(l=0, r=0, t=10, b=10),
        legend=dict(orientation="h", y=1.05, x=0))
    return fig


# ─── 合成オッズ計算 ────────────────────────────────────────────────────────────
def _calc_combined_odds(detail: list, n: int) -> float | None:
    """合成オッズ = 1 / Σ(1/odds_i)
    総投資額に対する実効回収倍率。N点均等購入時、当選オッズ/N の加重期待値。
    """
    combos = [d for d in detail[:n] if (d.get("live_odds") or d.get("expected_odds") or 0) > 0]
    if not combos:
        return None
    total_inv = sum(1 / (d.get("live_odds") or d["expected_odds"]) for d in combos)
    return round(1 / total_inv, 1) if total_inv > 0 else None


def _combined_odds_html(detail: list, sizes: list[tuple[int, str]]) -> str:
    """合成オッズバンドを生成。sizes=[(5,'上位5点'), (10,'上位10点')]"""
    parts = []
    for n, label in sizes:
        co = _calc_combined_odds(detail, n)
        val = f"{co:.1f}倍" if co else "─"
        parts.append(
            f"<span style='margin-right:16px'>"
            f"<span style='font-size:0.75em;color:#6b7280'>{label}</span> "
            f"<b style='font-size:1.05em'>{val}</b></span>"
        )
    return (
        "<div style='background:#f1f5f9;border-radius:6px;padding:6px 12px;"
        "margin-bottom:8px;font-size:0.9em'>"
        "🔄 合成オッズ　" + "".join(parts) + "</div>"
    )


# ─── 推奨買い目カード ──────────────────────────────────────────────────────────
def _rec_card(d, idx, show_medal=True):
    cat   = d["category"]
    c_col = CATEGORY_COLORS.get(cat, "#6B7280")
    o_str = f"{d['expected_odds']:.1f}倍" if d["expected_odds"] else "─"
    ev_str = f"{d['ev']:.2f}" if d["ev"] else "─"
    ev_cls = "rec-ev-good" if (d["ev"] and d["ev"] >= 0.80) else "rec-ev-bad"
    live_badge = (
        "<span class='rec-live'>LIVE</span>"
    ) if d.get("live_odds") else ""
    rank_html = f"<span class='rec-rank'>{idx + 1}</span>" if show_medal else ""
    return (
        f"<div class='rec-card'>"
        f"<div class='rec-head'>"
        f"{rank_html}"
        f"<span class='rec-combo'>{_h(d['combo'])}</span>"
        f"<span class='rec-badge' style='background:{c_col}'>{_h(cat)}</span>"
        f"{live_badge}"
        f"</div>"
        f"<div class='rec-stats'>"
        f"<span>ODDS <b>{_h(o_str)}</b></span>"
        f"<span>PROB <b>{d['prob']:.1f}%</b></span>"
        f"<span>EV <b class='{ev_cls}'>{_h(ev_str)}</b></span>"
        f"</div>"
        f"</div>"
    )


def _race_card_html(race_no, title, status_cls, status_text, scheduled_time=None, sanrentan=None):
    """レース一覧用の高密度カードHTML"""
    time_html = f"<span class='race-time'>{_h(scheduled_time)}</span>" if scheduled_time else ""
    if sanrentan:
        payout_fmt = f"¥{sanrentan:,}"
        payout_html = f"<div class='race-payout'>3連単 {payout_fmt}</div>"
    else:
        payout_html = ""
    return (
        f"<div class='race-card'>"
        f"<div class='race-card-top'>"
        f"<span class='race-no'>{_h(race_no)}R</span>"
        f"{time_html}"
        f"<span class='status-badge {status_cls}'>{_h(status_text)}</span>"
        f"</div>"
        f"<div class='race-title'>{_h(title)}</div>"
        f"{payout_html}"
        f"</div>"
    )


# ─── 会場定数 ─────────────────────────────────────────────────────────────────
ALL_VENUES = [
    ("01","桐生"),("02","戸田"),("03","江戸川"),("04","平和島"),("05","多摩川"),("06","浜名湖"),
    ("07","蒲郡"),("08","常滑"),("09","津"),("10","三国"),("11","びわこ"),("12","住之江"),
    ("13","尼崎"),("14","鳴門"),("15","丸亀"),("16","児島"),("17","宮島"),("18","徳山"),
    ("19","下関"),("20","若松"),("21","芦屋"),("22","福岡"),("23","唐津"),("24","大村"),
]

# レース開催時間帯（ナイター/モーニング/デイ）
VENUE_RACE_TYPE = {
    "01": "night",   # 桐生   ナイター
    "02": "day",     # 戸田   デイ
    "03": "day",     # 江戸川 デイ
    "04": "day",     # 平和島 デイ
    "05": "day",     # 多摩川 デイ
    "06": "day",     # 浜名湖 デイ
    "07": "night",   # 蒲郡   ナイター
    "08": "day",     # 常滑   デイ
    "09": "day",     # 津     デイ
    "10": "morning", # 三国   モーニング
    "11": "day",     # びわこ デイ
    "12": "night",   # 住之江 ナイター
    "13": "day",     # 尼崎   デイ
    "14": "morning", # 鳴門   モーニング
    "15": "night",   # 丸亀   ナイター
    "16": "day",     # 児島   デイ
    "17": "day",     # 宮島   デイ
    "18": "morning", # 徳山   モーニング
    "19": "night",   # 下関   ナイター
    "20": "night",   # 若松   ナイター
    "21": "morning", # 芦屋   モーニング
    "22": "day",     # 福岡   デイ
    "23": "morning", # 唐津   モーニング
    "24": "night",   # 大村   ナイター
}
RACE_TYPE_ICON  = {"night": "🌙", "morning": "🌅", "day": ""}
RACE_TYPE_LABEL = {"night": "ナイター", "morning": "モーニング", "day": "デイ"}


def _venue_card_html(vn, vc, rc=0, res=0, status="none", selected=False, meet_day=None):
    """新デザイン会場カードHTML（プログレスバー付き）"""
    type_key = VENUE_RACE_TYPE.get(vc, "day")
    icon = RACE_TYPE_ICON.get(type_key, "")
    label = RACE_TYPE_LABEL.get(type_key, "デイ")

    if status == "none":
        return (
            f"<div class='vc-card vc-none'>"
            f"<div class='vc-head vc-none-head'>"
            f"<div class='vc-name-row'>"
            f"<span class='vc-name'>{_h(vn)}</span>"
            f"<span class='vc-status-badge vc-badge-none'>本日なし</span>"
            f"</div></div>"
            f"<div class='vc-none-dash'>─</div>"
            f"</div>"
        )

    if selected:
        card_cls = "vc-card vc-active"
    elif status == "ended":
        card_cls = "vc-card vc-ended"
    elif type_key == "night":
        card_cls = "vc-card vc-night"
    elif type_key == "morning":
        card_cls = "vc-card vc-morning"
    else:
        card_cls = "vc-card vc-active"

    if status == "ended":
        head_cls   = "vc-head vc-ended-head"
        badge_cls  = "vc-status-badge vc-badge-done"
        badge_text = "終了"
        fill_cls   = ""
    elif type_key == "night":
        head_cls   = "vc-head vc-night-head"
        badge_cls  = "vc-status-badge vc-badge-night"
        badge_text = f"{icon} ナイター"
        fill_cls   = "night"
    elif type_key == "morning":
        head_cls   = "vc-head vc-morning-head"
        badge_cls  = "vc-status-badge vc-badge-morning"
        badge_text = f"{icon} モーニング"
        fill_cls   = "morning"
    else:
        head_cls   = "vc-head"
        badge_cls  = "vc-status-badge vc-badge-live"
        badge_text = "進行中"
        fill_cls   = ""

    pct = int(res / rc * 100) if rc > 0 else 0
    meet_html = (
        f"<span style='font-size:9px;color:#9ca3af;margin-left:3px'>({meet_day}日目)</span>"
        if meet_day else ""
    )
    prog_html = (
        f"<div class='vc-prog'>"
        f"<div class='vc-prog-bar'><div class='vc-prog-fill {fill_cls}' style='width:{pct}%'></div></div>"
        f"<div class='vc-prog-text'>{res}/{rc}R</div>"
        f"</div>"
    ) if rc > 0 else ""

    return (
        f"<div class='{card_cls}'>"
        f"<div class='{head_cls}'>"
        f"<div class='vc-name-row'>"
        f"<span class='vc-name'>{_h(vn)}{meet_html}</span>"
        f"<span class='{badge_cls}'>{badge_text}</span>"
        f"</div></div>"
        f"{prog_html}"
        f"</div>"
    )


# ─── Page: Home ───────────────────────────────────────────────────────────────


@st.cache_data(ttl=60)
def get_payout_summary(date):
    """今日の払戻一覧（会場別・全レース）"""
    with _conn() as c:
        races = c.execute("""
            SELECT r.id, r.venue_code, r.race_no, r.scheduled_time,
                   COALESCE(v.venue_name, r.venue_code) as venue_name
            FROM races r
            LEFT JOIN venues v ON v.venue_code = r.venue_code
            WHERE r.date = ?
            ORDER BY r.venue_code, r.race_no
        """, (date,)).fetchall()
        if not races:
            return {}
        # JOIN を使用（libsql-experimental の IN句パラメータ制限を回避）
        payouts_raw = c.execute(
            "SELECT p.race_id, p.combination, p.payout FROM payouts p "
            "JOIN races r ON r.id = p.race_id "
            "WHERE p.bet_type='3連単' AND r.date=?", (date,)
        ).fetchall()
        payout_map = {r["race_id"]: {"combination": r["combination"], "payout": r["payout"]}
                      for r in payouts_raw}
        results_raw = c.execute(
            "SELECT rre.race_id, rre.boat_no, rre.rank FROM race_result_entries rre "
            "JOIN races r ON r.id = rre.race_id "
            "WHERE rre.rank <= 3 AND r.date=? ORDER BY rre.race_id, rre.rank", (date,)
        ).fetchall()
        results_map = {}
        for r in results_raw:
            results_map.setdefault(r["race_id"], {})[r["rank"]] = r["boat_no"]

        # おすすめレース照合用：(venue_code, race_no) → {category: combo} のマップ
        recs_raw = c.execute("""
            SELECT venue_code, race_no, category, combo
            FROM daily_recommendations
            WHERE date = ?
        """, (date,)).fetchall()
        # (vc, rno) → カテゴリ別combo辞書
        rec_map = {}
        for r in recs_raw:
            key = (r["venue_code"], r["race_no"])
            rec_map.setdefault(key, {})[r["category"]] = r["combo"]

    venue_order = [vc for vc, _ in ALL_VENUES]
    venues = {}
    for r in races:
        vc  = r["venue_code"]
        rno = r["race_no"]
        venues.setdefault(vc, {"venue_name": r["venue_name"], "races": []})
        rid   = r["id"]
        top3  = results_map.get(rid, {})
        pdata = payout_map.get(rid)
        recs  = rec_map.get((vc, rno), {})

        # 的中判定: おすすめの combo が払戻 combination と一致するか
        hit_combo = None
        if pdata and recs:
            actual_combo = pdata["combination"]
            for cat, combo in recs.items():
                if combo == actual_combo:
                    hit_combo = cat  # "honmei" / "chuana" / "ana"
                    break

        venues[vc]["races"].append({
            "race_no":        rno,
            "scheduled_time": r["scheduled_time"],
            "top3":           top3,
            "payout":         pdata,
            "has_result":     bool(top3.get(1)),
            "recs":           recs,       # {category: combo}
            "hit_combo":      hit_combo,  # 的中カテゴリ名 or None
        })
    return {vc: venues[vc] for vc in venue_order if vc in venues}


@st.cache_data(ttl=15)
def get_deadline_races(date):
    """締切順レース一覧（結果未確定のもの）"""
    with _conn() as c:
        rows = c.execute("""
            SELECT r.venue_code, r.race_no, r.race_title, r.scheduled_time,
                   v.venue_name,
                   EXISTS (
                       SELECT 1 FROM race_result_entries rre
                       WHERE rre.race_id = r.id AND rre.rank = 1
                   ) as has_result
            FROM races r
            LEFT JOIN venues v ON v.venue_code = r.venue_code
            WHERE r.date = ? AND r.scheduled_time IS NOT NULL
            ORDER BY r.scheduled_time, r.venue_code, r.race_no
        """, (date,)).fetchall()
    return [dict(r) for r in rows]


def show_home():
    date   = latest_date()
    venues = get_venues(date)
    venue_map = {v["venue_code"]: v for v in venues}

    _page_header(
        "BoatAI 競艇予想",
        "開催・締切・払戻をひとつの画面で確認できます。",
        "Dashboard",
        [
            f"{date[:4]}/{date[4:6]}/{date[6:8]}",
            f"開催 {len(venues)}会場",
            "🌙 ナイター",
            "🌅 モーニング",
        ],
    )

    # session_state で初期タブを制御（サイドバーから直接遷移可能）
    _HOME_TABS = ["🏟 開催一覧", "⏰ 締切順", "💴 払戻一覧"]
    if "home_tab" not in st.session_state:
        st.session_state.home_tab = _HOME_TABS[0]
    _active_tab = st.radio(
        "ホームタブ", _HOME_TABS, horizontal=True,
        key="home_tab", label_visibility="collapsed"
    )

    if _active_tab == "🏟 開催一覧":
        # 凡例
        legend_html = (
            "<div style='display:flex;gap:14px;margin-bottom:12px;flex-wrap:wrap'>"
            "<span style='display:flex;align-items:center;gap:5px;font-size:11px;color:#6b7280'>"
            "<span style='width:9px;height:9px;border-radius:50%;background:#3b82f6;display:inline-block'></span>デイ開催中</span>"
            "<span style='display:flex;align-items:center;gap:5px;font-size:11px;color:#6b7280'>"
            "<span style='width:9px;height:9px;border-radius:50%;background:#7c3aed;display:inline-block'></span>ナイター</span>"
            "<span style='display:flex;align-items:center;gap:5px;font-size:11px;color:#6b7280'>"
            "<span style='width:9px;height:9px;border-radius:50%;background:#d97706;display:inline-block'></span>モーニング</span>"
            "<span style='display:flex;align-items:center;gap:5px;font-size:11px;color:#6b7280'>"
            "<span style='width:9px;height:9px;border-radius:50%;background:#9ca3af;border:1px dashed #9ca3af;display:inline-block'></span>本日なし</span>"
            "</div>"
        )
        st.markdown(legend_html, unsafe_allow_html=True)

        for row_start in range(0, len(ALL_VENUES), 4):
            cols = st.columns(4)
            for i, (vc, vn) in enumerate(ALL_VENUES[row_start:row_start + 4]):
                v = venue_map.get(vc)
                with cols[i]:
                    if v is None:
                        st.markdown(_venue_card_html(vn, vc, status="none"), unsafe_allow_html=True)
                    else:
                        rc, res, bi = v["race_count"], v["result_count"], v["bi_count"]
                        all_done = res >= rc
                        status = "ended" if all_done else "active"
                        st.markdown(_venue_card_html(vn, vc, rc, res, status, meet_day=v.get("meet_day")), unsafe_allow_html=True)
                        if not all_done:
                            c1, c2 = st.columns(2)
                            if c1.button("一覧", key=f"vl_{vc}", use_container_width=True):
                                nav("races", venue_code=vc, venue_name=vn)
                            if c2.button("一気見", key=f"vo_{vc}", use_container_width=True):
                                nav("overview", venue_code=vc, venue_name=vn)

    elif _active_tab == "⏰ 締切順":
        dl_races = get_deadline_races(date)
        now_dt   = datetime.now(_JST)   # Streamlit Cloud はUTCのため JST を明示
        now_time = now_dt.strftime("%H:%M")
        now_5min = (now_dt + timedelta(minutes=5)).strftime("%H:%M")

        # 更新ボタン（Streamlit は自動再実行しないため手動更新が必要）
        _ref_col, _time_col = st.columns([1, 6])
        with _ref_col:
            if st.button("🔄 更新", key="dl_refresh"):
                st.cache_data.clear()   # 全キャッシュをリセット（結果・予想含む）
                st.rerun()
        with _time_col:
            st.caption(f"最終確認: {now_time}　🟠直前5分以内  🔴締切済")

        # 推奨買い目マップ (venue_code, race_no) -> {"honmei": combo, ...}
        with _conn() as _dc:
            _recs_raw = _dc.execute(
                "SELECT venue_code, race_no, category, combo FROM daily_recommendations WHERE date=?",
                (date,)
            ).fetchall()
        _dl_rec_map = {}
        for _rr in _recs_raw:
            _dl_rec_map.setdefault((_rr["venue_code"], _rr["race_no"]), {})[_rr["category"]] = _rr["combo"]

        if not dl_races:
            st.info("締切時刻データがありません。")
        else:
            pending = [r for r in dl_races if not r["has_result"]]
            done    = [r for r in dl_races if r["has_result"]]
            st.caption(f"未終了: {len(pending)}R　終了済み: {len(done)}R")
            venue_name_map = dict(ALL_VENUES)

            for r in pending:
                t         = r["scheduled_time"] or ""
                vn        = r["venue_name"] or venue_name_map.get(r["venue_code"], r["venue_code"])
                rno       = r["race_no"]
                raw_title = r["race_title"] or ""
                # 「4R」のように番号そのままのタイトルは重複するため非表示
                title     = raw_title if raw_title and raw_title != f"{rno}R" else ""
                vc        = r["venue_code"]

                if t and t <= now_time:
                    urgency = "soon"
                    time_disp = f"{t} 締切"
                elif t and t <= now_5min:
                    urgency = "now"
                    time_disp = f"{t} 直前"
                else:
                    urgency = "normal"
                    time_disp = t

                # 推奨買い目・カテゴリ取得（honmei優先）
                _rec_info = _dl_rec_map.get((vc, rno), {})
                _rec_cat  = next((c for c in ("honmei", "chuana", "ana") if c in _rec_info), None)
                _rec_combo = _rec_info.get(_rec_cat) if _rec_cat else None

                row_col, btn_col = st.columns([10, 2])
                with row_col:
                    st.markdown(
                        _deadline_row_html(vn, rno, title, time_disp, urgency,
                                           rec_combo=_rec_combo, rec_cat=_rec_cat),
                        unsafe_allow_html=True
                    )
                with btn_col:
                    st.markdown("<div style='margin-top:3px'></div>", unsafe_allow_html=True)
                    if st.button("詳細 →", key=f"dl_{vc}_{rno}", use_container_width=True):
                        nav("detail", venue_code=vc, venue_name=vn, race_no=rno)

    elif _active_tab == "💴 払戻一覧":
        payout_data = get_payout_summary(date)
        if not payout_data:
            st.info("本日の払戻データがまだありません。")
        else:
            venue_items = list(payout_data.items())
            for row_start in range(0, len(venue_items), 2):
                pair = venue_items[row_start:row_start + 2]
                vc_cols = st.columns(len(pair))
                for ci, (vc, vdata) in enumerate(pair):
                    with vc_cols[ci]:
                        vn          = vdata["venue_name"]
                        races_done  = sum(1 for r in vdata["races"] if r["has_result"])
                        races_total = len(vdata["races"])

                        # 会場ヘッダー
                        st.markdown(
                            f"<div class='payout-head'>"
                            f"{vn}　<span>"
                            f"一般　{races_done}/{races_total}R完了</span></div>",
                            unsafe_allow_html=True
                        )

                        # レース行一覧（完了=金額ボタン、未完了=時刻テキスト）
                        CAT_BADGE = {"honmei": "◎", "chuana": "△", "ana": "☆"}
                        for race in vdata["races"]:
                            rno      = race["race_no"]
                            pdata    = race["payout"]
                            recs     = race.get("recs", {})
                            hit_cat  = race.get("hit_combo")
                            # おすすめバッジ（レース番号の後ろに表示）
                            rec_badge = ""
                            if recs:
                                cats = [CAT_BADGE.get(c, "") for c in ("honmei", "chuana", "ana") if c in recs]
                                rec_badge = "".join(cats)
                            if race["has_result"] and pdata:
                                boats  = pdata["combination"].split("-")
                                waku_h = "".join(_waku(int(b)) for b in boats if b.isdigit())
                                amt    = pdata["payout"]
                                # 的中マーク
                                hit_mark = ""
                                if hit_cat:
                                    hit_mark = (
                                        f"<span class='ba-pill ba-pill-strong'>的中 {CAT_BADGE.get(hit_cat,'')}</span>"
                                    )
                                info_col, amt_col = st.columns([6, 4])
                                with info_col:
                                    badge_html = (
                                        f"<span class='ba-pill'>{rec_badge}</span>"
                                    ) if rec_badge else ""
                                    st.markdown(
                                        f"<div class='payout-line-row'>"
                                        f"<span class='payout-race-no'>{rno}R</span>{badge_html}"
                                        f"<span class='payout-boats'>{waku_h}</span>"
                                        f"{hit_mark}"
                                        f"</div>",
                                        unsafe_allow_html=True
                                    )
                                with amt_col:
                                    if st.button(f"{amt:,}円 →", key=f"pay_{vc}_{rno}",
                                                 use_container_width=True):
                                        nav("detail", venue_code=vc, venue_name=vn, race_no=rno)
                            elif race["has_result"]:
                                badge_html = (
                                    f"<span class='ba-pill'>{rec_badge}</span>"
                                ) if rec_badge else ""
                                st.markdown(
                                    f"<div class='payout-line-row'>"
                                    f"<span class='payout-race-no'>{rno}R</span>{badge_html}"
                                    f"<span class='payout-empty'>─</span></div>",
                                    unsafe_allow_html=True
                                )
                            else:
                                stime = race["scheduled_time"] or "─"
                                badge_html = (
                                    f"<span class='ba-pill'>{rec_badge}</span>"
                                ) if rec_badge else ""
                                st.markdown(
                                    f"<div class='payout-line-row'>"
                                    f"<span class='payout-race-no'>{rno}R</span>{badge_html}"
                                    f"<span class='payout-time'>{stime}</span>"
                                    f"</div>",
                                    unsafe_allow_html=True
                                )
                        st.markdown("")


# ─── Page: Race list ──────────────────────────────────────────────────────────
def show_races():
    date  = latest_date()
    vc    = st.session_state.venue_code
    vn    = st.session_state.venue_name or vc
    races = get_races(date, vc)

    top_c1, top_c2, top_c3 = st.columns([4, 2, 2])
    with top_c1:
        if st.button("← トップに戻る"): nav("home")
    with top_c2:
        if st.button("📋 一気見モード", use_container_width=True):
            nav("overview", venue_code=vc, venue_name=vn)
    with top_c3:
        if st.button("⚡ 直前情報を今すぐ取得", type="primary", use_container_width=True):
            with st.spinner("直前情報・オッズを取得中..."):
                try:
                    result = _run_db_writer(
                        [sys.executable, "refresh_before.py"],
                        "直前情報更新",
                        timeout=180,
                    )
                    st.cache_data.clear()
                    st.success("✅ 更新しました")
                except Exception as e:
                    st.error(f"エラー: {e}")

    _meet_day = get_meet_day(date, vc)
    _page_header(
        f"{vn} レース一覧",
        "予想したいレースを選択してください。",
        "Venue",
        [f"{date[:4]}/{date[4:6]}/{date[6:8]}", f"開催{_meet_day}日目", f"{len(races)}R"],
    )
    if not races:
        st.warning("レースデータが見つかりません。")
        return
    for row_start in range(0, len(races), 4):
        cols = st.columns(4)
        for i, r in enumerate(races[row_start:row_start + 4]):
            if r["has_result"]:
                status_cls, badge_text = "status-done", "確定済み"
            elif r["has_bi"]:
                status_cls, badge_text = "status-live", "直前情報"
            else:
                status_cls, badge_text = "status-ready", "予想可"
            with cols[i]:
                st.markdown(_race_card_html(r["race_no"], r["race_title"], status_cls, badge_text, r.get("scheduled_time"), r.get("sanrentan")), unsafe_allow_html=True)
                if st.button("予想を見る", key=f"r_{r['race_no']}", use_container_width=True):
                    nav("detail", race_no=r["race_no"])


# ─── Page: Venue overview（一気見） ───────────────────────────────────────────
def show_overview():
    date = latest_date()
    vc   = st.session_state.venue_code
    vn   = st.session_state.venue_name or vc

    c1, c2 = st.columns([6, 2])
    with c1:
        if st.button("← レース一覧"): nav("races")
    with c2:
        if st.button("⚡ 直前情報を今すぐ取得", type="primary", use_container_width=True):
            with st.spinner("取得中..."):
                try:
                    _run_db_writer(
                        [sys.executable, "refresh_before.py"],
                        "直前情報更新",
                        timeout=180,
                    )
                    st.cache_data.clear()
                    st.success("✅ 更新しました")
                except Exception as e:
                    st.error(f"エラー: {e}")

    _meet_day_ov = get_meet_day(date, vc)
    _page_header(
        f"{vn} 全レース一気見",
        "勝率と推奨買い目をレース横断でざっと確認できます。",
        "Overview",
        [f"{date[:4]}/{date[4:6]}/{date[6:8]}", f"開催{_meet_day_ov}日目"],
    )

    races = get_races(date, vc)
    if not races:
        st.warning("レースデータが見つかりません。")
        return

    for r in races:
        race_no = r["race_no"]
        status  = "✅ 確定" if r["has_result"] else "⚡ 直前" if r["has_bi"] else "📋 予想可"

        time_label = f"　🕐{r['scheduled_time']}" if r.get("scheduled_time") else ""
        with st.expander(f"**{race_no}R**　{r['race_title']}{time_label}　{status}", expanded=True):
            pred, err = get_prediction(date, vc, race_no, st.session_state.get("model_mode", "XGBoost ML"))
            if err:
                st.error(f"予想エラー: {err}")
                continue

            boats   = pred["boats"]
            rec_det = pred.get("recommended_3t_detail", [])

            col_boats, col_recs = st.columns([5, 5])

            with col_boats:
                st.markdown("<p class='sec-label'>艇別 勝率</p>", unsafe_allow_html=True)
                for b in sorted(boats, key=lambda x: -x["win_prob"]):
                    st.markdown(
                        _boat_prob_row(b, COURSE_COLORS.get(b["boat_no"], "#1768c9")),
                        unsafe_allow_html=True
                    )

            with col_recs:
                st.markdown("<p class='sec-label'>推奨買い目（EV上位）</p>", unsafe_allow_html=True)
                top5 = rec_det[:5] if rec_det else []
                if top5:
                    for idx, d in enumerate(top5):
                        st.markdown(_rec_card(d, idx), unsafe_allow_html=True)
                else:
                    st.caption("推奨データなし")

            result = get_result(date, vc, race_no)
            if result:
                top3 = [r2 for r2 in result["entries"] if r2["rank"] <= 3]
                sanrentan = result.get("sanrentan")
                if len(top3) == 3:
                    actual = "-".join(str(r2["boat_no"]) for r2 in top3)
                    payout_str = f"　💰¥{sanrentan:,}" if sanrentan else ""
                    # カテゴリ別買い目も含めて的中チェック（詳細ページと一貫性を保つ）
                    all_pred = (
                        {d["combo"] for d in rec_det}
                        | {d["combo"] for d in pred.get("honmei_detail", [])}
                        | {d["combo"] for d in pred.get("chuana_detail", [])}
                        | {d["combo"] for d in pred.get("ana_detail", [])}
                        | set(pred.get("recommended_3t", []))
                    )
                    if actual in all_pred:
                        st.success(f"🎯 確定: **{actual}**　推奨買い目に的中！{payout_str}")
                    else:
                        st.info(f"確定: **{actual}**{payout_str}")

            if st.button(f"詳細を見る →", key=f"ov_detail_{race_no}"):
                nav("detail", race_no=race_no)

            st.divider()


# ─── Page: Race detail ────────────────────────────────────────────────────────
def show_detail():
    date    = latest_date()
    vc      = st.session_state.venue_code
    vn      = st.session_state.venue_name or vc
    race_no = st.session_state.race_no

    col_back, col_ov, col_odds = st.columns([3, 3, 3])
    with col_back:
        if st.button("← レース一覧"): nav("races")
    with col_ov:
        if st.button("📋 一気見モードで見る"):
            nav("overview")
    with col_odds:
        if st.button("📊 オッズ一覧", use_container_width=True):
            nav("odds")

    _meet_day_d = get_meet_day(date, vc)
    _page_header(
        f"{vn} {race_no}R",
        "スコア、勝率、買い目、直前情報をまとめて確認できます。",
        "Race Detail",
        [f"{date[:4]}/{date[4:6]}/{date[6:8]}", f"開催{_meet_day_d}日目", "予想・推奨"],
    )
    # ヘッダーを先行表示してから予想取得（前ページの残像を防ぐ）
    with st.spinner("予想データ読み込み中..."):
        pred, err = get_prediction(date, vc, race_no, st.session_state.get("model_mode", "XGBoost ML"))
    if err:
        st.error(f"予想データ取得エラー: {err}")
        return

    ri       = pred["race_info"]
    boats    = pred["boats"]
    rec      = pred["recommended_3t"]
    rec_det  = pred.get("recommended_3t_detail", [])
    ev_det   = pred.get("ev_recs_detail", [])

    with _conn() as _c:
        _stime_row = _c.execute(
            "SELECT scheduled_time FROM races WHERE date=? AND venue_code=? AND race_no=?",
            (date, vc, race_no)
        ).fetchone()
    _stime = _stime_row[0] if _stime_row and _stime_row[0] else ""
    _stime_str = f"　🕐{_stime}" if _stime else ""
    st.caption(f"{ri['race_title'] or ''}{_stime_str}")

    tab_pred, tab_ev, tab_entry = st.tabs(["📊 予想・推奨", "💡 EV分析", "📋 出走表・枠別成績"])

    # ── タブ1: 予想・推奨 ────────────────────────────────────────────────────
    with tab_pred:
        left, right = st.columns([5, 5])

        with left:
            st.subheader("艇別スコア")
            st.plotly_chart(score_chart(boats), use_container_width=True)

            st.subheader("勝率予測")
            for b in sorted(boats, key=lambda x: -x["win_prob"]):
                c_comp = b["components"]
                tansho = b.get("tansho_odds")
                form   = c_comp.get("form_score", 0.5)
                form_icon = "🔥" if form >= 0.75 else "📈" if form >= 0.55 else "📉" if form < 0.35 else ""
                tansho_str = f"　単勝 {tansho:.1f}倍" if tansho else ""
                bar_color = COURSE_COLORS.get(b["boat_no"], "#9e9e9e")
                st.markdown(
                    _boat_prob_row(
                        b,
                        bar_color,
                        f"score {b['score']:.0f}{tansho_str} {form_icon}".strip(),
                    ),
                    unsafe_allow_html=True
                )

            st.subheader("3連対率（3着以内）")
            for b in sorted(boats, key=lambda x: -(x.get("top3_prob") or 0)):
                bar_color = COURSE_COLORS.get(b["boat_no"], "#9e9e9e")
                st.markdown(_boat_top3_row(b, bar_color), unsafe_allow_html=True)

        with right:
            calib = pred.get("venue_calib", 1.0)
            calib_note = f"　会場補正: {calib:.3f}" if abs(calib - 1.0) > 0.01 else ""
            st.subheader(f"推奨買い目{calib_note}")
            st.caption("展開予測・オッズブレンド適用済み　ライブオッズでパターン分類")

            honmei_det = pred.get("honmei_detail", [])
            chuana_det = pred.get("chuana_detail", [])
            ana_det    = pred.get("ana_detail", [])

            tab_hm, tab_cu, tab_an = st.tabs([
                f"🔵 本命 ≤25倍 ({len(honmei_det)}点)",
                f"🟡 中穴 25〜80倍 ({len(chuana_det)}点)",
                f"🔴 穴 >80倍 ({len(ana_det)}点)",
            ])

            def _det_conf(detail, cat):
                if not detail: return None
                top = detail[0]
                return top.get("prob") if cat == "honmei" else (top.get("ev") or top.get("prob"))

            with tab_hm:
                conf_hm = _det_conf(honmei_det, "honmei")
                st.markdown(f"自信度　{_stars_html(conf_hm, 'honmei')}", unsafe_allow_html=True)
                if honmei_det:
                    st.markdown(
                        _combined_odds_html(honmei_det, [(5, "上位5点"), (10, "上位10点")]),
                        unsafe_allow_html=True
                    )
                    for idx, d in enumerate(honmei_det):
                        st.markdown(_rec_card(d, idx), unsafe_allow_html=True)
                else:
                    st.caption("オッズ25倍以下の組み合わせなし")

            with tab_cu:
                conf_cu = _det_conf(chuana_det, "chuana")
                st.markdown(f"自信度　{_stars_html(conf_cu, 'chuana')}", unsafe_allow_html=True)
                if chuana_det:
                    st.markdown(
                        _combined_odds_html(chuana_det, [(10, "上位10点"), (15, "上位15点")]),
                        unsafe_allow_html=True
                    )
                    for idx, d in enumerate(chuana_det):
                        st.markdown(_rec_card(d, idx), unsafe_allow_html=True)
                else:
                    st.caption("該当なし")

            with tab_an:
                conf_an = _det_conf(ana_det, "ana")
                st.markdown(f"自信度　{_stars_html(conf_an, 'ana')}", unsafe_allow_html=True)
                if ana_det:
                    st.markdown(
                        _combined_odds_html(ana_det, [(10, "上位10点"), (15, "上位15点")]),
                        unsafe_allow_html=True
                    )
                    for idx, d in enumerate(ana_det):
                        st.markdown(_rec_card(d, idx), unsafe_allow_html=True)
                else:
                    st.caption("該当なし")

            # 確定結果
            result = get_result(date, vc, race_no)
            if result:
                st.divider()
                st.subheader("確定結果")
                st.markdown(_result_html(result["entries"]), unsafe_allow_html=True)
                top3 = [r for r in result["entries"] if r["rank"] <= 3]
                sanrentan = result.get("sanrentan")
                if len(top3) == 3:
                    actual = "-".join(str(r["boat_no"]) for r in top3)
                    st.markdown(
                        _result_combo_card(actual, sanrentan),
                        unsafe_allow_html=True
                    )
                    hit_key = f"balloons_{date}_{vc}_{race_no}"
                    # 3パターン全体の推奨に含まれるか確認
                    all_pred = (
                        {d["combo"] for d in honmei_det}
                        | {d["combo"] for d in chuana_det}
                        | {d["combo"] for d in ana_det}
                        | {d["combo"] for d in rec_det}
                    )
                    if actual in all_pred:
                        if not st.session_state.get(hit_key):
                            st.balloons()
                            st.session_state[hit_key] = True
                        # どのパターンで当たったか
                        hm_hit = actual in {d["combo"] for d in honmei_det}
                        cu_hit = actual in {d["combo"] for d in chuana_det}
                        an_hit = actual in {d["combo"] for d in ana_det}
                        patt = ("本命" if hm_hit else "中穴" if cu_hit else "穴")
                        st.success(f"🎯 **{patt}**パターンに的中！")
                    else:
                        st.info(f"推奨範囲外（実際: {actual}）")

    # ── タブ2: EV分析 ───────────────────────────────────────────────────────
    with tab_ev:
        st.subheader("💡 期待値（EV）分析")
        st.caption("EV > 1.0 → 理論上プラス収支　　EV 0.75〜1.0 → 普通　　EV < 0.75 → 割高")

        if ev_det:
            ev_rows = []
            for d in ev_det:
                ev_rows.append({
                    "買い目": d["combo"],
                    "カテゴリ": d["category"],
                    "確率": f"{d['prob']:.1f}%",
                    "想定オッズ": f"{d['expected_odds']:.1f}倍" if d["expected_odds"] else "─",
                    "LIVE": "✓" if d.get("live_odds") else "─",
                    "EV": d["ev"] or 0,
                })
            ev_df = pd.DataFrame(ev_rows)

            def ev_color(val):
                if isinstance(val, float):
                    if val >= 1.0:  return "background-color: #dcfce7; color: #166534"
                    if val >= 0.80: return "background-color: #dbeafe; color: #1e40af"
                    if val >= 0.75: return ""
                    return "background-color: #fee2e2; color: #991b1b"
                return ""

            st.dataframe(
                ev_df.style.map(ev_color, subset=["EV"]),
                use_container_width=True, hide_index=True
            )

            combos = [d["combo"] for d in ev_det]
            evs    = [d["ev"] or 0 for d in ev_det]
            colors = ["#34d399" if e >= 1.0 else "#60a5fa" if e >= 0.80 else "#f87171" for e in evs]
            fig = go.Figure(go.Bar(
                x=combos, y=evs, marker_color=colors,
                text=[f"{e:.2f}" for e in evs], textposition="outside",
                hovertemplate="<b>%{x}</b><br>EV: %{y:.3f}<extra></extra>",
            ))
            fig.add_hline(y=1.0, line_dash="dash", line_color="#059669",
                          annotation_text="EV=1.0", annotation_position="top right")
            fig.add_hline(y=0.75, line_dash="dot", line_color="#9ca3af",
                          annotation_text="EV=0.75")
            fig.update_layout(
                xaxis_title="買い目", yaxis_title="期待値（EV）",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                height=320, margin=dict(l=0, r=0, t=30, b=0),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("EV推奨データが取得できませんでした。")

        # ── オッズ歪み自動検出 ────────────────────────────────────────────────
        st.markdown("---")
        st.subheader("🔍 オッズ歪み検出")
        st.caption("モデルが高く評価しているのに市場オッズが高い（＝市場が過小評価している）買い目を検出します。")

        with _conn() as _c_ev:
            _race_id_ev = _c_ev.execute(
                "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
                (date, vc, race_no)
            ).fetchone()

        if _race_id_ev:
            _rid_ev = _race_id_ev[0]

            try:
                with _conn() as c:
                    anomalies = _analysis.get_odds_anomalies(c, _rid_ev, min_gap=3.0)
            except Exception:
                anomalies = []

            if not anomalies:
                st.info("predictionsデータがないか、歪みが検出されませんでした。（backtestデータ蓄積後に有効になります）")
            else:
                for a in anomalies:
                    gap_color = "#078760" if a["gap"] >= 10 else ("#0f68d9" if a["gap"] >= 5 else "#6b7280")
                    st.markdown(
                        f"<div style='background:#fff;border:0.5px solid #d8e3ed;"
                        f"border-radius:10px;padding:12px 14px;margin-bottom:8px'>"
                        f"<div style='display:flex;align-items:center;gap:10px'>"
                        f"<span style='font-size:22px;font-weight:700;letter-spacing:3px'>"
                        f"{a['combo']}</span>"
                        f"<span style='font-size:11px;background:#f1f5f9;padding:2px 8px;"
                        f"border-radius:999px;color:#6b7280'>モデル #{a['model_rank']}</span>"
                        f"<span style='font-size:11px;font-weight:700;color:{gap_color};"
                        f"margin-left:auto'>+{a['gap']:.1f}pt 過小評価</span>"
                        f"</div>"
                        f"<div style='display:flex;gap:16px;font-size:12px;color:#6b7280;"
                        f"margin-top:6px'>"
                        f"<span>オッズ <b style='color:#374151'>{a['odds']:.1f}倍</b></span>"
                        f"<span>市場確率 <b style='color:#374151'>{a['market_prob']:.1f}%</b></span>"
                        f"<span>モデル確率 <b style='color:{gap_color}'>{a['model_prob']:.1f}%</b></span>"
                        f"</div></div>",
                        unsafe_allow_html=True
                    )
        else:
            st.info("レースデータが見つかりません。")

    # ── タブ3: 出走表・直前情報 ───────────────────────────────────────────────
    with tab_entry:
        with st.spinner("データ読み込み中..."):
          with _conn() as conn2:
            entry_details = {r["boat_no"]: dict(r) for r in conn2.execute("""
                SELECT boat_no, player_no, player_class, age,
                       flying_count, late_count, avg_start_timing,
                       national_win_rate, national_2ring_rate,
                       local_win_rate, local_2ring_rate,
                       motor_no, motor_2ring_rate, boat_no_hull, boat_2ring_rate
                FROM entries WHERE race_id=(
                    SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?
                ) ORDER BY boat_no
            """, (date, vc, race_no)).fetchall()}

            boats_for_cs = {b["boat_no"]: (b["components"].get("player_no") or
                            entry_details.get(b["boat_no"], {}).get("player_no"), b["start_course"])
                            for b in boats}

            # ── course_stats: 全選手分を1クエリで取得 ──────────────────────
            pno_cs_pairs = [(pno, cs) for bn, (pno, cs) in boats_for_cs.items() if pno and cs is not None]
            cs_rows = {}
            if pno_cs_pairs:
                where_cs = " OR ".join(["(player_no=? AND course_no=?)"] * len(pno_cs_pairs))
                params_cs = [x for pair in pno_cs_pairs for x in pair]
                cs_latest: dict = {}
                for r in conn2.execute(
                    f"SELECT player_no, course_no, win_rate_1st, win_rate_2nd, win_rate_3rd,"
                    f" entry_rate, avg_st, fetched_date FROM course_stats WHERE {where_cs}",
                    params_cs
                ).fetchall():
                    key = (r["player_no"], r["course_no"])
                    if key not in cs_latest or (r["fetched_date"] or "") > (cs_latest[key]["fetched_date"] or ""):
                        cs_latest[key] = dict(r)
                for bn, (pno, cs) in boats_for_cs.items():
                    cs_rows[bn] = cs_latest.get((pno, cs), {})

            # ── race_result_entries: 全選手分を1クエリで取得してPython集計 ──
            # 6艇×4クエリ(24 HTTP)→ 1クエリに削減
            from collections import Counter as _Counter
            lose_data:    dict = {}
            trick_detail: dict = {}
            all_pnos = [pno for pno, _ in boats_for_cs.values() if pno]
            if all_pnos:
                pno_ph = ",".join(["?"] * len(all_pnos))
                rre_raw = conn2.execute(f"""
                    SELECT me.player_no, me.boat_no, me.rank, me.winning_trick,
                           w.winning_trick AS winner_trick, w.start_course AS winner_course
                    FROM race_result_entries me
                    JOIN race_result_entries w ON w.race_id = me.race_id AND w.rank = 1
                    WHERE me.player_no IN ({pno_ph})
                """, all_pnos).fetchall()

                # player_no → 行リスト
                from collections import defaultdict as _dd
                player_rows: dict = _dd(list)
                for r in rre_raw:
                    player_rows[r["player_no"]].append(r)

                for bn, (pno, _) in boats_for_cs.items():
                    if not pno:
                        continue
                    rows_p = player_rows.get(pno, [])

                    # lose_data
                    loses_p = [r for r in rows_p if r["rank"] != 1]
                    total_ld = len(loses_p)
                    if total_ld > 0:
                        tm = _Counter(r["winner_trick"] for r in loses_p)
                        lose_data[bn] = {
                            "total":             total_ld,
                            "nige_lose":         round((tm.get("逃げ", 0) + tm.get("抜き", 0)) / total_ld * 100, 1),
                            "sashi_lose":        round(tm.get("差し", 0) / total_ld * 100, 1),
                            "makuri_lose":       round(tm.get("まくり", 0) / total_ld * 100, 1),
                            "makuri_sashi_lose": round(tm.get("まくり差し", 0) / total_ld * 100, 1),
                        }

                    # trick_detail
                    wins_all_p  = [r for r in rows_p if r["rank"] == 1]
                    rows_bn     = [r for r in rows_p if r["boat_no"] == bn]
                    wins_bn     = [r for r in rows_bn if r["rank"] == 1]
                    loses_bn    = [r for r in rows_bn if r["rank"] != 1]
                    wa_cnt = _Counter(r["winning_trick"] for r in wins_all_p)
                    wb_cnt = _Counter(r["winning_trick"] for r in wins_bn)
                    lb_cnt = _Counter((r["winner_course"], r["winner_trick"]) for r in loses_bn)
                    lb_top = sorted(lb_cnt.items(), key=lambda x: -x[1])[:12]
                    trick_detail[bn] = {
                        "pno":              pno,
                        "win_all":          sorted(wa_cnt.items(), key=lambda x: -x[1]),
                        "total_wins_all":   len(wins_all_p),
                        "runs_boat":        len(rows_bn),
                        "win_boat":         sorted(wb_cnt.items(), key=lambda x: -x[1]),
                        "total_wins_boat":  len(wins_bn),
                        "lose_boat":        [(sc, wt, cnt) for (sc, wt), cnt in lb_top],
                        "total_loses_boat": len(loses_bn),
                    }

            # ── before_info: 1クエリ ─────────────────────────────────────
            prev_info = {r["boat_no"]: dict(r) for r in conn2.execute("""
                SELECT b.boat_no, b.exhibition_time, b.tilt,
                       b.exhibit_course, b.exhibit_st,
                       b.prev_race_venue, b.prev_race_date, b.prev_entry_course,
                       b.prev_start_timing, b.prev_finish,
                       b.lap_time, b.mawariashi_time, b.straight_time
                FROM before_info b
                WHERE b.id IN (
                    SELECT MAX(id) FROM before_info
                    WHERE race_id=(
                        SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?
                    )
                    GROUP BY boat_no
                )
                ORDER BY b.boat_no
            """, (date, vc, race_no)).fetchall()}

        sub1, sub2, sub3, sub4, sub5, sub6 = st.tabs(["📋 出走表（フル）", "🎯 枠別成績", "🔄 前走・直前情報", "⚙️ モーター情報", "👥 選手相性", "🎯 出目パターン"])

        with sub1:
            rows = []
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                c  = b["components"]
                ed = entry_details.get(b["boat_no"], {})
                rows.append({
                    "艇":      b["boat_no"],
                    "CS":      b["start_course"],
                    "選手名":  b["player_name"],
                    "支部":    b.get("branch") or ed.get("branch") or "─",
                    "階級":    ed.get("player_class", "─"),
                    "年齢":    ed.get("age", "─"),
                    "F数":     ed.get("flying_count", 0),
                    "全国WR":  f"{c['national_win_rate']:.1f}%",
                    "全国2連": f"{ed.get('national_2ring_rate', 0):.1f}%",
                    "当地WR":  f"{c['local_win_rate']:.1f}%",
                    "当地2連": f"{ed.get('local_2ring_rate', 0):.1f}%",
                    "M№":       ed.get("motor_no", "─"),
                    "モーター2連": f"{c['motor_2ring']:.1f}%",
                    "ボート2連":  f"{ed.get('boat_2ring_rate', 0):.1f}%",
                    "チルト":   f"{c['tilt']:.1f}" if c.get("tilt") is not None else "─",
                    "展示T":    f"{c['exhibition_time']:.2f}" if c.get("exhibition_time") else "─",
                    "展示ST":   f"{c['start_timing']:.2f}" if c["start_timing"] is not None else "─",
                    "単勝":    f"{b['tansho_odds']:.1f}倍" if b.get("tansho_odds") else "─",
                    "スコア":  b["score"],
                    "勝率":    f"{b['win_prob']:.1f}%",
                })
            entry_df = pd.DataFrame(rows)
            st.dataframe(
                entry_df.style.background_gradient(subset=["スコア"], cmap="Blues"),
                use_container_width=True, hide_index=True,
                column_config={"スコア": st.column_config.NumberColumn(format="%.0f")}
            )

            trick_rows = []
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                c_comp = b["components"]
                ta = c_comp.get("trick_aptitude", 0)
                ed = entry_details.get(b["boat_no"], {})
                trick_rows.append({
                    "艇":           b["boat_no"],
                    "CS":           b["start_course"],
                    "選手名":       b["player_name"],
                    "逃げ%":        f"{ed.get('nige_rate', 0):.1f}" if ed.get("nige_rate") else "─",
                    "差し%":        f"{ed.get('sashi_rate', 0):.1f}" if ed.get("sashi_rate") else "─",
                    "まくり%":      f"{ed.get('makuri_rate', 0):.1f}" if ed.get("makuri_rate") else "─",
                    "まくり差し%":  f"{ed.get('makuri_sashi_rate', 0):.1f}" if ed.get("makuri_sashi_rate") else "─",
                    "今節調子":     f"{c_comp.get('form_score', 0.5):.2f}",
                    "決まり手適性": f"{ta:.1f}%",
                    "ST安定性":     f"{c_comp.get('st_consistency', 0.5):.2f}",
                })
            has_trick = any(r["逃げ%"] != "─" for r in trick_rows)
            if has_trick:
                st.caption("決まり手率・派生指標")
                st.dataframe(pd.DataFrame(trick_rows), use_container_width=True, hide_index=True)

            # ── 今節成績 / 過去10走 ─────────────────────────────────────────
            st.markdown("---")

            # player_noをDBから直接取得（predictキャッシュに依存しない）
            with _conn() as _pno_conn:
                _direct_pno_rows = _pno_conn.execute("""
                    SELECT e.boat_no, e.player_no FROM entries e
                    JOIN races r ON r.id=e.race_id
                    WHERE r.date=? AND r.venue_code=? AND r.race_no=?
                    ORDER BY e.boat_no
                """, (date, vc, race_no)).fetchall()
            _boat_pno = {r["boat_no"]: r["player_no"] for r in _direct_pno_rows}
            _player_nos = [p for p in _boat_pno.values() if p]

            if _player_nos:
                # 今節開始日を計算（連続する日付を遡る）
                with _conn() as _ms_conn:
                    _past_dates = [r[0] for r in _ms_conn.execute(
                        "SELECT DISTINCT date FROM races WHERE venue_code=? AND date<=? ORDER BY date DESC",
                        (vc, date)
                    ).fetchall()]
                _meet_start = _past_dates[0] if _past_dates else date
                for _i in range(1, len(_past_dates)):
                    try:
                        _d0 = datetime.strptime(_past_dates[_i - 1], "%Y%m%d")
                        _d1 = datetime.strptime(_past_dates[_i], "%Y%m%d")
                        if (_d0 - _d1).days == 1:
                            _meet_start = _past_dates[_i]
                        else:
                            break
                    except Exception:
                        break

                _ph = ",".join("?" * len(_player_nos))
                with _conn() as _dc:
                    # 今節成績（節開始日〜当日, rre.player_no 直接参照）
                    _ks_rows = _dc.execute(f"""
                        SELECT rre.player_no, r.date, r.race_no, rre.boat_no, rre.rank, rre.start_timing
                        FROM race_result_entries rre
                        JOIN races r ON r.id=rre.race_id
                        WHERE rre.player_no IN ({_ph})
                          AND r.venue_code=?
                          AND r.date BETWEEN ? AND ?
                          AND rre.rank IS NOT NULL
                        ORDER BY rre.player_no, r.date, r.race_no
                    """, (*_player_nos, vc, _meet_start, date)).fetchall()

                    # 過去10走: 全選手まとめて1クエリ（correlated subquery排除）
                    _pno_ph2 = ",".join("?" * len(_player_nos))
                    _all_hist_rows = _dc.execute(f"""
                        SELECT rre.player_no, r.id AS race_id, r.date, r.venue_code, r.race_no,
                               rre.boat_no, rre.rank, rre.start_timing
                        FROM race_result_entries rre
                        JOIN races r ON r.id = rre.race_id
                        WHERE rre.player_no IN ({_pno_ph2})
                          AND rre.rank IS NOT NULL
                        ORDER BY rre.player_no, r.date DESC, r.race_no DESC
                        LIMIT 600
                    """, _player_nos).fetchall()

                    # 各レースの3連単を別途1クエリで取得
                    _hist_race_ids = list({r["race_id"] for r in _all_hist_rows})
                    _combo_map: dict = {}
                    if _hist_race_ids:
                        _rid_ph = ",".join("?" * len(_hist_race_ids))
                        for _cr in _dc.execute(f"""
                            SELECT b1.race_id,
                                   b1.boat_no || '-' || b2.boat_no || '-' || b3.boat_no AS combo3t
                            FROM race_result_entries b1
                            JOIN race_result_entries b2 ON b2.race_id=b1.race_id AND b2.rank=2
                            JOIN race_result_entries b3 ON b3.race_id=b1.race_id AND b3.rank=3
                            WHERE b1.race_id IN ({_rid_ph}) AND b1.rank=1
                        """, _hist_race_ids).fetchall():
                            _combo_map[_cr["race_id"]] = _cr["combo3t"]

                    # Python側で艇番ごとに上位10件を抽出
                    from collections import defaultdict as _dd2
                    _hist_by_key: dict = _dd2(list)
                    for _hr in _all_hist_rows:
                        _key = (_hr["player_no"], _hr["boat_no"])
                        if len(_hist_by_key[_key]) < 10:
                            _hist_by_key[_key].append(
                                dict(_hr) | {"combo3t": _combo_map.get(_hr["race_id"])}
                            )

                    _hist_map = {}
                    for _b2 in boats:
                        _pno2 = _boat_pno.get(_b2["boat_no"])
                        _bno2 = _b2["boat_no"]
                        _hist_map[_bno2] = _hist_by_key.get((_pno2, _bno2), []) if _pno2 else []

                from collections import defaultdict as _dd
                _ks_map = _dd(list)
                for _r in _ks_rows:
                    _ks_map[_r["player_no"]].append((
                        _r["date"], _r["race_no"], _r["boat_no"], _r["rank"], _r["start_timing"]
                    ))

                _RANK_COLOR = {1: "#c0392b", 2: "#1565c0", 3: "#2e7d32", 4: "#555", 5: "#555", 6: "#555"}
                _RANK_BG    = {1: "#fdecea", 2: "#e3f2fd", 3: "#e8f5e9", 4: "#f5f5f5", 5: "#f5f5f5", 6: "#f5f5f5"}

                # ── 今節成績 ──────────────────────────────────────────────────
                with st.expander("📅 今節成績（同会場）", expanded=False):
                    _sorted_boats = sorted(boats, key=lambda x: x["boat_no"])
                    _ks_cols = st.columns(len(_sorted_boats))
                    for _ci, _b in enumerate(_sorted_boats):
                        _pno = _boat_pno.get(_b["boat_no"])
                        _races_k = _ks_map.get(_pno, [])
                        with _ks_cols[_ci]:
                            _bn = _b["boat_no"]
                            st.markdown(
                                f"{_waku(_bn)}<span style='font-size:12px;font-weight:bold'>{_b['player_name']}</span>",
                                unsafe_allow_html=True
                            )
                            if not _races_k:
                                st.caption("なし")
                            else:
                                for _rd, _rno, _rbn, _rank, _st in _races_k:
                                    _rc  = _RANK_COLOR.get(_rank, "#555")
                                    _rbg = _RANK_BG.get(_rank, "#f5f5f5")
                                    _st_str = f"ST.{_st}" if _st else ""
                                    st.markdown(
                                        f"<div style='font-size:11px;margin:2px 0;padding:3px 5px;"
                                        f"border-radius:4px;background:{_rbg};color:#333'>"
                                        f"<b style='color:{_rc}'>{_rank}着</b>&nbsp;"
                                        f"{_rd[4:6]}/{_rd[6:]}&nbsp;{_rno}R&nbsp;"
                                        f"<span style='color:#888'>{_st_str}</span></div>",
                                        unsafe_allow_html=True
                                    )

                # ── 過去10走 ─────────────────────────────────────────────────
                _venue_names = {
                    "01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川",
                    "06":"浜名湖","07":"蒲郡","08":"常滑","09":"津","10":"三国",
                    "11":"びわこ","12":"住之江","13":"尼崎","14":"鳴門","15":"丸亀",
                    "16":"児島","17":"宮島","18":"徳山","19":"下関","20":"若松",
                    "21":"芦屋","22":"福岡","23":"唐津","24":"大村"
                }
                with st.expander("📊 過去10走（同艇番）", expanded=False):
                    _tab_labels = [
                        f"{_b['boat_no']}号 {_b['player_name']}"
                        for _b in sorted(boats, key=lambda x: x["boat_no"])
                    ]
                    _hist_tabs = st.tabs(_tab_labels)
                    for _ti, _b in enumerate(sorted(boats, key=lambda x: x["boat_no"])):
                        _hist = _hist_map.get(_b["boat_no"], [])
                        with _hist_tabs[_ti]:
                            if not _hist:
                                st.caption("過去レース結果なし")
                            else:
                                _hrows = []
                                for _hr in _hist:
                                    _hd   = _hr["date"] if isinstance(_hr, dict) else _hr[0]
                                    _hvc  = _hr["venue_code"] if isinstance(_hr, dict) else _hr[1]
                                    _hrno = _hr["race_no"] if isinstance(_hr, dict) else _hr[2]
                                    _hbn  = _hr["boat_no"] if isinstance(_hr, dict) else _hr[3]
                                    _hrank= _hr["rank"] if isinstance(_hr, dict) else _hr[4]
                                    _hst  = _hr["start_timing"] if isinstance(_hr, dict) else _hr[5]
                                    _combo= _hr["combo3t"] if isinstance(_hr, dict) else _hr[6]
                                    _hrows.append({
                                        "日付":    f"{_hd[:4]}/{_hd[4:6]}/{_hd[6:]}",
                                        "会場":    _venue_names.get(_hvc, _hvc),
                                        "R":       _hrno,
                                        "艇番":    _hbn,
                                        "着順":    _hrank,
                                        "ST":      _hst or "─",
                                        "3連単":   _combo or "─",
                                    })
                                st.dataframe(
                                    pd.DataFrame(_hrows),
                                    use_container_width=True,
                                    hide_index=True,
                                    column_config={
                                        "着順": st.column_config.NumberColumn(format="%d着"),
                                    }
                                )

        with sub2:
            st.caption("コース別成績・平均ST・負け手")
            cs_table = []
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                cs_d = cs_rows.get(b["boat_no"], {})
                ed  = entry_details.get(b["boat_no"], {})
                ld  = lose_data.get(b["boat_no"])
                cs_table.append({
                    "艇":           b["boat_no"],
                    "CS":           b["start_course"],
                    "選手名":       b["player_name"],
                    "コース1着%":   f"{cs_d['win_rate_1st']:.1f}"   if cs_d.get("win_rate_1st")  is not None else "─",
                    "コース2着%":   f"{cs_d['win_rate_2nd']:.1f}"   if cs_d.get("win_rate_2nd")  is not None else "─",
                    "コース3着%":   f"{cs_d['win_rate_3rd']:.1f}"   if cs_d.get("win_rate_3rd")  is not None else "─",
                    "進入率%":      f"{cs_d['entry_rate']:.1f}"     if cs_d.get("entry_rate")    is not None else "─",
                    "コース平均ST": f"{cs_d['avg_st']:.2f}"         if cs_d.get("avg_st")        is not None else "─",
                    "平均ST":       f"{ed['avg_start_timing']:.2f}" if ed.get("avg_start_timing") is not None else "─",
                    "まくられ率":   f"{ld['makuri_lose']:.0f}%"      if ld else "─",
                    "差され率":     f"{ld['sashi_lose']:.0f}%"       if ld else "─",
                    "まくり差され率":f"{ld['makuri_sashi_lose']:.0f}%" if ld else "─",
                })
            st.dataframe(pd.DataFrame(cs_table), use_container_width=True, hide_index=True)
            with st.expander("ℹ️ 各列の見方"):
                st.markdown("""
**コース1〜3着%**: そのコースからの着率（─はサンプル数不足）　**進入率%**: そのコースから実際にスタートする割合
**コース平均ST**: そのコースでの平均ST（小さいほど早い）　**平均ST**: 全コース通算の平均ST
**まくられ率**: 負けた時に相手が「まくり」で1着を取った割合　**差され率**: 同「差し」　**まくり差され率**: 同「まくり差し」
""")

            # ── 艇番別 決まり手詳細 ───────────────────────────────────────────
            st.markdown("---")
            st.markdown("**艇番別 決まり手詳細**")

            TRICK_COLOR = {
                "逃げ":       ("#0f68d9", "#dbeafe"),
                "差し":       ("#078760", "#dcfce7"),
                "まくり":     ("#d33f49", "#fee2e2"),
                "まくり差し": ("#c77a05", "#fef3c7"),
                "抜き":       ("#4b5563", "#f3f4f6"),
                "恵まれ":     ("#6d28d9", "#ede9fe"),
                "抵抗":       ("#92400e", "#fdf3e3"),
            }
            BOAT_COLORS = {1: "#1d4ed8", 2: "#374151", 3: "#dc2626",
                           4: "#15803d", 5: "#b45309", 6: "#6d28d9"}

            def _trick_bar_row(label, cnt, total, color, max_cnt):
                if total == 0:
                    return ""
                pct = cnt / total * 100
                bar_pct = cnt / max_cnt * 100 if max_cnt > 0 else 0
                return (
                    f"<div style='display:flex;align-items:center;gap:6px;margin:4px 0'>"
                    f"<span style='font-size:11px;width:120px;flex-shrink:0;color:#374151'>{label}</span>"
                    f"<div style='flex:1;height:8px;background:#f1f5f9;border-radius:4px;overflow:hidden'>"
                    f"<div style='width:{bar_pct:.1f}%;height:100%;background:{color};border-radius:4px'></div>"
                    f"</div>"
                    f"<span style='font-size:11px;font-weight:700;width:72px;text-align:right;"
                    f"flex-shrink:0;color:#374151'>"
                    f"{cnt}回 <span style='font-weight:400;color:#9ca3af'>{pct:.0f}%</span></span>"
                    f"</div>"
                )

            # ── 上段：全体傾向サマリーカード（6艇横並び）
            cards_html = (
                "<div style='display:grid;grid-template-columns:repeat(6,1fr);"
                "gap:8px;margin-bottom:20px'>"
            )
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                bn  = b["boat_no"]
                td  = trick_detail.get(bn)
                bc  = BOAT_COLORS.get(bn, "#374151")
                badges = ""
                if td and td["win_all"] and td["total_wins_all"] > 0:
                    for trick, cnt in td["win_all"][:3]:
                        pct = cnt / td["total_wins_all"] * 100
                        tc, tbg = TRICK_COLOR.get(trick, ("#374151", "#f3f4f6"))
                        badges += (
                            f"<span style='display:inline-block;font-size:10px;"
                            f"font-weight:700;padding:2px 5px;border-radius:4px;"
                            f"margin:1px;background:{tbg};color:{tc}'>"
                            f"{trick}{pct:.0f}%</span>"
                        )
                else:
                    badges = "<span style='font-size:10px;color:#9ca3af'>データなし</span>"
                cards_html += (
                    f"<div style='background:#fff;border-radius:10px;padding:10px 8px;"
                    f"text-align:center;border:1.5px solid #e5e7eb'>"
                    f"<div style='font-size:20px;font-weight:800;color:{bc}'>{bn}</div>"
                    f"<div style='font-size:11px;color:#6b7280;margin:2px 0 6px;"
                    f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"
                    f"{html.escape(b['player_name'])}</div>"
                    f"{badges}</div>"
                )
            cards_html += "</div>"
            st.markdown(cards_html, unsafe_allow_html=True)

            # ── 下段：艇番別詳細カード
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                bn  = b["boat_no"]
                td  = trick_detail.get(bn)
                if not td:
                    continue

                runs   = td["runs_boat"]
                w_boat = td["total_wins_boat"]
                l_boat = td["total_loses_boat"]
                win_rate_pct = w_boat / runs * 100 if runs > 0 else 0
                bc = BOAT_COLORS.get(bn, "#374151")

                # 積み上げバー（勝ち種別:青系 / 負け種別:赤系）
                stacked_segs = ""
                if runs > 0:
                    win_grad = ["#0f68d9", "#3b82f6", "#60a5fa", "#93c5fd"]
                    for i, (trick, cnt) in enumerate((td["win_boat"] or [])[:4]):
                        stacked_segs += (
                            f"<div style='flex:{cnt / runs * 100:.1f};"
                            f"background:{win_grad[min(i, len(win_grad)-1)]}'></div>"
                        )
                    lose_grad = ["#d33f49", "#ef4444", "#f87171", "#fca5a5", "#fecaca"]
                    for i, (sc, trick, cnt) in enumerate((td["lose_boat"] or [])[:5]):
                        stacked_segs += (
                            f"<div style='flex:{cnt / runs * 100:.1f};"
                            f"background:{lose_grad[min(i, len(lose_grad)-1)]}'></div>"
                        )

                # 勝ちバー
                if td["win_boat"] and td["total_wins_boat"] > 0:
                    max_w = max(cnt for _, cnt in td["win_boat"])
                    win_rows = "".join(
                        _trick_bar_row(
                            trick, cnt, td["total_wins_boat"],
                            TRICK_COLOR.get(trick, ("#0f68d9", "#dbeafe"))[0], max_w
                        )
                        for trick, cnt in td["win_boat"]
                    )
                else:
                    win_rows = "<div style='font-size:11px;color:#9ca3af'>データなし</div>"

                # 負けバー
                if td["lose_boat"] and td["total_loses_boat"] > 0:
                    max_l = max(cnt for _, _, cnt in td["lose_boat"])
                    lose_rows = "".join(
                        _trick_bar_row(
                            f"{trick}（{sc}コース）", cnt, td["total_loses_boat"],
                            TRICK_COLOR.get(trick, ("#d33f49", "#fee2e2"))[0], max_l
                        )
                        for sc, trick, cnt in td["lose_boat"]
                    )
                    if bn == 6:
                        lose_rows += ("<div style='font-size:10px;color:#9ca3af;"
                                      "margin-top:4px'>※前付けなし時はほぼ逃し負けのみ</div>")
                else:
                    lose_rows = "<div style='font-size:11px;color:#9ca3af'>データなし</div>"

                st.markdown(f"""
<div style='background:#fff;border-radius:10px;padding:14px;
            border:1.5px solid #e5e7eb;margin-bottom:10px'>
  <div style='display:flex;align-items:center;gap:8px;margin-bottom:10px'>
    <div style='background:{bc};color:#fff;font-weight:800;font-size:14px;
                width:28px;height:28px;border-radius:50%;display:flex;
                align-items:center;justify-content:center'>{bn}</div>
    <div>
      <div style='font-weight:700;font-size:13px'>{html.escape(b['player_name'])}</div>
      <div style='font-size:11px;color:#6b7280'>
        艇番{bn}&nbsp;出走&nbsp;<b style='color:#1a1a2e'>{runs}回</b>&nbsp;
        <b style='color:#0f68d9'>{w_boat}勝</b>&nbsp;/&nbsp;
        <b style='color:#d33f49'>{l_boat}敗</b>&nbsp;
        勝率{win_rate_pct:.0f}%
      </div>
    </div>
  </div>
  <div style='display:flex;height:12px;border-radius:6px;overflow:hidden;margin-bottom:10px'>
    {stacked_segs}
  </div>
  <div style='display:grid;grid-template-columns:1fr 1fr;gap:12px'>
    <div style='background:#f8fafc;border-radius:8px;padding:10px'>
      <div style='font-size:11px;font-weight:700;color:#374151;margin-bottom:6px;
                  padding-bottom:4px;border-bottom:1px solid #e5e7eb'>
        🏆 勝ち方&nbsp;<span style='font-weight:400;color:#9ca3af'>{td['total_wins_boat']}回</span>
      </div>
      {win_rows}
    </div>
    <div style='background:#f8fafc;border-radius:8px;padding:10px'>
      <div style='font-size:11px;font-weight:700;color:#374151;margin-bottom:6px;
                  padding-bottom:4px;border-bottom:1px solid #e5e7eb'>
        💦 負け方&nbsp;<span style='font-weight:400;color:#9ca3af'>{td['total_loses_boat']}回</span>
      </div>
      {lose_rows}
    </div>
  </div>
</div>""", unsafe_allow_html=True)

        with sub3:
            st.caption("直前情報・前走成績")

            # ── 展示タイム順位を計算（速い順に1位〜）──────────────────────────
            ex_times = {}
            for b in boats:
                pv = prev_info.get(b["boat_no"], {})
                et = pv.get("exhibition_time") or b["components"].get("exhibition_time")
                if et:
                    ex_times[b["boat_no"]] = float(et)

            # 速い（小さい）順にランク付け
            sorted_ex = sorted(ex_times.items(), key=lambda x: x[1])
            ex_rank = {bn: rank + 1 for rank, (bn, _) in enumerate(sorted_ex)}

            # ── 表示テーブル ───────────────────────────────────────────────────
            prev_table = []
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                pv = prev_info.get(b["boat_no"], {})
                et = pv.get("exhibition_time") or b["components"].get("exhibition_time")
                tilt_val = pv.get("tilt") if pv.get("tilt") is not None else b["components"].get("tilt")
                ex_st = pv.get("exhibit_st") or b["components"].get("start_timing")
                rank = ex_rank.get(b["boat_no"])
                rank_label = f"{rank}位" if rank else "─"

                lap   = pv.get("lap_time")
                mawari = pv.get("mawariashi_time")
                choku  = pv.get("straight_time")

                prev_table.append({
                    "艇":         b["boat_no"],
                    "選手名":     b["player_name"],
                    "展示T(秒)":  f"{et:.2f}" if et else "─",
                    "展示順位":   rank_label,
                    "一周/半周":  f"{lap:.2f}" if lap else "─",
                    "まわり足":   f"{mawari:.2f}" if mawari else "─",
                    "直線":       f"{choku:.2f}" if choku else "─",
                    "チルト":     f"{tilt_val:.1f}" if tilt_val is not None else "─",
                    "展示ST":     (f"{ex_st:.2f}" if isinstance(ex_st, float) else ex_st) if ex_st else "─",
                    "前走場":     pv.get("prev_race_venue") or "─",
                    "前走日":     pv.get("prev_race_date") or "─",
                    "前走CS":     str(pv.get("prev_entry_course")) if pv.get("prev_entry_course") is not None else "─",
                    "前走ST":     str(pv.get("prev_start_timing")) if pv.get("prev_start_timing") is not None else "─",
                    "前走着":     str(pv.get("prev_finish")) if pv.get("prev_finish") is not None else "─",
                })
            prev_df = pd.DataFrame(prev_table)
            st.dataframe(prev_df, use_container_width=True, hide_index=True)

            with st.expander("ℹ️ 展示タイムの見方"):
                st.markdown("""
**展示T(秒)**: 直線150mの展示タイム。**小さいほど速い**。
**展示順位**: 6艇中の速さ順位（1位＝最速）。
**一周/半周**: 会場別のコース周回タイム。小さいほどモーター出力が高い。
**まわり足**: ターン性能の指標。**小さいほど旋回が速い**（重要指標）。
**直線**: 直線スピード。小さいほど速い。
**チルト**: プロペラのチルト角度。プラスほど伸び型、マイナスほど出足型。
**展示ST**: スタート展示でのスタートタイミング。小さいほど積極的。
※ 一周/半周・まわり足・直線は会場公式サイト対応会場のみ表示（江戸川・津は非対応）。
""")

            # ── スタート展示 視覚表示 ─────────────────────────────────────────
            st.markdown("---")
            st.caption("🚤 スタート展示")

            # exhibit_courseが取得できている艇を並び順に並べる
            exhibit_data = []
            for b in boats:
                pv = prev_info.get(b["boat_no"], {})
                ex_course = pv.get("exhibit_course") or b["components"].get("exhibit_course")
                ex_st     = pv.get("exhibit_st") or b["components"].get("start_timing")
                if ex_course:
                    exhibit_data.append({
                        "boat_no":    b["boat_no"],
                        "ex_course":  int(ex_course),
                        "ex_st":      str(ex_st) if ex_st else "─",
                        "name":       b["player_name"],
                    })

            if not exhibit_data:
                st.info("展示データ未取得（直前情報スクレイプ前）")
            else:
                exhibit_data.sort(key=lambda x: x["ex_course"])

                # 前付け発生チェック
                has_maekuke = any(d["boat_no"] != d["ex_course"] for d in exhibit_data)
                if has_maekuke:
                    st.warning("⚠️ **前付け発生** — 進入コースが枠番と異なります")

                # 枠色定義
                BOAT_BG   = {1:"#e8eaed",2:"#1a1a1a",3:"#c0392b",4:"#1565c0",5:"#f9a825",6:"#2e7d32"}
                BOAT_FG   = {1:"#000000",2:"#ffffff",3:"#ffffff",4:"#ffffff",5:"#000000",6:"#ffffff"}

                # HTML テーブル生成
                rows_html = ""
                for d in exhibit_data:
                    bn        = d["boat_no"]
                    ex_course = d["ex_course"]
                    ex_st_str = d["ex_st"]
                    name      = d["name"]
                    is_mfk    = bn != ex_course

                    bg  = BOAT_BG.get(bn, "#9e9e9e")
                    fg  = BOAT_FG.get(bn, "#ffffff")
                    is_flying = isinstance(ex_st_str, str) and ex_st_str.startswith("F")
                    st_bg = "#c0392b" if is_flying else "transparent"
                    st_fg = "#ffffff" if is_flying else "inherit"
                    maekuke_badge = (
                        f'<span style="font-size:10px;background:#e67e22;color:#fff;'
                        f'border-radius:3px;padding:1px 4px;margin-left:4px;">前付</span>'
                        if is_mfk else ""
                    )
                    row_bg = "#fff8e1" if is_mfk else "transparent"

                    rows_html += f"""
                    <tr style="background:{row_bg};">
                      <td style="text-align:center;padding:6px 10px;
                                 background:{bg};color:{fg};font-weight:bold;
                                 font-size:16px;width:40px;border-radius:4px;">
                        {bn}
                      </td>
                      <td style="padding:6px 12px;font-size:13px;">
                        {name}{maekuke_badge}
                      </td>
                      <td style="text-align:center;padding:6px 8px;
                                 background:{st_bg};color:{st_fg};
                                 font-weight:bold;font-size:14px;width:70px;border-radius:4px;">
                        {ex_st_str}
                      </td>
                    </tr>"""

                table_html = f"""
                <table style="border-collapse:separate;border-spacing:0 4px;width:100%;max-width:400px;">
                  <thead>
                    <tr style="background:#1565c0;color:#fff;font-size:12px;">
                      <th style="padding:6px 10px;border-radius:4px 0 0 4px;">枠</th>
                      <th style="padding:6px 12px;text-align:left;">選手</th>
                      <th style="padding:6px 10px;border-radius:0 4px 4px 0;">展示ST</th>
                    </tr>
                  </thead>
                  <tbody>{rows_html}</tbody>
                </table>"""
                st.html(table_html)

                # 前付け選手の過去成績表示
                if has_maekuke:
                    maekuke_boats = [d for d in exhibit_data if d["boat_no"] != d["ex_course"]]
                    with st.expander(f"📊 前付け選手の進入コース別過去成績 ({len(maekuke_boats)}名)"):
                        with _conn() as conn_mk:
                            for d in maekuke_boats:
                                bn        = d["boat_no"]
                                ex_course = d["ex_course"]
                                pno = next((b["components"].get("player_no") or
                                            entry_details.get(bn, {}).get("player_no")
                                            for b in boats if b["boat_no"] == bn), None)
                                if not pno:
                                    continue
                                # そのコースでの過去成績
                                stats = conn_mk.execute("""
                                    SELECT COUNT(*) total,
                                           SUM(CASE WHEN rre.rank=1 THEN 1 ELSE 0 END) wins,
                                           AVG(rre.rank) avg_rank
                                    FROM before_info bi
                                    JOIN races r ON r.id = bi.race_id
                                    JOIN entries e ON e.race_id = bi.race_id AND e.boat_no = bi.boat_no
                                    JOIN race_result_entries rre ON rre.race_id = bi.race_id AND rre.boat_no = bi.boat_no
                                    WHERE e.player_no = ?
                                      AND bi.exhibit_course = ?
                                      AND bi.exhibit_course != bi.boat_no
                                """, (pno, ex_course)).fetchone()
                                total = stats[0] or 0
                                wins  = stats[1] or 0
                                avg_r = stats[2]
                                name = d["name"]
                                if total >= 3:
                                    wr = wins / total * 100
                                    st.markdown(
                                        f"**{name}**（{bn}枠→{ex_course}コース進入）  "
                                        f"前付け {total}回 / 勝率 **{wr:.0f}%** / 平均着順 {avg_r:.1f}"
                                    )
                                else:
                                    st.markdown(
                                        f"**{name}**（{bn}枠→{ex_course}コース進入）  "
                                        f"前付けサンプル少（{total}回）"
                                    )

        with sub4:
            st.caption("⚙️ モーター2連対率・今節の使用記録（過去10日間・同会場）")

            # 各艇のモーター情報を整理
            motor_list = []
            for b in sorted(boats, key=lambda x: x["boat_no"]):
                bn  = b["boat_no"]
                ed  = entry_details.get(bn, {})
                cp  = b["components"]
                pv  = prev_info.get(bn, {})
                motor_list.append({
                    "boat_no":    bn,
                    "player_name": b["player_name"],
                    "motor_no":   ed.get("motor_no"),
                    "motor_2ring": cp.get("motor_2ring", 0) or 0,
                    "boat_2ring": ed.get("boat_2ring_rate", 0) or 0,
                    "exhibition_time": pv.get("exhibition_time") or cp.get("exhibition_time"),
                    "tilt": pv.get("tilt") if pv.get("tilt") is not None else cp.get("tilt"),
                })

            # ── モーター2連対率ランキング ──
            st.markdown("#### モーター2連対率ランキング")
            motor_sorted = sorted(motor_list, key=lambda x: x["motor_2ring"], reverse=True)
            for rank_i, m in enumerate(motor_sorted):
                rate  = m["motor_2ring"]
                bn    = m["boat_no"]
                mno   = m["motor_no"] or "─"
                et    = m["exhibition_time"]
                tilt  = m["tilt"]
                col_waku, col_info, col_bar = st.columns([1, 5, 4])
                with col_waku:
                    st.markdown(_waku(bn), unsafe_allow_html=True)
                with col_info:
                    et_str   = f"{et:.2f}" if et else "─"
                    tilt_str = f"{tilt:.1f}" if tilt is not None else "─"
                    st.markdown(
                        f"**M{mno}** {m['player_name']}　"
                        f"2連: **{rate:.1f}%**　"
                        f"ボート: {m['boat_2ring']:.1f}%　"
                        f"展示T: {et_str}　チルト: {tilt_str}"
                    )
                with col_bar:
                    st.progress(min(rate / 100, 1.0))

            st.divider()

            # ── 今節の使用記録（前走選手・成績） ──
            st.markdown("#### 今節の同モーター使用記録")
            motor_nos_valid = [m["motor_no"] for m in motor_list if m["motor_no"]]
            if motor_nos_valid:
                from datetime import timedelta
                ten_ago = (datetime.strptime(date, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
                ph_m = ",".join("?" * len(motor_nos_valid))
                with _conn() as mc:
                    hist_rows = mc.execute(f"""
                        SELECT e.motor_no, r.date, r.race_no, e.boat_no,
                               e.player_name, e.national_win_rate,
                               rre.rank, rre.start_course
                        FROM entries e
                        JOIN races r ON r.id = e.race_id
                        LEFT JOIN race_result_entries rre
                            ON rre.race_id = r.id AND rre.boat_no = e.boat_no
                        WHERE r.venue_code = ? AND r.date >= ? AND r.date <= ?
                              AND e.motor_no IN ({ph_m})
                        ORDER BY e.motor_no, r.date DESC, r.race_no DESC
                    """, [vc, ten_ago, date] + motor_nos_valid).fetchall()
                    # before_info の展示タイムを別途取得
                    bi_rows = mc.execute(f"""
                        SELECT bi.race_id, bi.boat_no, bi.exhibition_time, bi.tilt
                        FROM before_info bi
                        WHERE bi.id IN (
                            SELECT MAX(id) FROM before_info
                            WHERE race_id IN (
                                SELECT id FROM races
                                WHERE venue_code=? AND date>=? AND date<=?
                            )
                            GROUP BY race_id, boat_no
                        )
                    """, (vc, ten_ago, date)).fetchall()
                    bi_map = {(r["race_id"], r["boat_no"]): dict(r) for r in bi_rows}
                    race_id_map = {
                        (r["date"], r["race_no"]): r["id"]
                        for r in mc.execute(
                            "SELECT id, date, race_no FROM races WHERE venue_code=? AND date>=? AND date<=?",
                            (vc, ten_ago, date)
                        ).fetchall()
                    }

                # motor_no ごとにグループ化
                from collections import defaultdict
                hist_by_motor = defaultdict(list)
                for h in hist_rows:
                    hist_by_motor[h["motor_no"]].append(h)

                for m in motor_list:
                    mno = m["motor_no"]
                    if not mno:
                        continue
                    st.markdown(
                        f"<div style='margin-top:10px'>{_waku(m['boat_no'])} "
                        f"<b>M{mno}</b> {m['player_name']}　"
                        f"2連: {m['motor_2ring']:.1f}%</div>",
                        unsafe_allow_html=True
                    )
                    records = hist_by_motor.get(mno, [])
                    if records:
                        tbl = []
                        for h in records[:8]:  # 直近8件
                            rid = race_id_map.get((h["date"], h["race_no"]))
                            bi  = bi_map.get((rid, h["boat_no"]), {}) if rid else {}
                            et  = bi.get("exhibition_time") if bi else None
                            tilt_h = bi.get("tilt") if bi else None
                            tbl.append({
                                "日付": f"{h['date'][4:6]}/{h['date'][6:]}",
                                "R":    h["race_no"],
                                "使用選手": h["player_name"] or "─",
                                "全国WR": f"{h['national_win_rate']:.1f}%" if h["national_win_rate"] else "─",
                                "CS":   h["start_course"] or "─",
                                "着":   h["rank"] or "─",
                                "展示T": f"{et:.2f}" if et else "─",
                                "チルト": f"{tilt_h:.1f}" if tilt_h is not None else "─",
                            })
                        st.dataframe(pd.DataFrame(tbl), use_container_width=True, hide_index=True)
                    else:
                        st.caption("今節の使用記録なし")
            else:
                st.info("モーター番号データがありません。")

        # ── sub5: 選手相性マトリクス ───────────────────────────────────────────
        with sub5:
            st.subheader("👥 選手相性マトリクス")
            st.caption("今日の出走メンバー間の直接対決勝率（過去の同一レース出走時の着順比較）。行の選手が列の選手に勝った確率（%）。")

            player_nos_s5 = [b["player_no"] for b in boats if b.get("player_no")]

            @st.cache_data(ttl=1800, show_spinner=False)
            def _cached_matrix(pnos):
                with _conn() as c:
                    return _analysis.get_compatibility_matrix(c, pnos, min_count=3)

            @st.cache_data(ttl=1800, show_spinner=False)
            def _cached_pnames(pnos):
                with _conn() as c:
                    return _analysis.get_player_names(c, pnos)

            mat = _cached_matrix(tuple(player_nos_s5))
            pnames = _cached_pnames(tuple(player_nos_s5))

            if mat.empty:
                st.info("対戦データが不足しています（各ペア最低3戦必要）。")
            else:
                # 行・列ラベルを「艇番 選手名」に変換
                def _label(pno):
                    name = pnames.get(pno, pno)
                    boat = next((b["boat_no"] for b in boats if b.get("player_no") == pno), "")
                    return f"{boat}号 {name}" if boat else name

                mat.index   = [_label(p) for p in mat.index]
                mat.columns = [_label(p) for p in mat.columns]

                def _style_cell(val):
                    if pd.isna(val):   return "color:#e5e7eb"
                    if val >= 65:      return "background-color:#dcfce7;color:#166534;font-weight:700"
                    if val >= 55:      return "background-color:#dbeafe;color:#1e40af"
                    if val <= 35:      return "background-color:#fee2e2;color:#991b1b"
                    return ""

                st.dataframe(
                    mat.style.format("{:.1f}", na_rep="─").map(_style_cell),
                    use_container_width=True,
                )
                st.caption("🟢 65%以上（有利）　🔵 55-65%　⚪ 35-55%　🔴 35%以下（不利）　─ データ不足（3戦未満）")

                # 各選手の総合相性スコア（行平均）
                st.markdown("---")
                st.markdown("**対戦相性スコア（このメンバー内での優位性）**")
                avg_scores = mat.mean(axis=1, skipna=True).sort_values(ascending=False)
                for label, score in avg_scores.items():
                    if pd.isna(score):
                        continue
                    color = "#078760" if score >= 55 else ("#d33f49" if score <= 45 else "#374151")
                    st.markdown(
                        f"<div style='display:flex;align-items:center;gap:8px;margin:4px 0'>"
                        f"<span style='width:130px;font-size:13px;color:#374151'>{label}</span>"
                        f"<div style='flex:1;height:14px;background:#f1f5f9;border-radius:4px;overflow:hidden'>"
                        f"<div style='width:{score:.0f}%;height:100%;background:{color};"
                        f"opacity:0.7;border-radius:4px'></div></div>"
                        f"<span style='width:48px;text-align:right;font-size:13px;"
                        f"font-weight:700;color:{color}'>{score:.1f}%</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )

        # ── sub6: 出目パターン ────────────────────────────────────────────────
        with sub6:
            st.subheader("🎯 選手別 出目パターン")
            st.caption("過去のレース結果から、各選手が絡んだ時に出やすい3連単の形を集計しています（コース番号ベース）。")

            COURSE_COLORS_S6 = COURSE_COLORS  # 艇番カラーと統一
            TRICK_LABELS = ["逃げ", "差し", "まくり", "まくり差し", "抜き", "恵まれ"]

            def _course_chip(c):
                color = COURSE_COLORS_S6.get(int(c), "#9ca3af")
                return (
                    f"<span style='display:inline-flex;align-items:center;justify-content:center;"
                    f"width:20px;height:20px;border-radius:4px;background:{color};"
                    f"color:#fff;font-size:12px;font-weight:700'>{c}</span>"
                )

            def _combo_str(c1, c2, c3):
                return (
                    f"{_course_chip(c1)}"
                    f"<span style='color:#9ca3af;font-size:11px;margin:0 2px'>-</span>"
                    f"{_course_chip(c2)}"
                    f"<span style='color:#9ca3af;font-size:11px;margin:0 2px'>-</span>"
                    f"{_course_chip(c3)}"
                )

            @st.cache_data(ttl=3600, show_spinner=False)
            def _cached_outcome(player_no, start_course):
                with _conn() as c:
                    return _analysis.get_player_outcome_patterns(
                        c, player_no, min_count=2, start_course=start_course
                    )

            for b in sorted(boats, key=lambda x: x["boat_no"]):
                bn    = b["boat_no"]
                pno   = b.get("player_no", "")
                pname = b["player_name"]
                cs    = b.get("start_course") or bn
                color = COURSE_COLORS_S6.get(bn, "#374151")

                with st.expander(
                    f"{'①②③④⑤⑥'[bn-1]} {pname}　（今日のコース: {cs}）",
                    expanded=(bn == 1)
                ):
                    if not pno:
                        st.caption("選手データなし")
                        continue

                    pat = _cached_outcome(pno, cs)
                    win_data    = pat["win"]
                    place2_data = pat["place2"]
                    place3_data = pat["place3"]

                    # ── 1着時（決まり手別） ──────────────────────────────────
                    st.markdown(
                        f"<div style='font-size:13px;font-weight:600;color:#0f68d9;"
                        f"margin-bottom:4px'>🏆 1着時の出目（コース{cs}から・決まり手別）</div>",
                        unsafe_allow_html=True
                    )
                    if not win_data:
                        st.caption(f"コース{cs}での1着データなし（戦数不足）")
                    else:
                        for kt in TRICK_LABELS:
                            kd = win_data.get(kt)
                            if not kd:
                                continue
                            total_w   = kd["total"]
                            p2_dist   = kd.get("p2_dist", [])
                            combos    = kd.get("combos", [])
                            self_c    = kd.get("self_course", cs)

                            st.markdown(
                                f"<span style='font-size:12px;font-weight:600;"
                                f"color:#374151'>{kt}</span>"
                                f"<span style='font-size:11px;color:#9ca3af;margin-left:6px'>"
                                f"{total_w}回</span>",
                                unsafe_allow_html=True
                            )

                            # ① 2着コース分布（ユーザーが最も知りたい情報）
                            if p2_dist:
                                p2_html = "<div style='margin:4px 0 2px;font-size:11px;color:#607086'>2着コース分布:</div>"
                                p2_html += "<div style='display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px'>"
                                for pd2 in p2_dist:
                                    c2col = COURSE_COLORS_S6.get(pd2["p2"], "#9ca3af")
                                    p2_html += (
                                        f"<div style='display:flex;align-items:center;gap:4px;"
                                        f"background:#f1f5f9;border-radius:6px;padding:3px 8px'>"
                                        f"{_course_chip(pd2['p2'])}"
                                        f"<span style='font-size:12px;font-weight:700;color:#374151'>"
                                        f"{pd2['pct']:.0f}%</span>"
                                        f"<span style='font-size:10px;color:#9ca3af'>({pd2['cnt']}回)</span>"
                                        f"</div>"
                                    )
                                p2_html += "</div>"
                                st.markdown(p2_html, unsafe_allow_html=True)

                            # ② 上位3連単パターン（修正済み: self_course使用）
                            rows_html = ""
                            for combo in combos[:5]:
                                p2, p3 = combo["p2"], combo["p3"]
                                rows_html += (
                                    f"<div style='display:flex;align-items:center;"
                                    f"gap:8px;margin:2px 0'>"
                                    f"{_course_chip(self_c)}"
                                    f"<span style='color:#9ca3af;font-size:11px'>-</span>"
                                    f"{_course_chip(p2)}"
                                    f"<span style='color:#9ca3af;font-size:11px'>-</span>"
                                    f"{_course_chip(p3)}"
                                    f"<div style='flex:1;height:8px;background:#f1f5f9;"
                                    f"border-radius:4px;overflow:hidden;margin:0 6px'>"
                                    f"<div style='width:{min(combo['pct'], 100)}%;height:100%;"
                                    f"background:{color};opacity:0.7;border-radius:4px'></div></div>"
                                    f"<span style='font-size:12px;font-weight:700;"
                                    f"color:#374151;width:56px;text-align:right'>"
                                    f"{combo['cnt']}回 <span style='font-weight:400;"
                                    f"color:#9ca3af'>{combo['pct']:.0f}%</span></span>"
                                    f"</div>"
                                )
                            if rows_html:
                                st.markdown(rows_html, unsafe_allow_html=True)
                            st.markdown("<div style='margin-bottom:10px'></div>", unsafe_allow_html=True)

                    st.markdown("---")

                    # ── 2着時 ────────────────────────────────────────────────
                    col_p2, col_p3 = st.columns(2)
                    with col_p2:
                        total2 = place2_data.get("total", 0)
                        st.markdown(
                            f"<div style='font-size:13px;font-weight:600;color:#078760;"
                            f"margin-bottom:6px'>2着時の出目</div>"
                            f"<div style='font-size:11px;color:#9ca3af;margin-bottom:6px'>"
                            f"計{total2}回</div>",
                            unsafe_allow_html=True
                        )
                        if not place2_data["combos"]:
                            st.caption("データなし")
                        else:
                            for combo in place2_data["combos"][:5]:
                                p1, p3 = combo["p1"], combo["p3"]
                                rows_html2 = (
                                    f"<div style='display:flex;align-items:center;"
                                    f"gap:6px;margin:3px 0'>"
                                    f"{_course_chip(p1)}"
                                    f"<span style='color:#9ca3af;font-size:11px'>-</span>"
                                    f"{_course_chip(cs)}"
                                    f"<span style='color:#9ca3af;font-size:11px'>-</span>"
                                    f"{_course_chip(p3)}"
                                    f"<span style='font-size:12px;font-weight:700;"
                                    f"color:#374151;margin-left:auto'>"
                                    f"{combo['cnt']}回 "
                                    f"<span style='font-weight:400;color:#9ca3af'>"
                                    f"{combo['pct']:.0f}%</span></span>"
                                    f"</div>"
                                )
                                st.markdown(rows_html2, unsafe_allow_html=True)

                    with col_p3:
                        total3 = place3_data.get("total", 0)
                        st.markdown(
                            f"<div style='font-size:13px;font-weight:600;color:#c77a05;"
                            f"margin-bottom:6px'>3着時の出目</div>"
                            f"<div style='font-size:11px;color:#9ca3af;margin-bottom:6px'>"
                            f"計{total3}回</div>",
                            unsafe_allow_html=True
                        )
                        if not place3_data["combos"]:
                            st.caption("データなし")
                        else:
                            for combo in place3_data["combos"][:5]:
                                p1, p2 = combo["p1"], combo["p2"]
                                rows_html3 = (
                                    f"<div style='display:flex;align-items:center;"
                                    f"gap:6px;margin:3px 0'>"
                                    f"{_course_chip(p1)}"
                                    f"<span style='color:#9ca3af;font-size:11px'>-</span>"
                                    f"{_course_chip(p2)}"
                                    f"<span style='color:#9ca3af;font-size:11px'>-</span>"
                                    f"{_course_chip(cs)}"
                                    f"<span style='font-size:12px;font-weight:700;"
                                    f"color:#374151;margin-left:auto'>"
                                    f"{combo['cnt']}回 "
                                    f"<span style='font-weight:400;color:#9ca3af'>"
                                    f"{combo['pct']:.0f}%</span></span>"
                                    f"</div>"
                                )
                                st.markdown(rows_html3, unsafe_allow_html=True)


# ─── Page: Analysis ───────────────────────────────────────────────────────────
def show_analysis():
    _page_header(
        "高度分析",
        "過去レースデータから出目パターン・選手相性などを分析します。",
        "Advanced Analysis",
        ["データ分析", "出目分析"],
    )

    KIMARI_TE_LIST = ["逃げ", "差し", "まくり", "まくり差し", "抜き", "恵まれ"]
    COURSE_COLORS_A = COURSE_COLORS  # 艇番カラーと統一

    with _conn() as conn_a:
        _vc_rows = conn_a.execute(
            "SELECT DISTINCT venue_code FROM races ORDER BY venue_code"
        ).fetchall()
        venues_df = pd.DataFrame([{"venue_code": r[0]} for r in _vc_rows])
    venue_opts = ["全会場"] + [
        f"{c} {_analysis.venue_name(c)}" for c in venues_df["venue_code"]
    ]

    # ─── タブ ──────────────────────────────────────────────────────────────────
    atab1, atab2 = st.tabs(["🎯 出目分析", "📋 決まり手サマリー"])

    # ── 出目分析 ──────────────────────────────────────────────────────────────
    with atab1:
        st.subheader("決まり手×1着コース → 2・3着コース分布")
        st.caption("1着艇の進入コースと決まり手を選ぶと、2・3着に入りやすいコースが分かります。")

        col_f1, col_f2, col_f3 = st.columns([3, 3, 3])
        with col_f1:
            sel_venue = st.selectbox("会場", venue_opts, key="an_venue")
            sel_vc = None if sel_venue == "全会場" else sel_venue[:2]
        with col_f2:
            sel_course = st.selectbox(
                "1着コース", [1, 2, 3, 4, 5, 6], key="an_course",
                format_func=lambda x: f"{x}コース"
            )
        with col_f3:
            sel_kt = st.selectbox("決まり手", KIMARI_TE_LIST, key="an_kt")

        @st.cache_data(ttl=3600, show_spinner=False)
        def _cached_heatmap(vc, course, kt):
            with _conn() as c:
                return _analysis.get_kimari_te_heatmap(c, course, kt, vc)

        hm = _cached_heatmap(sel_vc, sel_course, sel_kt)

        if hm["total"] == 0:
            st.info("該当データがありません。フィルタを変えてみてください。")
        else:
            st.caption(f"サンプル数: **{hm['total']:,}** レース")

            def _course_bar(title, dist):
                st.markdown(
                    f"<div style='font-size:13px;font-weight:600;color:#374151;"
                    f"margin:12px 0 6px'>{title}</div>",
                    unsafe_allow_html=True
                )
                if not dist:
                    st.caption("データなし")
                    return
                max_pct = max(dist.values()) if dist else 1
                for course in range(1, 7):
                    pct = dist.get(course, 0)
                    bar_w = pct / max_pct * 100 if max_pct > 0 else 0
                    color = COURSE_COLORS_A.get(course, "#9ca3af")
                    st.markdown(
                        f"<div style='display:flex;align-items:center;gap:8px;margin:3px 0'>"
                        f"<span style='width:24px;text-align:center;font-size:13px;"
                        f"font-weight:700;color:{color}'>{course}</span>"
                        f"<div style='flex:1;height:18px;background:#f1f5f9;"
                        f"border-radius:4px;overflow:hidden'>"
                        f"<div style='width:{bar_w:.1f}%;height:100%;background:{color};"
                        f"border-radius:4px'></div></div>"
                        f"<span style='width:44px;text-align:right;font-size:13px;"
                        f"font-weight:700;color:#374151'>{pct:.1f}%</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )

            col_p2, col_p3 = st.columns(2)
            with col_p2:
                _course_bar("2着コース分布", hm["place2"])
            with col_p3:
                _course_bar("3着コース分布", hm["place3"])

            # 上位コンボ表示
            st.markdown("---")
            st.markdown("**出現率上位の3連単コンボ**")

            @st.cache_data(ttl=3600, show_spinner=False)
            def _cached_dist(vc, course, kt):
                with _conn() as c:
                    return _analysis.get_kimari_te_distribution(c, course, kt, vc)

            dist_df = _cached_dist(sel_vc, sel_course, sel_kt)
            if not dist_df.empty:
                top_combos = (
                    dist_df.groupby(["place2_course", "place3_course"])["cnt"]
                    .sum()
                    .reset_index()
                    .sort_values("cnt", ascending=False)
                    .head(5)
                )
                total = dist_df["cnt"].sum()
                combo_cols = st.columns(min(5, len(top_combos)))
                for i, (_, row) in enumerate(top_combos.iterrows()):
                    p1, p2, p3 = int(sel_course), int(row["place2_course"]), int(row["place3_course"])
                    pct = row["cnt"] / total * 100
                    with combo_cols[i]:
                        c1 = COURSE_COLORS_A.get(p1, "#9ca3af")
                        c2 = COURSE_COLORS_A.get(p2, "#9ca3af")
                        c3 = COURSE_COLORS_A.get(p3, "#9ca3af")
                        st.markdown(
                            f"<div style='background:#fff;border:0.5px solid #d8e3ed;"
                            f"border-radius:10px;padding:10px;text-align:center'>"
                            f"<div style='font-size:20px;font-weight:700;letter-spacing:3px'>"
                            f"<span style='color:{c1}'>{p1}</span>-"
                            f"<span style='color:{c2}'>{p2}</span>-"
                            f"<span style='color:{c3}'>{p3}</span></div>"
                            f"<div style='font-size:13px;color:#6b7280;margin-top:4px'>"
                            f"{pct:.1f}%　{int(row['cnt']):,}件</div>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

    # ── 決まり手サマリー ──────────────────────────────────────────────────────
    with atab2:
        st.subheader("決まり手別 出現率")

        col_sv, _ = st.columns([3, 6])
        with col_sv:
            sel_venue2 = st.selectbox("会場", venue_opts, key="an_venue2")
            sel_vc2 = None if sel_venue2 == "全会場" else sel_venue2[:2]

        @st.cache_data(ttl=3600, show_spinner=False)
        def _cached_summary(vc):
            with _conn() as c:
                return _analysis.get_kimari_te_summary(c, vc)

        sum_df = _cached_summary(sel_vc2)

        if sum_df.empty:
            st.info("データがありません。")
        else:
            TRICK_COLORS = {
                "逃げ": "#0f68d9", "差し": "#078760", "まくり": "#d33f49",
                "まくり差し": "#c77a05", "抜き": "#6b7280", "恵まれ": "#6d28d9",
            }
            max_cnt = sum_df["cnt"].max()
            for _, row in sum_df.iterrows():
                kt   = row["winning_trick"]
                cnt  = int(row["cnt"])
                pct  = float(row["pct"])
                color = TRICK_COLORS.get(kt, "#9ca3af")
                bar_w = cnt / max_cnt * 100
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:10px;margin:5px 0'>"
                    f"<span style='width:80px;font-size:13px;font-weight:600;"
                    f"color:{color}'>{kt}</span>"
                    f"<div style='flex:1;height:20px;background:#f1f5f9;"
                    f"border-radius:4px;overflow:hidden'>"
                    f"<div style='width:{bar_w:.1f}%;height:100%;background:{color};"
                    f"opacity:0.8;border-radius:4px'></div></div>"
                    f"<span style='width:80px;text-align:right;font-size:13px;"
                    f"font-weight:700;color:#374151'>{pct}%</span>"
                    f"<span style='width:70px;text-align:right;font-size:11px;"
                    f"color:#9ca3af'>{cnt:,}件</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )

            # 1着コース別 決まり手ヒートマップ
            st.markdown("---")
            st.subheader("1着コース別 決まり手傾向")
            st.caption("各コースから1着になった時の決まり手の割合")

            @st.cache_data(ttl=3600, show_spinner=False)
            def _cached_course_kt(vc):
                with _conn() as c:
                    df = _analysis.get_kimari_te_distribution(c, venue_code=vc, min_count=1)
                if df.empty:
                    return pd.DataFrame()
                grouped = (
                    df.groupby(["winner_course", "kimari_te"])["cnt"]
                    .sum()
                    .reset_index()
                )
                total_by_course = grouped.groupby("winner_course")["cnt"].transform("sum")
                grouped["pct"] = (grouped["cnt"] / total_by_course * 100).round(1)
                return grouped.pivot_table(
                    index="winner_course", columns="kimari_te",
                    values="pct", fill_value=0
                )

            pivot = _cached_course_kt(sel_vc2)
            if not pivot.empty:
                # 列順を固定
                cols_order = [c for c in KIMARI_TE_LIST if c in pivot.columns]
                pivot = pivot[cols_order]
                pivot.index.name = "1着コース"

                def _color_cell(val):
                    if val >= 50: return "background-color:#dbeafe;color:#1e40af;font-weight:700"
                    if val >= 20: return "background-color:#dcfce7;color:#166534"
                    if val >= 5:  return ""
                    return "color:#d1d5db"

                st.dataframe(
                    pivot.style.format("{:.1f}%").map(_color_cell),
                    use_container_width=True
                )


# ─── Page: History ────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def _get_history_meta():
    """過去ページ用: 全日付リスト + 会場マップ（5分キャッシュ）"""
    with _conn() as c:
        dates     = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM races ORDER BY date DESC").fetchall()]
        venue_map = {r[0]: r[1] for r in c.execute(
            "SELECT venue_code, venue_name FROM venues").fetchall()}
    return dates, venue_map


@st.cache_data(ttl=120)
def _get_history_venue_summary(sel_date: str):
    """指定日の会場別レース数・結果数（2分キャッシュ）"""
    with _conn() as c:
        rows = c.execute("""
            SELECT r.venue_code,
                   COUNT(*) as race_count,
                   SUM(CASE WHEN rre.race_id IS NOT NULL THEN 1 ELSE 0 END) as result_count
            FROM races r
            LEFT JOIN (SELECT DISTINCT race_id FROM race_result_entries) rre ON r.id = rre.race_id
            WHERE r.date=?
            GROUP BY r.venue_code
            ORDER BY r.venue_code
        """, (sel_date,)).fetchall()
    return [dict(r) for r in rows]


def show_history():
    _page_header(
        "過去レース結果",
        "会場ごとの結果、払戻、AI予想の的中状況を確認できます。",
        "History",
    )

    dates, venue_map = _get_history_meta()

    if not dates:
        st.info("過去データがありません。")
        return

    # 日付セレクタ（年・月・日）
    _date_set = set(dates)
    _years  = sorted({d[:4] for d in dates}, reverse=True)
    _sel_year = st.selectbox("年", _years, format_func=lambda y: f"{y}年", key="hist_year")
    _months = sorted({d[4:6] for d in dates if d[:4] == _sel_year})
    _sel_month = st.selectbox("月", _months, index=len(_months)-1, format_func=lambda m: f"{int(m)}月", key="hist_month")
    _days = sorted({d[6:8] for d in dates if d[:4] == _sel_year and d[4:6] == _sel_month})
    _sel_day = st.selectbox("日", _days, index=len(_days)-1, format_func=lambda d: f"{int(d)}日", key="hist_day")
    sel_date = f"{_sel_year}{_sel_month}{_sel_day}"

    rows = _get_history_venue_summary(sel_date)

    if not rows:
        st.info("この日の開催データがありません。")
        return

    venue_data = {r["venue_code"]: r for r in rows}

    # 会場グリッド（公式アプリ風カード）― 全24会場を表示（非開催はグレー）
    st.markdown(f"**{sel_date[:4]}/{sel_date[4:6]}/{sel_date[6:8]}　{len(venue_data)}会場**")

    sel_vc = st.session_state.get("hist_vc")
    if sel_vc and sel_vc not in venue_data:
        sel_vc = None
        st.session_state["hist_vc"] = None

    for row_start in range(0, len(ALL_VENUES), 4):
        cols = st.columns(4)
        for i, (vc, vn) in enumerate(ALL_VENUES[row_start:row_start + 4]):
            with cols[i]:
                if vc not in venue_data:
                    # 非開催会場はグレー表示（ボタンなし）
                    st.markdown(_venue_card_html(vn, vc, status="none"), unsafe_allow_html=True)
                else:
                    vd = venue_data[vc]
                    rc, res = vd["race_count"], vd["result_count"]
                    all_done = res >= rc
                    status = "ended" if all_done else "active"
                    is_sel = sel_vc == vc
                    st.markdown(
                        _venue_card_html(vn, vc, rc, res, status, selected=is_sel),
                        unsafe_allow_html=True
                    )
                    if st.button("✓ 選択中" if is_sel else "結果を見る",
                                 key=f"hv_{sel_date}_{vc}", use_container_width=True):
                        st.session_state["hist_vc"] = vc
                        st.rerun()

    if not sel_vc:
        st.info("上の会場カードを選択するとレース結果が表示されます。")
        return

    # 選択会場のレース結果
    vn = venue_map.get(sel_vc, sel_vc)
    st.divider()
    st.subheader(f"🏟 {vn}　{sel_date[:4]}/{sel_date[4:6]}/{sel_date[6:8]}")

    with _conn() as c:
        races = c.execute(
            "SELECT id, race_no, race_title FROM races WHERE date=? AND venue_code=? ORDER BY race_no",
            (sel_date, sel_vc)).fetchall()

        # 全レースのデータを1コネクション・3クエリで一括取得（N×1コネクションを廃止）
        race_ids = [r["id"] for r in races]
        all_results_raw = []
        all_payouts_raw = []
        all_preds_raw = []
        if race_ids:
            all_results_raw = c.execute(
                "SELECT rre.race_id, rre.rank, rre.boat_no, rre.player_name, rre.start_timing "
                "FROM race_result_entries rre "
                "JOIN races r ON r.id = rre.race_id "
                "WHERE r.date=? AND r.venue_code=? ORDER BY rre.race_id, rre.rank",
                (sel_date, sel_vc)
            ).fetchall()
            all_payouts_raw = c.execute(
                "SELECT p.race_id, p.bet_type, p.combination, p.payout, p.popularity "
                "FROM payouts p JOIN races r ON r.id = p.race_id "
                "WHERE r.date=? AND r.venue_code=? "
                "ORDER BY p.race_id, CASE p.bet_type "
                "WHEN '3連単' THEN 1 WHEN '3連複' THEN 2 WHEN '2連単' THEN 3 "
                "WHEN '2連複' THEN 4 ELSE 5 END, p.popularity",
                (sel_date, sel_vc)
            ).fetchall()
            all_preds_raw = c.execute(
                "SELECT p.race_id, p.top5_combos, p.actual_combo, p.hit_top3, p.hit_top5, "
                "p.top5_honmei, p.top5_chuana, p.top5_ana "
                "FROM predictions p JOIN races r ON r.id = p.race_id "
                "WHERE r.date=? AND r.venue_code=?",
                (sel_date, sel_vc)
            ).fetchall()

    # race_id → データ辞書に変換（Python側でグループ化）
    from collections import defaultdict
    _results_by_race: dict = defaultdict(list)
    for r in all_results_raw:
        _results_by_race[r[0]].append(r)
    _payouts_by_race: dict = defaultdict(list)
    for r in all_payouts_raw:
        _payouts_by_race[r[0]].append(r)
    _preds_by_race: dict = {r[0]: r for r in all_preds_raw}

    for race in races:
        race_id = race["id"]
        race_no = race["race_no"]
        raw_title = race["race_title"] or ""
        title = raw_title if raw_title and raw_title != f"{race_no}R" else ""

        results = _results_by_race[race_id]
        payouts = _payouts_by_race[race_id]
        pred    = _preds_by_race.get(race_id)

        top3 = [r for r in results if r["rank"] and r["rank"] <= 3]
        top3_str = "-".join(str(r["boat_no"]) for r in top3) if len(top3) == 3 else "未確定"
        pay_3t = next((p for p in payouts if p["bet_type"] == "3連単" and p["combination"] == top3_str), None)
        pay_str = f"　{pay_3t['payout']:,}円" if pay_3t else ""
        title_part = f" {title}" if title else ""

        # 的中バッジ
        if pred and pred["actual_combo"]:
            if pred["hit_top3"]:
                hit_badge = "　🎯 **Top3的中**"
            elif pred["hit_top5"]:
                hit_badge = "　✅ Top5的中"
            else:
                hit_badge = "　❌ ハズレ"
        else:
            hit_badge = ""

        with st.expander(f"**{race_no}R**{title_part}　　3連単: `{top3_str}`{pay_str}{hit_badge}"):
            col_r, col_p = st.columns([4, 6])

            with col_r:
                st.markdown("<p class='sec-label'>着順</p>", unsafe_allow_html=True)
                if results:
                    st.markdown(_result_html([dict(r) for r in results]), unsafe_allow_html=True)
                else:
                    st.caption("結果なし")

            with col_p:
                if payouts:
                    st.markdown("<p class='sec-label'>払戻金</p>", unsafe_allow_html=True)
                    st.markdown(
                        _payout_html([dict(p) for p in payouts]),
                        unsafe_allow_html=True
                    )
                else:
                    st.caption("払戻データなし")

            # 予想結果セクション
            if pred:
                import json as _json
                actual = pred["actual_combo"]
                hm_combos = _json.loads(pred["top5_honmei"]) if pred["top5_honmei"] else []
                cu_combos = _json.loads(pred["top5_chuana"]) if pred["top5_chuana"] else []
                an_combos = _json.loads(pred["top5_ana"])    if pred["top5_ana"]    else []

                st.divider()
                st.markdown("<p class='sec-label'>AI予想</p>", unsafe_allow_html=True)

                def _combo_chips(combos, actual):
                    st.markdown(_combo_grid_html(combos, actual), unsafe_allow_html=True)

                if hm_combos or cu_combos or an_combos:
                    for label, combos in [
                        ("🔵 本命", hm_combos),
                        ("🟡 中穴", cu_combos),
                        ("🔴 穴",   an_combos),
                    ]:
                        if combos:
                            hit_mark = "　✅" if actual in combos else ""
                            st.caption(f"{label}{hit_mark}")
                            _combo_chips(combos, actual)
                else:
                    # 旧データ（パターン列なし）はTop5のみ表示
                    top5 = _json.loads(pred["top5_combos"]) if pred["top5_combos"] else []
                    st.caption("Top5")
                    _combo_chips(top5, actual)
            else:
                st.caption("予想データなし（backtest未実行）")


# ─── Page: Finance ────────────────────────────────────────────────────────────
VENUE_OPTIONS = ["01","03","04","06","07","08","09","12","13","14","15","16","18","21","22","23"]
VENUE_NAMES   = {"01":"桐生","03":"江戸川","04":"平和島","06":"浜名湖","07":"蒲郡",
                 "08":"常滑","09":"津","12":"住之江","13":"尼崎","14":"鳴門",
                 "15":"丸亀","16":"児島","18":"徳山","21":"芦屋","22":"福岡","23":"唐津"}


def show_finance():
    ensure_bets()
    _page_header(
        "収支管理",
        "購入記録と損益推移をチェックできます。",
        "Finance",
    )
    with st.expander("➕ 購入記録を追加", expanded=True):
        with st.form("bet_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            fin_date   = c1.text_input("日付 (YYYYMMDD)", value=latest_date())
            fin_vc     = c2.selectbox("会場", VENUE_OPTIONS, format_func=lambda x: VENUE_NAMES.get(x, x))
            fin_rno    = c3.number_input("レース番号", min_value=1, max_value=12, value=1, step=1)
            c4, c5, c6 = st.columns(3)
            fin_combo  = c4.text_input("買い目 (例: 1-4-5)")
            fin_btype  = c5.selectbox("賭け式", ["3連単","3連複","2連単","2連複"])
            fin_amount = c6.number_input("購入金額（円）", min_value=100, step=100, value=500)
            if st.form_submit_button("📝 記録する", use_container_width=True, type="primary"):
                combo = fin_combo.strip()
                if not combo:
                    st.error("買い目を入力してください。")
                else:
                    add_bet(fin_date, fin_vc, int(fin_rno), combo, fin_btype, int(fin_amount))
                    st.success(f"記録しました: {combo}　{int(fin_amount):,}円")
                    st.rerun()

    if st.button("🔄 結果を再チェック（未確定分）"):
        n = refresh_bets()
        st.toast(f"{n}件の結果を更新しました。" if n else "更新対象はありません。")
        st.rerun()

    df = get_bets()
    if df.empty:
        st.info("購入記録がありません。")
        return

    decided  = df[df["result"] != "unknown"]
    wins     = int((decided["result"] == "win").sum())
    total_d  = len(decided)
    total_p  = int(decided["profit"].sum()) if total_d else 0
    total_s  = int(decided["amount"].sum()) if total_d else 0
    win_rate = wins / total_d * 100 if total_d else 0.0
    recovery = (total_s + total_p) / total_s * 100 if total_s else 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("総購入回数", f"{len(df)}回")
    m2.metric("的中率", f"{win_rate:.1f}%", f"{wins}勝 / {total_d - wins}敗")
    m3.metric("回収率", f"{recovery:.1f}%", f"{recovery - 100:+.1f}%")
    m4.metric("累積損益", f"{total_p:+,}円")

    fig = pnl_chart(decided)
    if fig:
        st.subheader("損益グラフ")
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("購入履歴")
    RESULT_MAP = {"win": "✅ 的中", "lose": "❌ 外れ", "unknown": "⏳ 未確定"}
    disp = df.copy()
    disp["結果"] = disp["result"].map(RESULT_MAP)
    disp["払戻"] = disp.apply(lambda row: f"{row['payout']}円/100円" if row["payout"] > 0 else "─", axis=1)
    disp["損益"] = disp["profit"].apply(lambda x: f"+{x:,}円" if x > 0 else (f"{x:,}円" if x < 0 else "─"))
    show = disp.rename(columns={"date": "日付", "venue_name": "会場", "race_no": "R",
        "combination": "買い目", "bet_type": "賭け式", "amount": "金額(円)", "id": "ID"})[
        ["日付", "会場", "R", "買い目", "賭け式", "金額(円)", "結果", "払戻", "損益", "ID"]]
    st.dataframe(show, use_container_width=True, hide_index=True)

    with st.expander("🗑 記録を削除"):
        del_id = st.number_input("削除するID", min_value=1, step=1, key="del_id")
        if st.button("削除する", type="primary"):
            delete_bet(int(del_id))
            st.success(f"ID {int(del_id)} を削除しました。")
            st.rerun()


# ─── Sidebar ──────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def _get_accuracy_summary():
    """全体集計（5分キャッシュ）— plain tuple を返す（pickle対応）"""
    with _conn() as c:
        row = c.execute("""
            SELECT COUNT(*) as total,
                   SUM(hit_top3) as h3, SUM(hit_top5) as h5,
                   SUM(CASE WHEN hit_honmei_5  IS NOT NULL THEN 1 ELSE 0 END) as cnt_hm5,
                   SUM(hit_honmei_5) as hm5,
                   SUM(CASE WHEN hit_chuana_10 IS NOT NULL THEN 1 ELSE 0 END) as cnt_cu10,
                   SUM(hit_chuana_10) as cu10,
                   SUM(CASE WHEN hit_ana_10    IS NOT NULL THEN 1 ELSE 0 END) as cnt_an10,
                   SUM(hit_ana_10) as an10
            FROM predictions WHERE actual_combo IS NOT NULL
        """).fetchone()
        if row is None:
            return None
        # _DictRow → plain tuple（Streamlit cache_data の pickle に対応）
        return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0),
                int(row[3] or 0), int(row[4] or 0),
                int(row[5] or 0), int(row[6] or 0),
                int(row[7] or 0), int(row[8] or 0))


@st.cache_data(ttl=300)
def _get_venue_accuracy():
    """会場別精度（5分キャッシュ）— plain tuple リストを返す"""
    with _conn() as c:
        rows = c.execute("""
            SELECT r.venue_code, v.venue_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN p.hit_honmei_5  IS NOT NULL THEN 1 ELSE 0 END) as cnt_hm5,
                   SUM(p.hit_honmei_5) as hm5,
                   SUM(CASE WHEN p.hit_chuana_10 IS NOT NULL THEN 1 ELSE 0 END) as cnt_cu10,
                   SUM(p.hit_chuana_10) as cu10,
                   SUM(CASE WHEN p.hit_ana_10    IS NOT NULL THEN 1 ELSE 0 END) as cnt_an10,
                   SUM(p.hit_ana_10) as an10
            FROM predictions p
            JOIN races r ON r.id = p.race_id
            LEFT JOIN venues v ON v.venue_code = r.venue_code
            WHERE p.actual_combo IS NOT NULL
            GROUP BY r.venue_code
            ORDER BY (SUM(p.hit_honmei_5) * 1.0 / NULLIF(SUM(CASE WHEN p.hit_honmei_5 IS NOT NULL THEN 1 ELSE 0 END), 0)) DESC
        """).fetchall()
        return [tuple(r) for r in rows]


@st.cache_data(ttl=300)
def _get_hit_dates():
    """的中あり日付一覧（5分キャッシュ）— plain tuple リストを返す"""
    with _conn() as c:
        rows = c.execute("""
            SELECT DISTINCT r.date
            FROM predictions p
            JOIN races r ON r.id = p.race_id
            WHERE p.actual_combo IS NOT NULL
              AND (p.hit_honmei = 1 OR p.hit_chuana = 1 OR p.hit_ana = 1)
            ORDER BY r.date DESC
        """).fetchall()
        return [tuple(r) for r in rows]


def _init_predictions_table():
    """
    predictionsテーブル初期化（セッション内1回のみ実行）。
    PRAGMA table_info で既存カラムを先に確認し、
    不足分だけ ALTER TABLE → Turso HTTP通信を最小化。
    """
    if st.session_state.get("_predictions_table_ready"):
        return
    from db_connect import open_db as _open_db
    conn = _open_db()
    try:
        # テーブル作成（存在しない場合のみ）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id      INTEGER NOT NULL UNIQUE,
                predicted_at TEXT    NOT NULL,
                top5_combos  TEXT    NOT NULL,
                actual_combo TEXT,
                hit_top3     INTEGER,
                hit_top5     INTEGER,
                top5_honmei  TEXT,
                top5_chuana  TEXT,
                top5_ana     TEXT,
                hit_honmei   INTEGER,
                hit_chuana   INTEGER,
                hit_ana      INTEGER,
                hit_honmei_5  INTEGER,
                hit_chuana_10 INTEGER,
                hit_ana_10    INTEGER,
                FOREIGN KEY (race_id) REFERENCES races(id)
            )
        """)
        conn.commit()
        # 既存カラムを1回のクエリで確認（ALTER TABLE を無駄に叩かない）
        existing = {r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
        need_cols = [
            ("top5_honmei","TEXT"), ("top5_chuana","TEXT"), ("top5_ana","TEXT"),
            ("hit_honmei","INTEGER"), ("hit_chuana","INTEGER"), ("hit_ana","INTEGER"),
            ("hit_honmei_5","INTEGER"), ("hit_chuana_10","INTEGER"), ("hit_ana_10","INTEGER"),
        ]
        for col, typ in need_cols:
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
                except Exception:
                    pass
        conn.commit()
    finally:
        conn.close()
    st.session_state["_predictions_table_ready"] = True


def show_accuracy():
    _page_header(
        "予想精度レポート",
        "Top3/Top5とパターン別の的中傾向を確認できます。",
        "Accuracy",
    )

    # テーブル初期化（セッション内1回のみ → 2回目以降はスキップ）
    _init_predictions_table()

    from db_connect import open_db as _open_db

    # 全体集計（キャッシュ済み）
    row = _get_accuracy_summary()

    total = row[0]
    if total == 0:
        st.warning("予想データがまだありません。backtest.pyを実行してください。")
        return

    h3, h5 = row[1] or 0, row[2] or 0
    cnt_hm, hm = row[3] or 0, row[4] or 0
    cnt_cu, cu = row[5] or 0, row[6] or 0
    cnt_an, an = row[7] or 0, row[8] or 0

    tab_summary, tab_hits = st.tabs(["📈 精度サマリー", "🎯 的中レース一覧"])

    # ── タブ1: 精度サマリー ──
    with tab_summary:
        st.markdown("<p class='sec-label'>全体集計</p>", unsafe_allow_html=True)
        col0, col1, col2, col3 = st.columns(4)
        col0.markdown(
            _acc_metric_card("対象レース数", f"{total:,}件", "結果確定済み", "slate"),
            unsafe_allow_html=True
        )
        col1.markdown(
            _acc_metric_card(
                "本命的中率（5点）",
                f"{hm/cnt_hm*100:.1f}%" if cnt_hm else "─",
                f"{hm}/{cnt_hm}件" if cnt_hm else "データなし",
                "blue"
            ),
            unsafe_allow_html=True
        )
        col2.markdown(
            _acc_metric_card(
                "中穴的中率（10点）",
                f"{cu/cnt_cu*100:.1f}%" if cnt_cu else "─",
                f"{cu}/{cnt_cu}件" if cnt_cu else "データなし",
                "amber"
            ),
            unsafe_allow_html=True
        )
        col3.markdown(
            _acc_metric_card(
                "穴的中率（10点）",
                f"{an/cnt_an*100:.1f}%" if cnt_an else "─",
                f"{an}/{cnt_an}件" if cnt_an else "データなし",
                "red"
            ),
            unsafe_allow_html=True
        )
        st.caption("※ランダム期待値：本命5点=4.2% / 中穴10点=8.3% / 穴10点=8.3%（3連単120通り）")

        st.divider()
        st.subheader("会場別精度")

        venue_rows = _get_venue_accuracy()

        venue_data = []
        for vr in venue_rows:
            vc, vn, tot = vr[0], vr[1], vr[2]
            vcnt_hm, vhm = vr[3] or 0, vr[4] or 0
            vcnt_cu, vcu = vr[5] or 0, vr[6] or 0
            vcnt_an, van = vr[7] or 0, vr[8] or 0
            venue_data.append({
                "会場":       vn or vc,
                "レース数":   tot,
                "本命率(5点)":  round(vhm/vcnt_hm*100, 1) if vcnt_hm else None,
                "中穴率(10点)": round(vcu/vcnt_cu*100, 1) if vcnt_cu else None,
                "穴率(10点)":   round(van/vcnt_an*100, 1) if vcnt_an else None,
            })

        _pct_col = lambda label: st.column_config.NumberColumn(label, format="%.1f%%")
        st.dataframe(
            pd.DataFrame(venue_data),
            use_container_width=True,
            hide_index=True,
            column_config={
                "本命率(5点)":  _pct_col("本命率(5点)"),
                "中穴率(10点)": _pct_col("中穴率(10点)"),
                "穴率(10点)":   _pct_col("穴率(10点)"),
            },
        )

    # ── タブ2: 的中レース一覧 ──
    with tab_hits:
        import json as _json2

        # 利用可能な日付一覧（的中あり・キャッシュ済み）
        hit_dates_raw = _get_hit_dates()
        hit_date_labels = [f"{d[0][:4]}/{d[0][4:6]}/{d[0][6:]}" for d in hit_dates_raw]
        hit_date_raws   = [d[0] for d in hit_dates_raw]

        st.markdown("<p class='sec-label'>絞り込み</p>", unsafe_allow_html=True)
        col_f0, col_f1, col_f2 = st.columns([2, 3, 2])
        sel_hit_date  = col_f0.selectbox("日付", ["全日程"] + hit_date_labels, key="acc_date_filter")
        hit_filter    = col_f1.selectbox(
            "的中条件",
            ["全的中", "本命的中(Top10)", "本命Top5的中", "中穴的中(Top15)", "中穴Top10的中", "穴的中(Top15)", "穴Top10的中"],
            key="acc_hit_filter"
        )
        venue_list    = ["全会場"] + [vr[1] or vr[0] for vr in venue_rows]
        venue_filter  = col_f2.selectbox("会場", venue_list, key="acc_venue_filter")

        # WHERE 条件構築
        hit_cond_map = {
            "全的中":          "(p.hit_honmei = 1 OR p.hit_chuana = 1 OR p.hit_ana = 1)",
            "本命的中(Top10)": "p.hit_honmei = 1",
            "本命Top5的中":    "p.hit_honmei_5 = 1",
            "中穴的中(Top15)": "p.hit_chuana = 1",
            "中穴Top10的中":   "p.hit_chuana_10 = 1",
            "穴的中(Top15)":   "p.hit_ana = 1",
            "穴Top10的中":     "p.hit_ana_10 = 1",
        }
        hit_cond   = hit_cond_map[hit_filter]
        extra_cond = ""
        venue_params: list = []
        raw_d: str | None = None

        if sel_hit_date != "全日程":
            raw_d = hit_date_raws[hit_date_labels.index(sel_hit_date)]
            extra_cond += f" AND r.date = '{raw_d}'"

        if venue_filter != "全会場":
            extra_cond += " AND v.venue_name = ?"
            venue_params = [venue_filter]

        conn = _open_db()
        try:
            hit_rows = conn.execute(f"""
                SELECT r.date, r.venue_code, v.venue_name, r.race_no,
                       p.top5_combos, p.actual_combo,
                       p.hit_top3, p.hit_top5,
                       py.payout,
                       p.top5_honmei, p.top5_chuana, p.top5_ana
                FROM predictions p
                JOIN races r ON r.id = p.race_id
                LEFT JOIN venues v ON v.venue_code = r.venue_code
                LEFT JOIN payouts py ON py.race_id = r.id
                    AND py.bet_type = '3連単'
                    AND py.combination = p.actual_combo
                WHERE {hit_cond}
                    AND p.actual_combo IS NOT NULL
                    {extra_cond}
                ORDER BY r.date DESC, r.venue_code, r.race_no
                LIMIT 300
            """, venue_params).fetchall()

            st.caption(f"{hit_filter}: {len(hit_rows)}件（最大300件）")

            # ── 絞り込み時サマリー（日付 or 会場が指定されている場合） ──────────
            if raw_d is not None or venue_filter != "全会場":
                _sum_cond = "p.actual_combo IS NOT NULL"
                _sum_params: list = []
                if raw_d is not None:
                    _sum_cond += " AND r.date = ?"
                    _sum_params.append(raw_d)
                if venue_filter != "全会場":
                    _sum_cond += " AND v.venue_name = ?"
                    _sum_params.append(venue_filter)
                _vj = ("LEFT JOIN venues v ON v.venue_code = r.venue_code"
                       if venue_filter != "全会場" else "")

                _total = conn.execute(
                    f"SELECT COUNT(*) FROM predictions p "
                    f"JOIN races r ON r.id = p.race_id {_vj} WHERE {_sum_cond}",
                    _sum_params
                ).fetchone()[0]
                _hits = conn.execute(
                    f"SELECT COUNT(*) FROM predictions p "
                    f"JOIN races r ON r.id = p.race_id {_vj} "
                    f"WHERE {_sum_cond} AND {hit_cond}",
                    _sum_params
                ).fetchone()[0]
                _pay = conn.execute(
                    f"""SELECT COALESCE(SUM(py.payout), 0)
                        FROM predictions p
                        JOIN races r ON r.id = p.race_id {_vj}
                        LEFT JOIN payouts py ON py.race_id = r.id
                            AND py.bet_type = '3連単'
                            AND py.combination = p.actual_combo
                        WHERE {_sum_cond} AND {hit_cond}""",
                    _sum_params
                ).fetchone()[0]
                _rate = (_hits / _total * 100) if _total > 0 else 0.0
                dc1, dc2, dc3 = st.columns(3)
                dc1.metric("総レース数", f"{_total}R")
                dc2.metric("レース精度", f"{_hits}/{_total}　({_rate:.1f}%)")
                dc3.metric("的中合計払戻", f"{int(_pay):,}円")
        finally:
            conn.close()

        for hr in hit_rows:
            date_s = hr[0]
            vname  = hr[2] or hr[1]
            rno    = hr[3]
            top5   = _json2.loads(hr[4]) if hr[4] else []
            actual = hr[5]
            payout = hr[8]
            hit3   = hr[6]
            hm_combos = _json2.loads(hr[9]) if hr[9] else []
            cu_combos = _json2.loads(hr[10]) if hr[10] else []
            an_combos = _json2.loads(hr[11]) if hr[11] else []

            patt = ("🔵本命" if actual in hm_combos else
                    "🟡中穴" if actual in cu_combos else
                    "🔴穴"   if actual in an_combos else "🎯")
            pay_s = f"　{payout:,}円" if payout else ""
            date_disp = f"{date_s[:4]}/{date_s[4:6]}/{date_s[6:]}"

            # 自信度（combo順位ベース）
            if actual in hm_combos:
                conf_stars, conf_rank, conf_total = _combo_rank_stars(actual, hm_combos)
                conf_label = f"本命 {conf_rank}/{conf_total}番手"
            elif actual in cu_combos:
                conf_stars, conf_rank, conf_total = _combo_rank_stars(actual, cu_combos)
                conf_label = f"中穴 {conf_rank}/{conf_total}番手"
            elif actual in an_combos:
                conf_stars, conf_rank, conf_total = _combo_rank_stars(actual, an_combos)
                conf_label = f"穴 {conf_rank}/{conf_total}番手"
            else:
                conf_stars, conf_rank, conf_total = "", 0, 0
                conf_label = ""

            conf_disp = f"　{conf_stars}" if conf_stars else ""
            with st.expander(f"{patt} 的中　{date_disp} {vname} {rno}R　`{actual}`{pay_s}{conf_disp}"):
                st.markdown(
                    _hit_summary_html(patt, date_disp, vname, rno, actual, payout),
                    unsafe_allow_html=True
                )
                if conf_label:
                    st.caption(f"自信度 {conf_stars}　{conf_label}")
                cols_r = st.columns([2, 1])
                with cols_r[0]:
                    st.markdown("<p class='sec-label'>AI予想 Top5</p>", unsafe_allow_html=True)
                    st.markdown(_combo_grid_html(top5, actual), unsafe_allow_html=True)
                with cols_r[1]:
                    if hm_combos or cu_combos or an_combos:
                        st.markdown("<p class='sec-label'>パターン別</p>", unsafe_allow_html=True)
                        for label, combos in [("🔵本命", hm_combos), ("🟡中穴", cu_combos), ("🔴穴", an_combos)]:
                            if combos:
                                hlit = "✅" if actual in combos else ""
                                st.markdown(f"{label} {hlit}: `{'  '.join(combos)}`")


@st.cache_data(ttl=30)
def _get_odds_data(date: str, vc: str, race_no: int):
    """オッズページ用データ一括取得（30秒キャッシュ）"""
    with _conn() as c:
        race_row = c.execute(
            "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
            (date, vc, race_no)
        ).fetchone()
        if not race_row:
            return None
        race_id = race_row["id"]
        odds_rows = c.execute(
            "SELECT combination, odds FROM odds_3t WHERE race_id=? ORDER BY odds",
            (race_id,)
        ).fetchall()
        recs_rows = c.execute(
            "SELECT category, combo FROM daily_recommendations "
            "WHERE date=? AND venue_code=? AND race_no=?",
            (date, vc, race_no)
        ).fetchall()
        payout_row = c.execute(
            "SELECT combination FROM payouts WHERE race_id=? AND bet_type='3連単'",
            (race_id,)
        ).fetchone()
        entry_rows = c.execute(
            "SELECT boat_no, player_name FROM entries WHERE race_id=? ORDER BY boat_no",
            (race_id,)
        ).fetchall()
    return {
        "odds":       [(r["combination"], r["odds"]) for r in odds_rows],
        "rec_combos": {r["combo"]: r["category"] for r in recs_rows},
        "actual_combo": payout_row["combination"] if payout_row else None,
        "boats_info": {r["boat_no"]: r["player_name"] for r in entry_rows},
    }


def show_odds():
    """3連単オッズ一覧（120通り）"""
    date    = latest_date()
    vc      = st.session_state.venue_code
    vn      = st.session_state.venue_name or vc
    race_no = st.session_state.race_no

    col_back, col_det = st.columns([3, 3])
    with col_back:
        if st.button("← レース詳細"): nav("detail")
    with col_det:
        if st.button("← レース一覧"): nav("races")

    _page_header(
        f"{vn} {race_no}R オッズ一覧",
        "3連単120通りを公式表に近い形式で確認できます。",
        "Odds",
        [f"{date[:4]}/{date[4:6]}/{date[6:8]}", "3連単"],
    )

    _data = _get_odds_data(date, vc, race_no)
    if _data is None:
        st.warning("レースデータが見つかりません。")
        return
    odds_rows    = [{"combination": c, "odds": o} for c, o in _data["odds"]]
    rec_combos   = _data["rec_combos"]
    actual_combo = _data["actual_combo"]
    boats_info   = _data["boats_info"]

    if not odds_rows:
        st.info("このレースのオッズデータがありません。")
        return

    CAT_BADGE = {"honmei": "◎", "chuana": "△", "ana": "☆"}
    CAT_COLOR = {"honmei": "#1768c9", "chuana": "#e8960c", "ana": "#d33f49"}

    # 人気順ソート済みのデータを表示（グリッドレイアウト: 5列）
    # 統計
    valid = [r["odds"] for r in odds_rows if r["odds"] and r["odds"] > 0]
    st.caption(
        f"取得通り数: {len(odds_rows)}/120　"
        + (f"最低: {min(valid):.1f}倍　最高: {max(valid):.1f}倍" if valid else "")
    )

    if actual_combo:
        st.success(f"✅ 確定: **{actual_combo}**")

    # 凡例
    legend_parts = []
    for cat, badge in CAT_BADGE.items():
        if any(r["combo"] == c for c in rec_combos for c in [c]):
            pass
        legend_parts.append(
            f"<span style='color:{CAT_COLOR[cat]};font-weight:700'>{badge}</span>=おすすめ{cat}"
        )
    if rec_combos:
        st.markdown(
            "<div style='font-size:12px;margin-bottom:8px'>"
            + "　".join(
                f"<span style='color:{CAT_COLOR[cat]};font-weight:700'>{CAT_BADGE[cat]}</span> {cat}"
                for cat in ("honmei", "chuana", "ana") if any(c == cat for c in rec_combos.values())
            )
            + "</div>",
            unsafe_allow_html=True
        )

    # ── ボートレース公式形式の3連単オッズ表 ──
    # 列=1着艇(6列)、行グループ=2着艇(5グループ×4行)、セル=3着オッズ
    odds_dict = {r["combination"]: r["odds"] for r in odds_rows if r["combination"] and r["odds"]}

    WAKU_BG  = {1:"#5a5a5a", 2:"#1a1a1a", 3:"#cc3333", 4:"#1a5fa8", 5:"#b89000", 6:"#1e8040"}
    WAKU_TXT = {1:"#fff",    2:"#fff",    3:"#fff",    4:"#fff",    5:"#fff",    6:"#fff"}
    ALL_BOATS = [1, 2, 3, 4, 5, 6]
    CAT_BG   = {"honmei": "#e8f3ff", "chuana": "#fff8e0", "ana": "#fff0f0"}

    def _wb(n, sz=10):
        """艇番バッジHTML"""
        return (f"<span style='display:inline-flex;align-items:center;justify-content:center;"
                f"width:{sz+6}px;height:{sz+6}px;background:{WAKU_BG.get(n,'#888')};"
                f"color:{WAKU_TXT.get(n,'#fff')};border-radius:2px;font-size:{sz}px;"
                f"font-weight:bold;flex-shrink:0;line-height:1'>{n}</span>")

    # 1〜3番人気（オッズ昇順上位3）
    _sorted_by_odds = sorted(
        [(c, v) for c, v in odds_dict.items() if v and v > 0], key=lambda x: x[1]
    )
    _top3_combos = {c for c, _ in _sorted_by_odds[:3]}

    def _odds_html(b1, b2, b3):
        combo    = f"{b1}-{b2}-{b3}"
        v        = odds_dict.get(combo)
        is_hit   = (combo == actual_combo)
        cat      = rec_combos.get(combo)
        mark     = {"honmei": "◎", "chuana": "△", "ana": "☆"}.get(cat, "")
        hit_mark = "✅" if is_hit else ""
        if v and v > 0:
            if combo in _top3_combos:
                clr = "#1565c0"          # 1〜3番人気: 青
            elif v >= 1000:
                clr = "#e8960c"          # 10万舟(1000倍以上): オレンジ
            elif v >= 100:
                clr = "#cc3333"          # 100倍以上: 赤
            else:
                clr = "#1a1a2e"          # その他: 黒
            txt = f"{v:.1f}"
        else:
            clr, txt = "#bbb", "─"
        cell_bg = "#d6f5d6" if is_hit else CAT_BG.get(cat, "transparent")
        return txt, clr, hit_mark + mark, cell_bg

    # テーブル構築
    TH = ""
    TD = ""

    html_parts = [
        "<div class='odds-shell'>",
        "<table class='odds-table'>",
        "<colgroup><col style='width:26px'>",
        *[f"<col style='width:calc((100% - 26px)/6)'>" for _ in ALL_BOATS],
        "</colgroup><thead><tr>",
        f"<th style='background:#667085'></th>",
    ]
    for b1 in ALL_BOATS:
        name = boats_info.get(b1, "")
        html_parts.append(
            f"<th style='background:{WAKU_BG[b1]}'>"
            f"<div style='font-size:14px;font-weight:bold'>{b1}</div>"
            f"<div style='font-size:10px;font-weight:normal;opacity:.9'>{_h(name)}</div></th>"
        )
    html_parts.append("</tr></thead><tbody>")

    for g in range(5):       # 2着艇グループ (5通り)
        for s in range(4):   # 3着艇 (4通り)
            bt = "border-top:2px solid #b8c7da;" if s == 0 else ""
            html_parts.append(f"<tr style='{bt}'>")

            # 左端ラベル（rowspan=4、グループ先頭行のみ）
            if s == 0:
                ref_b2 = [b for b in ALL_BOATS if b != 1][g]
                html_parts.append(
                    f"<td rowspan='4' class='odds-group-label'>{_wb(ref_b2, 14)}</td>"
                )

            for b1 in ALL_BOATS:
                b2_list = [b for b in ALL_BOATS if b != b1]
                b2      = b2_list[g]
                b3_list = [b for b in ALL_BOATS if b != b1 and b != b2]
                b3      = b3_list[s]
                txt, clr, mark, bg = _odds_html(b1, b2, b3)

                inner = (
                    f"<div class='odds-cell'>"
                    + (f"{_wb(b2)}" if s == 0 else f"<span style='width:16px'></span>")
                    + f"{_wb(b3)}"
                    + f"<span class='odds-value' style='color:{clr}'>"
                    + f"{mark}{txt}</span></div>"
                )
                html_parts.append(f"<td style='background:{bg}'>{inner}</td>")

            html_parts.append("</tr>")

    html_parts.append("</tbody></table></div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def _init_recommend_table():
    """daily_recommendationsテーブル初期化（セッション内1回のみ）"""
    if st.session_state.get("_recommend_table_ready"):
        return
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_recommendations (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                date           TEXT    NOT NULL,
                generated_at   TEXT    NOT NULL,
                venue_code     TEXT    NOT NULL,
                race_no        INTEGER NOT NULL,
                category       TEXT    NOT NULL,
                day_rank       INTEGER NOT NULL,
                combo          TEXT    NOT NULL,
                prob           REAL,
                expected_odds  REAL,
                ev             REAL,
                confidence     REAL,
                actual_combo   TEXT,
                hit            INTEGER,
                checked_at     TEXT,
                UNIQUE(date, venue_code, race_no, category)
            )
        """)
    st.session_state["_recommend_table_ready"] = True


@st.cache_data(ttl=60)
def _get_recs(date: str):
    """今日のおすすめ＋過去精度をまとめて取得（キャッシュ60秒）"""
    with _conn() as c:
        recs = c.execute("""
            SELECT dr.*, r.scheduled_time
            FROM daily_recommendations dr
            LEFT JOIN races r
                ON r.date = dr.date
               AND r.venue_code = dr.venue_code
               AND r.race_no = dr.race_no
            WHERE dr.date = ? ORDER BY dr.category, dr.day_rank
        """, (date,)).fetchall()
        summary = c.execute("""
            SELECT category,
                   COUNT(*) as total,
                   SUM(hit)  as hits,
                   AVG(CASE WHEN hit=1 THEN expected_odds END) as avg_odds_hit
            FROM daily_recommendations
            WHERE hit IS NOT NULL
            GROUP BY category
        """).fetchall()
        history = c.execute("""
            SELECT dr.date, dr.category, dr.day_rank,
                   dr.venue_code, dr.race_no, dr.combo,
                   dr.prob, dr.expected_odds, dr.ev,
                   dr.actual_combo, dr.hit
            FROM daily_recommendations dr
            WHERE dr.hit IS NOT NULL
            ORDER BY dr.date DESC, dr.category, dr.day_rank
            LIMIT 150
        """).fetchall()
    # キャッシュのpickle互換性のためtupleに変換
    return (
        [dict(r) for r in recs],
        [dict(r) for r in summary],
        [dict(r) for r in history],
    )


def show_recommend():
    _page_header(
        "今日のおすすめレース",
        "本命・中穴・穴の3カテゴリで注目レースを整理します。",
        "Recommendations",
    )

    _init_recommend_table()

    date = latest_date()
    date_str = f"{date[:4]}/{date[4:6]}/{date[6:8]}"

    VENUE_MAP = dict(ALL_VENUES)
    CAT_LABEL = {"honmei": "本命", "chuana": "中穴", "ana": "穴"}
    CAT_COLOR = {"honmei": "#1768c9", "chuana": "#d78a00", "ana": "#d33f49"}
    CAT_ICON  = {"honmei": "◎", "chuana": "△", "ana": "☆"}

    recs_raw, summary_raw, history_raw = _get_recs(date)
    recs = recs_raw

    tab_today, tab_history = st.tabs([f"📅 {date_str}のおすすめ", "📊 過去精度"])

    with tab_today:
        if not recs:
            st.info(f"{date_str} のおすすめデータがまだありません。")
            if st.button("🔄 今日のおすすめを生成", type="primary"):
                with st.spinner("全レースを予想中…（1〜2分かかります）"):
                    _run_db_writer(
                        [sys.executable, "recommend.py", "--date", date, "--quiet"],
                        "おすすめ生成",
                        timeout=300,
                    )
                st.rerun()
        else:
            gen_at = recs[0]["generated_at"] if recs else ""
            st.caption(f"生成日時: {gen_at}")
            if st.button("🔄 再生成", key="rec_regen"):
                with st.spinner("再生成中…"):
                    _run_db_writer(
                        [sys.executable, "recommend.py", "--date", date, "--quiet"],
                        "おすすめ再生成",
                        timeout=300,
                    )
                st.rerun()

            # カテゴリ別に3列で表示
            _CAT_COLOR = {"honmei": "#0f68d9", "chuana": "#c77a05", "ana": "#d33f49"}
            _CAT_LABEL = {"honmei": "◎ 本命", "chuana": "△ 中穴", "ana": "☆ 穴"}
            _CAT_DESC  = {"honmei": "≤25倍", "chuana": "25〜80倍", "ana": ">80倍"}
            cols = st.columns(3)
            for col_i, cat in enumerate(["honmei", "chuana", "ana"]):
                cat_recs = sorted(
                    [r for r in recs if r["category"] == cat],
                    key=lambda r: r["day_rank"]
                )[:5]

                with cols[col_i]:
                    # カテゴリ見出し
                    st.markdown(
                        f"<div style='display:flex;align-items:center;gap:8px;"
                        f"padding:6px 0 8px;margin-bottom:4px;"
                        f"border-bottom:2px solid {_CAT_COLOR[cat]}'>"
                        f"<span style='font-size:14px;font-weight:500;"
                        f"color:{_CAT_COLOR[cat]}'>{_h(_CAT_LABEL[cat])}</span>"
                        f"<span style='font-size:11px;color:#9ca3af'>{_h(_CAT_DESC[cat])}</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )
                    if not cat_recs:
                        st.caption("データなし")
                        continue

                    for r in cat_recs:
                        vn = VENUE_MAP.get(r["venue_code"], r["venue_code"])

                        # Top5コンボを展開
                        top5 = []
                        if r["top5_combos_json"]:
                            try:
                                top5 = json.loads(r["top5_combos_json"])
                            except Exception:
                                pass
                        if not top5:
                            top5 = [{"combo": r["combo"], "prob": r["prob"],
                                     "expected_odds": r["expected_odds"], "ev": r["ev"]}]

                        actual = r["actual_combo"]

                        st.markdown(
                            _recommend_card_html(
                                r["day_rank"], vn, r["race_no"], top5,
                                actual=actual, hit=r["hit"],
                                scheduled_time=r["scheduled_time"],
                                confidence=r["confidence"],
                                category=cat,
                            ),
                            unsafe_allow_html=True
                        )

    with tab_history:
        # 過去精度サマリー（キャッシュ済み）
        summary = summary_raw

        if not summary:
            st.info("まだ結果照合されたデータがありません。毎晩22時以降に自動更新されます。")
        else:
            st.subheader("カテゴリ別的中率")
            c1, c2, c3 = st.columns(3)
            for col_i, (c_col, cat) in enumerate(zip([c1, c2, c3], ["honmei", "chuana", "ana"])):
                row = next((r for r in summary if r["category"] == cat), None)
                if row:
                    rate = (row["hits"] or 0) / row["total"] * 100
                    c_col.metric(
                        f"{CAT_ICON[cat]} {CAT_LABEL[cat]}",
                        f"{rate:.1f}%",
                        f"{int(row['hits'] or 0)}/{row['total']}件"
                    )

            st.divider()

        # 日付別履歴（キャッシュ済み）
        history = history_raw

        if history:
            st.subheader("過去の推薦履歴（結果確定分）")
            rows_data = []
            for r in history:
                rows_data.append({
                    "日付": f"{r['date'][:4]}/{r['date'][4:6]}/{r['date'][6:8]}",
                    "カテゴリ": CAT_LABEL.get(r["category"], r["category"]),
                    "順位": r["day_rank"],
                    "会場": VENUE_MAP.get(r["venue_code"], r["venue_code"]),
                    "R": r["race_no"],
                    "推薦": r["combo"],
                    "確率": f"{r['prob']:.1f}%" if r["prob"] else "─",
                    "オッズ": f"{r['expected_odds']:.1f}" if r["expected_odds"] else "─",
                    "EV": f"{r['ev']:.2f}" if r["ev"] else "─",
                    "結果": "✅" if r["hit"] == 1 else "❌",
                })
            st.dataframe(pd.DataFrame(rows_data), use_container_width=True, hide_index=True)


# ─── Page: ML厳選レース ───────────────────────────────────────────────────────
def show_ml_recommend():
    import math
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    _page_header(
        "ML厳選レース",
        "XGBoostモデルの自信度（エントロピー）が高いレースを自動選定します。",
        "ML Select",
        pills=["XGBoost", "エントロピー基準"],
    )

    date = latest_date()
    from db_connect import open_db as _open_db
    conn = _open_db()

    rows = conn.execute("""
        SELECT r.venue_code, r.race_no, r.id, r.scheduled_time, r.race_title,
               (SELECT r1.boat_no || '-' || r2.boat_no || '-' || r3.boat_no
                FROM race_result_entries r1
                JOIN race_result_entries r2 ON r2.race_id = r1.race_id AND r2.rank = 2
                JOIN race_result_entries r3 ON r3.race_id = r1.race_id AND r3.rank = 3
                WHERE r1.race_id = r.id AND r1.rank = 1) AS result_combo
        FROM races r
        WHERE r.date = ?
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.scheduled_time, r.venue_code, r.race_no
    """, (date,)).fetchall()
    conn.close()

    if not rows:
        st.info("本日の予測対象レースがありません（データなし）")
        return

    race_keys      = tuple((r[0], r[1]) for r in rows)
    scheduled_map  = {(r[0], r[1]): (r[3] or "??:??", r[4] or "", r[5]) for r in rows}
    venue_name_map = dict(ALL_VENUES)

    # ── キャッシュキー（5分TTL） ──
    cache_key = f"ml_recommend_{date}_{len(race_keys)}"
    force_refresh = st.button("🔄 再計算", help="キャッシュをクリアして再予測します")
    if force_refresh:
        st.session_state.pop("ml_recommend_cache", None)
        st.session_state.pop("ml_recommend_key", None)

    if (st.session_state.get("ml_recommend_key") != cache_key
            or "ml_recommend_cache" not in st.session_state):

        prog = st.progress(0, text=f"🤖 {len(race_keys)}レースをML予測中...")

        def _run_one(vcode, rno):
            try:
                from ml_predict import predict_ml
                return (vcode, rno), predict_ml(date, vcode, rno)
            except Exception:
                return (vcode, rno), None

        results: dict = {}
        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_run_one, vc, rno): (vc, rno) for vc, rno in race_keys}
            for future in _as_completed(futures):
                key, res = future.result()
                results[key] = res
                done += 1
                prog.progress(done / len(race_keys),
                              text=f"🤖 予測中... {done}/{len(race_keys)}R")

        prog.empty()
        st.session_state["ml_recommend_cache"] = results
        st.session_state["ml_recommend_key"]   = cache_key
    else:
        results = st.session_state["ml_recommend_cache"]

    # ── スコアリング ──
    def _entropy(boats):
        probs = [max(b.get("win_prob", 0), 0) / 100 for b in boats]
        total = sum(probs) or 1.0
        probs = [p / total for p in probs]
        return -sum(p * math.log2(p + 1e-10) for p in probs)

    def _grade(ent):
        if ent < 1.5: return "S", "#16a34a"
        if ent < 1.9: return "A", "#1768c9"
        if ent < 2.2: return "B", "#d78a00"
        return "C", "#9ca3af"

    ranked = []
    for (vcode, rno), res in results.items():
        if not res:
            continue
        boats = res.get("boats", [])
        if not boats:
            continue
        ent = _entropy(boats)
        grade, gcolor = _grade(ent)
        top_detail  = (res.get("recommended_3t_detail") or [{}])[0]
        top_combo   = top_detail.get("combo", "---")
        combo_prob  = top_detail.get("prob") or 0   # probはすでに%値
        live_odds   = top_detail.get("live_odds") or top_detail.get("expected_odds") or 0
        top_boat    = boats[0] if boats else {}
        stime, title, result_combo = scheduled_map.get((vcode, rno), ("??:??", "", None))
        # 的中判定（result_combo が取得できていれば比較）
        is_hit = None
        if result_combo:
            is_hit = (top_combo == result_combo)
        ranked.append({
            "vcode": vcode, "rno": rno,
            "venue": venue_name_map.get(vcode, vcode),
            "stime": stime, "title": title,
            "entropy": ent, "grade": grade, "gcolor": gcolor,
            "top_combo": top_combo, "combo_prob": combo_prob,
            "live_odds": live_odds,
            "top_boat_no":   top_boat.get("start_course", "?"),
            "top_boat_prob": top_boat.get("win_prob", 0),
            "result_combo":  result_combo,
            "is_hit":        is_hit,
        })

    ranked.sort(key=lambda x: x["entropy"])

    # ── サマリー ──
    n_s = sum(1 for r in ranked if r["grade"] == "S")
    n_a = sum(1 for r in ranked if r["grade"] == "A")
    n_b = sum(1 for r in ranked if r["grade"] == "B")
    c0, c1, c2, c3 = st.columns(4)
    c0.metric("対象レース", f"{len(ranked)}R")
    c1.metric("🟢 Sグレード", f"{n_s}R", help="エントロピー < 1.5（最有力）")
    c2.metric("🔵 Aグレード", f"{n_a}R", help="エントロピー 1.5〜1.9")
    c3.metric("🟡 Bグレード", f"{n_b}R", help="エントロピー 1.9〜2.2")

    # ── フィルタ ──
    gf = st.radio("グレード", ["全て", "S のみ", "A以上", "B以上"], horizontal=True)
    show_grades = {"全て": {"S","A","B","C"}, "S のみ": {"S"},
                   "A以上": {"S","A"}, "B以上": {"S","A","B"}}[gf]
    filtered = [r for r in ranked if r["grade"] in show_grades]

    if not filtered:
        st.info("該当レースなし")
        return

    st.caption(f"エントロピー昇順（小さいほどモデルの自信が高い）　{date[:4]}/{date[4:6]}/{date[6:8]}")
    st.markdown("---")

    for r in filtered:
        waku_html  = _waku(r["top_boat_no"])
        combo_disp = str(r["top_combo"]).replace("-", " ＞ ")
        odds_badge = (f"<span style='background:#f0fdf4;color:#16a34a;font-size:11px;"
                      f"padding:2px 6px;border-radius:4px;border:0.5px solid #bbf7d0'>"
                      f"オッズ {r['live_odds']:.1f}倍</span>"
                      if r["live_odds"] > 0 else "")
        ent_bar_w  = max(4, int((2.585 - r["entropy"]) / 2.585 * 100))

        card_html = f"""
<div style='background:#fff;border:1px solid #e5e7eb;border-radius:10px;
            padding:14px 16px;margin-bottom:8px;
            border-left:5px solid {r["gcolor"]}'>
  <div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:8px'>
    <div style='display:flex;align-items:center;gap:8px'>
      <span style='background:{r["gcolor"]};color:#fff;font-weight:700;font-size:12px;
                   padding:2px 9px;border-radius:999px;letter-spacing:0.5px'>{r["grade"]}</span>
      <span style='font-weight:600;font-size:15px'>{_h(r["venue"])} {r["rno"]}R</span>
      <span style='color:#9ca3af;font-size:12px'>{r["stime"]}</span>
    </div>
    <div style='font-size:11px;color:#6b7280'>
      ε = {r["entropy"]:.3f}
      <div style='width:60px;height:4px;background:#f3f4f6;border-radius:2px;display:inline-block;vertical-align:middle;margin-left:4px'>
        <div style='width:{ent_bar_w}%;height:100%;background:{r["gcolor"]};border-radius:2px'></div>
      </div>
    </div>
  </div>
  <div style='display:flex;align-items:center;gap:14px;font-size:13px;flex-wrap:wrap'>
    <div>1着予想: {waku_html} <b>{r["top_boat_prob"]:.1f}%</b></div>
    <div style='color:#374151'>本命3連単: <b>{combo_disp}</b></div>
    <div style='color:#9ca3af;font-size:11px'>確率 {r["combo_prob"]:.1f}%</div>
    {odds_badge}
  </div>
</div>"""
        st.markdown(card_html, unsafe_allow_html=True)

        # 結果表示（card_html外で個別レンダリング — 埋め込みだとHTMLが崩れるため）
        if r["is_hit"] is True:
            st.success(f"✓ 的中　{r['result_combo']}", icon=None)
        elif r["is_hit"] is False:
            st.error(f"✗ ハズレ　実際: {r['result_combo']}", icon=None)

        if st.button(f"→ 詳細を見る", key=f"mlr_{r['vcode']}_{r['rno']}"):
            st.session_state.venue_code = r["vcode"]
            st.session_state.venue_name = r["venue"]
            st.session_state.race_no    = r["rno"]
            nav("detail")


def show_sidebar():
    with st.sidebar:
        st.markdown(
            "<div style='font-size:18px;font-weight:500;color:#fff;padding:0 4px 14px;"
            "border-bottom:0.5px solid rgba(255,255,255,0.12);margin-bottom:8px;"
            "display:flex;align-items:center;gap:8px'>⛵ BoatAI</div>",
            unsafe_allow_html=True
        )
        page = st.session_state.page

        st.markdown("<span class='sidebar-section-label'>メイン</span>", unsafe_allow_html=True)
        if st.button("🏠 ホーム",         use_container_width=True,
                     type="primary" if page == "home" else "secondary"):
            st.session_state.home_tab = "🏟 開催一覧"; nav("home")
        if st.button("⭐ おすすめ",        use_container_width=True,
                     type="primary" if page == "recommend" else "secondary"):
            nav("recommend")
        if st.button("🎯 ML厳選",          use_container_width=True,
                     type="primary" if page == "ml_recommend" else "secondary"):
            nav("ml_recommend")
        st.markdown("<span class='sidebar-section-label'>ホームタブ</span>", unsafe_allow_html=True)
        if st.button("🏟 開催一覧",       use_container_width=True):
            st.session_state.home_tab = "🏟 開催一覧"; nav("home")
        if st.button("⏰ 締切順",          use_container_width=True):
            st.session_state.home_tab = "⏰ 締切順";   nav("home")
        if st.button("💴 払戻一覧",        use_container_width=True):
            st.session_state.home_tab = "💴 払戻一覧"; nav("home")
        st.divider()
        st.markdown("<span class='sidebar-section-label'>分析</span>", unsafe_allow_html=True)
        if st.button("📜 過去レース結果",  use_container_width=True,
                     type="primary" if page == "history" else "secondary"):
            nav("history")
        if st.button("💰 収支管理",        use_container_width=True,
                     type="primary" if page == "finance" else "secondary"):
            nav("finance")
        if st.button("📊 予想精度",        use_container_width=True,
                     type="primary" if page == "accuracy" else "secondary"):
            nav("accuracy")
        if st.button("🔬 高度分析",        use_container_width=True,
                     type="primary" if page == "analysis" else "secondary"):
            nav("analysis")
        st.divider()
        st.markdown("<span class='sidebar-section-label'>予測モデル</span>", unsafe_allow_html=True)
        model_mode = st.radio(
            "モデル選択",
            ["ルールベース", "XGBoost ML"],
            index=1 if st.session_state.get("model_mode", "XGBoost ML") == "XGBoost ML" else 0,
            label_visibility="collapsed",
        )
        if model_mode != st.session_state.get("model_mode", "XGBoost ML"):
            st.session_state["model_mode"] = model_mode
            get_prediction.clear()
            st.rerun()
        badge_color = "#0f68d9" if model_mode == "XGBoost ML" else "#607086"
        st.markdown(
            f"<div style='font-size:10px;color:{badge_color};padding:2px 4px 8px'>"
            f"{'🤖 ML (AUC 0.875)' if model_mode == 'XGBoost ML' else '📐 ルールベース'}</div>",
            unsafe_allow_html=True
        )
        st.divider()
        if page in ("races", "detail", "overview"):
            st.markdown(
                f"<div style='font-size:11px;color:rgba(255,255,255,0.55);padding:0 4px'>"
                f"📍 {st.session_state.venue_name or ''}"
                + (f"<br>　└ {st.session_state.race_no}R" if page == "detail" else "")
                + "</div>",
                unsafe_allow_html=True
            )
            st.divider()
        date = latest_date()
        st.markdown(
            f"<div style='font-size:11px;color:rgba(255,255,255,0.35);padding:0 4px;line-height:1.8'>"
            f"📅 {date[:4]}/{date[4:6]}/{date[6:8]}<br>"
            f"🔄 自動収集: 8:00〜21:30</div>",
            unsafe_allow_html=True
        )


# ─── Main ─────────────────────────────────────────────────────────────────────
show_sidebar()
{
    "home":         show_home,
    "recommend":    show_recommend,
    "ml_recommend": show_ml_recommend,
    "races":        show_races,
    "overview":     show_overview,
    "detail":       show_detail,
    "odds":         show_odds,
    "history":      show_history,
    "finance":      show_finance,
    "accuracy":     show_accuracy,
    "analysis":     show_analysis,
}.get(st.session_state.page, show_home)()
