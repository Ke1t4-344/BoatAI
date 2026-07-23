#!/usr/bin/env python3
"""
recommend.py — 1日のおすすめレース生成・結果照合

カテゴリ別（本命/中穴/穴）に自信度の高いレースを各5R選定し、
各レースのTop5コンボを daily_recommendations テーブルに保存する。

実行:
    python3 recommend.py                  # 本日のおすすめを生成
    python3 recommend.py --date 20260701  # 指定日
    python3 recommend.py --check          # 過去の推薦結果を照合・更新
    python3 recommend.py --check --date 20260701
    python3 recommend.py --accuracy       # 精度サマリーを表示
"""

import sqlite3
import json
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from predict import predict, DB_PATH

CATEGORY_KEYS = ["honmei", "chuana", "ana"]
CATEGORY_LABELS = {"honmei": "本命", "chuana": "中穴", "ana": "穴"}
TOP_N = 5  # カテゴリごとに推薦するレース数


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_recommendations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            date             TEXT    NOT NULL,
            generated_at     TEXT    NOT NULL,
            venue_code       TEXT    NOT NULL,
            race_no          INTEGER NOT NULL,
            category         TEXT    NOT NULL,
            day_rank         INTEGER NOT NULL,
            combo            TEXT    NOT NULL,
            prob             REAL,
            expected_odds    REAL,
            ev               REAL,
            confidence       REAL,
            top5_combos_json TEXT,
            actual_combo     TEXT,
            hit              INTEGER,
            checked_at       TEXT,
            UNIQUE(date, venue_code, race_no, category)
        )
    """)
    # 既存テーブルへの top5_combos_json カラム追加（マイグレーション）
    try:
        conn.execute("ALTER TABLE daily_recommendations ADD COLUMN top5_combos_json TEXT")
    except Exception:
        pass
    conn.commit()


def get_target_date(conn: sqlite3.Connection, date_arg: str | None,
                    check_mode: bool = False) -> str:
    if date_arg:
        return date_arg
    if check_mode:
        # チェックモードは「今日の日付」固定。
        # DBには翌日データが先行入荷されることがあるため、
        # MAX(date)を使うと翌日が返ってしまい当日分がスキップされる。
        return datetime.now().strftime("%Y%m%d")
    row = conn.execute("""
        SELECT r.date FROM races r
        WHERE EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.date DESC LIMIT 1
    """).fetchone()
    return row[0] if row else datetime.now().strftime("%Y%m%d")


def get_races_for_date(conn: sqlite3.Connection, date: str) -> list[tuple]:
    return conn.execute("""
        SELECT DISTINCT r.venue_code, r.race_no
        FROM races r
        WHERE r.date = ?
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.venue_code, r.race_no
    """, (date,)).fetchall()


def _confidence(category: str, detail: list) -> float:
    """
    自信度スコア。
    本命: Top1確率が高いほど◎（確率がはっきり抜けている）
    中穴/穴: EV（期待値）優先、なければ確率
    """
    if not detail:
        return 0.0
    top = detail[0]
    if category == "honmei":
        return top.get("prob") or 0.0
    else:
        ev = top.get("ev")
        return ev if (ev and ev > 0) else (top.get("prob") or 0.0)


def generate(conn: sqlite3.Connection, date: str, verbose: bool = True):
    races = get_races_for_date(conn, date)
    if not races:
        print(f"対象レースなし: {date}")
        return

    print(f"対象: {date} / {len(races)}レース — 予想実行中...")

    candidates = {cat: [] for cat in CATEGORY_KEYS}
    errors = 0

    for venue_code, race_no in races:
        try:
            result = predict(date, venue_code, race_no)
        except Exception as e:
            errors += 1
            if verbose:
                print(f"  SKIP {venue_code}-{race_no}R: {e}")
            continue

        # カテゴリ別の上位5コンボを取得
        for cat in CATEGORY_KEYS:
            detail = result.get(f"{cat}_detail", [])
            if not detail:
                continue
            top = detail[0]
            conf = _confidence(cat, detail)

            # Top5コンボをJSON化して保存
            # カテゴリのdetailが5件未満の場合、全体Top5（recommended_3t_detail）で補完
            top5 = [
                {
                    "combo":         d["combo"],
                    "prob":          d.get("prob"),
                    "expected_odds": d.get("expected_odds"),
                    "ev":            d.get("ev"),
                }
                for d in detail[:5]
            ]
            if len(top5) < 5:
                existing = {c["combo"] for c in top5}
                for d in result.get("recommended_3t_detail", []):
                    if len(top5) >= 5:
                        break
                    if d["combo"] not in existing:
                        top5.append({
                            "combo":         d["combo"],
                            "prob":          d.get("prob"),
                            "expected_odds": d.get("expected_odds"),
                            "ev":            d.get("ev"),
                        })
                        existing.add(d["combo"])

            candidates[cat].append({
                "venue_code":      venue_code,
                "race_no":         race_no,
                "combo":           top["combo"],        # Top1（的中判定基準）
                "prob":            top.get("prob"),
                "expected_odds":   top.get("expected_odds"),
                "ev":              top.get("ev"),
                "confidence":      conf,
                "top5_combos":     top5,
            })

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    saved_total = 0

    # 当日の既存データを全削除してからクリーンに挿入（再生成時の重複防止）
    conn.execute("DELETE FROM daily_recommendations WHERE date=?", (date,))
    conn.commit()

    for cat in CATEGORY_KEYS:
        # 同率の場合は venue_code → race_no の辞書順で安定させる
        ranked = sorted(
            candidates[cat],
            key=lambda x: (-x["confidence"], x["venue_code"], x["race_no"])
        )[:TOP_N]  # 厳密に5件のみ

        for rank, c in enumerate(ranked, 1):
            conn.execute("""
                INSERT INTO daily_recommendations
                  (date, generated_at, venue_code, race_no, category,
                   day_rank, combo, prob, expected_odds, ev, confidence,
                   top5_combos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date, now, c["venue_code"], c["race_no"], cat,
                rank, c["combo"], c["prob"], c["expected_odds"], c["ev"],
                c["confidence"], json.dumps(c["top5_combos"], ensure_ascii=False),
            ))
            saved_total += 1

        if verbose:
            label = CATEGORY_LABELS[cat]
            print(f"\n  【{label}】上位{len(ranked)}レース:")
            for rank, c in enumerate(ranked, 1):
                top5_combos = [x["combo"] for x in c["top5_combos"]]
                print(f"    {rank}. {c['venue_code']}-{c['race_no']:2d}R  "
                      f"Top5: {' / '.join(top5_combos)}  [自信度:{c['confidence']:.2f}]")

    conn.commit()
    print(f"\n保存完了: {saved_total}件 (エラー: {errors}件)")


def check(conn: sqlite3.Connection, date: str, verbose: bool = True):
    """実際の結果とTop5を照合して hit を更新"""
    rows = conn.execute("""
        SELECT dr.id, dr.venue_code, dr.race_no, dr.combo,
               dr.top5_combos_json, dr.category, dr.day_rank
        FROM daily_recommendations dr
        WHERE dr.date = ? AND dr.hit IS NULL
        ORDER BY dr.category, dr.day_rank
    """, (date,)).fetchall()

    if not rows:
        print(f"照合対象なし: {date}")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = 0

    for rec_id, venue_code, race_no, top_combo, top5_json, category, day_rank in rows:
        actual = conn.execute("""
            SELECT rre.boat_no FROM race_result_entries rre
            JOIN races r ON r.id = rre.race_id
            WHERE r.date=? AND r.venue_code=? AND r.race_no=?
              AND rre.rank IN (1,2,3) AND rre.boat_no IS NOT NULL
            ORDER BY rre.rank
        """, (date, venue_code, race_no)).fetchall()

        if len(actual) < 3:
            continue  # 結果未確定

        actual_combo = f"{actual[0][0]}-{actual[1][0]}-{actual[2][0]}"

        # Top5のいずれかに入っていれば的中
        top5_combos = [x["combo"] for x in json.loads(top5_json)] if top5_json else [top_combo]
        hit = 1 if actual_combo in top5_combos else 0

        conn.execute("""
            UPDATE daily_recommendations
            SET actual_combo=?, hit=?, checked_at=?
            WHERE id=?
        """, (actual_combo, hit, now, rec_id))
        updated += 1

        if verbose:
            label = CATEGORY_LABELS.get(category, category)
            mark = "✅" if hit else "❌"
            print(f"  {mark} {label}{day_rank}位  {venue_code}-{race_no}R  "
                  f"実際:{actual_combo}  Top5:{' '.join(top5_combos)}")

    conn.commit()
    print(f"\n照合完了: {updated}件更新")


def print_accuracy(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT category,
               COUNT(*) as total,
               SUM(hit)  as hits
        FROM daily_recommendations
        WHERE hit IS NOT NULL
        GROUP BY category
    """).fetchall()

    if not rows:
        print("精度データなし（まだ結果照合が行われていません）")
        return

    print("\n=== 推薦精度サマリー（Top5的中率）===")
    for cat, total, hits in rows:
        label = CATEGORY_LABELS.get(cat, cat)
        rate = hits / total * 100 if total > 0 else 0
        print(f"  {label:<4}: {rate:5.1f}%  ({hits}/{total})")


def main():
    parser = argparse.ArgumentParser(description="1日のおすすめレース生成・照合")
    parser.add_argument("--date",     help="対象日 YYYYMMDD (デフォルト: 最新日)")
    parser.add_argument("--check",    action="store_true", help="過去の結果照合モード")
    parser.add_argument("--accuracy", action="store_true", help="精度サマリーを表示")
    parser.add_argument("--quiet",    action="store_true", help="詳細出力を抑制")
    args = parser.parse_args()

    from db_lock import acquire_write_lock, release_write_lock
    # --accuracy は読み取り専用なのでロック不要。他はDB書き込みを行うためロック必要。
    need_lock = not args.accuracy
    if need_lock:
        acquire_write_lock(wait=True, timeout=300)

    conn = None
    try:
        from db_connect import open_db
        conn = open_db()
        init_db(conn)

        date = get_target_date(conn, args.date, check_mode=args.check)

        if args.accuracy:
            print_accuracy(conn)
        elif args.check:
            check(conn, date, verbose=not args.quiet)
        else:
            generate(conn, date, verbose=not args.quiet)

    finally:
        if conn is not None:
            conn.close()
        if need_lock:
            release_write_lock()


if __name__ == "__main__":
    main()
