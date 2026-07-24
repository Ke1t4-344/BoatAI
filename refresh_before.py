#!/usr/bin/env python3
"""
refresh_before.py — 直前情報・オッズを今すぐ取得（レース直前用）

「結果未確定のレース」のみを対象にすることで高速化:
  - 全レースをなめる旧実装と異なり、completed済みはスキップ
  - 3連単・単勝・2連単オッズ、直前情報、確定結果を更新
"""

import sqlite3
import sys
import logging
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    fetch,
    parse_odds_3t, parse_odds_tansho, parse_odds_2t,
    parse_before_info, parse_race_result,
    save_odds, save_odds_tansho, save_odds_2t,
    save_before_info, save_weather,
    save_race_result_entries, save_payouts, save_st_history_from_result,
    init_db, BASE_URL, TODAY, DB_PATH, VENUE_NAMES,
)
from db_lock import acquire_write_lock, release_write_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("refresh_before")


def find_pending_races(conn) -> list[tuple[str, int, int]]:
    """結果未確定かつ出走表あり のレース一覧"""
    return conn.execute("""
        SELECT r.venue_code, r.race_no, r.id
        FROM races r
        LEFT JOIN race_result_entries rre
               ON rre.race_id = r.id AND rre.rank = 1
        WHERE r.date = ?
          AND rre.id IS NULL
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.venue_code, r.race_no
    """, (TODAY,)).fetchall()


def needs_before_info(conn, race_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM before_info WHERE race_id=? AND exhibition_time IS NOT NULL",
        (race_id,)
    ).fetchone()
    return row[0] == 0


def refresh():
    log.info("=== 直前情報・オッズ更新 開始 (%s) ===", TODAY)
    acquire_write_lock(wait=True, timeout=180)

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        init_db(conn)

        pending = find_pending_races(conn)
        if not pending:
            log.info("未確定レースなし → 終了")
            return

        log.info("対象: %d レース（確定済みはスキップ）", len(pending))

        for vcode, rno, race_id in pending:
            vname = VENUE_NAMES.get(vcode, vcode)
            log.info("  %s %dR (race_id=%d)", vname, rno, race_id)

            # 3連単オッズ
            soup = fetch(f"{BASE_URL}/odds3t",
                         params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup:
                combo_map = parse_odds_3t(soup)
                if combo_map:
                    save_odds(conn, race_id, combo_map)
                    conn.commit()

            # 単勝オッズ
            soup = fetch(f"{BASE_URL}/oddstkf",
                         params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup:
                tansho_map = parse_odds_tansho(soup)
                if tansho_map:
                    save_odds_tansho(conn, race_id, tansho_map)
                    conn.commit()

            # 2連単オッズ
            soup = fetch(f"{BASE_URL}/odds2tf",
                         params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup:
                combo_2t = parse_odds_2t(soup)
                if combo_2t:
                    save_odds_2t(conn, race_id, combo_2t)
                    conn.commit()

            # 直前情報（未取得のみ）
            if needs_before_info(conn, race_id):
                soup = fetch(f"{BASE_URL}/beforeinfo",
                             params={"rno": rno, "jcd": vcode, "hd": TODAY})
                if soup:
                    bi_entries, weather = parse_before_info(soup)
                    if bi_entries:
                        save_before_info(conn, race_id, bi_entries)
                        save_weather(conn, race_id, weather)
                        conn.commit()

            # 確定結果
            soup = fetch(f"{BASE_URL}/raceresult",
                         params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup:
                res_entries, payouts = parse_race_result(soup)
                if res_entries:
                    save_race_result_entries(conn, race_id, res_entries)
                    save_payouts(conn, race_id, payouts)
                    save_st_history_from_result(conn, race_id, TODAY, vcode, rno, res_entries)
                    conn.commit()
    finally:
        if conn is not None:
            conn.close()
        release_write_lock()
        log.info("=== 完了 ===")


if __name__ == "__main__":
    refresh()
