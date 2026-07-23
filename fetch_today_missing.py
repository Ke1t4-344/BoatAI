#!/usr/bin/env python3
"""
本日の出走表欠損レースを一括補完 (7/19用)
使い方: python3 fetch_today_missing.py
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    DB_PATH, BASE_URL, fetch, TODAY,
    parse_entries, save_entries,
    parse_race_result, save_race_result_entries, save_payouts,
    VENUE_NAMES,
)
from db_lock import acquire_write_lock, release_write_lock

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# 7/19 出走表欠損レース (DB確認: 21件)
TARGETS = [
    ('11', 12, 332265),  # 16:18
    ('12', 5,  332330),  # 17:31
    ('16', 4,  332389),  # 13:20
    ('16', 8,  332393),  # 15:42
    ('16', 12, 332397),  # 17:48
    ('17', 4,  332377),  # 12:12
    ('17', 8,  332381),  # 14:25
    ('17', 12, 332385),  # 16:50
    ('18', 3,  332352),  # 09:38
    ('18', 8,  332357),  # 12:00
    ('19', 3,  332400),  # 16:42
    ('19', 7,  332404),  # 18:33
    ('19', 11, 332408),  # 20:17
    ('20', 4,  332341),  # 17:12
    ('20', 9,  332346),  # 19:18
    ('21', 3,  332412),  # 09:24
    ('21', 7,  332416),  # 11:15
    ('21', 11, 332420),  # 13:21
    ('22', 3,  332364),  # 13:16
    ('22', 7,  332368),  # 15:13
    ('22', 12, 332373),  # 18:00
]

RETRY = 2        # 失敗時リトライ回数
RETRY_WAIT = 5   # 秒


def fetch_with_retry(url, params):
    for attempt in range(1, RETRY + 2):
        soup = fetch(url, params=params)
        if soup:
            return soup
        if attempt <= RETRY:
            log.warning("  取得失敗 (試行%d) → %d秒後リトライ", attempt, RETRY_WAIT)
            time.sleep(RETRY_WAIT)
    return None


def main():
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")

    saved_entries = 0
    saved_results = 0
    failed = []

    for vcode, rno, race_id in TARGETS:
        vname = VENUE_NAMES.get(vcode, vcode)
        log.info("── %s %dR (race_id=%d) ──", vname, rno, race_id)
        params = {"rno": rno, "jcd": vcode, "hd": TODAY}

        # ── 出走表 ──────────────────────────────────────────────────
        current = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE race_id=?", (race_id,)
        ).fetchone()[0]

        if current == 0:
            soup = fetch_with_retry(f"{BASE_URL}/racelist", params)
            if soup:
                entries = parse_entries(soup)
                if entries:
                    acquire_write_lock(wait=True, timeout=60)
                    try:
                        save_entries(conn, race_id, entries)
                        conn.commit()
                    finally:
                        release_write_lock()
                    log.info("  出走表: %d艇 保存 ✅", len(entries))
                    saved_entries += 1
                else:
                    log.warning("  出走表: ページに出走表データなし")
                    failed.append(f"{vname}{rno}R(出走表なし)")
            else:
                log.warning("  出走表: フェッチ失敗")
                failed.append(f"{vname}{rno}R(取得失敗)")
        else:
            log.info("  出走表: 取得済み(%d艇) スキップ", current)

        # ── 確定結果 ─────────────────────────────────────────────────
        current_result = conn.execute(
            "SELECT COUNT(*) FROM race_result_entries WHERE race_id=?", (race_id,)
        ).fetchone()[0]

        if current_result == 0:
            soup_r = fetch_with_retry(f"{BASE_URL}/raceresult", params)
            if soup_r:
                finish_entries, payouts = parse_race_result(soup_r)
                if finish_entries:
                    acquire_write_lock(wait=True, timeout=60)
                    try:
                        save_race_result_entries(conn, race_id, finish_entries)
                        if payouts:
                            save_payouts(conn, race_id, payouts)
                        conn.commit()
                    finally:
                        release_write_lock()
                    combo = "-".join(str(e["boat_no"]) for e in finish_entries[:3])
                    log.info("  結果: %s 保存（払戻%d件）✅", combo, len(payouts))
                    saved_results += 1
                else:
                    log.info("  結果: 未確定（レース未終了 or ページに結果なし）")
        else:
            log.info("  結果: 取得済み スキップ")

        time.sleep(0.5)  # サーバー負荷軽減

    conn.close()

    log.info("")
    log.info("=== 完了: 出走表 %d件 / 結果 %d件 保存 ===", saved_entries, saved_results)
    if failed:
        log.warning("取得できなかったレース: %s", ", ".join(failed))


if __name__ == "__main__":
    main()
