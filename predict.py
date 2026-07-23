"""
predict.py — BoatAI ルールベーススコアリング予測モデル (v2: 2026-07-08)

ベース指標と重み（合計100%）:
  コース別1着率      30%  (v1: 50.6%)
  コース別3連対率    15%  (v1: なし ← 新規)
  全国勝率          20%  (v1: 25.1%)
  展示タイム         15%  (v1: 13.6%)
  モーター2連対率     8%  (v1:  5.6%)
  コース別平均ST      5%  (v1: なし ← 新規)
  当地勝率           5%  (v1:  5.2%)
  スタートタイミング   2%  (v1:  0.01%)

補正・ボーナス（ベースに加算）:
  今節調子          +8%  (v1: +6%)
  決まり手適性      +6%  (v1: +4%)
  今節成績（日数加味）+5%  (v1: なし ← 新規)
  ST安定性          +3%  (v1: +3%)
  1号艇ボーナス      0%  (v1: +15%, v2: +8%→0% コース別統計に内包済みのため廃止)

推奨買い目の考え方（3パターン × 5通り）:
  本命: ライブオッズ ≤ 25倍 — 安定した軸狙い
  中穴: ライブオッズ 25〜80倍 — バランス型の穴狙い
  穴:   ライブオッズ > 80倍  — 高配当狙い

特徴:
  - 展開予測: 決まり手率（逃げ・差し・まくり）を用いたコンボ確率補正
  - オッズブレンド: モデル確率50% + 市場確率50%（単勝オッズ利用可時）
  - 会場補正: 過去バックテスト結果から会場別の精度傾向を学習
  - バックアップ: predict_backup_20260708.py (v1)
"""

import sqlite3
import itertools
import math
from pathlib import Path
from typing import Optional

DB_PATH = str(Path(__file__).resolve().parent / "boatai.db")

# ---------------------------------------------------------------------------
# モジュールレベルキャッシュ（バックテスト等の大量処理で high-cost クエリを削減）
# warm_cache(conn) を呼ぶとキャッシュが有効になる。通常利用では呼ばなくてよい。
# ---------------------------------------------------------------------------
_cache: dict = {}


def warm_cache(conn) -> None:
    """
    バックテスト等の大量処理前に高コストクエリを一括プリロードする。
    warm_cache() 呼び出し後は predict() 内の以下クエリがキャッシュに切り替わる:
      - _get_historical_avg_odds  (120クエリ/レース → 0)
      - venue_course1_rate        (1集計クエリ/レース → 0)
      - _get_venue_calibration    (1集計クエリ/レース → 0)
    """
    import time
    global _cache
    t0 = time.time()

    # 1. 過去3連単オッズ平均: (venue_code, combination) → avg_odds
    print("キャッシュ構築中: 過去3連単オッズ平均...", flush=True)
    rows = conn.execute("""
        SELECT r.venue_code, o.combination,
               AVG(o.odds) AS avg_odds, COUNT(*) AS cnt
        FROM odds_3t o
        JOIN races r ON r.id = o.race_id
        WHERE o.odds IS NOT NULL AND o.odds > 0
        GROUP BY r.venue_code, o.combination
        HAVING COUNT(*) >= 5
    """).fetchall()
    hist_odds: dict = {}
    for row in rows:
        hist_odds[(row[0], row[1])] = round(row[2], 1)
    _cache["hist_odds"] = hist_odds
    print(f"  過去オッズ: {len(hist_odds):,}件", flush=True)

    # 2. 会場別1コース1着率: venue_code → rate(%)
    print("キャッシュ構築中: 会場別1コース1着率...", flush=True)
    rows = conn.execute("""
        SELECT r.venue_code,
               COUNT(*) AS total,
               SUM(CASE WHEN rre.rank = 1 THEN 1 ELSE 0 END) AS wins
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE rre.start_course = 1
        GROUP BY r.venue_code
    """).fetchall()
    venue_c1: dict = {}
    for row in rows:
        vc, total, wins = row[0], row[1], row[2]
        venue_c1[vc] = (wins / total * 100 if total and total >= 10 else COURSE1_NATIONAL_AVG)
    _cache["venue_course1"] = venue_c1
    print(f"  会場別1コース率: {len(venue_c1)}会場", flush=True)

    # 3. 会場補正係数: venue_code → calibration(0.7〜1.3)
    print("キャッシュ構築中: 会場補正係数...", flush=True)
    rows = conn.execute("""
        SELECT r.venue_code, COUNT(*) AS total, SUM(p.hit_top3) AS hits
        FROM predictions p
        JOIN races r ON r.id = p.race_id
        WHERE p.hit_top3 IS NOT NULL
        GROUP BY r.venue_code
    """).fetchall()
    NATIONAL_AVG = 0.186
    venue_calib: dict = {}
    for row in rows:
        vc, total, hits = row[0], row[1], row[2]
        if total and total >= 20 and hits is not None:
            ratio = (hits / total) / NATIONAL_AVG
            venue_calib[vc] = max(0.7, min(1.3, ratio))
        else:
            venue_calib[vc] = 1.0
    _cache["venue_calib"] = venue_calib
    print(f"  会場補正: {len(venue_calib)}会場", flush=True)

    elapsed = round(time.time() - t0, 1)
    print(f"キャッシュ構築完了 ({elapsed}秒)\n", flush=True)

WEIGHTS = {
    "course_top1_rate":    0.30,   # コース別1着率       (v1: 0.5059 "course_win_rate")
    "course_top3_rate":    0.15,   # コース別3連対率      (v1: なし 新規)
    "national_win_rate":   0.20,   # 全国勝率            (v1: 0.2510)
    "local_win_rate":      0.05,   # 当地勝率            (v1: 0.0515)
    "motor_2ring":         0.08,   # モーター2連対率      (v1: 0.0558)
    "exhibition_time":     0.15,   # 展示タイム           (v1: 0.1358)
    "start_timing":        0.02,   # スタートタイミング    (v1: 0.0001)
    "course_avg_st":       0.05,   # コース別平均ST       (v1: なし 新規)
}

# 1コースが最も有利: 全国平均55%、以降コース順に低下
COURSE_DEFAULT_WIN_RATE  = {1: 55.0, 2: 15.0, 3: 9.5, 4: 7.5, 5: 6.5, 6: 5.5}
COURSE_DEFAULT_TOP3_RATE = {1: 68.0, 2: 42.0, 3: 35.0, 4: 28.0, 5: 20.0, 6: 15.0}
COURSE_DEFAULT_AVG_ST    = {1: 0.17, 2: 0.18, 3: 0.19, 4: 0.20, 5: 0.21, 6: 0.23}
COURSE1_BASE_BONUS       = 0.00   # 1コースへの上乗せボーナス (v1: 0.15, v2: 0.08→0.00 コース別統計に内包済みのため廃止)
COURSE1_NATIONAL_AVG    = 55.0
TANSHO_RETURN_RATE      = 0.75   # 3連単の控除後還元率


def _get_meet_day_form(
    conn, player_no: int, venue_code: str, race_date: str
) -> Optional[float]:
    """
    今節（同一会場・同一開催節）の成績を日数加重スコアで返す。

    スコア = Σ (rank_score × day_weight) / Σ day_weight
    rank_score: 1着=1.0, 2着=0.7, 3着=0.4, 4〜6着=0.0
    day_weight : 当日=1.0, 1日前=0.8, 2日前=0.64 … (0.8^日数)
    race_date は YYYYMMDD 形式。
    """
    from datetime import datetime, timedelta
    try:
        td = datetime.strptime(race_date, "%Y%m%d").date()
        start_date = (td - timedelta(days=6)).strftime("%Y%m%d")
        cur = conn.cursor()
        # 今節 = 同一会場・直近6日以内（標準6日開催）
        cur.execute("""
            SELECT r.date AS race_date, rre.rank
            FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE rre.player_no = ?
              AND r.venue_code  = ?
              AND r.date        <  ?
              AND r.date        >= ?
            ORDER BY r.date DESC
        """, (player_no, venue_code, race_date, start_date))
        rows = cur.fetchall()
    except Exception:
        return None

    if not rows:
        return None

    rank_score_map = {1: 1.0, 2: 0.7, 3: 0.4}
    total_w = 0.0
    total_s = 0.0
    for row in rows:
        try:
            rd   = datetime.strptime(row["race_date"], "%Y%m%d").date()
            days = (td - rd).days
            if days < 0:
                continue
            w = 0.8 ** days
            s = rank_score_map.get(row["rank"], 0.0)
            total_w += w
            total_s += s * w
        except Exception:
            continue

    if total_w == 0:
        return None
    return total_s / total_w


def _parse_st(st_str: Optional[str]) -> Optional[float]:
    if not st_str or not st_str.strip():
        return None
    try:
        return float(st_str.strip())
    except ValueError:
        return None


def _normalize(values: list) -> list:
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


def _combo_prob(boats_by_no: dict, combo_str: str) -> float:
    """
    3連単の確率を推定（条件付き確率モデル）
    P(a-b-c) = P(a 1着) × P(b 2着 | a 1着) × P(c 3着 | a-b 確定)
    2着・3着は残りの艇の win_prob を相対比率で計算
    """
    parts = combo_str.split('-')
    if len(parts) != 3:
        return 0.0
    try:
        a, b, c = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 0.0

    all_nos = list(boats_by_no.keys())
    pa = boats_by_no.get(a, {}).get('win_prob', 0) / 100.0

    # 2着: aを除いた残りでの相対確率
    remaining_after_a = {k: v for k, v in boats_by_no.items() if k != a}
    sum_after_a = sum(v['win_prob'] for v in remaining_after_a.values())
    if sum_after_a <= 0:
        return 0.0
    pb_given_a = (boats_by_no.get(b, {}).get('win_prob', 0)) / sum_after_a

    # 3着: a,bを除いた残りでの相対確率
    remaining_after_ab = {k: v for k, v in remaining_after_a.items() if k != b}
    sum_after_ab = sum(v['win_prob'] for v in remaining_after_ab.values())
    if sum_after_ab <= 0:
        return 0.0
    pc_given_ab = (boats_by_no.get(c, {}).get('win_prob', 0)) / sum_after_ab

    return pa * pb_given_a * pc_given_ab


def _parse_meet_results_text(text: str) -> float:
    """
    meet_standings.results_text を解析して今節調子スコア(0〜1)を返す
    実際の形式: 全角数字（FF11〜FF16 = '１'〜'６'）で着順を表す
    例: '３　２　１' = 3着, 2着, 1着
    F/L: フライング/遅れペナルティ
    """
    if not text:
        return 0.5
    # 全角数字 '１'〜'６' = U+FF11〜U+FF16
    full_digit_map = {chr(0xFF10 + i): i for i in range(1, 7)}
    scores = []
    for ch in text:
        if ch in full_digit_map:
            rank = full_digit_map[ch]
            if rank == 1:
                scores.append(1.0)
            elif rank == 2:
                scores.append(0.65)
            elif rank == 3:
                scores.append(0.35)
            else:
                scores.append(0.0)
        elif ch in ('Ｆ', 'F'):
            scores.append(-0.5)
        elif ch in ('Ｌ', 'L'):
            scores.append(-0.3)
        elif ch in ('Ｋ', 'K', '欠'):
            scores.append(0.0)
        # U+3000 (ideographic space) → skip
    if not scores:
        return 0.33  # データなし → 中立値
    # 各レース得点の平均（0=6着以下, 0.35=3着, 0.65=2着, 1.0=1着）
    return max(0.0, min(1.0, sum(scores) / len(scores)))


def _get_meet_form(cur, venue_code: str, player_nos: list) -> dict[str, float]:
    """今節各選手の調子スコアを取得"""
    if not player_nos:
        return {}
    ph = ",".join("?" * len(player_nos))
    cur.execute(
        f"SELECT player_no, results_text FROM meet_standings "
        f"WHERE venue_code=? AND player_no IN ({ph}) ORDER BY date DESC",
        [venue_code] + player_nos
    )
    seen = {}
    for row in cur.fetchall():
        pno = row["player_no"]
        if pno not in seen:
            seen[pno] = _parse_meet_results_text(row["results_text"])
    return seen


def _get_st_variance(cur, player_nos: list) -> dict[str, float | None]:
    """
    st_history から選手ごとのST標準偏差を計算（小さいほど安定）
    before_info の exhibit_st も使用
    """
    if not player_nos:
        return {}
    ph = ",".join("?" * len(player_nos))
    cur.execute(
        f"SELECT player_no, start_timing FROM st_history "
        f"WHERE player_no IN ({ph}) AND start_timing IS NOT NULL "
        f"ORDER BY race_date DESC",
        player_nos
    )
    rows_by_player: dict[str, list[float]] = {}
    for row in cur.fetchall():
        pno = row["player_no"]
        try:
            st = float(row["start_timing"])
            if -1.0 <= st <= 1.0:  # 有効なST値
                rows_by_player.setdefault(pno, []).append(st)
        except (ValueError, TypeError):
            pass

    result = {}
    for pno, times in rows_by_player.items():
        if len(times) < 2:
            result[pno] = None
        else:
            mean = sum(times) / len(times)
            variance = sum((t - mean) ** 2 for t in times) / len(times)
            result[pno] = round(variance ** 0.5, 4)
    return result


def _get_tansho_odds(cur, race_id: int) -> dict[int, float]:
    """単勝オッズ: boat_no → odds"""
    cur.execute(
        "SELECT boat_no, odds FROM odds_tansho WHERE race_id=? AND odds IS NOT NULL AND odds > 0",
        (race_id,)
    )
    return {row["boat_no"]: row["odds"] for row in cur.fetchall()}


def _get_live_odds(cur, race_id: int) -> dict:
    cur.execute(
        "SELECT combination, odds FROM odds_3t WHERE race_id=? AND odds IS NOT NULL AND odds > 0",
        (race_id,)
    )
    return {row["combination"]: row["odds"] for row in cur.fetchall()}


def _get_historical_avg_odds(cur, venue_code: str, combination: str) -> Optional[float]:
    # キャッシュがある場合はクエリ不要（バックテスト高速化の要）
    hist_odds = _cache.get("hist_odds")
    if hist_odds is not None:
        return hist_odds.get((venue_code, combination))
    # キャッシュなし: 従来どおり都度クエリ
    cur.execute("""
        SELECT AVG(o.odds), COUNT(*)
        FROM odds_3t o
        JOIN races r ON r.id = o.race_id
        WHERE r.venue_code = ? AND o.combination = ?
          AND o.odds IS NOT NULL AND o.odds > 0
    """, (venue_code, combination))
    row = cur.fetchone()
    if row and row[1] and row[1] >= 5 and row[0]:
        return round(row[0], 1)
    return None


def _flow_adjustment_factor(a: int, b: int, c: int,
                             boats_by_no: dict, trick_data: dict) -> float:
    """
    展開予測による3連単確率補正係数
    競艇の典型的な決まり手（逃げ・差し・まくり・まくり差し）を考慮

    - コース1: 逃げ（nige_rate）
    - コース2: 差し（sashi_rate）or まくり（makuri_rate）
    - コース3: まくり + まくり差し（makuri_rate + makuri_sashi_rate）
    - コース4〜6: まくり（makuri_rate）が主体

    返値: 0.3〜2.0の補正係数（1.0=補正なし）
    """
    a_course = boats_by_no.get(a, {}).get('start_course', a)
    b_course = boats_by_no.get(b, {}).get('start_course', b)

    td_a = trick_data.get(a, {})

    # ---- A が1着を取る展開適性 ----
    if a_course == 1:
        nige = td_a.get('nige_rate') or 40.0               # NULL(新人等)は平均以下と想定
        a_factor = nige / 55.0                              # 全国平均55%を基準に正規化
    elif a_course == 2:
        sashi  = td_a.get('sashi_rate') or 0
        makuri = td_a.get('makuri_rate') or 0
        dominant = max(sashi, makuri)
        a_factor = dominant / 15.0 if dominant > 0 else 1.0
    elif a_course == 3:
        # 3コース: まくり・まくり差しの両方が有効
        makuri    = td_a.get('makuri_rate') or 0
        makuri_sa = td_a.get('makuri_sashi_rate') or 0
        total     = makuri + makuri_sa
        a_factor  = total / 12.0 if total > 0 else 1.0
    else:  # 4〜6コース
        makuri    = td_a.get('makuri_rate') or 0
        makuri_sa = td_a.get('makuri_sashi_rate') or 0
        total     = makuri + makuri_sa * 0.5          # 4コース以降まくり差しは少ない
        a_factor  = total / 8.0 if total > 0 else 0.85

    a_factor = max(0.4, min(2.0, a_factor))

    # ---- B が2着を取る展開適性（Aの展開パターンを踏まえて） ----
    if a_course == 1:
        # インが逃げた場合: 2コース差し → 典型的2着
        if b_course == 2:
            b_factor = 1.25
        elif b_course == 3:
            b_factor = 1.05   # まくり差しで2着もある
        elif b_course == 4:
            b_factor = 0.90
        else:
            b_factor = 0.75
    elif a_course in (2, 3):
        # 2〜3コースが1着: インが崩れる展開
        if b_course == 1:
            b_factor = 0.85   # 1コースが着拾い
        elif b_course < a_course:
            b_factor = 0.95
        elif b_course == a_course + 1:
            b_factor = 1.10   # 連動しやすい
        else:
            b_factor = 0.90
    else:
        # 4コース以降がまくった場合: インが大崩れ
        if b_course < a_course:
            b_factor = 0.80
        elif b_course == a_course + 1:
            b_factor = 1.10
        else:
            b_factor = 0.95

    return max(0.3, min(2.0, a_factor * b_factor))


def _get_maekuke_stats(cur, player_no: str, exhibit_course: int) -> Optional[float]:
    """
    前付け時（exhibit_course != boat_no）のそのコースでの過去勝率（%）を返す。
    サンプル3件未満の場合はNone。
    """
    try:
        cur.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN rre.rank=1 THEN 1 ELSE 0 END) wins
            FROM before_info bi
            JOIN races r ON r.id = bi.race_id
            JOIN entries e ON e.race_id = bi.race_id AND e.boat_no = bi.boat_no
            JOIN race_result_entries rre ON rre.race_id = bi.race_id AND rre.boat_no = bi.boat_no
            WHERE e.player_no = ?
              AND bi.exhibit_course = ?
              AND bi.exhibit_course != bi.boat_no
        """, (player_no, exhibit_course))
        row = cur.fetchone()
        if row and row["total"] >= 3:
            return (row["wins"] / row["total"]) * 100.0
        return None
    except Exception:
        return None


def _get_weather(cur, race_id: int) -> dict:
    """weather テーブルから気象データを取得。なければ空dict。"""
    row = cur.execute("""
        SELECT wind_speed, wind_direction, wave_height, water_temp, weather_desc
        FROM weather WHERE race_id = ?
    """, (race_id,)).fetchone()
    return dict(row) if row else {}


def _weather_adjustments(weather: dict) -> dict:
    """
    風速・波高に応じたスコア重み補正係数を返す。

    データ分析結果（2026-06 時点）:
      - 波高5cm以上: Top3的中率 15.8% (穏やか時 24.8% より約9pt低下) → 明確補正対象
      - 波高3-4cm:  若干低下だが誤差範囲内 → 軽微補正
      - 風速4m以上: 逆に精度が高い傾向あり → 過剰補正しない
      - 風速7m以上: データ不足のため保守的な補正のみ

    補正の考え方:
      - 高波 → 展示タイムの信頼性が低下するため et_weight を削減
      - 高波 → エンジン出力が効きやすくなるため motor_weight を増加
      - 高波 → 1コース有利の幅がやや縮小するため course1_reduction を適用
    """
    wind = weather.get("wind_speed") or 0.0
    wave = weather.get("wave_height") or 0.0

    # 波高による補正（主要シグナル）
    if wave >= 5.0:
        et_weight_factor    = 0.70  # 展示T 30%削減（高波で信頼性低下）
        motor_weight_factor = 1.30  # モーター 30%増（エンジン差が効く）
        course1_reduction   = 0.90  # 1コースボーナス 10%削減
    elif wave >= 3.0:
        et_weight_factor    = 0.90
        motor_weight_factor = 1.10
        course1_reduction   = 0.97
    else:
        et_weight_factor    = 1.0
        motor_weight_factor = 1.0
        course1_reduction   = 1.0

    # 風速による補正（保守的。4m以上は実測で精度が高いため触らない）
    # 7m以上は未知領域のため軽微なコース補正のみ
    if wind >= 7.0:
        course1_reduction *= 0.85

    return {
        "course1_reduction":   course1_reduction,
        "et_weight_factor":    et_weight_factor,
        "motor_weight_factor": motor_weight_factor,
        "wind_speed":          wind,
        "wave_height":         wave,
        "is_rough":            wave >= 5.0 or wind >= 7.0,
    }


def _get_venue_calibration(cur, venue_code: str) -> float:
    """
    会場別の予想補正係数を取得
    過去のバックテスト結果（predictions）から会場の特性を学習
    返値: 0.7〜1.3（1.0=補正なし、データ不足時は1.0）
    """
    # キャッシュがある場合はクエリ不要
    venue_calib = _cache.get("venue_calib")
    if venue_calib is not None:
        return venue_calib.get(venue_code, 1.0)
    # キャッシュなし: 従来どおり都度クエリ
    try:
        cur.execute("""
            SELECT COUNT(*) as total, SUM(hit_top3) as hits
            FROM predictions p
            JOIN races r ON r.id = p.race_id
            WHERE r.venue_code = ? AND p.hit_top3 IS NOT NULL
        """, (venue_code,))
        row = cur.fetchone()
        if row and row["total"] and row["total"] >= 20 and row["hits"] is not None:
            venue_rate = row["hits"] / row["total"]
            NATIONAL_AVG_RATE = 0.186   # バックテスト実績: 18.6%
            ratio = venue_rate / NATIONAL_AVG_RATE
            return max(0.7, min(1.3, ratio))
    except Exception:
        pass
    return 1.0


def _categorize(ev: Optional[float], prob: float, odds: Optional[float], is_honmei_top3: bool = False) -> str:
    """
    カテゴリ判定
    - 確率が高い組み合わせ（本命系）を優先的に ◎ / ○ で表示
    - EV ≥ 1.0 は正期待値として △/☆ で表示
    """
    if odds is None:
        return "─"
    # 本命判定（確率 or オッズで判断）
    if prob > 20.0 or (odds < 8.0 and prob > 5.0):
        return "◎ 本命"
    if prob > 8.0 or (odds < 20.0 and prob > 3.0):
        return "○ 準本命"
    if prob > 3.0 or odds < 40.0:
        return "▲ 対抗"
    # EV系
    if ev is not None:
        if ev >= 1.2 and odds > 100.0:
            return "☆ 大穴"
        if ev >= 1.0 and odds > 50.0:
            return "△ 中穴"
        if ev >= 1.0:
            return "△ 穴"
        if ev >= 0.75:
            return "✕ 参考"
    return "✕ 割高"


def predict(date: str, venue_code: str, race_no: int, conn=None) -> dict:
    _own_conn = conn is None
    if _own_conn:
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
    elif conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- レース取得 ----
    cur.execute(
        "SELECT id, race_title FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (date, venue_code, race_no),
    )
    race = cur.fetchone()
    if race is None:
        if _own_conn: conn.close()
        raise ValueError(f"Race not found: {date} venue={venue_code} race_no={race_no}")
    race_id = race["id"]

    # ---- entries ----
    cur.execute("""
        SELECT boat_no, player_no, player_name,
               avg_start_timing, national_win_rate, local_win_rate, motor_2ring_rate
        FROM entries WHERE race_id=? ORDER BY boat_no
    """, (race_id,))
    entries = {row["boat_no"]: dict(row) for row in cur.fetchall()}

    # ---- before_info ----
    cur.execute(
        "SELECT boat_no, exhibition_time, tilt, exhibit_course, exhibit_st "
        "FROM before_info WHERE race_id=? ORDER BY boat_no",
        (race_id,),
    )
    before = {row["boat_no"]: dict(row) for row in cur.fetchall()}

    # ---- course_stats ----
    player_nos = [e["player_no"] for e in entries.values()]
    ph = ",".join("?" * len(player_nos))
    cur.execute(
        f"SELECT player_no, course_no, win_rate_1st, win_rate_2nd, win_rate_3rd, avg_st "
        f"FROM course_stats "
        f"WHERE player_no IN ({ph}) ORDER BY fetched_date DESC",
        player_nos,
    )
    cs_data: dict = {}
    for row in cur.fetchall():
        pno, cno = row["player_no"], row["course_no"]
        if pno not in cs_data:
            cs_data[pno] = {}
        if cno not in cs_data[pno]:
            cs_data[pno][cno] = {
                "top1":   row["win_rate_1st"],
                "top3":   (row["win_rate_2nd"] or 0) + (row["win_rate_3rd"] or 0),
                "avg_st": row["avg_st"],
            }

    # ---- 会場別1コース1着率 ----
    _venue_c1_cache = _cache.get("venue_course1")
    if _venue_c1_cache is not None:
        venue_course1_rate = _venue_c1_cache.get(venue_code, COURSE1_NATIONAL_AVG)
    else:
        cur.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN rre.rank = 1 THEN 1 ELSE 0 END) as wins
            FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE r.venue_code = ? AND rre.start_course = 1
        """, (venue_code,))
        row = cur.fetchone()
        venue_course1_rate = (
            row["wins"] / row["total"] * 100
            if row and row["total"] and row["total"] >= 10
            else COURSE1_NATIONAL_AVG
        )

    # ---- 天候データ・補正係数 ----
    weather = _get_weather(cur, race_id)
    wadj    = _weather_adjustments(weather)
    w_et    = WEIGHTS["exhibition_time"] * wadj["et_weight_factor"]
    w_motor = WEIGHTS["motor_2ring"]     * wadj["motor_weight_factor"]

    # ---- ライブオッズ ----
    live_odds_map = _get_live_odds(cur, race_id)
    tansho_map    = _get_tansho_odds(cur, race_id)

    # ---- 派生指標取得 ----
    player_nos_list = [e["player_no"] for e in entries.values() if e["player_no"]]
    meet_form  = _get_meet_form(cur, venue_code, player_nos_list)
    st_var_map = _get_st_variance(cur, player_nos_list)

    # ---- entries: 決まり手率・branch 取得 ----
    cur.execute("""
        SELECT boat_no, branch, nige_rate, sashi_rate, makuri_rate,
               makuri_sashi_rate, teiko_rate, megumi_rate
        FROM entries WHERE race_id=?
    """, (race_id,))
    trick_data = {row["boat_no"]: dict(row) for row in cur.fetchall()}

    # ---- 今節成績（日数加重）事前取得 ----
    meet_day_map: dict = {}
    for entry in entries.values():
        pno = entry["player_no"]
        if pno and pno not in meet_day_map:
            meet_day_map[pno] = _get_meet_day_form(conn, pno, venue_code, date)

    # ---- 生データ収集 ----
    boats = []
    for boat_no, entry in entries.items():
        b = before.get(boat_no, {})
        player_no = entry["player_no"]
        start_course = b.get("exhibit_course") or boat_no

        # course_stats: top1 / top3 / avg_st
        cs_entry = cs_data.get(player_no, {}).get(start_course)
        if cs_entry and isinstance(cs_entry, dict):
            course_top1 = cs_entry["top1"] or COURSE_DEFAULT_WIN_RATE.get(start_course, 8.0)
            course_top3 = cs_entry["top3"] if cs_entry["top3"] else COURSE_DEFAULT_TOP3_RATE.get(start_course, 20.0)
            course_avg_st = cs_entry["avg_st"] if cs_entry["avg_st"] else COURSE_DEFAULT_AVG_ST.get(start_course, 0.20)
        else:
            course_top1   = COURSE_DEFAULT_WIN_RATE.get(start_course, 8.0)
            course_top3   = COURSE_DEFAULT_TOP3_RATE.get(start_course, 20.0)
            course_avg_st = COURSE_DEFAULT_AVG_ST.get(start_course, 0.20)

        local_wr = entry["local_win_rate"] or entry["national_win_rate"]
        exhibition_time = b.get("exhibition_time")
        tilt = b.get("tilt")
        exhibit_st = _parse_st(b.get("exhibit_st"))
        effective_st = exhibit_st if exhibit_st is not None else entry["avg_start_timing"]

        # 決まり手適性スコア（展開コースと得意な決まり手のマッチング）
        td = trick_data.get(boat_no, {})
        if start_course == 1:
            trick_aptitude = td.get("nige_rate") or 0.0       # 1コース: 逃げ率
        elif start_course == 2:
            nr = (td.get("sashi_rate") or 0.0)
            mr = (td.get("makuri_rate") or 0.0)
            trick_aptitude = (nr + mr) / 2
        else:
            trick_aptitude = td.get("makuri_rate") or 0.0     # 3コース以降: まくり率

        # 前付け補正
        # exhibit_course != boat_no のとき、そのコースでの過去前付け勝率でスコアを補正
        MAEKUKE_MAX = 0.04  # 補正上限（±4%）
        b_exhibit_course = b.get("exhibit_course")
        maekuke_correction = 0.0
        maekuke_wr = None
        if b_exhibit_course and int(b_exhibit_course) != boat_no:
            maekuke_wr = _get_maekuke_stats(cur, player_no, int(b_exhibit_course))
            baseline_wr = COURSE_DEFAULT_WIN_RATE.get(int(b_exhibit_course), 8.0)
            if maekuke_wr is not None:
                ratio = maekuke_wr / baseline_wr
                maekuke_correction = MAEKUKE_MAX * (ratio - 1.0)
                maekuke_correction = max(-MAEKUKE_MAX, min(MAEKUKE_MAX, maekuke_correction))
            else:
                maekuke_correction = -0.015  # データなし → 不確実性による軽微ペナルティ

        # 今節調子スコア
        form_score = meet_form.get(player_no, 0.5)

        # 今節成績（日数加重）スコア
        meet_day_score = meet_day_map.get(player_no)  # None = データなし

        # STばらつき（小さいほど安定 → 高スコア）
        st_sd = st_var_map.get(player_no)
        st_consistency = max(0.0, 1.0 - (st_sd * 10)) if st_sd is not None else 0.5

        # 単勝オッズ整合性（市場の評価との乖離）
        tansho_odds = tansho_map.get(boat_no)
        if tansho_odds and tansho_odds > 0:
            market_win_prob = 0.75 / tansho_odds  # 単勝控除率75%
        else:
            market_win_prob = None

        boats.append({
            "boat_no":     boat_no,
            "player_no":   player_no,
            "player_name": entry["player_name"].replace("　", " ").strip(),
            "start_course": start_course,
            "branch":       td.get("branch"),
            "tansho_odds":  tansho_odds,
            "market_win_prob": market_win_prob,
            "_raw": {
                "course_top1_rate":   course_top1,
                "course_top3_rate":   course_top3,
                "course_avg_st":      course_avg_st,
                "national_win_rate":  entry["national_win_rate"],
                "local_win_rate":     local_wr,
                "motor_2ring":        entry["motor_2ring_rate"],
                "exhibition_time":    exhibition_time,
                "tilt":               tilt,
                "start_timing":       effective_st,
                "form_score":         form_score,
                "meet_day_score":     meet_day_score,
                "trick_aptitude":     trick_aptitude,
                "st_consistency":     st_consistency,
                "maekuke_correction": maekuke_correction,  # 各艇ごとに保存（バグ修正）
                "maekuke_wr":         maekuke_wr,
            },
        })

    # ---- 正規化 ----
    def col(key): return [b["_raw"][key] for b in boats]

    norm_course_top1  = _normalize(col("course_top1_rate"))
    norm_course_top3  = _normalize(col("course_top3_rate"))
    # avg_st: 小さいほど良い（フライング気味=小さいが速い）→反転
    norm_course_avgst = [1.0 - v for v in _normalize(col("course_avg_st"))]
    norm_national     = _normalize(col("national_win_rate"))
    norm_local        = _normalize(col("local_win_rate"))
    norm_motor        = _normalize(col("motor_2ring"))
    norm_et           = [1.0 - v for v in _normalize(col("exhibition_time"))]
    norm_st           = [1.0 - v for v in _normalize(col("start_timing"))]
    norm_form         = _normalize(col("form_score"))
    norm_meet_day     = _normalize(col("meet_day_score"))   # None は平均値で補完
    norm_trick        = _normalize(col("trick_aptitude"))
    norm_st_consist   = _normalize(col("st_consistency"))

    # ---- スコア計算 ----
    FORM_WEIGHT      = 0.08   # 今節調子          (v1: 0.06)
    TRICK_WEIGHT     = 0.06   # 決まり手適性       (v1: 0.04)
    ST_CON_WEIGHT    = 0.03   # STばらつき安定性   (v1: 0.03)
    MEET_DAY_WEIGHT  = 0.05   # 今節成績（日数加重）(v1: なし)

    course1_bonus = (
        COURSE1_BASE_BONUS
        * max(1.0, venue_course1_rate / COURSE1_NATIONAL_AVG)
        * wadj["course1_reduction"]  # 天候補正
    )
    w = WEIGHTS
    for i, boat in enumerate(boats):
        raw = boat["_raw"]
        base = (
            norm_course_top1[i]  * w["course_top1_rate"]
            + norm_course_top3[i]  * w["course_top3_rate"]
            + norm_national[i]     * w["national_win_rate"]
            + norm_local[i]        * w["local_win_rate"]
            + norm_motor[i]        * w_motor           # 天候補正済み
            + norm_et[i]           * w_et              # 天候補正済み
            + norm_st[i]           * w["start_timing"]
            + norm_course_avgst[i] * w["course_avg_st"]
            + norm_form[i]         * FORM_WEIGHT
            + norm_trick[i]        * TRICK_WEIGHT
            + norm_meet_day[i]     * MEET_DAY_WEIGHT
            + norm_st_consist[i]   * ST_CON_WEIGHT
        )
        bonus = course1_bonus if boat["start_course"] == 1 else 0.0

        # 単勝オッズ整合性ボーナス: 市場が過小評価している場合に上乗せ
        model_raw_prob = base / (1 + COURSE1_BASE_BONUS)  # 正規化前推定
        market_prob = boat.get("market_win_prob")
        odds_bonus = 0.0
        if market_prob and market_prob > 0 and model_raw_prob > 0:
            ratio = model_raw_prob / market_prob
            if ratio > 1.2:  # モデルが市場より20%以上高く評価 → プラスボーナス
                odds_bonus = min(0.05, (ratio - 1.0) * 0.05)
            elif ratio < 0.7:  # 市場が大幅に上回る → 軽微なペナルティ
                odds_bonus = max(-0.03, (ratio - 1.0) * 0.03)

        mk_corr = raw["maekuke_correction"]
        mk_wr   = raw["maekuke_wr"]
        boat["score_raw"] = base + bonus + odds_bonus + mk_corr
        boat["components"] = {
            "course_top1_rate":  round(raw["course_top1_rate"], 2),
            "course_top3_rate":  round(raw["course_top3_rate"], 2),
            "course_avg_st":     round(raw["course_avg_st"], 3) if raw["course_avg_st"] is not None else None,
            "national_win_rate": round(raw["national_win_rate"] or 0, 2),
            "local_win_rate":    round(raw["local_win_rate"] or 0, 2),
            "motor_2ring":       round(raw["motor_2ring"] or 0, 2),
            "exhibition_time":   raw["exhibition_time"],
            "tilt":              raw["tilt"],
            "start_timing":      round(raw["start_timing"], 3) if raw["start_timing"] is not None else None,
            "course1_bonus":     round(bonus, 4),
            "form_score":        round(raw["form_score"], 3),
            "meet_day_score":    round(raw["meet_day_score"], 3) if raw["meet_day_score"] is not None else None,
            "trick_aptitude":    round(raw["trick_aptitude"], 1),
            "st_consistency":    round(raw["st_consistency"], 3),
            "odds_bonus":        round(odds_bonus, 4),
            "maekuke_correction": round(mk_corr, 4) if mk_corr != 0.0 else None,
            "maekuke_wr":        round(mk_wr, 1) if mk_wr is not None else None,
        }
        # 天候補正が実際に適用された場合のみ記録
        if wadj["is_rough"] or wadj["et_weight_factor"] != 1.0 or wadj["motor_weight_factor"] != 1.0:
            boat["weather_adj"] = {
                "wind_speed":          wadj["wind_speed"],
                "wave_height":         wadj["wave_height"],
                "et_weight_factor":    round(wadj["et_weight_factor"], 2),
                "motor_weight_factor": round(wadj["motor_weight_factor"], 2),
                "course1_reduction":   round(wadj["course1_reduction"], 2),
            }

    max_s = max(b["score_raw"] for b in boats)
    min_s = min(b["score_raw"] for b in boats)
    span  = max_s - min_s
    for boat in boats:
        if span > 0:
            # 10〜100ptにマッピング（最下位でも10pt以上）
            boat["score"] = round(10.0 + (boat["score_raw"] - min_s) / span * 90.0, 1)
        else:
            boat["score"] = 50.0

    # ---- 勝率予測（softmax）----
    # temperature=15: 1コースが圧倒的に有利な差をより反映させるため少し低め
    exps  = [math.exp(b["score"] / 15) for b in boats]
    total = sum(exps)
    for i, boat in enumerate(boats):
        boat["win_prob"] = round(exps[i] / total * 100, 1)

    # ---- 単勝オッズとのブレンド（50% モデル + 50% 市場）----
    for boat in boats:
        market_prob = boat.get("market_win_prob")
        if market_prob and market_prob > 0:
            blended = (boat["win_prob"] / 100 * 0.5) + (market_prob * 0.5)
            boat["win_prob_blended"] = round(blended * 100, 1)
        else:
            boat["win_prob_blended"] = boat["win_prob"]

    boats.sort(key=lambda x: x["score"], reverse=True)
    # コンボ計算用: ブレンド後の確率をwin_probとして使用
    boats_by_no = {b["boat_no"]: {**b, "win_prob": b["win_prob_blended"]} for b in boats}

    # ---- 会場補正係数取得 ----
    venue_calib = _get_venue_calibration(cur, venue_code)

    # ---- 全艇の全3連単（120通り）を生成してスコアリング ----
    all_nos = [b["boat_no"] for b in boats]

    # Step 1: 展開補正を含む生確率を計算
    raw_prob_map: dict[str, float] = {}
    for p in itertools.permutations(all_nos, 3):
        combo = f"{p[0]}-{p[1]}-{p[2]}"
        base_prob   = _combo_prob(boats_by_no, combo)
        flow_factor = _flow_adjustment_factor(p[0], p[1], p[2], boats_by_no, trick_data)
        raw_prob_map[combo] = base_prob * flow_factor * venue_calib

    # Step 2: 正規化（展開補正後の合計が1になるよう）
    total_raw = sum(raw_prob_map.values())
    if total_raw <= 0:
        total_raw = 1.0

    # ---- 3連対率: 全120コンボから各艇が3着以内に入る確率を集計 ----
    top3_prob_map = {no: 0.0 for no in all_nos}
    for combo_str, raw_p in raw_prob_map.items():
        norm_p = raw_p / total_raw
        for bn in map(int, combo_str.split("-")):
            top3_prob_map[bn] += norm_p
    for boat in boats:
        boat["top3_prob"] = round(top3_prob_map[boat["boat_no"]] * 100, 1)

    all_candidates = []
    for p in itertools.permutations(all_nos, 3):
        combo  = f"{p[0]}-{p[1]}-{p[2]}"
        prob   = raw_prob_map[combo] / total_raw     # 正規化済み確率
        live_o = live_odds_map.get(combo)
        hist_o = _get_historical_avg_odds(cur, venue_code, combo)

        if live_o:
            exp_o = live_o
        elif hist_o:
            exp_o = hist_o
        elif prob > 0:
            exp_o = round(TANSHO_RETURN_RATE / prob, 1)
        else:
            exp_o = None

        ev = round(prob * exp_o, 3) if (exp_o and prob > 0) else 0.0
        category = _categorize(ev if ev else None, prob * 100, exp_o)

        all_candidates.append({
            "combo":         combo,
            "prob":          round(prob * 100, 2),
            "live_odds":     live_o,
            "hist_odds":     hist_o,
            "expected_odds": exp_o,
            "ev":            ev,
            "category":      category,
        })

    # ---- 3パターン分類 ----
    # ライブ3連単オッズがある場合: 絶対値閾値（≤25 / 25-80 / >80）で分類
    # ない場合: 確率順位で分類（常に各5通りを保証）
    by_prob = sorted(all_candidates, key=lambda x: x["prob"], reverse=True)

    has_live_3t = bool(live_odds_map)   # ライブ3連単オッズが存在するか

    # expected_odds を使って分類（live → hist → model-implied の優先順で自動選択済み）
    def _eff_odds(c: dict) -> float:
        return c["expected_odds"] or float("inf")

    if has_live_3t:
        # ライブオッズあり: live_odds を最優先
        def _eff_odds(c: dict) -> float:           # noqa: F811
            return c["live_odds"] or c["expected_odds"] or float("inf")

    # 本命専用スコア: モデル確率30% + 市場確率70%
    # 低オッズ（市場が高確率と見ている）ほど上位に来るよう調整
    def _honmei_blend_score(c: dict) -> float:
        model_p = c["prob"]   # 0〜100
        odds = _eff_odds(c)
        if odds and odds < float("inf") and odds > 0:
            market_p = TANSHO_RETURN_RATE * 100 / odds   # 控除率換算の市場確率
        else:
            market_p = model_p   # オッズ情報なし → モデルのみ
        return model_p * 0.3 + market_p * 0.7

    # expected_odds（hist or model-implied）による閾値分類
    honmei_pool = [c for c in by_prob if _eff_odds(c) <= 25]
    honmei_top  = sorted(honmei_pool, key=_honmei_blend_score, reverse=True)[:10]
    chuana_top  = [c for c in by_prob if 25 < _eff_odds(c) <= 80][:15]
    ana_pool    = sorted(
        [c for c in by_prob if _eff_odds(c) > 80],
        key=lambda x: x["ev"] or 0, reverse=True
    )
    ana_top = ana_pool[:15]

    # フォールバック: expected_odds が全くない場合は確率順位で補完
    if not honmei_top:
        honmei_top = by_prob[:10]
    if not chuana_top:
        chuana_top = by_prob[10:25]
    if not ana_top:
        ana_top = sorted(by_prob[25:60], key=lambda x: x["ev"] or 0, reverse=True)[:15]

    honmei_detail = [dict(rank=i + 1, **c) for i, c in enumerate(honmei_top)]
    chuana_detail = [dict(rank=i + 1, **c) for i, c in enumerate(chuana_top)]
    ana_detail    = [dict(rank=i + 1, **c) for i, c in enumerate(ana_top)]

    # recommended_3t_detail = 3カテゴリの合計から確率上位
    _cat_map: dict[str, dict] = {}
    for c in honmei_top + chuana_top + ana_top:
        if c["combo"] not in _cat_map:
            _cat_map[c["combo"]] = c
    all_by_prob = sorted(_cat_map.values(), key=lambda x: x["prob"], reverse=True)[:10]
    ev_sorted   = sorted(all_candidates, key=lambda x: x["ev"] or 0, reverse=True)
    ev_detail   = [dict(rank=i + 1, **c) for i, c in enumerate(ev_sorted[:10])]

    recommended = [d["combo"] for d in all_by_prob[:5]]

    if _own_conn: conn.close()

    result_boats = [
        {
            "boat_no":           b["boat_no"],
            "player_name":       b["player_name"],
            "player_no":         b["player_no"],
            "start_course":      b["start_course"],
            "branch":            b.get("branch"),
            "tansho_odds":       b.get("tansho_odds"),
            "market_win_prob":   b.get("market_win_prob"),
            "score":             b["score"],
            "win_prob":          b["win_prob"],
            "win_prob_blended":  b.get("win_prob_blended", b["win_prob"]),
            "top3_prob":         b.get("top3_prob", 0.0),
            "components":        b["components"],
        }
        for b in boats
    ]

    return {
        "race_info":             {"date": date, "venue_code": venue_code,
                                  "race_no": race_no, "race_title": race["race_title"]},
        "boats":                 result_boats,
        "venue_calib":           round(venue_calib, 3),
        "weather":               {
            "wind_speed":        wadj["wind_speed"] or None,
            "wave_height":       wadj["wave_height"] or None,
            "is_rough":          wadj["is_rough"],
            "et_weight_factor":  round(wadj["et_weight_factor"], 2),
            "motor_weight_factor": round(wadj["motor_weight_factor"], 2),
            "course1_reduction": round(wadj["course1_reduction"], 2),
        },
        # 3パターン × 5通り（メイン出力）
        "honmei_detail":         honmei_detail,    # オッズ ≤ 25倍
        "chuana_detail":         chuana_detail,    # オッズ 25〜80倍
        "ana_detail":            ana_detail,       # オッズ > 80倍
        # 後方互換
        "recommended_3t":        recommended,
        "recommended_3t_detail": [dict(rank=i+1, **c) for i, c in enumerate(all_by_prob)],
        "ev_recs_detail":        ev_detail,
    }


def _print_pattern(label: str, detail: list) -> None:
    print(f"\n■ {label}:")
    if not detail:
        print("  （該当なし）")
        return
    print(f"  {'順':<4} {'買い目':<12} {'確率%':<8} {'オッズ':<12} {'EV':<8} カテゴリ")
    print(f"  {'-'*60}")
    for d in detail:
        o_str  = f"{d['expected_odds']:.1f}倍" if d["expected_odds"] else "  -  "
        ev_str = f"{d['ev']:.3f}"              if d["ev"]            else "  -  "
        print(f"  {d['rank']:<4} {d['combo']:<12} {d['prob']:<8.2f} {o_str:<12} {ev_str:<8} {d['category']}")


def print_prediction(result: dict) -> None:
    ri = result["race_info"]
    print(f"\n{'='*80}")
    print(f"  {ri['date']}  {ri['venue_code']}会場  {ri['race_no']}R  {ri['race_title'] or ''}")
    calib = result.get("venue_calib", 1.0)
    calib_str = f"  会場補正: {calib:.3f}" if calib != 1.0 else ""
    print(f"{'='*80}{calib_str}")
    print(f"{'艇':<3} {'選手名':<12} {'CS':<3} {'スコア':<7} {'勝率%':<8} {'勝率(混)':>8} "
          f"{'コースWR':<9} {'全国WR':<8} {'当地WR':<8} "
          f"{'モーター':<9} {'展示T':<7} {'チルト':<7} {'ST'}")
    print('-' * 103)
    for b in result["boats"]:
        c = b["components"]
        et    = f"{c['exhibition_time']:.2f}" if c["exhibition_time"] is not None else "  -  "
        tilt  = f"{c['tilt']:.1f}"            if c.get("tilt") is not None         else "  -  "
        st    = f"{c['start_timing']:.3f}"    if c["start_timing"] is not None      else "  -  "
        blend = f"{b.get('win_prob_blended', b['win_prob']):.1f}"
        print(f"{b['boat_no']:<3} {b['player_name']:<12} {b['start_course']:<3} "
              f"{b['score']:<7.1f} {b['win_prob']:<8.1f} {blend:>8} "
              f"{c['course_top1_rate']:<9.1f} {c['national_win_rate']:<8.2f} "
              f"{c['local_win_rate']:<8.2f} {c['motor_2ring']:<9.2f} "
              f"{et:<7} {tilt:<7} {st}")

    _print_pattern("本命（≤25倍）Top5", result.get("honmei_detail", []))
    _print_pattern("中穴（25〜80倍）Top5", result.get("chuana_detail", []))
    _print_pattern("穴（>80倍）Top5", result.get("ana_detail", []))
    print()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4:
        d, v, r = sys.argv[1], sys.argv[2], int(sys.argv[3])
    else:
        d, v, r = "20260625", "14", 1
    result = predict(d, v, r)
    print_prediction(result)
