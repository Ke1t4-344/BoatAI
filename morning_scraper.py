#!/usr/bin/env python3
"""
morning_scraper.py — 当日出走表の早期一括取得（並列版）

毎朝6:00に起動し、当日の全会場・全レースの出走表と発走時刻を
できるだけ早くDBに格納する。

取得対象:
  - 会場一覧・レース数
  - 発走予定時刻 (scheduled_time)
  - 出走表 (entries)

取得しないもの（focused/odds/live_scraperに任せる）:
  - オッズ
  - 直前情報
  - 確定結果
"""

import sqlite3
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    fetch,
    parse_entries, save_entries,
    upsert_venue, upsert_race,
    fetch_today_venues, fetch_race_schedule,
    init_db,
    VENUE_NAMES, BASE_URL, DB_PATH, TODAY,
)
from db_lock import acquire_write_lock, release_write_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MAX_WORKERS = 8
MAX_RETRIES = 3       # 個別レースの最大リトライ回数
RETRY_DELAY = 5       # リトライ間隔（秒）
RETRY_PASS_DELAY = 30 # 全体リトライパス前の待機時間（秒）


def open_db() -> sqlite3.Connection:
    from db_connect import open_db as _open
    return _open()


def already_have_entries(conn, race_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE race_id=?", (race_id,)
    ).fetchone()
    return row[0] >= 6


def _fetch_entries_with_retry(vcode: str, rno: int) -> list:
    """1レース分の出走表をリトライ付きで取得する。失敗時は空リストを返す。"""
    for attempt in range(1, MAX_RETRIES + 1):
        soup = fetch(f"{BASE_URL}/racelist",
                     params={"rno": rno, "jcd": vcode, "hd": TODAY})
        if soup:
            entries = parse_entries(soup)
            if entries:
                return entries
        if attempt < MAX_RETRIES:
            log.warning("  %s %dR: 取得失敗 (試行%d/%d) → %d秒後リトライ",
                        VENUE_NAMES.get(vcode, vcode), rno, attempt, MAX_RETRIES, RETRY_DELAY)
            time.sleep(RETRY_DELAY)
    return []


def _fetch_venue_data(vcode: str) -> dict | None:
    """1会場分のスケジュール＋全出走表をHTTP取得（DB書き込みなし）。リトライあり。"""
    vname = VENUE_NAMES.get(vcode, vcode)
    max_rno, schedule = fetch_race_schedule(vcode)
    if max_rno == 0:
        return None

    races = []
    for rno in range(1, max_rno + 1):
        entries = _fetch_entries_with_retry(vcode, rno)
        races.append((rno, schedule.get(rno), entries))
        status = f"{len(entries)}艇取得" if entries else "取得失敗"
        log.info("  %s %dR (発走:%s): %s",
                 vname, rno, schedule.get(rno, "??:??"), status)

    return {"vcode": vcode, "schedule": schedule, "races": races}


def _main_body() -> None:
    log.info("=== 朝スクレイパー 開始 (日付: %s) ===", TODAY)

    conn = open_db()
    init_db(conn)

    venue_codes = fetch_today_venues()
    if not venue_codes:
        log.warning("本日の開催会場が取得できませんでした。")
        conn.close()
        return

    log.info("本日の開催会場: %s (%d会場) → 並列取得開始", venue_codes, len(venue_codes))

    # 会場ごとに並列HTTP取得
    venue_results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch_venue_data, vcode): vcode for vcode in venue_codes}
        for future in as_completed(futures):
            data = future.result()
            if data:
                venue_results.append(data)

    # DB書き込み（ロック取得後に実行）
    acquire_write_lock(wait=True, timeout=600)
    failed_races: list[tuple[str, int, int]] = []  # 第1パスで出走表取得失敗 (vcode, rno, race_id)
    try:
        total_races = 0
        total_entries = 0
        for data in venue_results:
            vcode = data["vcode"]
            vname = VENUE_NAMES.get(vcode, vcode)
            log.info("═══ %s (%s) DB保存 ═══", vname, vcode)

            upsert_venue(conn, vcode)
            conn.commit()

            for rno, stime, entries in data["races"]:
                race_id = upsert_race(conn, vcode, rno, stime)
                conn.commit()

                if already_have_entries(conn, race_id):
                    continue

                if entries:
                    save_entries(conn, race_id, entries)
                    conn.commit()
                    total_entries += len(entries)
                    total_races += 1
                else:
                    failed_races.append((vcode, rno, race_id))

        conn.close()
        log.info("=== 朝スクレイパー 第1パス完了: %d レース / %d艇 (取得失敗: %d) ===",
                 total_races, total_entries, len(failed_races))
    finally:
        release_write_lock()

    # 第2パス: 出走表が取得できなかったレースをリトライ
    if failed_races:
        log.info("第2パス開始: %d件 → %d秒待機...", len(failed_races), RETRY_PASS_DELAY)
        time.sleep(RETRY_PASS_DELAY)

        retry_results: list[tuple[str, int, int, list]] = []
        for vcode, rno, race_id in failed_races:
            entries = _fetch_entries_with_retry(vcode, rno)
            retry_results.append((vcode, rno, race_id, entries))

        acquire_write_lock(wait=True, timeout=600)
        try:
            conn2 = open_db()
            retry_saved = 0
            for vcode, rno, race_id, entries in retry_results:
                if not entries:
                    log.warning("  [第2パス] %s %dR: 再取得失敗",
                                VENUE_NAMES.get(vcode, vcode), rno)
                    continue
                if already_have_entries(conn2, race_id):
                    continue
                save_entries(conn2, race_id, entries)
                conn2.commit()
                retry_saved += 1
                log.info("  [第2パス] %s %dR: %d艇保存",
                         VENUE_NAMES.get(vcode, vcode), rno, len(entries))
            conn2.close()
            log.info("=== 朝スクレイパー 第2パス完了: %d レース追加 ===", retry_saved)
        finally:
            release_write_lock()

        total_races += retry_saved

    log.info("=== 朝スクレイパー 完了: 合計 %d レース ===", total_races)


def main() -> None:
    # 多重起動防止（LaunchAgent が 6:00 と 6:30 に同時発火する場合の対策）
    PID_FILE = Path(__file__).parent / ".morning_scraper_running.pid"
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)   # プロセスが存在するか確認（例外なし = 存在する）
            log.warning("別インスタンスが実行中 (PID=%d)。二重起動を防止して終了します。", old_pid)
            return
        except (ProcessLookupError, PermissionError, ValueError):
            pass   # 古いPIDファイルが残っているだけ → 上書きして続行
    PID_FILE.write_text(str(os.getpid()))
    try:
        _main_body()
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
