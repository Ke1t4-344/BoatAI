"""
Phase 2-1: ルールベーススコアリング予測モデル

スコア構成要素と重み:
  コース別1着率    35%  (course_stats)
  全国勝率        15%  (entries)
  当地勝率        10%  (entries)
  モーター2連対率  15%  (entries)
  展示タイム       20%  (before_info)
  スタートタイミング  5%  (before_info.exhibit_st or entries.avg_start_timing)

1コース補正:
  score_raw に COURSE1_BASE_BONUS を加算。
  会場の1コース1着率が全国平均(55%)を上回る場合は比例して強化。
"""

import sqlite3
import itertools
import math
from typing import Optional

DB_PATH = "boatai.db"

WEIGHTS = {
    "course_win_rate":   0.35,
    "national_win_rate": 0.15,
    "local_win_rate":    0.10,
    "motor_2ring":       0.15,
    "exhibition_time":   0.20,
    "start_timing":      0.05,
}

# コース別平均1着率（全国統計ベースのフォールバック）
COURSE_DEFAULT_WIN_RATE = {1: 55.0, 2: 15.0, 3: 9.5, 4: 7.5, 5: 6.5, 6: 5.5}

# 1コース構造的有利ボーナス（score_raw への加算値、0-1 スケール）
COURSE1_BASE_BONUS = 0.12
COURSE1_NATIONAL_AVG = 55.0  # 全国平均1コース1着率 (%)


def _parse_st(st_str: Optional[str]) -> Optional[float]:
    """.04 形式のST文字列を float に変換。不正値は None。"""
    if not st_str or not st_str.strip():
        return None
    try:
        return float(st_str.strip())
    except ValueError:
        return None


def _normalize(values: list) -> list:
    """0-1 正規化。None は有効値の平均で補完。全値が同じなら 0.5 固定。"""
    valid = [v for v in values if v is not None]
    if not valid:
        return [0.5] * len(values)
    min_v, max_v = min(valid), max(valid)
    if max_v == min_v:
        return [0.5] * len(values)
    mean_v = sum(valid) / len(valid)
    return [
        ((v if v is not None else mean_v) - min_v) / (max_v - min_v)
        for v in values
    ]


def predict(date: str, venue_code: str, race_no: int) -> dict:
    """
    指定レースの艇別スコアと3連単推奨買い目を返す。

    Args:
        date:       'YYYYMMDD'
        venue_code: '14' (鳴門) など
        race_no:    1-12

    Returns:
        {
          'race_info': {'date', 'venue_code', 'race_no', 'race_title'},
          'boats': [
            {'boat_no', 'player_name', 'start_course',
             'score', 'win_prob', 'components'}, ...
          ],  # スコア降順
          'recommended_3t': ['1-2-3', ...],  # 上位5点
        }
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- レース取得 ----
    cur.execute(
        "SELECT id, race_title FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (date, venue_code, race_no),
    )
    race = cur.fetchone()
    if race is None:
        conn.close()
        raise ValueError(f"Race not found: {date} venue={venue_code} race_no={race_no}")
    race_id = race["id"]

    # ---- entries ----
    cur.execute(
        """
        SELECT boat_no, player_no, player_name,
               avg_start_timing, national_win_rate, local_win_rate,
               motor_2ring_rate
        FROM entries WHERE race_id=? ORDER BY boat_no
        """,
        (race_id,),
    )
    entries = {row["boat_no"]: dict(row) for row in cur.fetchall()}

    # ---- before_info ----
    cur.execute(
        "SELECT boat_no, exhibition_time, exhibit_course, exhibit_st "
        "FROM before_info WHERE race_id=? ORDER BY boat_no",
        (race_id,),
    )
    before = {row["boat_no"]: dict(row) for row in cur.fetchall()}

    # ---- course_stats（最新データ優先） ----
    player_nos = [e["player_no"] for e in entries.values()]
    ph = ",".join("?" * len(player_nos))
    cur.execute(
        f"""
        SELECT player_no, course_no, win_rate_1st
        FROM course_stats
        WHERE player_no IN ({ph})
        ORDER BY fetched_date DESC
        """,
        player_nos,
    )
    cs_data: dict = {}
    for row in cur.fetchall():
        pno, cno = row["player_no"], row["course_no"]
        if pno not in cs_data:
            cs_data[pno] = {}
        if cno not in cs_data[pno]:  # DESC なので最初が最新
            cs_data[pno][cno] = row["win_rate_1st"]

    # ---- 会場別1コース1着率（DB実績 → フォールバック全国平均） ----
    cur.execute(
        """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN rre.rank = 1 THEN 1 ELSE 0 END) as wins
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE r.venue_code = ? AND rre.start_course = 1
        """,
        (venue_code,),
    )
    row = cur.fetchone()
    if row and row["total"] and row["total"] >= 10:
        venue_course1_rate = row["wins"] / row["total"] * 100
    else:
        venue_course1_rate = COURSE1_NATIONAL_AVG

    conn.close()

    # ---- 生データ収集 ----
    boats = []
    for boat_no, entry in entries.items():
        b = before.get(boat_no, {})
        player_no = entry["player_no"]

        # 出走コース: 展示コースがあればそちら、なければ艇番
        start_course = b.get("exhibit_course") or boat_no

        # コース別1着率（個人成績→全国平均フォールバック）
        course_wr = cs_data.get(player_no, {}).get(start_course)
        if course_wr is None:
            course_wr = COURSE_DEFAULT_WIN_RATE.get(start_course, 8.0)

        # 当地勝率（0.0 = データなし → 全国勝率で代替）
        local_wr = entry["local_win_rate"]
        if not local_wr:
            local_wr = entry["national_win_rate"]

        # 展示タイム（None のままでも正規化時に補完）
        exhibition_time = b.get("exhibition_time")

        # スタートタイミング: 展示ST → avg_start_timing の順で採用
        exhibit_st = _parse_st(b.get("exhibit_st"))
        effective_st = exhibit_st if exhibit_st is not None else entry["avg_start_timing"]

        boats.append(
            {
                "boat_no": boat_no,
                "player_no": player_no,
                "player_name": entry["player_name"].replace("　", " ").strip(),
                "start_course": start_course,
                "_raw": {
                    "course_win_rate": course_wr,
                    "national_win_rate": entry["national_win_rate"],
                    "local_win_rate": local_wr,
                    "motor_2ring": entry["motor_2ring_rate"],
                    "exhibition_time": exhibition_time,
                    "start_timing": effective_st,
                },
            }
        )

    # ---- 正規化 ----
    def col(key):
        return [b["_raw"][key] for b in boats]

    norm_course_wr  = _normalize(col("course_win_rate"))
    norm_national   = _normalize(col("national_win_rate"))
    norm_local      = _normalize(col("local_win_rate"))
    norm_motor      = _normalize(col("motor_2ring"))
    # 展示タイム: 速い（小）ほど良い → 反転
    norm_et  = [1.0 - v for v in _normalize(col("exhibition_time"))]
    # ST: 早い（小）ほど良い → 反転
    norm_st  = [1.0 - v for v in _normalize(col("start_timing"))]

    # ---- スコア計算 ----
    # 1コースボーナス: 会場の1コース実績が全国平均を上回るほど強化
    course1_bonus_factor = max(1.0, venue_course1_rate / COURSE1_NATIONAL_AVG)
    course1_bonus = COURSE1_BASE_BONUS * course1_bonus_factor

    w = WEIGHTS
    for i, boat in enumerate(boats):
        raw = boat["_raw"]
        base = (
            norm_course_wr[i] * w["course_win_rate"]
            + norm_national[i] * w["national_win_rate"]
            + norm_local[i]   * w["local_win_rate"]
            + norm_motor[i]   * w["motor_2ring"]
            + norm_et[i]      * w["exhibition_time"]
            + norm_st[i]      * w["start_timing"]
        )
        bonus = course1_bonus if boat["start_course"] == 1 else 0.0
        boat["score_raw"] = base + bonus
        boat["components"] = {
            "course_win_rate":   round(raw["course_win_rate"], 2),
            "national_win_rate": round(raw["national_win_rate"], 2),
            "local_win_rate":    round(raw["local_win_rate"], 2),
            "motor_2ring":       round(raw["motor_2ring"], 2),
            "exhibition_time":   raw["exhibition_time"],
            "start_timing":      round(raw["start_timing"], 3) if raw["start_timing"] is not None else None,
            "course1_bonus":     round(bonus, 4),
        }

    # 0-100 スケール
    max_s = max(b["score_raw"] for b in boats)
    min_s = min(b["score_raw"] for b in boats)
    for boat in boats:
        if max_s > min_s:
            boat["score"] = round((boat["score_raw"] - min_s) / (max_s - min_s) * 100, 1)
        else:
            boat["score"] = 50.0

    # ---- 勝率予測（softmax 風） ----
    exps = [math.exp(b["score"] / 20) for b in boats]
    total = sum(exps)
    for i, boat in enumerate(boats):
        boat["win_prob"] = round(exps[i] / total * 100, 1)

    # スコア降順ソート
    boats.sort(key=lambda x: x["score"], reverse=True)

    # ---- 推奨3連単（上位5点） ----
    top3_nos = [b["boat_no"] for b in boats[:3]]
    recommended = [
        f"{p[0]}-{p[1]}-{p[2]}" for p in itertools.permutations(top3_nos)
    ]  # 6通り
    if len(boats) >= 4:
        top4_no = boats[3]["boat_no"]
        for p in itertools.permutations([top3_nos[0], top3_nos[1], top4_no]):
            combo = f"{p[0]}-{p[1]}-{p[2]}"
            if combo not in recommended:
                recommended.append(combo)

    # 出力整形
    result_boats = [
        {
            "boat_no":     b["boat_no"],
            "player_name": b["player_name"],
            "start_course": b["start_course"],
            "score":       b["score"],
            "win_prob":    b["win_prob"],
            "components":  b["components"],
        }
        for b in boats
    ]

    return {
        "race_info": {
            "date":       date,
            "venue_code": venue_code,
            "race_no":    race_no,
            "race_title": race["race_title"],
        },
        "boats":           result_boats,
        "recommended_3t":  recommended[:5],
    }


def print_prediction(result: dict) -> None:
    ri = result["race_info"]
    print(f"\n{'='*75}")
    print(f"  {ri['date']}  会場コード:{ri['venue_code']}  {ri['race_no']}R  {ri['race_title'] or ''}")
    print(f"{'='*75}")
    header = (
        f"{'艇':<3} {'選手名':<12} {'CS':<3} {'スコア':<7} {'勝率':<8} "
        f"{'コースWR%':<10} {'全国WR':<8} {'当地WR':<8} {'モーター%':<10} {'展示T':<7} {'ST':<7} {'課1補正'}"
    )
    print(header)
    print('-' * 85)
    for b in result["boats"]:
        c = b["components"]
        et = f"{c['exhibition_time']:.2f}" if c["exhibition_time"] is not None else "  -   "
        st = f"{c['start_timing']:.3f}" if c["start_timing"] is not None else "  -  "
        bonus = f"+{c['course1_bonus']:.3f}" if c.get("course1_bonus") else "  -   "
        print(
            f"{b['boat_no']:<3} {b['player_name']:<12} {b['start_course']:<3} "
            f"{b['score']:<7.1f} {b['win_prob']:<7.1f}% "
            f"{c['course_win_rate']:<10.1f} {c['national_win_rate']:<8.2f} "
            f"{c['local_win_rate']:<8.2f} {c['motor_2ring']:<10.2f} {et:<7} {st:<7} {bonus}"
        )

    print(f"\n推奨買い目（3連単 上位5点）:")
    for i, combo in enumerate(result["recommended_3t"], 1):
        print(f"  {i}. {combo}")
    print()


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 4:
        d, v, r = sys.argv[1], sys.argv[2], int(sys.argv[3])
    else:
        # デフォルト: 最新データの鳴門(14) 1R
        d, v, r = "20260625", "14", 1

    result = predict(d, v, r)
    print_prediction(result)
