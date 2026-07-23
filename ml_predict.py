#!/usr/bin/env python3
"""
ml_predict.py — XGBoost MLモデルを使った3連単予想（v2: 全データ活用版）

predict.py と同じ引数・戻り値形式で使えるラッパー。
app.py から import して切り替えることができる。

使い方:
  from ml_predict import predict_ml
  result = predict_ml(date, venue_code, race_no)   # predict() と互換
"""

import sqlite3
import json
import itertools
import pickle
import math
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np

DB_PATH    = Path(__file__).parent / "boatai.db"
MODELS_DIR = Path(__file__).parent / "models"

# ── 定数（ml_pipeline.py と同じ値） ───────────────────────────────────────
CLASS_MAP = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}
COURSE_DEFAULT_WIN_RATE  = {1: 55.0, 2: 15.0, 3: 9.5, 4: 7.5, 5: 6.5, 6: 5.5}
COURSE_DEFAULT_TOP3_RATE = {1: 68.0, 2: 42.0, 3: 35.0, 4: 28.0, 5: 20.0, 6: 15.0}
COURSE_DEFAULT_AVG_ST    = {1: 0.17, 2: 0.18, 3: 0.19, 4: 0.20, 5: 0.21, 6: 0.23}
COURSE_HIST_WIN_RATE     = {1: 0.525, 2: 0.135, 3: 0.090, 4: 0.095, 5: 0.085, 6: 0.070}
COURSE_HIST_TOP3_RATE    = {1: 0.660, 2: 0.380, 3: 0.320, 4: 0.270, 5: 0.195, 6: 0.175}
VENUES    = [f"{i:02d}" for i in range(1, 25)]
VENUE_MAP = {v: i for i, v in enumerate(VENUES)}
TANSHO_RETURN_RATE = 0.75

# ── モジュールレベルキャッシュ（初回ロード後はメモリ常駐）──────────────────
_models:             dict = {}
_feature_cols:       list = []
_cs_map:             dict = {}   # (player_no, course_no) → stats
_venue_c1:           dict = {}   # venue_code → 1コース1着率
_player_hist:        dict = {}   # player_no → {win_rate, top3_rate, avg_st, st_std, race_count}
_player_venue_hist:  dict = {}   # (player_no, venue_code) → {win_rate, top3_rate}
_player_course_hist: dict = {}   # (player_no, course_no) → {win_rate, top3_rate, avg_st}
_trick_hist:         dict = {}   # player_no → {pct_nige, pct_makuri, pct_sashi, pct_makurisashi}
_cache_ready = False


def _parse_st(val) -> Optional[float]:
    """展示ST文字列をfloatに変換。'F.07'等フライングコードはNone。"""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s[0].isalpha():
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _load_models():
    """モデルと全共通データをメモリにロード（初回のみ）"""
    global _models, _feature_cols, _cs_map, _venue_c1
    global _player_hist, _player_venue_hist, _player_course_hist, _trick_hist
    global _cache_ready
    if _cache_ready:
        return

    # ── XGBoostモデル ──
    for target in ["is_1st", "is_2nd", "is_3rd"]:
        p = MODELS_DIR / f"xgb_{target}.pkl"
        if not p.exists():
            raise FileNotFoundError(
                f"モデルが見つかりません: {p}\n"
                "先に ml_pipeline.py --step all を実行してください。"
            )
        with open(p, "rb") as f:
            _models[target] = pickle.load(f)

    with open(MODELS_DIR / "feature_cols.json") as f:
        _feature_cols = json.load(f)

    conn = sqlite3.connect(DB_PATH, timeout=120)

    # ── course_stats（公式ウェブ取得） ──
    rows = conn.execute("""
        SELECT player_no, course_no, win_rate_1st, win_rate_2nd, win_rate_3rd, avg_st
        FROM course_stats ORDER BY fetched_date DESC
    """).fetchall()
    for row in rows:
        key = (row[0], row[1])
        if key not in _cs_map:
            t1 = row[2] or 0.0
            t2 = row[3] or 0.0
            t3 = row[4] or 0.0
            _cs_map[key] = {
                "top1":   t1 if t1 > 0 else COURSE_DEFAULT_WIN_RATE.get(row[1], 8.0),
                "top3":   t2 + t3,
                "avg_st": row[5] or COURSE_DEFAULT_AVG_ST.get(row[1], 0.20),
            }

    # ── 会場別1コース1着率 ──
    rows = conn.execute("""
        SELECT r.venue_code, COUNT(*) AS total,
               SUM(CASE WHEN rre.rank=1 THEN 1 ELSE 0 END) AS wins
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.start_course = 1
        GROUP BY r.venue_code
    """).fetchall()
    for row in rows:
        vc, total, wins = row[0], row[1], row[2]
        _venue_c1[vc] = (wins / total * 100) if total >= 10 else 55.0

    # ── 選手別実績（race_result_entries 100%カバレッジ） ──
    ph_rows = conn.execute("""
        SELECT player_no,
               COUNT(*) as n,
               SUM(CASE WHEN rank=1 THEN 1.0 ELSE 0 END) as wins,
               SUM(CASE WHEN rank<=3 THEN 1.0 ELSE 0 END) as top3,
               AVG(CAST(start_timing AS REAL)) as avg_st,
               SUM(CAST(start_timing AS REAL)*CAST(start_timing AS REAL)) as st_sq_sum
        FROM race_result_entries
        WHERE player_no IS NOT NULL AND rank IS NOT NULL
          AND CAST(start_timing AS REAL) BETWEEN -0.5 AND 1.0
        GROUP BY player_no
    """).fetchall()
    for row in ph_rows:
        n = row[1]
        if n < 10:
            continue
        avg_st = float(row[4] or 0.18)
        st_sq_mean = float(row[5] or 0) / n
        st_var = max(0.0, st_sq_mean - avg_st ** 2)
        _player_hist[row[0]] = {
            "win_rate":   row[2] / n,
            "top3_rate":  row[3] / n,
            "avg_st":     avg_st,
            "st_std":     st_var ** 0.5,
            "race_count": n,
        }

    # ── 選手×会場別実績 ──
    pv_rows = conn.execute("""
        SELECT rre.player_no, r.venue_code, COUNT(*) as n,
               SUM(CASE WHEN rre.rank=1 THEN 1.0 ELSE 0 END) as wins,
               SUM(CASE WHEN rre.rank<=3 THEN 1.0 ELSE 0 END) as top3
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.player_no IS NOT NULL AND rre.rank IS NOT NULL
        GROUP BY rre.player_no, r.venue_code
    """).fetchall()
    for row in pv_rows:
        n = row[2]
        if n < 5:
            continue
        _player_venue_hist[(row[0], row[1])] = {
            "win_rate":   row[3] / n,
            "top3_rate":  row[4] / n,
            "race_count": n,
        }

    # ── 選手×コース別実績 ──
    pc_rows = conn.execute("""
        SELECT player_no, start_course, COUNT(*) as n,
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
        n = row[2]
        if n < 5:
            continue
        _player_course_hist[(row[0], row[1])] = {
            "win_rate":   row[3] / n,
            "top3_rate":  row[4] / n,
            "avg_st":     float(row[5] or 0.18),
            "race_count": n,
        }

    # ── 決まり手分布 ──
    tk_rows = conn.execute("""
        SELECT player_no,
               SUM(CASE WHEN rank=1 THEN 1.0 ELSE 0 END) as total_wins,
               SUM(CASE WHEN winning_trick='逃げ'      AND rank=1 THEN 1.0 ELSE 0 END),
               SUM(CASE WHEN winning_trick='まくり'    AND rank=1 THEN 1.0 ELSE 0 END),
               SUM(CASE WHEN winning_trick='差し'      AND rank=1 THEN 1.0 ELSE 0 END),
               SUM(CASE WHEN winning_trick='まくり差し' AND rank=1 THEN 1.0 ELSE 0 END)
        FROM race_result_entries
        WHERE player_no IS NOT NULL AND winning_trick IS NOT NULL
        GROUP BY player_no
        HAVING total_wins >= 3
    """).fetchall()
    for row in tk_rows:
        total = row[1] or 1
        _trick_hist[row[0]] = {
            "pct_nige":        row[2] / total,
            "pct_makuri":      row[3] / total,
            "pct_sashi":       row[4] / total,
            "pct_makurisashi": row[5] / total,
        }

    conn.close()
    _cache_ready = True
    print(f"[ml_predict] モデルロード完了: {len(_player_hist):,}選手の実績データ")


def _build_boat_features(boat_no: int, pno, row_entries, venue_code: str,
                          race_no: int, year: int, month: int, dow: int,
                          weather: dict, before_info: dict,
                          meet_motor: dict | None = None,
                          meet_player: dict | None = None) -> dict:
    """1艇分の特徴量dictを構築"""
    cs  = _cs_map.get((pno, boat_no), {})
    c_top1   = cs.get("top1",   COURSE_DEFAULT_WIN_RATE.get(boat_no, 8.0))
    c_top3   = cs.get("top3",   COURSE_DEFAULT_TOP3_RATE.get(boat_no, 20.0))
    c_avg_st = cs.get("avg_st", COURSE_DEFAULT_AVG_ST.get(boat_no, 0.20))
    has_cs   = 1 if (pno, boat_no) in _cs_map else 0

    nat_wr  = row_entries[8]  or 0.0
    nat_2r  = row_entries[9]  or 0.0
    loc_wr  = row_entries[10] or 0.0
    loc_2r  = row_entries[11] or 0.0
    mtr_2r  = row_entries[12] or 0.0
    bt_2r   = row_entries[13] or 0.0
    avg_st  = row_entries[7]  or 0.18
    fl_cnt  = min(row_entries[5] or 0, 5)
    lt_cnt  = min(row_entries[6] or 0, 5)
    age     = row_entries[3]  or 35
    wt      = row_entries[4]  or 52.0
    pclass  = CLASS_MAP.get(row_entries[2] or "", 2)
    ab_sc   = (nat_wr * c_top1) ** 0.5

    # ── 実績ベース特徴量 ──
    ph  = _player_hist.get(pno, {})
    pvh = _player_venue_hist.get((pno, venue_code), {})
    pch = _player_course_hist.get((pno, boat_no), {})
    tk  = _trick_hist.get(pno, {})

    ph_win    = ph.get("win_rate",   0.0)
    ph_top3   = ph.get("top3_rate",  0.0)
    ph_avg_st = ph.get("avg_st",     0.18)
    ph_st_std = ph.get("st_std",     0.05)
    ph_count  = min(ph.get("race_count", 0), 2000)

    pvh_win   = pvh.get("win_rate",  0.0)
    pvh_top3  = pvh.get("top3_rate", 0.0)
    pvh_count = min(pvh.get("race_count", 0), 500)

    pch_win    = pch.get("win_rate",  COURSE_HIST_WIN_RATE.get(boat_no, 0.08))
    pch_top3   = pch.get("top3_rate", COURSE_HIST_TOP3_RATE.get(boat_no, 0.25))
    pch_avg_st = pch.get("avg_st",    COURSE_DEFAULT_AVG_ST.get(boat_no, 0.20))
    pch_count  = min(pch.get("race_count", 0), 500)

    official_wr  = nat_wr / 100.0
    hist_wr_diff = ph_win - official_wr
    hist_st_diff = ph_avg_st - avg_st
    hist_ability = (ph_win * pch_win) ** 0.5

    return {
        "boat_no":   boat_no,
        "player_no": pno,
        # ── A: レース/会場 ──
        "venue_code":    VENUE_MAP.get(venue_code, 0),
        "race_no":       race_no,
        "year":          year,
        "month":         month,
        "day_of_week":   dow,
        "venue_c1_rate": _venue_c1.get(venue_code, 55.0),
        # ── A: 公式統計 ──
        "national_win_rate":   nat_wr,
        "national_2ring_rate": nat_2r,
        "local_win_rate":      loc_wr,
        "local_2ring_rate":    loc_2r,
        "motor_2ring_rate":    mtr_2r,
        "boat_2ring_rate":     bt_2r,
        "avg_start_timing":    avg_st,
        "flying_count":        fl_cnt,
        "late_count":          lt_cnt,
        "age":                 age,
        "weight":              wt,
        "player_class":        pclass,
        "boat_no_pos":         boat_no,
        "course_top1_rate":    c_top1,
        "course_top3_rate":    c_top3,
        "course_avg_st":       c_avg_st,
        "course_default_wr":   COURSE_DEFAULT_WIN_RATE.get(boat_no, 8.0),
        "course_advantage":    COURSE_DEFAULT_WIN_RATE.get(boat_no, 8.0),
        "has_course_stats":    has_cs,
        # ── B-1: 選手別実績 ──
        "hist_win_rate":    ph_win,
        "hist_top3_rate":   ph_top3,
        "hist_avg_st":      ph_avg_st,
        "hist_st_std":      ph_st_std,
        "hist_race_count":  ph_count,
        # ── B-2: 選手×会場別 ──
        "hist_venue_win_rate":   pvh_win,
        "hist_venue_top3_rate":  pvh_top3,
        "hist_venue_count":      pvh_count,
        # ── B-3: 選手×コース別 ──
        "hist_course_win_rate":   pch_win,
        "hist_course_top3_rate":  pch_top3,
        "hist_course_avg_st":     pch_avg_st,
        "hist_course_count":      pch_count,
        # ── B-4: 決まり手分布 ──
        "hist_pct_nige":        tk.get("pct_nige",        0.0),
        "hist_pct_makuri":      tk.get("pct_makuri",       0.0),
        "hist_pct_sashi":       tk.get("pct_sashi",        0.0),
        "hist_pct_makurisashi": tk.get("pct_makurisashi",  0.0),
        "has_trick_data":       1 if pno in _trick_hist else 0,
        # ── 差分・派生 ──
        "hist_wr_diff":     hist_wr_diff,
        "hist_st_diff":     hist_st_diff,
        "hist_ability":     hist_ability,
        "fl_late_risk":     min(fl_cnt + lt_cnt, 10),
        "motor_boat_combo": (mtr_2r + bt_2r) / 2,
        "ability_score":    ab_sc,
        # ── D: 天気（NaN可） ──
        "wind_speed":      weather.get("wind_speed"),
        "wave_height":     weather.get("wave_height"),
        "water_temp":      weather.get("water_temp"),
        "temperature":     weather.get("temperature"),
        "wind_direction":  weather.get("wind_direction"),
        # ── D: 直前情報（NaN可） ──
        "exhibition_time": before_info.get("exhibition_time"),
        "exhibit_st":      _parse_st(before_info.get("exhibit_st")),
        "tilt":            before_info.get("tilt"),
        # ── E: 節内成績（NaN可） ──
        "meet_motor_top2_rate":  (meet_motor or {}).get("meet_motor_top2_rate"),
        "meet_motor_race_count": (meet_motor or {}).get("meet_motor_race_count"),
        "meet_player_top2_rate": (meet_player or {}).get("meet_player_top2_rate"),
        "meet_player_avg_rank":  (meet_player or {}).get("meet_player_avg_rank"),
        "meet_player_race_count":(meet_player or {}).get("meet_player_race_count"),
    }


def _add_relative_features(boat_feats: list) -> None:
    """レース内相対特徴量をin-placeで追加"""
    rel_targets = [
        ("national_win_rate",     False),
        ("national_2ring_rate",   False),
        ("local_win_rate",        False),
        ("local_2ring_rate",      False),
        ("motor_2ring_rate",      False),
        ("boat_2ring_rate",       False),
        ("avg_start_timing",      True),
        ("course_top1_rate",      False),
        ("course_top3_rate",      False),
        ("ability_score",         False),
        ("hist_win_rate",         False),
        ("hist_top3_rate",        False),
        ("hist_avg_st",           True),
        ("hist_course_win_rate",  False),
        ("hist_ability",          False),
    ]
    for col, asc in rel_targets:
        vals = [b.get(col, 0.0) or 0.0 for b in boat_feats]
        mean_v = sum(vals) / max(len(vals), 1)
        max_v  = max(vals) if vals else 0.0
        sorted_idx = sorted(range(len(vals)), key=lambda i: vals[i], reverse=not asc)
        ranks = [0] * len(vals)
        for rank_pos, orig_idx in enumerate(sorted_idx):
            ranks[orig_idx] = rank_pos + 1
        for i, b in enumerate(boat_feats):
            b[f"{col}_rank"]    = ranks[i]
            b[f"{col}_vs_mean"] = vals[i] - mean_v
            b[f"{col}_vs_max"]  = vals[i] - max_v


def predict_ml(date: str, venue_code: str, race_no: int, conn=None) -> dict:
    """
    XGBoost MLモデルで3連単予想を行う。predict.py の predict() と互換の戻り値形式。
    """
    import pandas as pd

    _load_models()

    _own_conn = conn is None
    if _own_conn:
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")

    try:
        # ── レースID・タイトル取得 ──
        row = conn.execute(
            "SELECT id, race_title FROM races WHERE date=? AND venue_code=? AND race_no=?",
            (date, venue_code, race_no)
        ).fetchone()
        if row is None:
            raise ValueError(f"Race not found: {date} {venue_code} {race_no}R")
        race_id    = row[0]
        race_title = row[1] or ""

        # ── entries 取得 ──
        entries = conn.execute("""
            SELECT boat_no, player_no, player_class, age, weight,
                   flying_count, late_count, avg_start_timing,
                   national_win_rate, national_2ring_rate,
                   local_win_rate, local_2ring_rate,
                   motor_2ring_rate, boat_2ring_rate
            FROM entries WHERE race_id=? ORDER BY boat_no
        """, (race_id,)).fetchall()
        if not entries:
            raise ValueError("出走表データがありません")

        # ── 天気データ（NaN可） ──
        w_row = conn.execute(
            "SELECT wind_speed, wave_height, water_temp, temperature, wind_direction "
            "FROM weather WHERE race_id=?",
            (race_id,)
        ).fetchone()
        weather = {}
        if w_row:
            weather = {"wind_speed": w_row[0], "wave_height": w_row[1],
                       "water_temp": w_row[2], "temperature": w_row[3],
                       "wind_direction": w_row[4]}

        # ── 節内成績（NaN可 — 節初戦は0/None） ──
        # motor meet stats: (race_id, boat_no) → stats（ml_pipeline と同じキー設計）
        # 直近7日以内の同会場での成績を使用
        meet_motor_stats: dict = {}
        for row_e in conn.execute("""
            SELECT boat_no, motor_no FROM entries WHERE race_id=?
        """, (race_id,)).fetchall():
            bn, mno = row_e[0], row_e[1]
            if mno is None:
                continue
            m_row = conn.execute("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN rre.rank<=2 THEN 1 ELSE 0 END) as top2
                FROM entries e2
                JOIN races r2   ON r2.id = e2.race_id
                JOIN race_result_entries rre ON rre.race_id = e2.race_id AND rre.boat_no = e2.boat_no
                WHERE e2.motor_no = ?
                  AND r2.venue_code = ?
                  AND r2.date >= date(?, '-7 days')
                  AND r2.date < ?
            """, (mno, venue_code, date[:4]+'-'+date[4:6]+'-'+date[6:], date[:4]+'-'+date[4:6]+'-'+date[6:])).fetchone()
            n = m_row[0] or 0
            meet_motor_stats[bn] = {
                "meet_motor_top2_rate":  (m_row[1] / n) if n > 0 else None,
                "meet_motor_race_count": float(n),
            }

        # player meet stats: 同会場・直近7日の選手成績
        meet_player_stats: dict = {}
        for row_e in entries:
            bn, pno_e = row_e[0], row_e[1]
            p_row = conn.execute("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN rre.rank<=2 THEN 1 ELSE 0 END) as top2,
                       AVG(CAST(rre.rank AS REAL)) as avg_rank
                FROM race_result_entries rre
                JOIN races r2 ON r2.id = rre.race_id
                WHERE rre.player_no = ?
                  AND r2.venue_code = ?
                  AND r2.date >= date(?, '-7 days')
                  AND r2.date < ?
            """, (pno_e, venue_code, date[:4]+'-'+date[4:6]+'-'+date[6:], date[:4]+'-'+date[4:6]+'-'+date[6:])).fetchone()
            n = p_row[0] or 0
            meet_player_stats[bn] = {
                "meet_player_top2_rate":  (p_row[1] / n) if n > 0 else None,
                "meet_player_avg_rank":   p_row[2] if n > 0 else None,
                "meet_player_race_count": float(n),
            }

        # ── 直前情報（NaN可） ──
        bi_rows = conn.execute(
            "SELECT boat_no, exhibition_time, exhibit_st, tilt FROM before_info WHERE race_id=?",
            (race_id,)
        ).fetchall()
        bi_map = {r[0]: {"exhibition_time": r[1], "exhibit_st": r[2], "tilt": r[3]}
                  for r in bi_rows}

        # ── date features ──
        try:
            dt    = datetime.strptime(date, "%Y%m%d")
            year  = dt.year
            month = dt.month
            dow   = dt.weekday()
        except Exception:
            year, month, dow = 2025, 6, 0

        # ── 各艇の特徴量構築 ──
        boat_feats = []
        for row_e in entries:
            boat_no = row_e[0]
            pno     = row_e[1]
            feats = _build_boat_features(
                boat_no, pno, row_e, venue_code, race_no, year, month, dow,
                weather, bi_map.get(boat_no, {}),
                meet_motor_stats.get(boat_no),
                meet_player_stats.get(boat_no),
            )
            boat_feats.append(feats)

        if len(boat_feats) < 3:
            raise ValueError("艇数不足")

        _add_relative_features(boat_feats)

        # ── XGBoost 予測 ──
        X = pd.DataFrame([{col: b.get(col) for col in _feature_cols} for b in boat_feats])
        # スパース列以外は0埋め、スパース列はNaNのままXGBoostに渡す
        sparse_cols = {
            "wind_speed", "wave_height", "water_temp", "temperature", "wind_direction",
            "exhibition_time", "exhibit_st", "tilt",
            "meet_motor_top2_rate", "meet_motor_race_count",
            "meet_player_top2_rate", "meet_player_avg_rank", "meet_player_race_count",
        }
        for col in _feature_cols:
            if col not in sparse_cols and col in X.columns:
                X[col] = pd.to_numeric(X[col], errors="coerce").fillna(0)
            elif col in X.columns:
                X[col] = pd.to_numeric(X[col], errors="coerce")
        X = X.astype(float)

        boats_by_no = {}
        for i, b in enumerate(boat_feats):
            bn = b["boat_no"]
            boats_by_no[bn] = {
                **b,
                "prob_1st": float(_models["is_1st"].predict_proba(X.iloc[[i]])[:, 1][0]),
                "prob_2nd": float(_models["is_2nd"].predict_proba(X.iloc[[i]])[:, 1][0]),
                "prob_3rd": float(_models["is_3rd"].predict_proba(X.iloc[[i]])[:, 1][0]),
            }

        # ── 120通りコンボ確率 ──
        all_nos = sorted(boats_by_no.keys())
        combo_probs = []
        for a, b, c in itertools.permutations(all_nos, 3):
            p = (boats_by_no[a]["prob_1st"]
               * boats_by_no[b]["prob_2nd"]
               * boats_by_no[c]["prob_3rd"])
            combo_probs.append((f"{a}-{b}-{c}", p))

        total_p = sum(p for _, p in combo_probs)
        if total_p <= 0:
            total_p = 1.0
        combo_probs.sort(key=lambda x: x[1], reverse=True)

        # ── ライブオッズ（odds_3t）取得 ──
        # fetched_at が NULL の場合も含めて最新データを取得
        _max_fa = conn.execute(
            "SELECT MAX(fetched_at) FROM odds_3t WHERE race_id=?", (race_id,)
        ).fetchone()[0]
        if _max_fa is not None:
            live_odds_rows = conn.execute("""
                SELECT combination, odds FROM odds_3t
                WHERE race_id=? AND fetched_at=?
            """, (race_id, _max_fa)).fetchall()
        else:
            # fetched_at が全行NULL → そのままレース分を全取得
            live_odds_rows = conn.execute(
                "SELECT combination, odds FROM odds_3t WHERE race_id=?", (race_id,)
            ).fetchall()
        live_odds_map = {r[0]: r[1] for r in live_odds_rows if r[1] and r[1] > 0}

        # ── 結果整形 ──
        all_candidates = []
        for combo, raw_p in combo_probs:
            prob     = raw_p / total_p
            exp_o    = round(TANSHO_RETURN_RATE / prob, 1) if prob > 0 else None
            live_o   = live_odds_map.get(combo)
            # EVはライブオッズがあればそれを使い、なければ理論オッズで計算
            ev_val   = round(prob * live_o, 3) if live_o else round(prob * (exp_o or 0), 3)
            cat      = "◎" if exp_o and exp_o <= 25 else ("○" if exp_o and exp_o <= 80 else "△")
            all_candidates.append({
                "combo":         combo,
                "prob":          round(prob * 100, 2),
                "expected_odds": exp_o,
                "live_odds":     live_o,
                "hist_odds":     None,
                "ev":            ev_val,
                "category":      cat,
            })

        # カテゴリ別（本命10点・中穴15点・穴15点）
        honmei_t = [c for c in all_candidates if (c["expected_odds"] or 999) <= 25][:10]
        chuana_t = [c for c in all_candidates if 25 < (c["expected_odds"] or 999) <= 80][:15]
        ana_t    = sorted(
            [c for c in all_candidates if (c["expected_odds"] or 999) > 80],
            key=lambda x: x["ev"] or 0, reverse=True
        )[:15]
        if not honmei_t: honmei_t = all_candidates[:10]
        if not chuana_t: chuana_t = all_candidates[10:25]
        if not ana_t:    ana_t    = all_candidates[25:40]

        # boats リスト（app.py 表示用）
        boats_list = []
        for b in sorted(boats_by_no.values(), key=lambda x: x["prob_1st"], reverse=True):
            # player_name を entries テーブルから取得（なければ空）
            pname_row = conn.execute(
                "SELECT player_name FROM entries WHERE race_id=? AND boat_no=?",
                (race_id, b["boat_no"])
            ).fetchone()
            boats_list.append({
                "boat_no":           b["boat_no"],
                "player_no":         b["player_no"],
                "player_name":       pname_row[0] if pname_row else "",
                "start_course":      b["boat_no_pos"],
                "score":             round(b["prob_1st"] * 1000, 1),
                "win_prob":          round(b["prob_1st"] * 100, 1),
                "win_prob_blended":  round(b["prob_1st"] * 100, 1),
                "top3_prob":         round(b["prob_3rd"] * 100, 1),
                "tansho_odds":       None,
                "components": {
                    "national_win_rate":  b["national_win_rate"],
                    "local_win_rate":     b["local_win_rate"],
                    "motor_2ring":        b["motor_2ring_rate"],
                    "course_top1_rate":   b["course_top1_rate"],
                    "avg_start_timing":   b["avg_start_timing"],
                    "hist_win_rate":      round(b["hist_win_rate"] * 100, 1),
                    "hist_course_win":    round(b["hist_course_win_rate"] * 100, 1),
                    # 直前情報（NaN可）
                    "tilt":              b.get("tilt"),
                    "exhibition_time":   b.get("exhibition_time"),
                    "start_timing":      b.get("exhibit_st"),
                    # 任意キー（ルールベース互換）
                    "form_score":        0.5,
                    "trick_aptitude":    0.0,
                },
            })

        top_boat = max(boats_by_no.values(), key=lambda x: x["prob_1st"])

        top5_combos = [c["combo"] for c in all_candidates[:5]]

        # ── EV分析用: ライブオッズがある買い目をEV降順で返す ──
        ev_recs = []
        for c in all_candidates:
            if c["live_odds"] and c["live_odds"] > 0:
                ev_recs.append(c)
        # ライブオッズがない場合は理論EV上位を返す（グラフ表示用）
        if not ev_recs:
            ev_recs = all_candidates[:20]
        ev_recs = sorted(ev_recs, key=lambda x: x["ev"] or 0, reverse=True)[:20]
        ev_recs_detail = [dict(rank=i+1, **c) for i, c in enumerate(ev_recs)]

        return {
            "model":                 "xgboost_v2",
            # predict.py 互換キー
            "race_info":             {"date": date, "venue_code": venue_code,
                                      "race_no": race_no, "race_title": race_title},
            "recommended_3t":        top5_combos,
            # 詳細
            "boats":                 boats_list,
            "recommended_3t_detail": [dict(rank=i+1, **c) for i, c in enumerate(all_candidates[:10])],
            "honmei_detail":         [dict(rank=i+1, **c) for i, c in enumerate(honmei_t)],
            "chuana_detail":         [dict(rank=i+1, **c) for i, c in enumerate(chuana_t)],
            "ana_detail":            [dict(rank=i+1, **c) for i, c in enumerate(ana_t)],
            "top5_combos":           top5_combos,
            "ev_recs_detail":        ev_recs_detail,
            "venue_calib":           1.0,
            "confidence":            float(top_boat["prob_1st"]),
        }

    finally:
        if _own_conn:
            conn.close()


if __name__ == "__main__":
    import sys
    date       = sys.argv[1] if len(sys.argv) > 1 else "20260701"
    venue_code = sys.argv[2] if len(sys.argv) > 2 else "01"
    race_no    = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    print(f"ML予想: {date} 会場{venue_code} {race_no}R")
    result = predict_ml(date, venue_code, race_no)
    print(f"モデル: {result['model']}  信頼度: {result['confidence']:.3f}")
    print(f"Top5: {result['top5_combos']}")
    print("\n本命:")
    for c in result["honmei_detail"]:
        print(f"  {c['combo']}  odds={c['expected_odds']}倍  prob={c['prob']:.2f}%")
    print("\n中穴:")
    for c in result["chuana_detail"]:
        print(f"  {c['combo']}  odds={c['expected_odds']}倍  prob={c['prob']:.2f}%")
