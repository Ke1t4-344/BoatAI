#!/usr/bin/env python3
"""
ml_pipeline.py — BoatAI XGBoost MLモデル 学習・評価パイプライン（v2: 全データ活用版）

【特徴量グループ】
  A. 基本特徴量 (entries, 99%+ カバレッジ):
     - 全国勝率/2連対率、当地勝率/2連対率
     - モーター2連対率、艇2連対率
     - ST平均値（公式統計）、フライング/遅刻回数、年齢/体重、選手クラス
     - コース別(course_stats)勝率/3連対率/ST平均（公式ウェブ取得）

  B. 実績ベース特徴量 (race_result_entries, 100% カバレッジ):
     - 選手の過去実績: 勝率・3連対率・ST平均・ST標準偏差・レース数
     - 選手×会場別実績: 会場別の勝率・3連対率
     - 選手×コース別実績: コース別の実際の勝率・3連対率・ST平均
     - 出目パターン / 決まり手分布: 逃げ/まくり/差し/まくり差しの割合
     - 公式統計 vs 実績の差分特徴量（ST差・勝率差）

  C. レース内相対特徴量（6艇間の相対順位・差）

  D. スパース特徴量 (XGBoostがNaNを自動処理):
     - 天気: wind_speed, wave_height, water_temp (2024-04〜, ~60%カバレッジ)
     - 直前情報: exhibition_time, exhibit_st, tilt (~5%カバレッジ)

【使い方】
  conda activate boatai
  pip install xgboost scikit-learn pyarrow

  python3 ml_pipeline.py --step all        # 全ステップ（初回）
  python3 ml_pipeline.py --step extract    # 特徴量抽出のみ
  python3 ml_pipeline.py --step train      # 学習のみ（extract済み前提）
  python3 ml_pipeline.py --step eval       # 評価のみ（train済み前提）
"""

import sqlite3
import json
import argparse
import sys
import os
import itertools
import pickle
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

DB_PATH    = Path(__file__).parent / "boatai.db"
DATA_PATH  = Path(__file__).parent / "ml_data.parquet"
MODELS_DIR = Path(__file__).parent / "models"

# 選手クラスを数値に変換
CLASS_MAP = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}

# コース別デフォルト勝率（course_stats がない場合のフォールバック）
COURSE_DEFAULT_WIN_RATE  = {1: 55.0, 2: 15.0, 3: 9.5, 4: 7.5, 5: 6.5, 6: 5.5}
COURSE_DEFAULT_TOP3_RATE = {1: 68.0, 2: 42.0, 3: 35.0, 4: 28.0, 5: 20.0, 6: 15.0}
COURSE_DEFAULT_AVG_ST    = {1: 0.17, 2: 0.18, 3: 0.19, 4: 0.20, 5: 0.21, 6: 0.23}

# コース別の実績ベースデフォルト勝率（race_result_entries全体集計値）
COURSE_HIST_WIN_RATE  = {1: 0.525, 2: 0.135, 3: 0.090, 4: 0.095, 5: 0.085, 6: 0.070}
COURSE_HIST_TOP3_RATE = {1: 0.660, 2: 0.380, 3: 0.320, 4: 0.270, 5: 0.195, 6: 0.175}

VENUES = [f"{i:02d}" for i in range(1, 25)]
VENUE_MAP = {v: i for i, v in enumerate(VENUES)}

TANSHO_RETURN_RATE = 0.75  # 払戻率


# ===========================================================================
# STEP 1: 特徴量抽出
# ===========================================================================

def extract_features() -> pd.DataFrame:
    """
    DBからトレーニングデータを抽出してDataFrameを返す。
    1行 = 1レース × 1艇（6艇/レースなので総行数 ≒ 対象レース数 × 6）。
    """
    print("=" * 60)
    print("STEP 1: 特徴量抽出（v2: 全データ活用版）")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row

    # ── A-1. course_stats をメモリにロード ──────────────────────────
    print("course_stats ロード中...")
    cs_rows = conn.execute("""
        SELECT player_no, course_no,
               win_rate_1st, win_rate_2nd, win_rate_3rd, avg_st
        FROM course_stats
        ORDER BY fetched_date DESC
    """).fetchall()

    cs_map: dict = {}  # (player_no, course_no) → {top1, top3, avg_st}
    for row in cs_rows:
        key = (row["player_no"], row["course_no"])
        if key not in cs_map:  # 最新スナップショットのみ使用
            t1 = row["win_rate_1st"] or 0.0
            t2 = row["win_rate_2nd"] or 0.0
            t3 = row["win_rate_3rd"] or 0.0
            cs_map[key] = {
                "top1":   t1 if t1 > 0 else COURSE_DEFAULT_WIN_RATE.get(row["course_no"], 8.0),
                "top3":   t2 + t3,
                "avg_st": row["avg_st"] or COURSE_DEFAULT_AVG_ST.get(row["course_no"], 0.20),
            }
    print(f"  {len(cs_map):,} エントリ")

    # ── A-2. 会場別1コース1着率 ──────────────────────────────────
    print("会場別1コース統計ロード中...")
    venue_c1_rows = conn.execute("""
        SELECT r.venue_code,
               COUNT(*) AS total,
               SUM(CASE WHEN rre.rank = 1 THEN 1 ELSE 0 END) AS wins
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.start_course = 1
        GROUP BY r.venue_code
    """).fetchall()
    venue_c1_map = {}
    for row in venue_c1_rows:
        if row["total"] >= 10:
            venue_c1_map[row["venue_code"]] = row["wins"] / row["total"] * 100
        else:
            venue_c1_map[row["venue_code"]] = 55.0

    # ── B-1. 選手別実績統計（race_result_entries 100%カバレッジ） ──
    print("選手別実績統計ロード中（race_result_entries）...")
    player_hist = {}  # player_no → stats dict
    ph_rows = conn.execute("""
        SELECT player_no,
               COUNT(*) as race_count,
               SUM(CASE WHEN rank=1 THEN 1.0 ELSE 0 END) as wins,
               SUM(CASE WHEN rank<=3 THEN 1.0 ELSE 0 END) as top3,
               AVG(CAST(start_timing AS REAL)) as avg_st,
               SUM(CAST(start_timing AS REAL) * CAST(start_timing AS REAL)) as st_sq_sum
        FROM race_result_entries
        WHERE player_no IS NOT NULL AND rank IS NOT NULL
          AND CAST(start_timing AS REAL) BETWEEN -0.5 AND 1.0
        GROUP BY player_no
    """).fetchall()
    for row in ph_rows:
        n = row["race_count"]
        if n < 10:
            continue
        avg_st = float(row["avg_st"] or 0.18)
        # ST標準偏差: sqrt(E[X^2] - E[X]^2)
        st_sq_mean = float(row["st_sq_sum"] or 0) / n
        st_var = max(0.0, st_sq_mean - avg_st ** 2)
        player_hist[row["player_no"]] = {
            "win_rate":   row["wins"] / n,
            "top3_rate":  row["top3"] / n,
            "avg_st":     avg_st,
            "st_std":     st_var ** 0.5,
            "race_count": n,
        }
    print(f"  選手別実績: {len(player_hist):,} 選手")

    # ── B-2. 選手×会場別実績 ──────────────────────────────────────
    print("選手×会場別実績ロード中...")
    player_venue_hist = {}  # (player_no, venue_code) → stats
    pv_rows = conn.execute("""
        SELECT rre.player_no, r.venue_code,
               COUNT(*) as n,
               SUM(CASE WHEN rre.rank=1 THEN 1.0 ELSE 0 END) as wins,
               SUM(CASE WHEN rre.rank<=3 THEN 1.0 ELSE 0 END) as top3
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.player_no IS NOT NULL AND rre.rank IS NOT NULL
        GROUP BY rre.player_no, r.venue_code
    """).fetchall()
    for row in pv_rows:
        n = row["n"]
        if n < 5:
            continue
        player_venue_hist[(row["player_no"], row["venue_code"])] = {
            "win_rate":   row["wins"] / n,
            "top3_rate":  row["top3"] / n,
            "race_count": n,
        }
    print(f"  選手×会場別実績: {len(player_venue_hist):,} エントリ")

    # ── B-3. 選手×コース別実績（実際のコース = start_course） ──────
    print("選手×コース別実績ロード中...")
    player_course_hist = {}  # (player_no, course_no) → stats
    pc_rows = conn.execute("""
        SELECT player_no, start_course,
               COUNT(*) as n,
               SUM(CASE WHEN rank=1 THEN 1.0 ELSE 0 END) as wins,
               SUM(CASE WHEN rank<=3 THEN 1.0 ELSE 0 END) as top3,
               AVG(CAST(start_timing AS REAL)) as avg_st
        FROM race_result_entries
        WHERE player_no IS NOT NULL AND rank IS NOT NULL
          AND start_course BETWEEN 1 AND 6
          AND CAST(start_timing AS REAL) BETWEEN -0.5 AND 1.0
        GROUP BY player_no, start_course
    """).fetchall()
    for row in pc_rows:
        n = row["n"]
        if n < 5:
            continue
        player_course_hist[(row["player_no"], row["start_course"])] = {
            "win_rate":   row["wins"] / n,
            "top3_rate":  row["top3"] / n,
            "avg_st":     float(row["avg_st"] or 0.18),
            "race_count": n,
        }
    print(f"  選手×コース別実績: {len(player_course_hist):,} エントリ")

    # ── B-4. 決まり手分布（出目パターン） ─────────────────────────
    print("決まり手分布ロード中...")
    trick_hist = {}  # player_no → {pct_nige, pct_makuri, pct_sashi, pct_makurisashi}
    tk_rows = conn.execute("""
        SELECT player_no,
               SUM(CASE WHEN rank=1 THEN 1.0 ELSE 0 END) as total_wins,
               SUM(CASE WHEN winning_trick='逃げ'      AND rank=1 THEN 1.0 ELSE 0 END) as nige,
               SUM(CASE WHEN winning_trick='まくり'    AND rank=1 THEN 1.0 ELSE 0 END) as makuri,
               SUM(CASE WHEN winning_trick='差し'      AND rank=1 THEN 1.0 ELSE 0 END) as sashi,
               SUM(CASE WHEN winning_trick='まくり差し' AND rank=1 THEN 1.0 ELSE 0 END) as makurisashi
        FROM race_result_entries
        WHERE player_no IS NOT NULL AND winning_trick IS NOT NULL
        GROUP BY player_no
        HAVING total_wins >= 3
    """).fetchall()
    for row in tk_rows:
        total = row["total_wins"] or 1
        trick_hist[row["player_no"]] = {
            "pct_nige":        row["nige"] / total,
            "pct_makuri":      row["makuri"] / total,
            "pct_sashi":       row["sashi"] / total,
            "pct_makurisashi": row["makurisashi"] / total,
        }
    print(f"  決まり手分布: {len(trick_hist):,} 選手")

    # ── D-1. 天気データ ──────────────────────────────────────────
    print("天気データロード中...")
    weather_map = {}  # race_id → weather dict
    w_rows = conn.execute("""
        SELECT race_id, wind_speed, wave_height, water_temp, temperature, wind_direction
        FROM weather
    """).fetchall()
    for row in w_rows:
        weather_map[row["race_id"]] = {
            "wind_speed":     row["wind_speed"],
            "wave_height":    row["wave_height"],
            "water_temp":     row["water_temp"],
            "temperature":    row["temperature"],
            "wind_direction": row["wind_direction"],
        }
    print(f"  天気: {len(weather_map):,} レース")

    # ── D-2. 直前情報（before_info） ─────────────────────────────
    print("直前情報ロード中...")
    before_info_map = {}  # (race_id, boat_no) → {exhibition_time, exhibit_st, tilt}
    bi_rows = conn.execute("""
        SELECT race_id, boat_no, exhibition_time, exhibit_st, tilt
        FROM before_info
        WHERE exhibition_time IS NOT NULL OR exhibit_st IS NOT NULL
    """).fetchall()
    for row in bi_rows:
        before_info_map[(row["race_id"], row["boat_no"])] = {
            "exhibition_time": row["exhibition_time"],
            "exhibit_st":      row["exhibit_st"],
            "tilt":            row["tilt"],
        }
    print(f"  直前情報: {len(before_info_map):,} エントリ")

    # ── E-1. 節内モーター成績（race_result_entries + entries から計算） ──
    # キー: (race_id, boat_no) — メインループで boat_no は常に参照可能
    print("節内モーター成績ロード中...")
    m_df = pd.read_sql_query("""
        SELECT e.motor_no, e.boat_no, r.venue_code, r.date, r.race_no, r.id AS race_id,
               rre.rank
        FROM entries e
        JOIN races r ON r.id = e.race_id
        JOIN race_result_entries rre ON rre.race_id = e.race_id AND rre.boat_no = e.boat_no
        WHERE e.motor_no IS NOT NULL AND rre.rank IS NOT NULL
        ORDER BY e.motor_no, r.venue_code, r.date, r.race_no
    """, conn)
    motor_meet_map = {}
    if not m_df.empty:
        m_df["date_dt"] = pd.to_datetime(m_df["date"], format="%Y%m%d")
        m_df["is_top2"] = (m_df["rank"] <= 2).astype(float)
        m_df = m_df.sort_values(["motor_no","venue_code","date_dt","race_no"]).reset_index(drop=True)
        m_df["prev_date"] = m_df.groupby(["motor_no","venue_code"])["date_dt"].shift(1)
        m_df["day_gap"]   = (m_df["date_dt"] - m_df["prev_date"]).dt.days.fillna(999)
        m_df["new_meet"]  = (m_df["day_gap"] > 2).astype(int)
        m_df["meet_id"]   = m_df.groupby(["motor_no","venue_code"])["new_meet"].cumsum()
        m_df["cs"]        = m_df.groupby(["motor_no","venue_code","meet_id"])["is_top2"].cumsum()
        m_df["prev_top2"] = m_df.groupby(["motor_no","venue_code","meet_id"])["cs"].shift(1)
        m_df["prev_cnt"]  = m_df.groupby(["motor_no","venue_code","meet_id"]).cumcount()
        m_df["meet_motor_top2_rate"]  = m_df["prev_top2"] / m_df["prev_cnt"].replace(0, float("nan"))
        m_df["meet_motor_race_count"] = m_df["prev_cnt"].astype(float)
        rids = m_df["race_id"].values.astype(int)
        bnos = m_df["boat_no"].values.astype(int)
        t2rs = m_df["meet_motor_top2_rate"].values
        cnts = m_df["meet_motor_race_count"].values
        for i in range(len(rids)):
            motor_meet_map[(rids[i], bnos[i])] = {
                "meet_motor_top2_rate":  None if (t2rs[i] != t2rs[i]) else float(t2rs[i]),
                "meet_motor_race_count": float(cnts[i]),
            }
    print(f"  節内モーター: {len(motor_meet_map):,} エントリ")

    # ── E-2. 節内選手成績（同会場・同節での直近着順） ────────────────
    print("節内選手成績ロード中...")
    p_df = pd.read_sql_query("""
        SELECT rre.player_no, r.venue_code, r.date, r.race_no, r.id AS race_id,
               rre.rank
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.player_no IS NOT NULL AND rre.rank IS NOT NULL
        ORDER BY rre.player_no, r.venue_code, r.date, r.race_no
    """, conn)
    player_meet_map = {}
    if not p_df.empty:
        p_df["date_dt"] = pd.to_datetime(p_df["date"], format="%Y%m%d")
        p_df["is_top2"] = (p_df["rank"] <= 2).astype(float)
        p_df = p_df.sort_values(["player_no","venue_code","date_dt","race_no"]).reset_index(drop=True)
        p_df["prev_date"] = p_df.groupby(["player_no","venue_code"])["date_dt"].shift(1)
        p_df["day_gap"]   = (p_df["date_dt"] - p_df["prev_date"]).dt.days.fillna(999)
        p_df["new_meet"]  = (p_df["day_gap"] > 2).astype(int)
        p_df["meet_id"]   = p_df.groupby(["player_no","venue_code"])["new_meet"].cumsum()
        p_df["cs_top2"]   = p_df.groupby(["player_no","venue_code","meet_id"])["is_top2"].cumsum()
        p_df["cs_rank"]   = p_df.groupby(["player_no","venue_code","meet_id"])["rank"].cumsum()
        p_df["prev_top2"] = p_df.groupby(["player_no","venue_code","meet_id"])["cs_top2"].shift(1)
        p_df["prev_rank"] = p_df.groupby(["player_no","venue_code","meet_id"])["cs_rank"].shift(1)
        p_df["prev_cnt"]  = p_df.groupby(["player_no","venue_code","meet_id"]).cumcount()
        p_df["meet_player_top2_rate"] = p_df["prev_top2"] / p_df["prev_cnt"].replace(0, float("nan"))
        p_df["meet_player_avg_rank"]  = p_df["prev_rank"] / p_df["prev_cnt"].replace(0, float("nan"))
        p_df["meet_player_race_count"] = p_df["prev_cnt"].astype(float)
        rids  = p_df["race_id"].values.astype(int)
        pnos  = p_df["player_no"].values
        t2rs  = p_df["meet_player_top2_rate"].values
        avgrs = p_df["meet_player_avg_rank"].values
        cnts  = p_df["meet_player_race_count"].values
        for i in range(len(rids)):
            player_meet_map[(rids[i], pnos[i])] = {
                "meet_player_top2_rate":  None if (t2rs[i]  != t2rs[i])  else float(t2rs[i]),
                "meet_player_avg_rank":   None if (avgrs[i] != avgrs[i]) else float(avgrs[i]),
                "meet_player_race_count": float(cnts[i]),
            }
    print(f"  節内選手: {len(player_meet_map):,} エントリ")

    # ── メインクエリ ─────────────────────────────────────────────
    print("メインデータ取得中（数分かかる場合があります）...")
    rows = conn.execute("""
        SELECT
            r.id          AS race_id,
            r.date,
            r.venue_code,
            r.race_no,
            e.boat_no,
            e.player_no,
            e.player_class,
            e.age,
            e.weight,
            e.flying_count,
            e.late_count,
            e.avg_start_timing,
            e.national_win_rate,
            e.national_2ring_rate,
            e.local_win_rate,
            e.local_2ring_rate,
            e.motor_2ring_rate,
            e.boat_2ring_rate,
            rre.rank          AS actual_rank,
            rre.start_course  AS actual_start_course,
            rre.winning_trick
        FROM races r
        JOIN entries e           ON e.race_id = r.id
        JOIN race_result_entries rre
                                  ON rre.race_id = r.id
                                 AND rre.boat_no  = e.boat_no
        WHERE rre.rank IS NOT NULL
          AND rre.rank BETWEEN 1 AND 6
        ORDER BY r.date, r.venue_code, r.race_no, e.boat_no
    """).fetchall()
    conn.close()
    print(f"  {len(rows):,} 行取得")

    # ── DataFrame 構築 ─────────────────────────────────────────
    print("DataFrame 構築中...")
    records = []
    for row in rows:
        vc       = row["venue_code"]
        boat_no  = row["boat_no"]
        pno      = row["player_no"]
        race_id  = row["race_id"]
        actual_course = row["actual_start_course"] or boat_no

        # ── A: course_stats（公式ウェブ取得） ──
        cs = cs_map.get((pno, boat_no), {})
        c_top1   = cs.get("top1",   COURSE_DEFAULT_WIN_RATE.get(boat_no, 8.0))
        c_top3   = cs.get("top3",   COURSE_DEFAULT_TOP3_RATE.get(boat_no, 20.0))
        c_avg_st = cs.get("avg_st", COURSE_DEFAULT_AVG_ST.get(boat_no, 0.20))
        has_cs   = 1 if (pno, boat_no) in cs_map else 0

        # ── date features ──
        try:
            dt    = datetime.strptime(row["date"], "%Y%m%d")
            year  = dt.year
            month = dt.month
            dow   = dt.weekday()
        except Exception:
            year, month, dow = 2023, 6, 0

        # ── B-1: 選手別実績 ──
        ph  = player_hist.get(pno, {})
        ph_win    = ph.get("win_rate",   0.0)
        ph_top3   = ph.get("top3_rate",  0.0)
        ph_avg_st = ph.get("avg_st",     0.18)
        ph_st_std = ph.get("st_std",     0.05)
        ph_count  = min(ph.get("race_count", 0), 2000)

        # ── B-2: 選手×会場別実績 ──
        pvh = player_venue_hist.get((pno, vc), {})
        pvh_win   = pvh.get("win_rate",  0.0)
        pvh_top3  = pvh.get("top3_rate", 0.0)
        pvh_count = min(pvh.get("race_count", 0), 500)

        # ── B-3: 選手×コース別実績（割り当てコース = boat_no で参照） ──
        pch = player_course_hist.get((pno, boat_no), {})
        pch_win    = pch.get("win_rate",  COURSE_HIST_WIN_RATE.get(boat_no, 0.08))
        pch_top3   = pch.get("top3_rate", COURSE_HIST_TOP3_RATE.get(boat_no, 0.25))
        pch_avg_st = pch.get("avg_st",    COURSE_DEFAULT_AVG_ST.get(boat_no, 0.20))
        pch_count  = min(pch.get("race_count", 0), 500)

        # ── B-4: 決まり手分布 ──
        tk = trick_hist.get(pno, {})
        pct_nige        = tk.get("pct_nige",        0.0)
        pct_makuri      = tk.get("pct_makuri",       0.0)
        pct_sashi       = tk.get("pct_sashi",        0.0)
        pct_makurisashi = tk.get("pct_makurisashi",  0.0)
        has_trick       = 1 if pno in trick_hist else 0

        # ── 公式統計 vs 実績 の差分特徴量 ──
        official_wr  = (row["national_win_rate"] or 0.0) / 100.0  # %→割合変換
        hist_wr_diff = ph_win - official_wr     # プラス=実績が公式より良い
        official_st  = row["avg_start_timing"] or 0.18
        hist_st_diff = ph_avg_st - official_st  # マイナス=実績STの方が速い

        # ── D-1: 天気（NaN可） ──
        w = weather_map.get(race_id, {})
        wind_speed     = w.get("wind_speed")
        wave_height    = w.get("wave_height")
        water_temp     = w.get("water_temp")
        temperature    = w.get("temperature")
        wind_direction = w.get("wind_direction")

        # ── D-2: 直前情報（NaN可） ──
        bi = before_info_map.get((race_id, boat_no), {})
        exhibition_time = bi.get("exhibition_time")
        exhibit_st      = bi.get("exhibit_st")
        tilt_val        = bi.get("tilt")

        # ── E: 節内成績（NaN可 — 節初戦はNaN） ──
        mm = motor_meet_map.get((race_id, boat_no), {})   # boat_no == 艇番 → motor割り当て済み
        meet_motor_top2_rate  = mm.get("meet_motor_top2_rate")
        meet_motor_race_count = mm.get("meet_motor_race_count", 0.0)

        pm = player_meet_map.get((race_id, pno), {})
        meet_player_top2_rate  = pm.get("meet_player_top2_rate")
        meet_player_avg_rank   = pm.get("meet_player_avg_rank")
        meet_player_race_count = pm.get("meet_player_race_count", 0.0)

        # ── 派生特徴量 ──
        fl_late_risk     = min((row["flying_count"] or 0) + (row["late_count"] or 0), 10)
        motor_boat_combo = ((row["motor_2ring_rate"] or 0) + (row["boat_2ring_rate"] or 0)) / 2
        ability_score    = ((row["national_win_rate"] or 0) * c_top1) ** 0.5
        hist_ability     = (ph_win * pch_win) ** 0.5  # 実績ベースの能力スコア

        records.append({
            # ── ID（学習には使わない）──
            "race_id":   race_id,
            "date":      row["date"],
            "boat_no":   boat_no,
            "player_no": pno,

            # ── ラベル ──
            "actual_rank": row["actual_rank"],
            "is_1st":      1 if row["actual_rank"] == 1 else 0,
            "is_2nd":      1 if row["actual_rank"] == 2 else 0,
            "is_3rd":      1 if row["actual_rank"] <= 3 else 0,
            "is_top3":     1 if row["actual_rank"] <= 3 else 0,

            # ── A: レース/会場特徴量 ──
            "venue_code":    VENUE_MAP.get(vc, 0),
            "race_no":       row["race_no"],
            "year":          year,
            "month":         month,
            "day_of_week":   dow,
            "venue_c1_rate": venue_c1_map.get(vc, 55.0),

            # ── A: 艇・選手特徴量（entries 公式統計） ──
            "national_win_rate":   row["national_win_rate"]   or 0.0,
            "national_2ring_rate": row["national_2ring_rate"] or 0.0,
            "local_win_rate":      row["local_win_rate"]      or 0.0,
            "local_2ring_rate":    row["local_2ring_rate"]    or 0.0,
            "motor_2ring_rate":    row["motor_2ring_rate"]    or 0.0,
            "boat_2ring_rate":     row["boat_2ring_rate"]     or 0.0,
            "avg_start_timing":    row["avg_start_timing"]    or 0.18,
            "flying_count":        min(row["flying_count"] or 0, 5),
            "late_count":          min(row["late_count"]   or 0, 5),
            "age":                 row["age"]    or 35,
            "weight":              row["weight"] or 52.0,
            "player_class":        CLASS_MAP.get(row["player_class"] or "", 2),
            "boat_no_pos":         boat_no,

            # ── A: コース別統計（course_stats 公式） ──
            "course_top1_rate":    c_top1,
            "course_top3_rate":    c_top3,
            "course_avg_st":       c_avg_st,
            "course_default_wr":   COURSE_DEFAULT_WIN_RATE.get(boat_no, 8.0),
            "has_course_stats":    has_cs,

            # ── B-1: 選手別実績（race_result_entries 100%カバレッジ） ──
            "hist_win_rate":    ph_win,
            "hist_top3_rate":   ph_top3,
            "hist_avg_st":      ph_avg_st,
            "hist_st_std":      ph_st_std,
            "hist_race_count":  ph_count,

            # ── B-2: 選手×会場別実績 ──
            "hist_venue_win_rate":   pvh_win,
            "hist_venue_top3_rate":  pvh_top3,
            "hist_venue_count":      pvh_count,

            # ── B-3: 選手×コース別実績（実際のコースを使用） ──
            "hist_course_win_rate":   pch_win,
            "hist_course_top3_rate":  pch_top3,
            "hist_course_avg_st":     pch_avg_st,
            "hist_course_count":      pch_count,

            # ── B-4: 決まり手分布（出目パターン） ──
            "hist_pct_nige":        pct_nige,
            "hist_pct_makuri":      pct_makuri,
            "hist_pct_sashi":       pct_sashi,
            "hist_pct_makurisashi": pct_makurisashi,
            "has_trick_data":       has_trick,

            # ── 差分特徴量 ──
            "hist_wr_diff":     hist_wr_diff,   # 実績勝率 - 公式勝率
            "hist_st_diff":     hist_st_diff,   # 実績ST - 公式ST（マイナス=実績が速い）

            # ── 派生特徴量 ──
            "course_advantage":  COURSE_DEFAULT_WIN_RATE.get(boat_no, 8.0),
            "fl_late_risk":      fl_late_risk,
            "motor_boat_combo":  motor_boat_combo,
            "ability_score":     ability_score,
            "hist_ability":      hist_ability,

            # ── D-1: 天気（NaN可 → XGBoostが自動処理） ──
            "wind_speed":     wind_speed,
            "wave_height":    wave_height,
            "water_temp":     water_temp,
            "temperature":    temperature,
            "wind_direction": wind_direction,

            # ── D-2: 直前情報（NaN可） ──
            "exhibition_time": exhibition_time,
            "exhibit_st":      exhibit_st,
            "tilt":            tilt_val,

            # ── E: 節内成績（NaN可） ──
            "meet_motor_top2_rate":   meet_motor_top2_rate,
            "meet_motor_race_count":  meet_motor_race_count,
            "meet_player_top2_rate":  meet_player_top2_rate,
            "meet_player_avg_rank":   meet_player_avg_rank,
            "meet_player_race_count": meet_player_race_count,
        })

    df = pd.DataFrame(records)

    # ── レース内相対特徴量 ──────────────────────────────────────
    print("相対特徴量を計算中...")
    rel_targets = [
        ("national_win_rate",     False),
        ("national_2ring_rate",   False),
        ("local_win_rate",        False),
        ("local_2ring_rate",      False),
        ("motor_2ring_rate",      False),
        ("boat_2ring_rate",       False),
        ("avg_start_timing",      True),   # STは小さいほど良い
        ("course_top1_rate",      False),
        ("course_top3_rate",      False),
        ("ability_score",         False),
        ("hist_win_rate",         False),  # ★ 実績ベース
        ("hist_top3_rate",        False),  # ★
        ("hist_avg_st",           True),   # ★ STは小さいほど良い
        ("hist_course_win_rate",  False),  # ★
        ("hist_ability",          False),  # ★
    ]
    for col, asc in rel_targets:
        grp = df.groupby("race_id")[col]
        df[f"{col}_rank"]    = grp.rank(ascending=asc, method="min").astype(int)
        df[f"{col}_vs_mean"] = df[col] - grp.transform("mean")
        df[f"{col}_vs_max"]  = df[col] - grp.transform("max")

    print(f"\n特徴量構築完了: {len(df):,} 行 × {len(df.columns)} 列")
    print(f"  期間: {df['date'].min()} 〜 {df['date'].max()}")
    print(f"  レース数: {df['race_id'].nunique():,}")
    # スパース特徴量のカバレッジ
    for col in ["wind_speed", "wave_height", "wind_direction", "exhibition_time", "exhibit_st",
                "meet_motor_top2_rate", "meet_player_top2_rate"]:
        cov = df[col].notna().mean() * 100
        print(f"  {col} カバレッジ: {cov:.1f}%")
    return df


# ===========================================================================
# STEP 2: モデル学習
# ===========================================================================

def get_feature_cols() -> list:
    """学習に使う特徴量の列名リスト"""
    # ── A: 基本特徴量 ──
    base = [
        "venue_code", "race_no", "year", "month", "day_of_week", "venue_c1_rate",
        "national_win_rate", "national_2ring_rate",
        "local_win_rate",    "local_2ring_rate",
        "motor_2ring_rate",  "boat_2ring_rate",
        "avg_start_timing",  "flying_count", "late_count",
        "age", "weight", "player_class", "boat_no_pos",
        "course_top1_rate", "course_top3_rate", "course_avg_st",
        "course_default_wr", "course_advantage",
        "has_course_stats",
        # 派生
        "fl_late_risk", "motor_boat_combo", "ability_score",
    ]

    # ── B: 実績ベース特徴量 ──
    base += [
        # B-1: 選手別実績
        "hist_win_rate", "hist_top3_rate", "hist_avg_st", "hist_st_std", "hist_race_count",
        # B-2: 選手×会場別
        "hist_venue_win_rate", "hist_venue_top3_rate", "hist_venue_count",
        # B-3: 選手×コース別
        "hist_course_win_rate", "hist_course_top3_rate", "hist_course_avg_st", "hist_course_count",
        # B-4: 決まり手分布
        "hist_pct_nige", "hist_pct_makuri", "hist_pct_sashi", "hist_pct_makurisashi",
        "has_trick_data",
        # 差分
        "hist_wr_diff", "hist_st_diff",
        "hist_ability",
    ]

    # ── D: スパース特徴量（NaN可） ──
    base += [
        "wind_speed", "wave_height", "water_temp", "temperature", "wind_direction",
        "exhibition_time", "exhibit_st", "tilt",
    ]

    # ── E: 節内成績（NaN可） ──
    base += [
        "meet_motor_top2_rate", "meet_motor_race_count",
        "meet_player_top2_rate", "meet_player_avg_rank", "meet_player_race_count",
    ]

    # ── C: レース内相対特徴量 ──
    rel_cols = [
        "national_win_rate", "national_2ring_rate", "local_win_rate", "local_2ring_rate",
        "motor_2ring_rate", "boat_2ring_rate", "avg_start_timing",
        "course_top1_rate", "course_top3_rate", "ability_score",
        "hist_win_rate", "hist_top3_rate", "hist_avg_st",
        "hist_course_win_rate", "hist_ability",
    ]
    for col in rel_cols:
        base += [f"{col}_rank", f"{col}_vs_mean", f"{col}_vs_max"]

    return base


def train_models(df: pd.DataFrame) -> dict:
    """
    XGBoost で is_1st / is_2nd / is_3rd の3モデルを学習して保存。
    時系列分割: 訓練=2021〜2025年、テスト=2026年
    """
    try:
        import xgboost as xgb
        from sklearn.metrics import roc_auc_score
    except ImportError:
        print("ERROR: xgboost/sklearn が見つかりません。")
        print("  conda activate boatai && pip install xgboost scikit-learn pyarrow")
        sys.exit(1)

    print("=" * 60)
    print("STEP 2: モデル学習")
    print("=" * 60)
    MODELS_DIR.mkdir(exist_ok=True)

    feature_cols = get_feature_cols()
    # 特徴量カラムのうち存在するものだけ使う（将来の追加削除に対応）
    feature_cols = [c for c in feature_cols if c in df.columns]

    train_mask = df["date"] < "20260101"
    test_mask  = df["date"] >= "20260101"

    # スパース特徴量（NaN可）はそのまま渡す。XGBoostがNaN=missing扱い
    # 基本特徴量の欠損のみ0埋め
    sparse_cols = {"wind_speed", "wave_height", "water_temp", "temperature",
                   "exhibition_time", "exhibit_st", "tilt"}
    fill_cols   = [c for c in feature_cols if c not in sparse_cols]

    def prepare_X(mask):
        X = df[mask][feature_cols].copy()
        X[fill_cols] = X[fill_cols].fillna(0)
        # PyArrow文字列型を含む全カラムをfloatに強制変換
        # （exhibit_st='F.07'等フライングコードはNaNになる）
        for col in feature_cols:
            if col in X.columns:
                X[col] = pd.to_numeric(X[col], errors="coerce")
        return X.astype(float)

    X_train = prepare_X(train_mask)
    X_test  = prepare_X(test_mask)
    print(f"特徴量数: {len(feature_cols)}")
    print(f"訓練サンプル: {len(X_train):,} / テストサンプル: {len(X_test):,}")

    # XGBoost パラメータ（精度重視）
    xgb_params = dict(
        n_estimators          = 1500,
        max_depth             = 7,
        learning_rate         = 0.02,
        subsample             = 0.8,
        colsample_bytree      = 0.8,
        colsample_bylevel     = 0.7,
        min_child_weight      = 20,
        gamma                 = 0.5,
        reg_alpha             = 0.1,
        reg_lambda            = 1.0,
        tree_method           = "hist",
        random_state          = 42,
        n_jobs                = -1,
        eval_metric           = "auc",
        early_stopping_rounds = 50,
    )

    models = {}
    for target in ["is_1st", "is_2nd", "is_3rd"]:
        print(f"\n--- [{target}] 学習中 ---")
        y_train = df[train_mask][target].astype(int)
        y_test  = df[test_mask][target].astype(int)

        pos_rate = y_train.mean()
        scale_pw = (1 - pos_rate) / pos_rate
        print(f"  正例率: {pos_rate:.3f} → scale_pos_weight: {scale_pw:.2f}")

        model = xgb.XGBClassifier(**xgb_params, scale_pos_weight=scale_pw)
        model.fit(
            X_train, y_train,
            eval_set=[(X_test, y_test)],
            verbose=200,
        )

        y_pred = model.predict_proba(X_test)[:, 1]
        auc = roc_auc_score(y_test, y_pred)
        print(f"  テスト AUC: {auc:.4f}")

        path = MODELS_DIR / f"xgb_{target}.pkl"
        with open(path, "wb") as f:
            pickle.dump(model, f)
        print(f"  保存 → {path}")

        # 特徴量重要度 Top15
        imp = pd.Series(model.feature_importances_, index=feature_cols)
        print("  特徴量重要度 Top15:")
        for feat, score in imp.nlargest(15).items():
            print(f"    {feat}: {score:.4f}")

        models[target] = model

    # 特徴量リストを保存
    with open(MODELS_DIR / "feature_cols.json", "w") as f:
        json.dump(feature_cols, f, ensure_ascii=False, indent=2)
    print(f"\n特徴量リスト保存 → {MODELS_DIR}/feature_cols.json")

    return models


# ===========================================================================
# STEP 3: バックテスト評価
# ===========================================================================

def evaluate_models(df: pd.DataFrame, models: dict) -> dict:
    """
    MLモデルでテスト期間（2026年）の的中率を計算し、現行ルールベースと比較する。
    本命/中穴/穴はルールベースのオッズ基準カテゴリを使用（同一基準で比較）。
    Top5 / Top10 / Top15 を横断評価して多点買い戦略の効果を測定する。
    """
    print("=" * 60)
    print("STEP 3: バックテスト評価（本命/中穴/穴 × Top5/10/15）")
    print("=" * 60)

    feature_cols = get_feature_cols()
    feature_cols = [c for c in feature_cols if c in df.columns]

    sparse_cols = {"wind_speed", "wave_height", "water_temp", "temperature",
                   "exhibition_time", "exhibit_st", "tilt"}
    fill_cols   = [c for c in feature_cols if c not in sparse_cols]

    test_mask = df["date"] >= "20260101"
    df_test = df[test_mask].copy()

    def prepare_X(data):
        X = data[feature_cols].copy()
        X[fill_cols] = X[fill_cols].fillna(0)
        for col in feature_cols:
            if col in X.columns:
                X[col] = pd.to_numeric(X[col], errors="coerce")
        return X.astype(float)

    X_test = prepare_X(df_test)
    print(f"テストサンプル: {len(df_test):,} 行 ({df_test['race_id'].nunique():,}レース)")

    # ── ルールベースのカテゴリをDBから取得 ──
    # 本命/中穴/穴 = 現行predict.pyのオッズ基準（honmei≤25倍, chuana25〜80倍, ana>80倍）
    # 実際のpayoutsから3連単の払戻金でカテゴリを決定（≤25倍=本命, 25-80倍=中穴, >80倍=穴）
    print("実績オッズでレースカテゴリ分類中（payoutsテーブル）...")
    conn = sqlite3.connect(DB_PATH)
    payout_rows = conn.execute("""
        SELECT p.race_id, p.combination, p.payout,
               COALESCE(pred.hit_top3, 0) AS rb_hit3,
               COALESCE(pred.hit_top5, 0) AS rb_hit5_val,
               COALESCE(pred.hit_honmei, 0) AS rb_hit_hm,
               COALESCE(pred.hit_chuana, 0) AS rb_hit_ch,
               COALESCE(pred.hit_ana,    0) AS rb_hit_an,
               pred.top5_honmei, pred.top5_chuana, pred.top5_ana,
               pred.top5_combos, pred.actual_combo
        FROM payouts p
        JOIN races r ON r.id = p.race_id
        LEFT JOIN predictions pred ON pred.race_id = p.race_id
        WHERE p.bet_type = '3連単' AND r.date >= '20260101'
    """).fetchall()
    conn.close()
    import json as _json
    TOPS_RB = [3, 5, 10, 15]
    race_cat      = {}   # race_id → 'honmei'/'chuana'/'ana'
    rb_hit_by_cat = {}   # race_id → rb的中率（カテゴリ対応）
    # RBの買い目数別的中集計: rb_topn_stats[cat][n] = [hit_count, total_count]
    rb_topn_stats = {cat: {n: [0, 0] for n in TOPS_RB}
                     for cat in ["all", "honmei", "chuana", "ana"]}
    for row in payout_rows:
        rid, combo, payout = row[0], row[1], row[2]
        if payout is None:
            continue
        odds = payout / 100.0
        if odds <= 25:
            cat = "honmei"
            rb_h = row[5]
        elif odds <= 80:
            cat = "chuana"
            rb_h = row[6]
        else:
            cat = "ana"
            rb_h = row[7]
        race_cat[rid]      = cat
        rb_hit_by_cat[rid] = rb_h

        # JSON コンボリストから Top N 的中を計算
        actual = row[12]  # actual_combo
        if actual is None:
            continue
        # カテゴリ対応コンボリスト
        if cat == "honmei":
            cat_combos = _json.loads(row[8] or "[]")
        elif cat == "chuana":
            cat_combos = _json.loads(row[9] or "[]")
        else:
            cat_combos = _json.loads(row[10] or "[]")
        all_combos = _json.loads(row[11] or "[]")   # top5_combos (overall)
        for n in TOPS_RB:
            rb_topn_stats["all"][n][1]  += 1
            rb_topn_stats[cat][n][1]    += 1
            if actual in all_combos[:n]:
                rb_topn_stats["all"][n][0] += 1
            if actual in cat_combos[:n]:
                rb_topn_stats[cat][n][0]   += 1
    print(f"  本命(≤25倍): {sum(1 for c in race_cat.values() if c=='honmei'):,}レース")
    print(f"  中穴(25〜80倍): {sum(1 for c in race_cat.values() if c=='chuana'):,}レース")
    print(f"  穴(>80倍):  {sum(1 for c in race_cat.values() if c=='ana'):,}レース")

    # ── 各モデルの確率を付与 ──
    for target, model in models.items():
        df_test[f"prob_{target}"] = model.predict_proba(X_test)[:, 1]

    # ── カテゴリ別 × 買い目数別の集計構造 ──
    CATS = ["all", "honmei", "chuana", "ana"]
    TOPS = [3, 5, 10, 15]
    # stats[cat][top_n] = [hit_count, total_count, rb_hit_count]
    stats = {cat: {n: [0, 0, 0] for n in TOPS} for cat in CATS}

    print("コンボ的中率を計算中...")
    for race_id, grp in df_test.groupby("race_id"):
        if len(grp) < 3:
            continue

        boats_df = grp.set_index("boat_no")
        all_nos  = list(boats_df.index)

        rank_map = {row["boat_no"]: row["actual_rank"] for _, row in grp.iterrows()}
        r2b = {v: k for k, v in rank_map.items()}
        if not all(r in r2b for r in [1, 2, 3]):
            continue
        actual_combo = f"{r2b[1]}-{r2b[2]}-{r2b[3]}"

        # 120通りコンボ確率
        combo_probs = []
        for a, b, c in itertools.permutations(all_nos, 3):
            if not all(x in boats_df.index for x in [a, b, c]):
                continue
            p = (boats_df.loc[a, "prob_is_1st"]
               * boats_df.loc[b, "prob_is_2nd"]
               * boats_df.loc[c, "prob_is_3rd"])
            combo_probs.append((f"{a}-{b}-{c}", p))
        if not combo_probs:
            continue
        combo_probs.sort(key=lambda x: x[1], reverse=True)

        # 各買い目数での的中判定
        top_n_hits = {}
        for n in TOPS:
            top_n_hits[n] = 1 if actual_combo in [c for c, _ in combo_probs[:n]] else 0

        cat  = race_cat.get(race_id)        # 実際のオッズによるカテゴリ
        rb_h = rb_hit_by_cat.get(race_id, 0)

        for apply_cat in CATS:
            if apply_cat == "all" or apply_cat == cat:
                for n in TOPS:
                    stats[apply_cat][n][0] += top_n_hits[n]  # ML hit
                    stats[apply_cat][n][1] += 1              # total
                    if apply_cat != "all":
                        stats[apply_cat][n][2] += rb_h       # RB hit (カテゴリ行のみ)

    # ── 結果表示 ──
    def pct(hits, total):
        return f"{hits/max(total,1)*100:.1f}%" if total > 0 else "—"

    # DBから現行ルールベースの集計
    conn = sqlite3.connect(DB_PATH)
    rb_overall = conn.execute("""
        SELECT COUNT(*) as n,
               SUM(hit_top3) as h3, SUM(hit_top5) as h5,
               SUM(COALESCE(hit_honmei,0)) as hm,
               COUNT(CASE WHEN hit_honmei IS NOT NULL THEN 1 END) as cnt_hm,
               SUM(COALESCE(hit_chuana,0)) as hc,
               COUNT(CASE WHEN hit_chuana IS NOT NULL THEN 1 END) as cnt_hc,
               SUM(COALESCE(hit_ana,0)) as ha,
               COUNT(CASE WHEN hit_ana IS NOT NULL THEN 1 END) as cnt_ha
        FROM predictions p JOIN races r ON r.id = p.race_id
        WHERE r.date >= '20260101'
    """).fetchone()
    conn.close()
    rbn = rb_overall[0] or 1

    print(f"\n{'='*75}")
    print("【全レース: Top3/5/10/15 的中率比較】")
    print(f"{'買い目数':<10} {'現行RB':>10} {'XGBoost':>10}  {'改善':>7}")
    print(f"{'-'*42}")
    for n in TOPS:
        ml_h, ml_n, _ = stats["all"][n]
        ml_r = ml_h / max(ml_n, 1)
        rb_h_n, rb_n_n = rb_topn_stats["all"][n]
        rb_r = rb_h_n / max(rb_n_n, 1) if rb_n_n > 0 else None
        if rb_r is not None:
            diff = f"+{(ml_r-rb_r)*100:.1f}pt" if ml_r > rb_r else f"{(ml_r-rb_r)*100:.1f}pt"
            print(f"  Top{n:<6} {rb_r*100:>9.1f}% {ml_r*100:>9.1f}%  {diff:>7}")
        else:
            print(f"  Top{n:<6} {'—':>10} {ml_r*100:>9.1f}%  {'—':>7}")

    print(f"\n{'='*75}")
    print("【カテゴリ別比較（実際の3連単払戻金でレースを分類）】")
    print(f"  ※ 損益分岐点 = 買い目数 ÷ 平均オッズ (本命15倍・中穴45倍・穴200倍で概算)")
    print(f"  ※ 現行RB: backtest.py --date-from 20260101 --rerun 実行後は全N段階で比較可能")
    print()

    cat_labels = {
        "honmei": "本命（≤25倍）",
        "chuana": "中穴（25〜80倍）",
        "ana":    "穴  （>80倍）",
    }
    avg_odds = {"honmei": 15, "chuana": 45, "ana": 200}

    for cat, label in cat_labels.items():
        s = stats[cat]
        total_cat = s[5][1]
        if total_cat == 0:
            print(f"  {label}: データなし")
            continue

        print(f"  {label}  ({total_cat:,}レース)")
        print(f"  {'買い目数':<8} {'損益分岐':>8} {'現行RB':>9} {'XGBoost':>10} {'改善':>7}  {'目標':>10}")
        be_base = avg_odds[cat]
        for n in TOPS:
            ml_h, ml_n, _ = s[n]
            ml_r = ml_h / max(ml_n, 1)
            be   = n / be_base
            ev   = "★黒字" if ml_r > be else "  赤字"
            tgt  = ""
            if cat == "chuana" and n == 10: tgt = "← 目標50%"
            if cat == "chuana" and n == 15: tgt = "← 目標60%"
            if cat == "ana"    and n == 10: tgt = "← 参考"
            if cat == "ana"    and n == 15: tgt = "← 参考"
            rb_h_n, rb_n_n = rb_topn_stats[cat][n]
            rb_r_str = f"{rb_h_n/rb_n_n*100:>8.1f}%" if rb_n_n > 0 else f"{'—':>9}"
            diff_str = ""
            if rb_n_n > 0:
                rb_r = rb_h_n / rb_n_n
                diff_str = f"+{(ml_r-rb_r)*100:.1f}pt" if ml_r > rb_r else f"{(ml_r-rb_r)*100:.1f}pt"
            print(f"    Top{n:<4}  分岐{be*100:>4.0f}% {rb_r_str} {ml_r*100:>8.1f}%  {ev} {diff_str:>7}  {tgt}")
        print()

    print(f"{'='*75}")

    return {
        "ml_top3_all":  stats["all"][3][0] / max(stats["all"][3][1], 1),
        "ml_top5_all":  stats["all"][5][0] / max(stats["all"][5][1], 1),
        "ml_top10_all": stats["all"][10][0] / max(stats["all"][10][1], 1),
        "ml_top15_all": stats["all"][15][0] / max(stats["all"][15][1], 1),
        "ml_top10_chuana": stats["chuana"][10][0] / max(stats["chuana"][10][1], 1),
        "ml_top15_ana":    stats["ana"][15][0] / max(stats["ana"][15][1], 1),
    }


# ===========================================================================
# メイン
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="BoatAI MLパイプライン v2")
    parser.add_argument(
        "--step",
        choices=["extract", "train", "eval", "all"],
        default="all",
        help="実行するステップ (default: all)",
    )
    args = parser.parse_args()

    df = None
    models = {}

    if args.step in ("extract", "all"):
        df = extract_features()
        df.to_parquet(DATA_PATH, index=False)
        print(f"\nデータ保存 → {DATA_PATH}  ({DATA_PATH.stat().st_size/1024/1024:.1f} MB)")

    if args.step in ("train", "eval", "all"):
        if df is None:
            print(f"データ読み込み中: {DATA_PATH}")
            df = pd.read_parquet(DATA_PATH)
            print(f"  {len(df):,} 行")

        if args.step in ("train", "all"):
            models = train_models(df)
        else:
            print("モデルをロード中...")
            feature_cols = get_feature_cols()
            for target in ["is_1st", "is_2nd", "is_3rd"]:
                p = MODELS_DIR / f"xgb_{target}.pkl"
                with open(p, "rb") as f:
                    models[target] = pickle.load(f)
                print(f"  {target} ← {p}")

    if args.step in ("eval", "all"):
        if df is None:
            df = pd.read_parquet(DATA_PATH)
        evaluate_models(df, models)


if __name__ == "__main__":
    main()
