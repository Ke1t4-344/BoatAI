#!/usr/bin/env python3
"""
optimize_weights.py — predict.py の重みを数値最適化

バックテスト済みレースから特徴量を抽出し、scipy.optimize で
Top5的中率を最大化する重みを探索する。

実行:
    python3 optimize_weights.py              # 全件で最適化
    python3 optimize_weights.py --limit 3000 # 件数を絞って高速実行
    python3 optimize_weights.py --apply      # 結果を predict.py に自動反映
"""

import sqlite3
import itertools
import math
import json
import argparse
import re
from pathlib import Path

DB_PATH = Path(__file__).parent / "boatai.db"

# ── 現在の predict.py と同じデフォルト値 ─────────────────────────────────
DEFAULT_PARAMS = {
    "w_course":      0.5059,
    "w_national":    0.2510,
    "w_local":       0.0515,
    "w_motor":       0.0558,
    "w_et":          0.1358,
    "w_st":          0.0001,
    "w_form":        0.06,
    "w_trick":       0.04,
    "w_st_con":      0.03,
    "course1_bonus": 0.15,
    "temperature":   15.0,
}

COURSE_DEFAULT_WIN_RATE = {1: 55.0, 2: 15.0, 3: 9.5, 4: 7.5, 5: 6.5, 6: 5.5}
COURSE1_NATIONAL_AVG = 55.0


def _normalize(values):
    valid = [v for v in values if v is not None]
    if not valid:
        return [0.5] * len(values)
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return [0.5] * len(values)
    mean_v = sum(valid) / len(valid)
    return [((v if v is not None else mean_v) - mn) / (mx - mn) for v in values]


def score_race(feat_matrix, actual_combo, params, vc1_rate=55.0):
    """
    特徴量行列から Top5 に actual_combo が含まれるか判定。
    戻り値: 1=的中, 0=ハズレ, None=データ不足
    """
    if not feat_matrix or not actual_combo:
        return None

    p = params
    boats = list(feat_matrix)

    def col(k): return [b[k] for b in boats]

    norm_c  = _normalize(col("f_course"))
    norm_n  = _normalize(col("f_national"))
    norm_l  = _normalize(col("f_local"))
    norm_m  = _normalize(col("f_motor"))
    norm_et = [1.0 - v for v in _normalize(col("f_et"))]
    norm_st = [1.0 - v for v in _normalize(col("f_st"))]
    norm_fo = _normalize(col("f_form"))
    norm_tr = _normalize(col("f_trick"))
    norm_sc = _normalize(col("f_st_con"))

    c1_bonus = p["course1_bonus"] * max(1.0, vc1_rate / COURSE1_NATIONAL_AVG)
    temp = max(float(p["temperature"]), 1.0)

    for i, boat in enumerate(boats):
        base = (
            norm_c[i]  * p["w_course"]
            + norm_n[i]  * p["w_national"]
            + norm_l[i]  * p["w_local"]
            + norm_m[i]  * p["w_motor"]
            + norm_et[i] * p["w_et"]
            + norm_st[i] * p["w_st"]
            + norm_fo[i] * p["w_form"]
            + norm_tr[i] * p["w_trick"]
            + norm_sc[i] * p["w_st_con"]
        )
        boat["score_raw"] = base + (c1_bonus if boat["start_course"] == 1 else 0.0)

    mx = max(b["score_raw"] for b in boats)
    mn = min(b["score_raw"] for b in boats)
    for b in boats:
        b["sc"] = (b["score_raw"] - mn) / (mx - mn) * 100 if mx != mn else 50.0

    exps = [math.exp(b["sc"] / temp) for b in boats]
    total = sum(exps)
    boats_by_no = {b["boat_no"]: exps[i] / total * 100 for i, b in enumerate(boats)}

    all_nos = [b["boat_no"] for b in boats]
    probs = {}
    for perm in itertools.permutations(all_nos, 3):
        a, b_no, c = perm
        pa = boats_by_no.get(a, 0) / 100.0
        rem_a = {k: v for k, v in boats_by_no.items() if k != a}
        s_a = sum(rem_a.values())
        if s_a <= 0: continue
        pb = boats_by_no.get(b_no, 0) / s_a
        rem_ab = {k: v for k, v in rem_a.items() if k != b_no}
        s_ab = sum(rem_ab.values())
        if s_ab <= 0: continue
        pc = boats_by_no.get(c, 0) / s_ab
        probs[f"{a}-{b_no}-{c}"] = pa * pb * pc

    top5 = sorted(probs, key=probs.get, reverse=True)[:5]
    return 1 if actual_combo in top5 else 0


def load_dataset(limit=0):
    """バックテスト済みレースの特徴量を抽出してキャッシュ"""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row

    limit_sql = f"LIMIT {limit}" if limit > 0 else ""
    races = conn.execute(f"""
        SELECT p.race_id, p.actual_combo, ra.venue_code, ra.date
        FROM predictions p
        JOIN races ra ON ra.id = p.race_id
        WHERE p.actual_combo IS NOT NULL AND p.hit_top5 IS NOT NULL
        ORDER BY ra.date DESC
        {limit_sql}
    """).fetchall()

    # 会場別1コース1着率
    vc1_map = {r["venue_code"]: r["rate"] for r in conn.execute("""
        SELECT r.venue_code,
               SUM(CASE WHEN rre.rank=1 THEN 1 ELSE 0 END)*100.0/COUNT(*) as rate
        FROM race_result_entries rre JOIN races r ON r.id=rre.race_id
        WHERE rre.start_course=1
        GROUP BY r.venue_code
    """).fetchall()}

    dataset = []
    for race in races:
        race_id = race["race_id"]
        vc = race["venue_code"]

        entries = conn.execute("""
            SELECT e.boat_no, e.player_no, e.national_win_rate, e.local_win_rate,
                   e.motor_2ring_rate, e.avg_start_timing,
                   e.nige_rate, e.sashi_rate, e.makuri_rate
            FROM entries e WHERE e.race_id=?
        """, (race_id,)).fetchall()
        if not entries:
            continue

        before_map = {r["boat_no"]: r for r in conn.execute(
            "SELECT boat_no, exhibition_time, exhibit_course, exhibit_st FROM before_info WHERE race_id=?",
            (race_id,)
        ).fetchall()}

        cs_map = {}
        for r in conn.execute("""
            SELECT e.boat_no, cs.course_no, cs.win_rate_1st
            FROM entries e JOIN course_stats cs ON cs.player_no=e.player_no
            WHERE e.race_id=? ORDER BY cs.fetched_date DESC
        """, (race_id,)).fetchall():
            if r["boat_no"] not in cs_map:
                cs_map[r["boat_no"]] = {}
            if r["course_no"] not in cs_map[r["boat_no"]]:
                cs_map[r["boat_no"]][r["course_no"]] = r["win_rate_1st"]

        form_map = {}
        full_digit_map = {chr(0xFF10+i): i for i in range(1, 7)}
        for r in conn.execute("""
            SELECT e.boat_no, ms.results_text
            FROM entries e JOIN meet_standings ms ON ms.player_no=e.player_no AND ms.venue_code=?
            WHERE e.race_id=? ORDER BY ms.date DESC
        """, (vc, race_id)).fetchall():
            if r["boat_no"] not in form_map:
                scores = []
                for ch in (r["results_text"] or ""):
                    if ch in full_digit_map:
                        rank = full_digit_map[ch]
                        scores.append(1.0 if rank==1 else 0.65 if rank==2 else 0.35 if rank==3 else 0.0)
                    elif ch in ('Ｆ','F'): scores.append(-0.5)
                    elif ch in ('Ｌ','L'): scores.append(-0.3)
                form_map[r["boat_no"]] = max(0.0, min(1.0, sum(scores)/len(scores))) if scores else 0.5

        feat_matrix = []
        for e in entries:
            bn = e["boat_no"]
            b = before_map.get(bn)
            start_course = (b["exhibit_course"] if b and b["exhibit_course"] else bn)
            et = b["exhibition_time"] if b else None
            try:
                exhibit_st = float(b["exhibit_st"]) if b and b["exhibit_st"] else None
            except (TypeError, ValueError):
                exhibit_st = None
            effective_st = exhibit_st if exhibit_st is not None else e["avg_start_timing"]
            course_wr = cs_map.get(bn, {}).get(start_course) or COURSE_DEFAULT_WIN_RATE.get(start_course, 8.0)
            local_wr = e["local_win_rate"] or e["national_win_rate"]
            if start_course == 1:
                trick = e["nige_rate"] or 0.0
            elif start_course == 2:
                trick = ((e["sashi_rate"] or 0) + (e["makuri_rate"] or 0)) / 2
            else:
                trick = e["makuri_rate"] or 0.0

            feat_matrix.append({
                "boat_no": bn, "start_course": start_course,
                "f_course": course_wr, "f_national": e["national_win_rate"],
                "f_local": local_wr, "f_motor": e["motor_2ring_rate"],
                "f_et": et, "f_st": effective_st,
                "f_form": form_map.get(bn, 0.5),
                "f_trick": trick, "f_st_con": 0.5,
            })

        dataset.append({
            "race_id": race_id, "actual": race["actual_combo"],
            "date": race["date"], "feat_matrix": feat_matrix,
            "vc1_rate": vc1_map.get(vc, COURSE1_NATIONAL_AVG),
        })

    conn.close()
    print(f"データセット: {len(dataset)}件 読み込み完了")
    return dataset


def evaluate(params, dataset):
    hits = sum(
        1 for d in dataset
        if score_race(d["feat_matrix"], d["actual"], params, d["vc1_rate"]) == 1
    )
    return hits / len(dataset) if dataset else 0.0


def optimize(dataset, train_frac=0.8):
    from scipy.optimize import differential_evolution

    sorted_data = sorted(dataset, key=lambda d: d["date"])
    split = int(len(sorted_data) * train_frac)
    train = sorted_data[:split]
    test  = sorted_data[split:]
    print(f"訓練: {len(train)}件  検証: {len(test)}件")

    base_train = evaluate(DEFAULT_PARAMS, train)
    base_test  = evaluate(DEFAULT_PARAMS, test)
    print(f"ベースライン — 訓練Top5: {base_train*100:.1f}%  検証Top5: {base_test*100:.1f}%\n")

    keys = list(DEFAULT_PARAMS.keys())
    bounds = [
        (0.1,  0.8),   # w_course
        (0.05, 0.5),   # w_national
        (0.0,  0.2),   # w_local
        (0.0,  0.2),   # w_motor
        (0.0,  0.4),   # w_et
        (0.0,  0.05),  # w_st
        (0.0,  0.15),  # w_form
        (0.0,  0.15),  # w_trick
        (0.0,  0.15),  # w_st_con
        (0.05, 0.4),   # course1_bonus
        (5.0,  30.0),  # temperature
    ]

    call_count = [0]
    best = [0.0]

    def objective(x):
        call_count[0] += 1
        p = {k: v for k, v in zip(keys, x)}
        rate = evaluate(p, train)
        if rate > best[0]:
            best[0] = rate
            print(f"  [{call_count[0]:4d}回] 訓練Top5: {rate*100:.2f}%  ★ 更新")
        elif call_count[0] % 200 == 0:
            print(f"  [{call_count[0]:4d}回] 訓練Top5: {rate*100:.2f}%")
        return -rate

    print("最適化開始（数分〜十数分かかります）...")
    result = differential_evolution(
        objective, bounds,
        maxiter=500, popsize=15, tol=1e-5,
        seed=42, workers=1, polish=True,
    )

    opt_params = {k: v for k, v in zip(keys, result.x)}
    train_rate = evaluate(opt_params, train)
    test_rate  = evaluate(opt_params, test)

    print(f"\n{'='*55}")
    print(f"{'':20} {'訓練':>10} {'検証':>10}")
    print(f"{'ベースライン':20} {base_train*100:>9.1f}% {base_test*100:>9.1f}%")
    print(f"{'最適化後':20} {train_rate*100:>9.1f}% {test_rate*100:>9.1f}%")
    print(f"{'改善':20} {(train_rate-base_train)*100:>+9.1f}pt {(test_rate-base_test)*100:>+9.1f}pt")
    print(f"{'='*55}")

    print(f"\n{'パラメータ':<20} {'変更前':>10} {'変更後':>10} {'差分':>10}")
    print("-"*52)
    for k, v in opt_params.items():
        old = DEFAULT_PARAMS[k]
        print(f"{k:<20} {old:>10.4f} {v:>10.4f} {v-old:>+10.4f}")

    return opt_params, train_rate, test_rate


def apply_to_predict(opt_params):
    predict_path = Path(__file__).parent / "predict.py"
    src = predict_path.read_text()

    # WEIGHTS ブロック置換
    new_w = (
        f'WEIGHTS = {{\n'
        f'    "course_win_rate":     {opt_params["w_course"]:.4f},\n'
        f'    "national_win_rate":   {opt_params["w_national"]:.4f},\n'
        f'    "local_win_rate":      {opt_params["w_local"]:.4f},\n'
        f'    "motor_2ring":         {opt_params["w_motor"]:.4f},\n'
        f'    "exhibition_time":     {opt_params["w_et"]:.4f},\n'
        f'    "start_timing":        {opt_params["w_st"]:.4f},\n'
        f'}}'
    )
    src = re.sub(r'WEIGHTS\s*=\s*\{[^}]+\}', new_w, src)
    src = re.sub(r'COURSE1_BASE_BONUS\s+=\s+[\d.]+',
                 f'COURSE1_BASE_BONUS      = {opt_params["course1_bonus"]:.4f}', src)
    src = re.sub(r'FORM_WEIGHT\s+=\s+[\d.]+',
                 f'FORM_WEIGHT   = {opt_params["w_form"]:.4f}', src)
    src = re.sub(r'TRICK_WEIGHT\s+=\s+[\d.]+',
                 f'TRICK_WEIGHT  = {opt_params["w_trick"]:.4f}', src)
    src = re.sub(r'ST_CON_WEIGHT\s+=\s+[\d.]+',
                 f'ST_CON_WEIGHT = {opt_params["w_st_con"]:.4f}', src)
    src = re.sub(r'(exps\s+=\s+\[math\.exp\(b\["score"\]\s*/\s*)[\d.]+',
                 rf'\g<1>{opt_params["temperature"]:.1f}', src)

    predict_path.write_text(src)
    print(f"predict.py を更新しました。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="使用レース数上限（0=全件）")
    parser.add_argument("--apply", action="store_true", help="結果を predict.py に反映")
    args = parser.parse_args()

    dataset = load_dataset(args.limit)
    if not dataset:
        print("データなし")
        return

    opt_params, train_rate, test_rate = optimize(dataset)

    out = Path(__file__).parent / "logs" / "optimized_weights.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump({"params": opt_params,
                   "train_top5": train_rate,
                   "test_top5": test_rate}, f, indent=2)
    print(f"\n結果を {out} に保存しました。")

    if args.apply:
        apply_to_predict(opt_params)
        print("--apply オプションで predict.py に反映済みです。")
    else:
        print("--apply を付けて再実行すると predict.py に反映されます。")


if __name__ == "__main__":
    main()
