#!/usr/bin/env python3
"""
backtest.py — 過去レースの遡り予想バッチ

対象: entries + race_result_entries が揃っているレース
処理: predict() を実行し predictions テーブルに結果を保存
精度: 3連単の実際の組み合わせが Top3/Top5 に含まれるかどうか

実行:
    python3 backtest.py              # 全未処理レースを対象
    python3 backtest.py --date 20260626       # 特定日のみ
    python3 backtest.py --venue 01            # 特定会場のみ
    python3 backtest.py --limit 100           # 最大処理件数指定
"""

import sqlite3
import json
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from db_lock import acquire_write_lock, release_write_lock
    _HAS_LOCK = True
except ImportError:
    _HAS_LOCK = False
    def acquire_write_lock(**_): pass
    def release_write_lock(): pass

DB_PATH = Path(__file__).parent / "boatai.db"

# predict.pyのimport（同ディレクトリ前提）
sys.path.insert(0, str(Path(__file__).parent))
from predict import predict, warm_cache


def get_actual_combo(conn: sqlite3.Connection, race_id: int) -> str | None:
    """race_result_entries から実際の3連単組み合わせを取得"""
    rows = conn.execute("""
        SELECT rank, boat_no FROM race_result_entries
        WHERE race_id = ? AND rank IN (1, 2, 3) AND boat_no IS NOT NULL
        ORDER BY rank
    """, (race_id,)).fetchall()

    if len(rows) < 3:
        return None
    rank_map = {r[0]: r[1] for r in rows}
    if 1 in rank_map and 2 in rank_map and 3 in rank_map:
        return f"{rank_map[1]}-{rank_map[2]}-{rank_map[3]}"
    return None


def process_race(conn: sqlite3.Connection, race_id: int, date: str,
                 venue_code: str, race_no: int) -> dict | None:
    """1レースを処理して予想結果を返す（書き込みはmainで一括）"""
    try:
        result = predict(date, venue_code, race_no, conn=conn)  # 接続を使い回す
    except Exception as e:
        return {"error": str(e)}

    # 全体Top5（後方互換 + デフォルト的中判定）
    top5 = [d["combo"] for d in result["recommended_3t_detail"][:5]]

    # 3パターン各5通り
    honmei_combos = [d["combo"] for d in result.get("honmei_detail", [])]
    chuana_combos = [d["combo"] for d in result.get("chuana_detail", [])]
    ana_combos    = [d["combo"] for d in result.get("ana_detail", [])]

    actual = get_actual_combo(conn, race_id)

    def hit(combos: list) -> int | None:
        if actual is None or not combos:
            return None
        return 1 if actual in combos else 0

    hit_top3    = hit(top5[:3])
    hit_top5    = hit(top5)
    hit_honmei  = hit(honmei_combos)
    hit_chuana  = hit(chuana_combos)
    hit_ana     = hit(ana_combos)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("""
            INSERT INTO predictions
              (race_id, predicted_at, top5_combos, actual_combo,
               hit_top3, hit_top5,
               top5_honmei, top5_chuana, top5_ana,
               hit_honmei, hit_chuana, hit_ana)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(race_id) DO UPDATE SET
                predicted_at = excluded.predicted_at,
                top5_combos  = excluded.top5_combos,
                actual_combo = excluded.actual_combo,
                hit_top3     = excluded.hit_top3,
                hit_top5     = excluded.hit_top5,
                top5_honmei  = excluded.top5_honmei,
                top5_chuana  = excluded.top5_chuana,
                top5_ana     = excluded.top5_ana,
                hit_honmei   = excluded.hit_honmei,
                hit_chuana   = excluded.hit_chuana,
                hit_ana      = excluded.hit_ana
        """, (race_id, now, json.dumps(top5), actual,
              hit_top3, hit_top5,
              json.dumps(honmei_combos), json.dumps(chuana_combos), json.dumps(ana_combos),
              hit_honmei, hit_chuana, hit_ana))
        # commit はmain側で1000件ごとにまとめて実行
    except sqlite3.OperationalError as e:
        return {"error": str(e)}

    return {
        "race_id":    race_id,
        "top5":       top5,
        "actual":     actual,
        "hit_top3":   hit_top3,
        "hit_top5":   hit_top5,
        "hit_honmei": hit_honmei,
        "hit_chuana": hit_chuana,
        "hit_ana":    hit_ana,
    }


def init_db(conn: sqlite3.Connection) -> None:
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
            FOREIGN KEY (race_id) REFERENCES races(id)
        )
    """)
    # 既存テーブルへのカラム追加（ALTER TABLE は重複エラーを無視）
    for col, typ in [
        ("top5_honmei", "TEXT"),
        ("top5_chuana", "TEXT"),
        ("top5_ana",    "TEXT"),
        ("hit_honmei",  "INTEGER"),
        ("hit_chuana",  "INTEGER"),
        ("hit_ana",     "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass   # 既にカラムが存在する場合はスキップ
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="過去レース遡り予想バッチ")
    parser.add_argument("--date",      help="対象日 (例: 20260626)")
    parser.add_argument("--date-from", dest="date_from", help="指定日以降を対象 (例: 20260101)")
    parser.add_argument("--venue",     help="対象会場コード (例: 01)")
    parser.add_argument("--limit",     type=int, default=0, help="最大処理件数 (0=無制限)")
    parser.add_argument("--rerun", "--force", dest="rerun", action="store_true", help="処理済みレースも再実行（全件上書き）")
    args = parser.parse_args()

    # 他プロセスが書き込み中なら即終了（競合によるDB破損を防ぐ）
    if _HAS_LOCK:
        try:
            acquire_write_lock(wait=False)
            release_write_lock()
        except Exception:
            print("エラー: 別の書き込みプロセスが稼働中です。")
            print("全スクレイパーを停止してから再実行してください:")
            print("  launchctl stop gui/$(id -u)/com.boatai.today_scraper")
            print("  launchctl stop gui/$(id -u)/com.boatai.focused_scraper")
            print("  launchctl stop gui/$(id -u)/com.boatai.historical_scraper")
            print("  rm -f ~/boatai/.db_write.lock   # ロックが残っていれば")
            sys.exit(1)

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # バックテスト全体でロックを保持（レースごとに取得・解放しない）
    if _HAS_LOCK:
        acquire_write_lock(wait=True, timeout=120)

    # 高コストクエリを一括キャッシュ（130クエリ/レース → 10クエリ/レースに削減）
    warm_cache(conn)

    # 対象レースを取得
    # 条件: entriesが存在 AND race_result_entriesが存在（結果確定済み）
    where_clauses = [
        "EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)",
        "EXISTS (SELECT 1 FROM race_result_entries rre WHERE rre.race_id = r.id AND rre.rank = 1)",
    ]
    params = []

    if not args.rerun:
        where_clauses.append("NOT EXISTS (SELECT 1 FROM predictions p WHERE p.race_id = r.id)")

    if args.date:
        where_clauses.append("r.date = ?")
        params.append(args.date)

    if args.date_from:
        where_clauses.append("r.date >= ?")
        params.append(args.date_from)

    if args.venue:
        where_clauses.append("r.venue_code = ?")
        params.append(args.venue)

    where_sql = " AND ".join(where_clauses)
    limit_sql = f"LIMIT {args.limit}" if args.limit > 0 else ""

    rows = conn.execute(f"""
        SELECT r.id, r.date, r.venue_code, r.race_no
        FROM races r
        WHERE {where_sql}
        ORDER BY r.date DESC, r.venue_code, r.race_no
        {limit_sql}
    """, params).fetchall()

    total = len(rows)
    print(f"対象レース: {total}件")
    if total == 0:
        print("処理対象なし")
        if _HAS_LOCK:
            release_write_lock()
        conn.close()
        return

    done = 0
    hit3 = hit5 = 0
    hit_honmei = hit_chuana = hit_ana = 0
    cnt_honmei = cnt_chuana = cnt_ana = 0
    errors = 0

    try:
        for i, (race_id, date, venue_code, race_no) in enumerate(rows, 1):
            res = process_race(conn, race_id, date, venue_code, race_no)

            if res and "error" in res:
                errors += 1
                print(f"  [{i}/{total}] {date} {venue_code} {race_no}R — エラー: {res['error']}")
            else:
                done += 1
                if res:
                    if res["hit_top3"] is not None:
                        if res["hit_top3"]: hit3 += 1
                        if res["hit_top5"]: hit5 += 1
                    if res["hit_honmei"] is not None:
                        cnt_honmei += 1
                        if res["hit_honmei"]: hit_honmei += 1
                    if res["hit_chuana"] is not None:
                        cnt_chuana += 1
                        if res["hit_chuana"]: hit_chuana += 1
                    if res["hit_ana"] is not None:
                        cnt_ana += 1
                        if res["hit_ana"]: hit_ana += 1

            if i % 1000 == 0:
                conn.commit()   # 1000件ごとにまとめてコミット（WAL sync 削減）

            if i % 50 == 0 or i == total:
                print(f"  [{i}/{total}] {date} {venue_code} {race_no}R | "
                      f"累計 Top3:{hit3/max(done,1)*100:.1f}% "
                      f"本命:{hit_honmei/max(cnt_honmei,1)*100:.1f}% "
                      f"中穴:{hit_chuana/max(cnt_chuana,1)*100:.1f}% "
                      f"穴:{hit_ana/max(cnt_ana,1)*100:.1f}%")

        # 残分の最終コミット
        conn.commit()

    finally:
        if _HAS_LOCK:
            release_write_lock()
        conn.close()

    print(f"\n=== 完了 ===")
    print(f"処理: {done}件 / エラー: {errors}件")
    if done > 0:
        print(f"Top3的中率:  {hit3/done*100:.1f}% ({hit3}/{done})")
        print(f"Top5的中率:  {hit5/done*100:.1f}% ({hit5}/{done})")
        if cnt_honmei:
            print(f"本命的中率:  {hit_honmei/cnt_honmei*100:.1f}% ({hit_honmei}/{cnt_honmei})")
        if cnt_chuana:
            print(f"中穴的中率:  {hit_chuana/cnt_chuana*100:.1f}% ({hit_chuana}/{cnt_chuana})")
        if cnt_ana:
            print(f"穴的中率:    {hit_ana/cnt_ana*100:.1f}% ({hit_ana}/{cnt_ana})")


if __name__ == "__main__":
    main()
