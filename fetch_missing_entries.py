#!/usr/bin/env python3
"""
7/18 出走表未取得レースを手動フェッチ
対象: 徳山2R/7R, 若松2R/7R, 芦屋1R/5R/6R/7R/12R
"""
import sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    DB_PATH, BASE_URL, _tl_fetch,
    parse_entries, save_entries,
    parse_race_result, save_race_result_entries, save_payouts,
    _update_prediction_result,
)
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DATE = "20260718"

# 対象レース: (venue_code, race_no, race_id)
TARGETS = [
    ("18", 2, 331163),
    ("18", 7, 331168),
    ("20", 2, 331175),
    ("20", 7, 331180),
    ("21", 1, 331198),
    ("21", 5, 331202),
    ("21", 6, 331203),
    ("21", 7, 331204),
    ("21", 12, 331209),
]

VENUE_NAME = {"18": "徳山", "20": "若松", "21": "芦屋"}


def main():
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")

    for vcode, rno, race_id in TARGETS:
        vname = VENUE_NAME.get(vcode, vcode)
        log.info("── %s %dR (race_id=%d) ──", vname, rno, race_id)
        params = {"rno": rno, "jcd": vcode, "hd": DATE}

        # ── 出走表 ──
        current_entries = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE race_id=?", (race_id,)
        ).fetchone()[0]

        if current_entries == 0:
            soup_list = _tl_fetch(f"{BASE_URL}/racelist", params=params)
            if soup_list:
                entries = parse_entries(soup_list)
                if entries:
                    save_entries(conn, race_id, entries)
                    conn.commit()
                    log.info("  出走表: %d艇 保存", len(entries))
                else:
                    log.warning("  出走表: データなし（ページに出走表なし）")
            else:
                log.warning("  出走表: フェッチ失敗")
        else:
            log.info("  出走表: 取得済み（%d艇）スキップ", current_entries)

        # ── 確定結果 ──
        current_result = conn.execute(
            "SELECT COUNT(*) FROM race_result_entries WHERE race_id=?", (race_id,)
        ).fetchone()[0]

        if current_result == 0:
            soup_result = _tl_fetch(f"{BASE_URL}/raceresult", params=params)
            if soup_result:
                finish_entries, payouts = parse_race_result(soup_result)
                if finish_entries:
                    save_race_result_entries(conn, race_id, finish_entries)
                    if payouts:
                        save_payouts(conn, race_id, payouts)
                    conn.commit()
                    combo = "-".join(str(e["boat_no"]) for e in finish_entries[:3])
                    log.info("  結果: %s 保存（払戻%d件）", combo, len(payouts))
                    # predictionsのactual_combo更新
                    try:
                        _update_prediction_result(conn, race_id)
                        conn.commit()
                    except Exception as e:
                        log.warning("  prediction更新スキップ: %s", e)
                else:
                    log.info("  結果: 未確定（まだ開催中か未終了）")
            else:
                log.warning("  結果: フェッチ失敗")
        else:
            log.info("  結果: 取得済みスキップ")

    conn.close()
    log.info("=== 完了 ===")


if __name__ == "__main__":
    main()
