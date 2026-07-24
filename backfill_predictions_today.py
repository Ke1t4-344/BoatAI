#!/usr/bin/env python3
"""
backfill_predictions_today.py
指定日（デフォルト: 当日）に結果が出ているレースについて、
  1. predictionsレコードがなければ predict_ml で予想を生成して保存
  2. actual_combo が NULL なら確定結果から更新
を一括実行するバックフィルスクリプト。

使い方:
    python3 backfill_predictions_today.py            # 当日
    python3 backfill_predictions_today.py 20260724   # 特定日
"""

import json
import sys
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db_connect import open_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _get_actual_combo(conn, race_id: int):
    """3連単実結果を取得（結果がなければ None）"""
    rows = conn.execute("""
        SELECT rank, boat_no FROM race_result_entries
        WHERE race_id=? AND rank IN (1,2,3) AND boat_no IS NOT NULL
        ORDER BY rank
    """, (race_id,)).fetchall()
    if len(rows) < 3:
        return None
    rank_map = {r[0]: r[1] for r in rows}
    return f"{rank_map[1]}-{rank_map[2]}-{rank_map[3]}"


def _update_actual_combo(conn, race_id: int, actual: str, pred_row):
    """predictions の actual_combo と hit フラグを更新"""
    def _hit(json_str, n=None):
        if not json_str:
            return None
        combos = json.loads(json_str)
        if n is not None:
            combos = combos[:n]
        return 1 if actual in combos else 0

    top5     = json.loads(pred_row[0]) if pred_row[0] else []
    hit_t3   = 1 if actual in top5[:3] else 0
    hit_t5   = 1 if actual in top5     else 0

    conn.execute("""
        UPDATE predictions
           SET actual_combo=?, hit_top3=?, hit_top5=?,
               hit_honmei=?, hit_chuana=?, hit_ana=?,
               hit_honmei_5=?, hit_chuana_10=?, hit_ana_10=?
         WHERE race_id=?
    """, (actual, hit_t3, hit_t5,
          _hit(pred_row[1]),     _hit(pred_row[2]),     _hit(pred_row[3]),
          _hit(pred_row[1], 5),  _hit(pred_row[2], 10), _hit(pred_row[3], 10),
          race_id))
    conn.commit()
    return hit_t3, hit_t5


def main():
    # 引数で日付を指定可能（省略時は当日）
    target_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    log.info("バックフィル対象日: %s", target_date)

    conn = open_db()

    # 対象日に結果が出ているレース一覧（race.dateを必ず取得）
    races_with_result = conn.execute("""
        SELECT DISTINCT r.id, r.date, r.venue_code, r.race_no
        FROM race_result_entries rre
        JOIN races r ON r.id = rre.race_id
        WHERE r.date = ? AND rre.rank = 1
        ORDER BY r.venue_code, r.race_no
    """, (target_date,)).fetchall()

    log.info("確定済みレース: %d件", len(races_with_result))

    saved = 0
    updated = 0
    skipped = 0

    for race in races_with_result:
        race_id   = race[0]
        race_date = race[1]   # ← 実際のレース日付を使う（TODAY ではない）
        vc        = race[2]
        rno       = race[3]

        actual = _get_actual_combo(conn, race_id)
        if actual is None:
            skipped += 1
            continue

        # predictions レコード確認
        pred_row = conn.execute("""
            SELECT top5_combos, top5_honmei, top5_chuana, top5_ana, actual_combo
            FROM predictions WHERE race_id=?
        """, (race_id,)).fetchone()

        if pred_row is None:
            # 予測レコードなし → predict_ml で生成（race_date を使用）
            log.info("  %s %s %dR: 予測生成中...", race_date, vc, rno)
            result = None
            try:
                from ml_predict import predict_ml
                result = predict_ml(race_date, vc, rno, conn=conn)
            except Exception as e:
                log.warning("  %s %s %dR: ML予想失敗 → %s", race_date, vc, rno, e)
                try:
                    from predict import predict as _predict
                    result = _predict(race_date, vc, rno)
                except Exception as e2:
                    log.warning("  %s %s %dR: ルールベースも失敗 → %s", race_date, vc, rno, e2)
                    skipped += 1
                    continue

            if result is None:
                skipped += 1
                continue

            top5        = [d["combo"] for d in result.get("recommended_3t_detail", [])[:5]]
            honmei_list = [d["combo"] for d in result.get("honmei_detail", [])]
            chuana_list = [d["combo"] for d in result.get("chuana_detail", [])]
            ana_list    = [d["combo"] for d in result.get("ana_detail", [])]
            now         = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            conn.execute("""
                INSERT INTO predictions
                  (race_id, predicted_at, top5_combos, top5_honmei, top5_chuana, top5_ana)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(race_id) DO UPDATE SET
                    predicted_at = excluded.predicted_at,
                    top5_combos  = excluded.top5_combos,
                    top5_honmei  = excluded.top5_honmei,
                    top5_chuana  = excluded.top5_chuana,
                    top5_ana     = excluded.top5_ana
            """, (race_id, now,
                  json.dumps(top5, ensure_ascii=False),
                  json.dumps(honmei_list, ensure_ascii=False),
                  json.dumps(chuana_list, ensure_ascii=False),
                  json.dumps(ana_list, ensure_ascii=False)))
            conn.commit()
            saved += 1

            # actual_combo 更新
            pred_row_new = conn.execute("""
                SELECT top5_combos, top5_honmei, top5_chuana, top5_ana, actual_combo
                FROM predictions WHERE race_id=?
            """, (race_id,)).fetchone()
            hit3, hit5 = _update_actual_combo(conn, race_id, actual, pred_row_new)
            log.info("  %s %s %dR: 保存+反映 actual=%s hit_top3=%d hit_top5=%d",
                     race_date, vc, rno, actual, hit3, hit5)

        elif pred_row[4] is None:
            # 予測あり・actual_combo なし → 更新のみ
            hit3, hit5 = _update_actual_combo(conn, race_id, actual, pred_row)
            updated += 1
            log.info("  %s %s %dR: actual_combo更新 actual=%s hit_top3=%d hit_top5=%d",
                     race_date, vc, rno, actual, hit3, hit5)
        else:
            skipped += 1

    conn.close()
    log.info("完了: 予測新規生成=%d件 / actual更新=%d件 / スキップ=%d件",
             saved, updated, skipped)


if __name__ == "__main__":
    main()
