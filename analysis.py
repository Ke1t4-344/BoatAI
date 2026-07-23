"""
analysis.py — BoatAI 高度分析モジュール

全関数は読み取り専用（DBへの書き込みなし）。
app.py から import して @st.cache_data と組み合わせて使用する。

実装機能:
    1. get_kimari_te_distribution  — 出目分析（決まり手×2・3着コース分布）
    2. get_kimari_te_summary       — 決まり手別出現率サマリー
    3. get_head_to_head            — 対戦履歴分析（2選手の直接対決）
    4. get_compatibility_matrix    — 選手相性マトリクス（全ペアWR）
    5. get_meet_trend              — 節間調子トレンド
    6. get_odds_anomalies          — オッズ歪み自動検出
    7. get_motor_performance       — モーター性能スコア（展示タイム回帰）
    8. get_st_course_profile       — ST分布×コース期待着順
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

import pandas as pd

DB_PATH = Path(__file__).parent / "boatai.db"

TAKE_RATE = 0.75  # ボートレースの払戻率

# モデル順位別の確率推定（予測上位5コンボへの重み）
MODEL_RANK_WEIGHTS = [0.40, 0.25, 0.15, 0.10, 0.10]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


# ──────────────────────────────────────────────────────────────
# 1 & 2. 出目分析
# ──────────────────────────────────────────────────────────────

def get_kimari_te_summary(
    conn: sqlite3.Connection,
    venue_code: Optional[str] = None,
) -> pd.DataFrame:
    """
    決まり手別の出現率サマリー（全会場 or 指定会場）。

    Returns:
        DataFrame: winning_trick, cnt, pct
    """
    if venue_code:
        sql = """
            SELECT rre.winning_trick, COUNT(*) AS cnt
            FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE rre.rank = 1
              AND rre.winning_trick IS NOT NULL
              AND r.venue_code = ?
            GROUP BY rre.winning_trick
            ORDER BY cnt DESC
        """
        df = pd.read_sql_query(sql, conn, params=[venue_code])
    else:
        sql = """
            SELECT winning_trick, COUNT(*) AS cnt
            FROM race_result_entries
            WHERE rank = 1 AND winning_trick IS NOT NULL
            GROUP BY winning_trick
            ORDER BY cnt DESC
        """
        df = pd.read_sql_query(sql, conn)

    if df.empty:
        return df
    df["pct"] = (df["cnt"] / df["cnt"].sum() * 100).round(1)
    return df


def get_kimari_te_distribution(
    conn: sqlite3.Connection,
    winner_course: Optional[int] = None,
    kimari_te: Optional[str] = None,
    venue_code: Optional[str] = None,
    min_count: int = 5,
) -> pd.DataFrame:
    """
    1着コース×決まり手ごとの 2・3着コース出現率を集計。

    Args:
        winner_course: 1着コース（1〜6、None で全コース）
        kimari_te:     決まり手（None で全種類）
        venue_code:    会場コード（None で全会場）
        min_count:     最低件数（これ未満の組み合わせは除外）

    Returns:
        DataFrame: winner_course, kimari_te, place2_course, place3_course, cnt, pct
    """
    conditions = [
        "w.rank = 1",
        "w.winning_trick IS NOT NULL",
        "w.start_course IS NOT NULL",
    ]
    params = []

    if winner_course:
        conditions.append("w.start_course = ?")
        params.append(winner_course)
    if kimari_te:
        conditions.append("w.winning_trick = ?")
        params.append(kimari_te)
    if venue_code:
        conditions.append("r.venue_code = ?")
        params.append(venue_code)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            w.start_course  AS winner_course,
            w.winning_trick AS kimari_te,
            p2.start_course AS place2_course,
            p3.start_course AS place3_course,
            COUNT(*)        AS cnt
        FROM race_result_entries w
        JOIN race_result_entries p2 ON p2.race_id = w.race_id AND p2.rank = 2
        JOIN race_result_entries p3 ON p3.race_id = w.race_id AND p3.rank = 3
        JOIN races r ON r.id = w.race_id
        WHERE {where}
        GROUP BY winner_course, kimari_te, place2_course, place3_course
        HAVING cnt >= {min_count}
        ORDER BY winner_course, kimari_te, cnt DESC
    """

    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return df

    # 1着コース×決まり手ごとの合計でパーセンテージ計算
    totals = df.groupby(["winner_course", "kimari_te"])["cnt"].transform("sum")
    df["pct"] = (df["cnt"] / totals * 100).round(1)
    return df


def get_kimari_te_heatmap(
    conn: sqlite3.Connection,
    winner_course: int,
    kimari_te: str,
    venue_code: Optional[str] = None,
) -> dict:
    """
    指定した1着コース×決まり手の 2着・3着コース分布を
    ヒートマップ用の dict 形式で返す。

    Returns:
        {"place2": {course: pct, ...}, "place3": {course: pct, ...}, "total": int}
    """
    df = get_kimari_te_distribution(
        conn, winner_course=winner_course, kimari_te=kimari_te,
        venue_code=venue_code, min_count=1,
    )
    if df.empty:
        return {"place2": {}, "place3": {}, "total": 0}

    total = df["cnt"].sum()

    place2 = (
        df.groupby("place2_course")["cnt"].sum() / total * 100
    ).round(1).to_dict()
    place3 = (
        df.groupby("place3_course")["cnt"].sum() / total * 100
    ).round(1).to_dict()

    return {"place2": place2, "place3": place3, "total": int(total)}


# ──────────────────────────────────────────────────────────────
# 3. 対戦履歴分析
# ──────────────────────────────────────────────────────────────

def get_head_to_head(
    conn: sqlite3.Connection,
    player_no_a: str,
    player_no_b: str,
    recent_n: int = 10,
) -> dict:
    """
    2選手の同一レース出走時の着順比較（直接対決履歴）。

    Returns:
        dict: total, a_wins, b_wins, a_win_rate, name_a, name_b, recent(list)
    """
    sql = """
        SELECT
            ra.player_no  AS player_a,
            ra.player_name AS name_a,
            ra.rank        AS rank_a,
            rb.player_no  AS player_b,
            rb.player_name AS name_b,
            rb.rank        AS rank_b,
            r.date,
            r.venue_code,
            r.race_no
        FROM race_result_entries ra
        JOIN race_result_entries rb
            ON rb.race_id = ra.race_id AND rb.player_no = ?
        JOIN races r ON r.id = ra.race_id
        WHERE ra.player_no = ?
          AND ra.rank IS NOT NULL
          AND rb.rank IS NOT NULL
        ORDER BY r.date DESC, r.race_no DESC
    """
    df = pd.read_sql_query(sql, conn, params=[player_no_b, player_no_a])

    if df.empty:
        return {
            "total": 0, "a_wins": 0, "b_wins": 0, "a_win_rate": 0.0,
            "name_a": player_no_a, "name_b": player_no_b, "recent": [],
        }

    total  = len(df)
    a_wins = int((df["rank_a"] < df["rank_b"]).sum())
    b_wins = int((df["rank_b"] < df["rank_a"]).sum())

    return {
        "total":      total,
        "a_wins":     a_wins,
        "b_wins":     b_wins,
        "a_win_rate": round(a_wins / total * 100, 1),
        "name_a":     df["name_a"].iloc[0],
        "name_b":     df["name_b"].iloc[0],
        "recent":     df.head(recent_n)[
            ["date", "venue_code", "race_no", "rank_a", "rank_b"]
        ].to_dict("records"),
    }


# ──────────────────────────────────────────────────────────────
# 4. 選手相性マトリクス
# ──────────────────────────────────────────────────────────────

def get_compatibility_matrix(
    conn: sqlite3.Connection,
    player_nos: list,
    min_count: int = 3,
) -> pd.DataFrame:
    """
    指定選手リスト内の全ペアの直接対決勝率マトリクスを返す。
    行Aが列Bに勝った確率（%）。

    Returns:
        DataFrame: pivot(index=player_a, columns=player_b, values=win_rate%)
                   選手名も付与したラベル付き
    """
    if len(player_nos) < 2:
        return pd.DataFrame()

    ph = ",".join("?" * len(player_nos))

    sql = f"""
        SELECT
            ra.player_no   AS player_a,
            ra.player_name AS name_a,
            rb.player_no   AS player_b,
            rb.player_name AS name_b,
            COUNT(*)       AS cnt,
            SUM(CASE WHEN ra.rank < rb.rank THEN 1 ELSE 0 END) AS a_wins
        FROM race_result_entries ra
        JOIN race_result_entries rb
            ON rb.race_id  = ra.race_id
           AND rb.player_no != ra.player_no
           AND rb.player_no IN ({ph})
        WHERE ra.player_no IN ({ph})
          AND ra.rank IS NOT NULL
          AND rb.rank IS NOT NULL
        GROUP BY ra.player_no, rb.player_no
        HAVING cnt >= {min_count}
    """

    df = pd.read_sql_query(sql, conn, params=player_nos + player_nos)

    if df.empty:
        return pd.DataFrame()

    df["win_rate"] = (df["a_wins"] / df["cnt"] * 100).round(1)

    pivot = df.pivot_table(
        index="player_a", columns="player_b",
        values="win_rate", aggfunc="first",
    )
    return pivot


def get_player_names(
    conn: sqlite3.Connection,
    player_nos: list,
) -> dict:
    """player_no → player_name のマッピングを返す"""
    ph = ",".join("?" * len(player_nos))
    sql = f"""
        SELECT player_no, player_name
        FROM entries
        WHERE player_no IN ({ph})
        GROUP BY player_no
        ORDER BY MAX(id) DESC
    """
    df = pd.read_sql_query(sql, conn, params=player_nos)
    return dict(zip(df["player_no"], df["player_name"]))


# ──────────────────────────────────────────────────────────────
# 5. 節間調子トレンド
# ──────────────────────────────────────────────────────────────

def get_meet_trend(
    conn: sqlite3.Connection,
    player_no: str,
    venue_code: str,
    date: str,       # YYYYMMDD
    days: int = 7,
) -> dict:
    """
    指定選手の今節（同会場・直近 days 日）の日別成績推移。

    Returns:
        dict: days(list), trend('up'|'down'|'flat'), trend_score(float)
    """
    dt        = datetime.strptime(date, "%Y%m%d")
    date_from = (dt - timedelta(days=days - 1)).strftime("%Y%m%d")

    sql = """
        SELECT r.date, rre.rank, r.race_no
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.player_no = ?
          AND r.venue_code  = ?
          AND r.date BETWEEN ? AND ?
          AND rre.rank IS NOT NULL
        ORDER BY r.date, r.race_no
    """
    df = pd.read_sql_query(sql, conn, params=[player_no, venue_code, date_from, date])

    if df.empty:
        return {"days": [], "trend": "flat", "trend_score": 0.0}

    by_day = (
        df.groupby("date")
        .agg(
            races     = ("rank", "count"),
            avg_rank  = ("rank", "mean"),
            win_count = ("rank", lambda x: int((x == 1).sum())),
        )
        .reset_index()
    )
    by_day["avg_rank"] = by_day["avg_rank"].round(2)
    days_list = by_day.to_dict("records")

    # トレンド判定: 前半と後半の平均着順を比較
    if len(days_list) >= 2:
        mid         = len(days_list) // 2
        early_avg   = by_day.iloc[:mid]["avg_rank"].mean()
        late_avg    = by_day.iloc[mid:]["avg_rank"].mean()
        trend_score = round(float(early_avg - late_avg), 2)   # 正=改善=上り調子
        trend       = "up" if trend_score > 0.5 else ("down" if trend_score < -0.5 else "flat")
    else:
        trend_score = 0.0
        trend       = "flat"

    return {"days": days_list, "trend": trend, "trend_score": trend_score}


# ──────────────────────────────────────────────────────────────
# 6. オッズ歪み自動検出
# ──────────────────────────────────────────────────────────────

def get_odds_anomalies(
    conn: sqlite3.Connection,
    race_id: int,
    min_gap: float = 5.0,
) -> list:
    """
    predictionsのモデル上位コンボ × odds_3tの市場確率を突合。
    「モデル評価 > 市場評価」のギャップが大きい組み合わせを返す。

    Args:
        min_gap: モデル確率 − 市場確率（%）の最小閾値

    Returns:
        list of dict: combo, model_rank, odds, market_prob, model_prob, gap
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT top5_combos FROM predictions WHERE race_id = ? ORDER BY predicted_at DESC LIMIT 1",
        (race_id,),
    )
    row = cur.fetchone()
    if not row:
        return []

    top5        = json.loads(row[0])
    model_probs = {
        combo: w for combo, w in zip(top5, MODEL_RANK_WEIGHTS)
    }

    # 最新フェッチのオッズのみ取得（fetched_at=NULLの場合も対応）
    max_fa = cur.execute(
        "SELECT MAX(fetched_at) FROM odds_3t WHERE race_id=?", (race_id,)
    ).fetchone()[0]
    if max_fa is not None:
        cur.execute(
            "SELECT combination, odds FROM odds_3t WHERE race_id=? AND fetched_at=?",
            (race_id, max_fa),
        )
    else:
        cur.execute(
            "SELECT combination, odds FROM odds_3t WHERE race_id=?",
            (race_id,),
        )
    odds_dict = {r[0]: r[1] for r in cur.fetchall()}

    results = []
    for rank, combo in enumerate(top5, 1):
        odds = odds_dict.get(combo)
        if not odds or odds <= 0:
            continue
        market_prob = round(TAKE_RATE / odds * 100, 1)
        model_prob  = round(model_probs[combo] * 100, 1)
        gap         = round(model_prob - market_prob, 1)
        if gap >= min_gap:
            results.append({
                "combo":       combo,
                "model_rank":  rank,
                "odds":        odds,
                "market_prob": market_prob,
                "model_prob":  model_prob,
                "gap":         gap,
            })

    results.sort(key=lambda x: x["gap"], reverse=True)
    return results


# ──────────────────────────────────────────────────────────────
# 7. モーター性能スコア
# ──────────────────────────────────────────────────────────────

def get_motor_performance(
    conn: sqlite3.Connection,
    race_id: int,
) -> pd.DataFrame:
    """
    展示タイム・直線タイム・周回タイムをZ標準化して
    モーター出力スコアを算出。

    Returns:
        DataFrame: boat_no, exhibition_time, tilt, straight_time,
                   lap_time, motor_score, motor_rank
    """
    sql = """
        SELECT boat_no, exhibition_time, tilt, straight_time, lap_time
        FROM before_info
        WHERE race_id = ? AND exhibition_time IS NOT NULL
        ORDER BY boat_no
    """
    df = pd.read_sql_query(sql, conn, params=[race_id])

    if df.empty or len(df) < 2:
        return df

    # Z標準化（標準偏差0のカラムはスキップ）
    def zscore(series):
        std = series.std()
        return (series - series.mean()) / std if std > 0 else pd.Series(0.0, index=series.index)

    df["straight_time"] = pd.to_numeric(df["straight_time"], errors="coerce")
    df["lap_time"]      = pd.to_numeric(df["lap_time"],      errors="coerce")

    z_ex  = zscore(df["exhibition_time"])
    z_st  = zscore(df["straight_time"].fillna(df["straight_time"].mean()))
    z_lap = zscore(df["lap_time"].fillna(df["lap_time"].mean()))

    # タイム系は小さいほど速い → 符号反転して高スコア=高性能
    df["motor_score"] = (-z_ex * 0.5 + -z_st * 0.3 + -z_lap * 0.2).round(3)
    df["motor_rank"]  = df["motor_score"].rank(ascending=False).astype(int)

    return df[["boat_no", "exhibition_time", "tilt", "straight_time",
               "lap_time", "motor_score", "motor_rank"]]


# ──────────────────────────────────────────────────────────────
# 8. ST分布×コース期待着順
# ──────────────────────────────────────────────────────────────

def get_st_course_profile(
    conn: sqlite3.Connection,
    player_no: str,
    course: int,
    min_samples: int = 5,
) -> dict:
    """
    選手のコース別ST分布と期待着順を st_history から集計。

    Returns:
        dict: course, samples, avg_st, std_st, avg_rank, win_rate, top3_rate
    """
    sql = """
        SELECT start_timing, finish_rank
        FROM st_history
        WHERE player_no = ?
          AND start_course = ?
          AND start_timing IS NOT NULL
          AND finish_rank  IS NOT NULL
    """
    df = pd.read_sql_query(sql, conn, params=[player_no, course])

    df["st"] = pd.to_numeric(df["start_timing"], errors="coerce")
    df = df.dropna(subset=["st"])

    if len(df) < min_samples:
        return {
            "course": course, "samples": len(df),
            "avg_st": None, "std_st": None,
            "avg_rank": None, "win_rate": None, "top3_rate": None,
        }

    return {
        "course":    course,
        "samples":   len(df),
        "avg_st":    round(float(df["st"].mean()), 3),
        "std_st":    round(float(df["st"].std()), 3),
        "avg_rank":  round(float(df["finish_rank"].mean()), 2),
        "win_rate":  round(float((df["finish_rank"] == 1).sum() / len(df) * 100), 1),
        "top3_rate": round(float((df["finish_rank"] <= 3).sum() / len(df) * 100), 1),
    }


def get_st_profiles_for_race(
    conn: sqlite3.Connection,
    race_id: int,
    min_samples: int = 5,
) -> pd.DataFrame:
    """
    レースの全出走選手のコース別STプロファイルをまとめて返す。
    before_info から今日のコース進入を取得してプロファイルと突合。

    Returns:
        DataFrame: boat_no, player_no, player_name, exhibit_course,
                   avg_st, std_st, avg_rank, win_rate, top3_rate, samples
    """
    # 今日の出走表とコース進入
    sql = """
        SELECT e.boat_no, e.player_no, e.player_name,
               bi.exhibit_course
        FROM entries e
        LEFT JOIN before_info bi ON bi.race_id = e.race_id AND bi.boat_no = e.boat_no
        WHERE e.race_id = ?
        ORDER BY e.boat_no
    """
    entries_df = pd.read_sql_query(sql, conn, params=[race_id])

    if entries_df.empty:
        return pd.DataFrame()

    rows = []
    for _, row in entries_df.iterrows():
        course  = int(row["exhibit_course"]) if row["exhibit_course"] else int(row["boat_no"])
        profile = get_st_course_profile(conn, str(row["player_no"]), course, min_samples)
        rows.append({**row.to_dict(), **profile})

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────
# 9. 選手別 出目パターン分析
# ──────────────────────────────────────────────────────────────

def get_player_outcome_patterns(
    conn: sqlite3.Connection,
    player_no: str,
    min_count: int = 2,
    start_course: Optional[int] = None,
) -> dict:
    """
    選手の過去レース結果から出目パターンを集計。

    start_course を指定すると、そのコースから出走したレースのみ集計。
    1着時: 決まり手別に「2着コース・3着コース」の分布（自コースは別途返す）
    2着時: 「1着コース-3着コース」の分布
    3着時: 「1着コース-2着コース」の分布
    """
    course_filter = "AND w.start_course = ?" if start_course else ""
    course_params = [player_no] + ([start_course] if start_course else [])

    # ── 1着時: 決まり手別に2着・3着コース集計 ──────────────────────────────
    sql_win = f"""
        SELECT
            w.winning_trick       AS kimari_te,
            w.start_course        AS self_course,
            p2.start_course       AS p2_course,
            p3.start_course       AS p3_course,
            COUNT(*)              AS cnt
        FROM race_result_entries w
        JOIN race_result_entries p2 ON p2.race_id = w.race_id AND p2.rank = 2
        JOIN race_result_entries p3 ON p3.race_id = w.race_id AND p3.rank = 3
        WHERE w.player_no = ?
          AND w.rank = 1
          AND w.winning_trick IS NOT NULL
          AND w.start_course  IS NOT NULL
          AND p2.start_course IS NOT NULL
          AND p3.start_course IS NOT NULL
          AND p2.start_course != w.start_course
          AND p3.start_course != w.start_course
          AND p2.start_course != p3.start_course
          {course_filter}
        GROUP BY kimari_te, self_course, p2_course, p3_course
        ORDER BY kimari_te, cnt DESC
    """
    df_win = pd.read_sql_query(sql_win, conn, params=course_params)

    win_by_trick = {}
    if not df_win.empty:
        for kt, grp in df_win.groupby("kimari_te"):
            total = int(grp["cnt"].sum())
            self_course_val = int(grp["self_course"].iloc[0])

            # 2着コース別の小計（ユーザーが最も知りたい情報）
            by_p2 = (
                grp.groupby("p2_course")["cnt"].sum()
                .reset_index()
                .sort_values("cnt", ascending=False)
            )
            p2_dist = [
                {
                    "p2":  int(r["p2_course"]),
                    "cnt": int(r["cnt"]),
                    "pct": round(int(r["cnt"]) / total * 100, 1),
                }
                for _, r in by_p2.iterrows()
            ]

            # 3連単コンボ（p2別にp3を展開）
            combos = []
            for _, row in by_p2.iterrows():
                p3_grp = (
                    grp[grp["p2_course"] == row["p2_course"]]
                    .groupby("p3_course")["cnt"].sum()
                    .reset_index()
                    .sort_values("cnt", ascending=False)
                )
                for _, p3r in p3_grp.iterrows():
                    combos.append({
                        "self": self_course_val,
                        "p2":   int(row["p2_course"]),
                        "p3":   int(p3r["p3_course"]),
                        "cnt":  int(p3r["cnt"]),
                        "pct":  round(int(p3r["cnt"]) / total * 100, 1),
                    })
            win_by_trick[kt] = {
                "total":       total,
                "self_course": self_course_val,
                "p2_dist":     p2_dist,      # 2着コース分布（新）
                "combos":      combos[:10],
            }

    # ── 2着時: 1着コース・3着コース集計 ────────────────────────────────────
    sql_p2 = """
        SELECT
            p1.start_course  AS p1_course,
            self.start_course AS self_course,
            p3.start_course  AS p3_course,
            COUNT(*)         AS cnt
        FROM race_result_entries self
        JOIN race_result_entries p1 ON p1.race_id = self.race_id AND p1.rank = 1
        JOIN race_result_entries p3 ON p3.race_id = self.race_id AND p3.rank = 3
        WHERE self.player_no = ?
          AND self.rank = 2
          AND self.start_course IS NOT NULL
        GROUP BY p1_course, self_course, p3_course
        ORDER BY cnt DESC
    """
    df_p2 = pd.read_sql_query(sql_p2, conn, params=[player_no])
    place2 = {"total": 0, "combos": []}
    if not df_p2.empty:
        total2 = int(df_p2["cnt"].sum())
        place2 = {
            "total": total2,
            "combos": [
                {
                    "p1":  int(r["p1_course"]),
                    "p3":  int(r["p3_course"]),
                    "cnt": int(r["cnt"]),
                    "pct": round(int(r["cnt"]) / total2 * 100, 1),
                }
                for _, r in df_p2.head(10).iterrows()
            ],
        }

    # ── 3着時: 1着コース・2着コース集計 ────────────────────────────────────
    sql_p3 = """
        SELECT
            p1.start_course  AS p1_course,
            p2.start_course  AS p2_course,
            self.start_course AS self_course,
            COUNT(*)         AS cnt
        FROM race_result_entries self
        JOIN race_result_entries p1 ON p1.race_id = self.race_id AND p1.rank = 1
        JOIN race_result_entries p2 ON p2.race_id = self.race_id AND p2.rank = 2
        WHERE self.player_no = ?
          AND self.rank = 3
          AND self.start_course IS NOT NULL
        GROUP BY p1_course, p2_course, self_course
        ORDER BY cnt DESC
    """
    df_p3 = pd.read_sql_query(sql_p3, conn, params=[player_no])
    place3 = {"total": 0, "combos": []}
    if not df_p3.empty:
        total3 = int(df_p3["cnt"].sum())
        place3 = {
            "total": total3,
            "combos": [
                {
                    "p1":  int(r["p1_course"]),
                    "p2":  int(r["p2_course"]),
                    "cnt": int(r["cnt"]),
                    "pct": round(int(r["cnt"]) / total3 * 100, 1),
                }
                for _, r in df_p3.head(10).iterrows()
            ],
        }

    win_total = int(df_win["cnt"].sum()) if not df_win.empty else 0

    return {
        "win":       win_by_trick,
        "place2":    place2,
        "place3":    place3,
        "win_total": win_total,
    }


# ──────────────────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────────────────

VENUE_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡", "08": "常滑",
    "09": "津",   "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島",
    "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}


def venue_name(code: str) -> str:
    return VENUE_NAMES.get(str(code).zfill(2), code)
