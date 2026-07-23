#!/usr/bin/env python3
"""
オッズスクレイパー: オッズ・確定結果の並列収集
- 結果未確定のレースのみを対象にオッズ・確定結果を更新
- 2分おき (LaunchAgent StartInterval=120) に自動実行
- live_scraper.py (直前情報・オリジナル展示) と役割分担

処理優先順位（並列実行）:
  1. 発走済みレース → 確定結果のみ並列取得（全会場を先に一周）
  2. 発走前レース   → オッズのみ並列取得
"""

import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    fetch,
    parse_odds_3t, parse_odds_2t,
    parse_race_result,
    save_odds, save_odds_2t,
    save_race_result_entries, save_payouts, save_st_history_from_result,
    VENUE_NAMES, BASE_URL, DB_PATH, TODAY,
)
from db_lock import acquire_write_lock, release_write_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# 時間制限: 8:00〜23:30 以外は何もせず終了
_now = datetime.now()
if not (8 <= _now.hour < 23 or (_now.hour == 23 and _now.minute <= 30)):
    sys.exit(0)

MAX_WORKERS = 8  # 並列スレッド数


def open_db() -> sqlite3.Connection:
    from db_connect import open_db as _open
    return _open()


def find_pending_races(conn) -> list[tuple[str, int, int, str | None]]:
    """当日のレースで「出走表あり・結果なし」のものをscheduled_time付きで返す。"""
    return conn.execute("""
        SELECT r.venue_code, r.race_no, r.id, r.scheduled_time
        FROM races r
        LEFT JOIN race_result_entries rre
               ON rre.race_id = r.id AND rre.rank = 1
        WHERE r.date = ?
          AND rre.id IS NULL
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.scheduled_time, r.venue_code, r.race_no
    """, (TODAY,)).fetchall()


def is_launched(scheduled_time: str | None, now: datetime) -> bool:
    """scheduled_time（'HH:MM'形式）が現在時刻以前なら発走済みとみなす。"""
    if not scheduled_time:
        return False
    try:
        t = datetime.strptime(scheduled_time, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        return t <= now
    except ValueError:
        return False


# ── HTTP フェッチ関数（並列実行用・DB書き込みなし） ──────────────

def _fetch_result(vcode: str, rno: int, race_id: int):
    """確定結果をHTTP取得して返す（DB書き込みなし）。"""
    soup = fetch(f"{BASE_URL}/raceresult",
                 params={"rno": rno, "jcd": vcode, "hd": TODAY})
    if not soup:
        return None
    res_entries, payouts = parse_race_result(soup)
    if not res_entries:
        return None
    return (vcode, rno, race_id, res_entries, payouts)


def _fetch_odds(vcode: str, rno: int, race_id: int):
    """3連単・2連単オッズをHTTP取得して返す（DB書き込みなし）。"""
    result = {}

    soup = fetch(f"{BASE_URL}/odds3t",
                 params={"rno": rno, "jcd": vcode, "hd": TODAY})
    if soup:
        combo_map = parse_odds_3t(soup)
        if combo_map:
            result["3t"] = combo_map

    soup = fetch(f"{BASE_URL}/odds2tf",
                 params={"rno": rno, "jcd": vcode, "hd": TODAY})
    if soup:
        combo_2t = parse_odds_2t(soup)
        if combo_2t:
            result["2t"] = combo_2t

    return (vcode, rno, race_id, result) if result else None


def main() -> None:
    now = datetime.now()
    log.info("=== オッズスクレイパー 開始 (日付: %s) ===", TODAY)

    # Step 1: pending取得のみロック取得し即解放（読み取り専用）
    acquire_write_lock(wait=True, timeout=180)
    try:
        conn = open_db()
        pending = find_pending_races(conn)
        conn.close()
    finally:
        release_write_lock()

    if not pending:
        log.info("未確定レースなし → 終了")
        return

    launched = [(v, rno, rid) for v, rno, rid, st in pending if is_launched(st, now)]
    upcoming  = [(v, rno, rid) for v, rno, rid, st in pending if not is_launched(st, now)]
    log.info("未確定レース: %d件 (発走済み:%d件 / 発走前:%d件)",
             len(pending), len(launched), len(upcoming))

    # Step 2: 並列フェッチ（ロックなし — focused_scraperが割り込み可能）
    result_data: list = []
    odds_data:   list = []

    if launched:
        log.info("── 確定結果フェーズ (%d件, 並列%d) ──", len(launched), MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(_fetch_result, v, rno, rid): (v, rno, rid)
                       for v, rno, rid in launched}
            for future in as_completed(futures):
                data = future.result()
                if data:
                    result_data.append(data)

    if upcoming:
        log.info("── オッズ更新フェーズ (%d件, 並列%d) ──", len(upcoming), MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(_fetch_odds, v, rno, rid): (v, rno, rid)
                       for v, rno, rid in upcoming}
            for future in as_completed(futures):
                data = future.result()
                if data:
                    odds_data.append(data)

    if not result_data and not odds_data:
        log.info("=== オッズスクレイパー 完了（書き込みなし） ===")
        return

    # Step 3: DB書き込み（ロック再取得）
    acquire_write_lock(wait=True, timeout=300)
    try:
        conn = open_db()

        # ── 優先①: 確定結果保存 ────────────────────────────────
        for data in result_data:
            vcode, rno, race_id, res_entries, payouts = data
            vname = VENUE_NAMES.get(vcode, vcode)
            save_race_result_entries(conn, race_id, res_entries)
            save_payouts(conn, race_id, payouts)
            save_st_history_from_result(conn, race_id, TODAY, vcode, rno, res_entries)
            conn.commit()
            log.info("  %s %dR: 確定結果保存 着順%d件 / 払戻%d件",
                     vname, rno, len(res_entries), len(payouts))

        # ── 優先②: オッズ保存 ────────────────────────────────
        for data in odds_data:
            vcode, rno, race_id, result = data
            vname = VENUE_NAMES.get(vcode, vcode)
            if "3t" in result:
                save_odds(conn, race_id, result["3t"])
                conn.commit()
                log.info("  %s %dR: 3連単オッズ更新 %d件", vname, rno, len(result["3t"]))
            if "2t" in result:
                save_odds_2t(conn, race_id, result["2t"])
                conn.commit()
                log.info("  %s %dR: 2連単オッズ更新 %d件", vname, rno, len(result["2t"]))

        conn.close()
    finally:
        release_write_lock()

    log.info("=== オッズスクレイパー 完了 ===")


if __name__ == "__main__":
    main()
