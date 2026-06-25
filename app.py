"""
app.py — BoatAI 競艇予想 Webアプリ
"""
import streamlit as st
import sqlite3
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from predict import predict as _predict, DB_PATH

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="BoatAI 競艇予想",
    page_icon="⛵",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Session state ────────────────────────────────────────────────────────────
for _k, _v in [("page", "home"), ("venue_code", None), ("venue_name", None), ("race_no", None)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


def nav(page, **kw):
    st.session_state.page = page
    for k, v in kw.items():
        st.session_state[k] = v
    st.rerun()


# ─── DB helpers ───────────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


@st.cache_data(ttl=120)
def latest_date():
    with _conn() as c:
        return c.execute("SELECT MAX(date) FROM races").fetchone()[0]


@st.cache_data(ttl=60)
def get_venues(date):
    with _conn() as c:
        race_rows = c.execute(
            "SELECT id, venue_code FROM races WHERE date=?", (date,)
        ).fetchall()
    if not race_rows:
        return []

    venue_races = {}
    for r in race_rows:
        vc = r["venue_code"]
        venue_races.setdefault(vc, []).append(r["id"])

    all_ids = [r["id"] for r in race_rows]
    ph = ",".join("?" * len(all_ids))
    with _conn() as c:
        result_ids = {r[0] for r in c.execute(
            f"SELECT DISTINCT race_id FROM race_result_entries WHERE race_id IN ({ph})", all_ids
        ).fetchall()}
        bi_ids = {r[0] for r in c.execute(
            f"SELECT DISTINCT race_id FROM before_info WHERE exhibition_time IS NOT NULL AND race_id IN ({ph})", all_ids
        ).fetchall()}
        venue_map = {r[0]: r[1] for r in c.execute("SELECT venue_code, venue_name FROM venues").fetchall()}

    return [
        {
            "venue_code":   vc,
            "venue_name":   venue_map.get(vc, vc),
            "race_count":   len(rids),
            "result_count": sum(1 for rid in rids if rid in result_ids),
            "bi_count":     sum(1 for rid in rids if rid in bi_ids),
        }
        for vc, rids in sorted(venue_races.items())
    ]


@st.cache_data(ttl=60)
def get_races(date, venue_code):
    with _conn() as c:
        races = c.execute(
            "SELECT id, race_no, race_title FROM races WHERE date=? AND venue_code=? ORDER BY race_no",
            (date, venue_code),
        ).fetchall()
    if not races:
        return []
    ids = [r["id"] for r in races]
    ph = ",".join("?" * len(ids))
    with _conn() as c:
        result_ids = {r[0] for r in c.execute(
            f"SELECT DISTINCT race_id FROM race_result_entries WHERE race_id IN ({ph})", ids
        ).fetchall()}
        bi_ids = {r[0] for r in c.execute(
            f"SELECT DISTINCT race_id FROM before_info WHERE exhibition_time IS NOT NULL AND race_id IN ({ph})", ids
        ).fetchall()}
    return [
        {
            "race_no":    r["race_no"],
            "race_title": r["race_title"] or f"{r['race_no']}R",
            "has_result": r["id"] in result_ids,
            "has_bi":     r["id"] in bi_ids,
        }
        for r in races
    ]


@st.cache_data(ttl=300)
def get_prediction(date, venue_code, race_no):
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
            WHERE r.date=? AND r.venue_code=? AND r.race_no=?
            ORDER BY rre.rank
        """, (date, venue_code, race_no)).fetchall()
    return [dict(r) for r in rows] if rows else None


# ─── Bets helpers ─────────────────────────────────────────────────────────────
def ensure_bets():
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT    NOT NULL,
            venue_code   TEXT    NOT NULL,
            race_no      INTEGER NOT NULL,
            combination  TEXT    NOT NULL,
            bet_type     TEXT    NOT NULL DEFAULT '3連単',
            amount       INTEGER NOT NULL,
            result       TEXT    NOT NULL DEFAULT 'unknown',
            payout       INTEGER NOT NULL DEFAULT 0,
            profit       INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT    NOT NULL
        )
    """)
    c.commit()
    c.close()


def check_bet_result(date, venue_code, race_no, combination, bet_type):
    with _conn() as c:
        n = c.execute("""
            SELECT COUNT(*) FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=?
        """, (date, venue_code, race_no)).fetchone()[0]
        if not n:
            return "unknown", 0
        pay = c.execute("""
            SELECT p.payout FROM payouts p
            JOIN races r ON r.id = p.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=?
              AND p.bet_type=? AND p.combination=?
        """, (date, venue_code, race_no, bet_type, combination)).fetchone()
    return ("win", pay["payout"]) if pay else ("lose", 0)


def add_bet(date, venue_code, race_no, combination, bet_type, amount):
    ensure_bets()
    result, payout = check_bet_result(date, venue_code, race_no, combination, bet_type)
    profit = (payout * amount // 100 - amount) if result == "win" else (-amount if result == "lose" else 0)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        "INSERT INTO bets (date,venue_code,race_no,combination,bet_type,amount,result,payout,profit,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (date, venue_code, race_no, combination, bet_type, amount,
         result, payout, profit, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    c.commit()
    c.close()


def refresh_bets():
    ensure_bets()
    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT id,date,venue_code,race_no,combination,bet_type,amount FROM bets WHERE result='unknown'"
    ).fetchall()
    n = 0
    for r in rows:
        res, payout = check_bet_result(r[1], r[2], r[3], r[4], r[5])
        if res != "unknown":
            profit = (payout * r[6] // 100 - r[6]) if res == "win" else -r[6]
            c.execute("UPDATE bets SET result=?,payout=?,profit=? WHERE id=?", (res, payout, profit, r[0]))
            n += 1
    c.commit()
    c.close()
    return n


def get_bets():
    ensure_bets()
    with _conn() as c:
        rows = c.execute("""
            SELECT b.id, b.date, COALESCE(v.venue_name, b.venue_code) AS venue_name,
                   b.race_no, b.combination, b.bet_type,
                   b.amount, b.result, b.payout, b.profit, b.created_at
            FROM bets b
            LEFT JOIN venues v ON v.venue_code = b.venue_code
            ORDER BY b.date DESC, b.created_at DESC
        """).fetchall()
    cols = ["id","date","venue_name","race_no","combination","bet_type",
            "amount","result","payout","profit","created_at"]
    return pd.DataFrame([dict(r) for r in rows], columns=cols) if rows else pd.DataFrame(columns=cols)


def delete_bet(bid):
    c = sqlite3.connect(DB_PATH)
    c.execute("DELETE FROM bets WHERE id=?", (bid,))
    c.commit()
    c.close()


# ─── Chart helpers ────────────────────────────────────────────────────────────
COURSE_COLORS = {
    1: "#F59E0B", 2: "#3B82F6", 3: "#10B981",
    4: "#8B5CF6", 5: "#EF4444", 6: "#6B7280",
}


def score_chart(boats):
    boats_asc = sorted(boats, key=lambda b: b["score"])
    labels = [f"{b['boat_no']}号艇  {b['player_name']}" for b in boats_asc]
    scores = [b["score"] for b in boats_asc]
    colors = [COURSE_COLORS.get(b["start_course"], "#6B7280") for b in boats_asc]
    texts  = [f"{s:.0f}pt" for s in scores]

    fig = go.Figure(go.Bar(
        x=scores, y=labels, orientation="h",
        text=texts, textposition="outside",
        marker_color=colors,
        hovertemplate="<b>%{y}</b><br>スコア: %{x:.1f}<extra></extra>",
    ))
    fig.update_layout(
        xaxis=dict(range=[0, 135], title="スコア", gridcolor="#1e2740"),
        yaxis=dict(title=""),
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font=dict(color="#e2e8f0", size=13),
        height=300, margin=dict(l=0, r=10, t=10, b=10),
        showlegend=False,
    )
    return fig


def pnl_chart(df):
    df = df[df["result"] != "unknown"].sort_values(["date", "created_at"]).copy()
    if df.empty:
        return None
    df["cumulative"] = df["profit"].cumsum()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=list(range(len(df))),
        y=df["profit"],
        marker_color=["#34d399" if p >= 0 else "#f87171" for p in df["profit"]],
        name="単発損益",
        hovertemplate="損益: %{y:+,}円<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=list(range(len(df))),
        y=df["cumulative"],
        mode="lines+markers",
        line=dict(color="#60a5fa", width=2),
        marker=dict(size=7, color="#60a5fa"),
        name="累積損益",
        hovertemplate="累積: %{y:+,}円<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="#475569", line_width=1)
    fig.update_layout(
        xaxis=dict(title="", showticklabels=False, gridcolor="#1e2740"),
        yaxis=dict(title="損益（円）", gridcolor="#1e2740"),
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font=dict(color="#e2e8f0"),
        height=300, margin=dict(l=0, r=0, t=10, b=10),
        legend=dict(orientation="h", y=1.05, x=0),
    )
    return fig


# ─── Page: Home ───────────────────────────────────────────────────────────────
def show_home():
    date    = latest_date()
    venues  = get_venues(date)
    fmt     = f"{date[:4]}/{date[4:6]}/{date[6:8]}"

    st.title("⛵ BoatAI 競艇予想")
    st.caption(f"開催日: {fmt}　{len(venues)}会場")
    st.divider()

    if not venues:
        st.info("本日の開催データがありません。")
        return

    for row_start in range(0, len(venues), 4):
        cols = st.columns(4)
        for i, v in enumerate(venues[row_start:row_start + 4]):
            rc, res, bi = v["race_count"], v["result_count"], v["bi_count"]
            if res >= rc:
                badge = "✅ 全レース確定"
            elif res > 0:
                badge = f"🏁 {res}/{rc}R 確定済み"
            elif bi > 0:
                badge = f"⚡ {bi}R 直前情報あり"
            else:
                badge = f"📋 {rc}R 予想可"
            with cols[i]:
                with st.container(border=True):
                    st.markdown(f"### {v['venue_name']}")
                    st.caption(badge)
                    if st.button("レース一覧 →", key=f"v_{v['venue_code']}", use_container_width=True):
                        nav("races", venue_code=v["venue_code"], venue_name=v["venue_name"])


# ─── Page: Race list ──────────────────────────────────────────────────────────
def show_races():
    date   = latest_date()
    vc     = st.session_state.venue_code
    vn     = st.session_state.venue_name or vc
    races  = get_races(date, vc)

    if st.button("← トップに戻る"):
        nav("home")

    st.title(f"🏟 {vn}")
    st.caption(f"開催日: {date[:4]}/{date[4:6]}/{date[6:8]}")
    st.divider()

    if not races:
        st.warning("レースデータが見つかりません。")
        return

    for row_start in range(0, len(races), 4):
        cols = st.columns(4)
        for i, r in enumerate(races[row_start:row_start + 4]):
            if r["has_result"]:
                status = "✅ 確定済み"
            elif r["has_bi"]:
                status = "⚡ 直前情報あり"
            else:
                status = "📋 予想可"
            with cols[i]:
                with st.container(border=True):
                    st.markdown(f"### {r['race_no']}R")
                    st.caption(r["race_title"])
                    st.caption(status)
                    if st.button("予想を見る", key=f"r_{r['race_no']}", use_container_width=True):
                        nav("detail", race_no=r["race_no"])


# ─── Page: Race detail ────────────────────────────────────────────────────────
def show_detail():
    date    = latest_date()
    vc      = st.session_state.venue_code
    vn      = st.session_state.venue_name or vc
    race_no = st.session_state.race_no

    if st.button("← レース一覧"):
        nav("races")

    st.title(f"⛵ {vn}　{race_no}R")

    pred, err = get_prediction(date, vc, race_no)
    if err:
        st.error(f"予想データ取得エラー: {err}")
        return

    ri    = pred["race_info"]
    boats = pred["boats"]
    rec   = pred["recommended_3t"]

    st.caption(f"{ri['date'][:4]}/{ri['date'][4:6]}/{ri['date'][6:8]}　{ri['race_title'] or ''}")
    st.divider()

    tab_pred, tab_entry = st.tabs(["📊 予想スコア", "📋 出走表"])

    with tab_pred:
        left, right = st.columns([6, 4])

        with left:
            st.subheader("艇別スコア")
            st.plotly_chart(score_chart(boats), use_container_width=True)

            # コース凡例
            leg_cols = st.columns(6)
            for ci, (cs, color) in enumerate(COURSE_COLORS.items()):
                leg_cols[ci].markdown(
                    f"<span style='background:{color};padding:2px 8px;"
                    f"border-radius:4px;font-size:0.8rem;color:#fff'>CS{cs}</span>",
                    unsafe_allow_html=True,
                )

        with right:
            # ── 推奨買い目 ──────────────────────────────────────────
            st.subheader("推奨買い目（3連単）")
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            for idx, combo in enumerate(rec):
                with st.container(border=True):
                    st.markdown(
                        f"<div style='display:flex;align-items:center;gap:14px;padding:4px 0'>"
                        f"<span style='font-size:1.3rem'>{medals[idx]}</span>"
                        f"<span style='font-size:1.9rem;font-weight:800;"
                        f"letter-spacing:5px;color:#93c5fd'>{combo}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

            # ── 勝率一覧 ─────────────────────────────────────────
            st.divider()
            st.subheader("勝率予測")
            for b in boats:
                bar_pct = int(b["win_prob"])
                st.markdown(
                    f"**{b['boat_no']}号艇** {b['player_name']}"
                    f"　`{b['win_prob']:.1f}%`　score:{b['score']:.0f}"
                )
                st.progress(bar_pct)

            # ── 確定結果（あれば） ────────────────────────────────
            result = get_result(date, vc, race_no)
            if result:
                st.divider()
                st.subheader("確定結果")
                top3 = [r for r in result if r["rank"] <= 3]
                for r in top3:
                    st.write(f"**{r['rank']}着**: {r['boat_no']}号艇 {r['player_name']}　ST:{r['start_timing']}")
                if len(top3) == 3:
                    actual = "-".join(str(r["boat_no"]) for r in top3)
                    st.success(f"3連単 確定: **{actual}**")
                    hit_key = f"balloons_{date}_{vc}_{race_no}"
                    if actual in rec:
                        if not st.session_state.get(hit_key):
                            st.balloons()
                            st.session_state[hit_key] = True
                        st.success("🎯 推奨買い目に的中！")
                    else:
                        st.info(f"推奨5点に含まれず（実際: {actual}）")

    with tab_entry:
        st.subheader("出走表")
        rows = []
        for b in sorted(boats, key=lambda x: x["boat_no"]):
            c = b["components"]
            rows.append({
                "艇":        b["boat_no"],
                "CS":        b["start_course"],
                "選手名":    b["player_name"],
                "全国勝率":  c["national_win_rate"],
                "当地勝率":  c["local_win_rate"],
                "モーター%": c["motor_2ring"],
                "展示T":     c["exhibition_time"],
                "ST":        c["start_timing"],
                "スコア":    b["score"],
                "勝率%":     b["win_prob"],
            })
        entry_df = pd.DataFrame(rows)
        st.dataframe(
            entry_df.style.background_gradient(subset=["スコア"], cmap="YlOrRd"),
            use_container_width=True,
            hide_index=True,
        )


# ─── Page: Finance ────────────────────────────────────────────────────────────
VENUE_OPTIONS = ["01","03","04","06","07","08","09","12","13","14","15","16","18","21","22","23"]
VENUE_NAMES   = {
    "01":"桐生","03":"江戸川","04":"平和島","06":"浜名湖","07":"蒲郡",
    "08":"常滑","09":"津","12":"住之江","13":"尼崎","14":"鳴門",
    "15":"丸亀","16":"児島","18":"徳山","21":"芦屋","22":"福岡","23":"唐津",
}


def show_finance():
    ensure_bets()
    st.title("💰 収支管理")

    # ── 入力フォーム ──────────────────────────────────────────────────────────
    with st.expander("➕ 購入記録を追加", expanded=True):
        with st.form("bet_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            fin_date   = c1.text_input("日付 (YYYYMMDD)", value=latest_date())
            fin_vc     = c2.selectbox("会場", VENUE_OPTIONS, format_func=lambda x: VENUE_NAMES.get(x, x))
            fin_rno    = c3.number_input("レース番号", min_value=1, max_value=12, value=1, step=1)

            c4, c5, c6 = st.columns(3)
            fin_combo  = c4.text_input("買い目 (例: 1-4-5)")
            fin_btype  = c5.selectbox("賭け式", ["3連単", "3連複", "2連単", "2連複"])
            fin_amount = c6.number_input("購入金額（円）", min_value=100, step=100, value=500)

            if st.form_submit_button("📝 記録する", use_container_width=True, type="primary"):
                combo = fin_combo.strip()
                if not combo:
                    st.error("買い目を入力してください。")
                else:
                    add_bet(fin_date, fin_vc, int(fin_rno), combo, fin_btype, int(fin_amount))
                    st.success(f"記録しました: {combo}  {int(fin_amount):,}円")
                    st.rerun()

    # 結果再チェック
    if st.button("🔄 結果を再チェック（未確定分）"):
        n = refresh_bets()
        st.toast(f"{n}件の結果を更新しました。" if n else "更新対象はありません。")
        st.rerun()

    df = get_bets()
    if df.empty:
        st.info("購入記録がありません。上のフォームから追加してください。")
        return

    # ── サマリーメトリクス ────────────────────────────────────────────────────
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
    m3.metric("回収率", f"{recovery:.1f}%", f"{recovery-100:+.1f}%")
    m4.metric("累積損益", f"{total_p:+,}円")

    # ── 損益グラフ ────────────────────────────────────────────────────────────
    fig = pnl_chart(decided)
    if fig:
        st.subheader("損益グラフ")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("確定した賭けが記録されるとグラフが表示されます。")

    # ── 履歴テーブル ──────────────────────────────────────────────────────────
    st.subheader("購入履歴")
    RESULT_MAP = {"win": "✅ 的中", "lose": "❌ 外れ", "unknown": "⏳ 未確定"}
    disp = df.copy()
    disp["結果"] = disp["result"].map(RESULT_MAP)
    disp["払戻"] = disp.apply(
        lambda row: f"{row['payout']}円/100円" if row["payout"] > 0 else "─", axis=1
    )
    disp["損益"] = disp["profit"].apply(
        lambda x: f"+{x:,}円" if x > 0 else (f"{x:,}円" if x < 0 else "─")
    )
    show = disp.rename(columns={
        "date": "日付", "venue_name": "会場", "race_no": "R",
        "combination": "買い目", "bet_type": "賭け式", "amount": "金額(円)",
        "id": "ID",
    })[["日付","会場","R","買い目","賭け式","金額(円)","結果","払戻","損益","ID"]]
    st.dataframe(show, use_container_width=True, hide_index=True)

    with st.expander("🗑 記録を削除"):
        del_id = st.number_input("削除するID（上の表のID列を参照）", min_value=1, step=1, key="del_id")
        if st.button("削除する", type="primary"):
            delete_bet(int(del_id))
            st.success(f"ID {int(del_id)} を削除しました。")
            st.rerun()


# ─── Sidebar ──────────────────────────────────────────────────────────────────
def show_sidebar():
    with st.sidebar:
        st.markdown("## ⛵ BoatAI")
        st.caption("競艇予想システム")
        st.divider()

        if st.button("🏠 トップ（開催一覧）", use_container_width=True):
            nav("home")
        if st.button("💰 収支管理", use_container_width=True):
            nav("finance")

        st.divider()
        page = st.session_state.page
        if page in ("races", "detail"):
            st.caption(f"📍 {st.session_state.venue_name or ''}")
            if page == "detail":
                st.caption(f"　└ {st.session_state.race_no}R")

        st.divider()
        date = latest_date()
        st.caption(f"📅 最終データ: {date[:4]}/{date[4:6]}/{date[6:8]}")


# ─── Main ─────────────────────────────────────────────────────────────────────
show_sidebar()
{
    "home":    show_home,
    "races":   show_races,
    "detail":  show_detail,
    "finance": show_finance,
}.get(st.session_state.page, show_home)()
