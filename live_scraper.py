#!/usr/bin/env python3
"""
ライブスクレイパー: 直前情報・オリジナル展示タイムの収集（並列版）
- 結果未確定レース: 直前情報・オリジナル展示を並列更新
- 結果確定済みでも直前情報が未取得なら取得（取りこぼし補完）
- 2分おき (LaunchAgent StartInterval=120) に自動実行
"""

import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    fetch,
    parse_before_info,
    save_before_info, save_weather,
    VENUE_NAMES, BASE_URL, DB_PATH, TODAY,
)
from venue_scraper import scrape_oriten_for_races, ensure_oriten_columns
from db_lock import acquire_write_lock, release_write_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# 時間制限: 8:00〜23:30 以外は何もせず終了
from datetime import datetime as _dt
_now = _dt.now()
if not (8 <= _now.hour < 23 or (_now.hour == 23 and _now.minute <= 30)):
    sys.exit(0)

MAX_WORKERS = 8


def open_db() -> sqlite3.Connection:
    from db_connect import open_db as _open
    return _open()


def find_pending_races(conn) -> list[tuple[str, int, int]]:
    """当日のレースで「出走表あり・結果なし」のものを返す。"""
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


def find_missing_before_info(conn) -> list[tuple[str, int, int]]:
    """確定済みだが直前情報が未取得のレース（取りこぼし補完用）"""
    return conn.execute("""
        SELECT r.venue_code, r.race_no, r.id
        FROM races r
        WHERE r.date = ?
          AND EXISTS (SELECT 1 FROM race_result_entries rre WHERE rre.race_id = r.id AND rre.rank = 1)
          AND NOT EXISTS (SELECT 1 FROM before_info bi WHERE bi.race_id = r.id AND bi.exhibition_time IS NOT NULL)
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.venue_code, r.race_no
    """, (TODAY,)).fetchall()


def needs_before_info(conn, race_id: int) -> bool:
    row = conn.execute("""
        SELECT COUNT(*) FROM before_info
        WHERE race_id = ? AND exhibition_time IS NOT NULL
    """, (race_id,)).fetchone()
    return row[0] == 0


def _http_fetch_before_info(vcode: str, rno: int, race_id: int):
    """直前情報をHTTP取得して返す（DB書き込みなし）。"""
    soup = fetch(f"{BASE_URL}/beforeinfo",
                 params={"rno": rno, "jcd": vcode, "hd": TODAY})
    if not soup:
        return None
    bi_entries, weather = parse_before_info(soup)
    if not bi_entries:
        return None
    return (vcode, rno, race_id, bi_entries, weather)


def main() -> None:
    """
    3フェーズ構成（scraper.py と同パターン）:
    Phase 1: DB状態確認（ロック保持・高速）
    Phase 2: 並列HTTPフェッチ（ロック解放中 — 他スクレイパーが割り込み可能）
    Phase 3: DB一括書き込み（ロック再取得）
    """
    log.info("=== ライブスクレイパー 開始 (日付: %s) ===", TODAY)

    # ── Phase 1: DB状態確認（ロック保持）─────────────────────────────
    acquire_write_lock(wait=True, timeout=180)
    conn = open_db()
    ensure_oriten_columns(conn)

    pending = find_pending_races(conn)
    log.info("未確定レース: %d件", len(pending)) if pending else log.info("未確定レースなし")
    bi_targets = [(v, rno, rid) for v, rno, rid in pending
                  if needs_before_info(conn, rid)]

    missing = find_missing_before_info(conn)
    if missing:
        log.info("直前情報取りこぼし補完対象: %d件", len(missing))
    # pendingとの重複を除いてマージ
    seen = {(v, rno, rid) for v, rno, rid in bi_targets}
    for item in missing:
        if item not in seen:
            bi_targets.append(item)
            seen.add(item)

    oriten_missing = conn.execute("""
        SELECT r.venue_code, r.race_no, r.id
        FROM races r
        WHERE r.date = ?
          AND EXISTS (
              SELECT 1 FROM before_info bi
              WHERE bi.race_id = r.id AND bi.exhibition_time IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM before_info bi
              WHERE bi.race_id = r.id AND bi.mawariashi_time IS NOT NULL
          )
        ORDER BY r.venue_code, r.race_no
    """, (TODAY,)).fetchall()

    conn.close()
    release_write_lock()   # ← HTTPフェッチ前に解放

    if not bi_targets and not oriten_missing:
        log.info("=== 取得対象なし。終了 ===")
        return

    # ── Phase 2: 並列HTTPフェッチ（ロック解放中）──────────────────────
    http_results: list[tuple] = []
    if bi_targets:
        log.info("直前情報 %d件 → 並列取得開始", len(bi_targets))
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(_http_fetch_before_info, v, rno, rid): (v, rno, rid)
                       for v, rno, rid in bi_targets}
            for future in as_completed(futures):
                data = future.result()
                if data:
                    http_results.append(data)

    # ── Phase 3: DB一括書き込み（ロック再取得）─────────────────────────
    oriten_targets: list[tuple] = []
    if http_results:
        acquire_write_lock(wait=True, timeout=60)
        conn = open_db()
        try:
            for vcode, rno, race_id, bi_entries, weather in http_results:
                vname = VENUE_NAMES.get(vcode, vcode)
                save_before_info(conn, race_id, bi_entries)
                save_weather(conn, race_id, weather)
                conn.commit()
                log.info("  %s %dR: 直前情報取得 %d艇", vname, rno, len(bi_entries))
                oriten_targets.append((vcode, rno, race_id))
        except Exception:
            log.exception("DB書き込み中にエラー発生")
        finally:
            conn.close()
            release_write_lock()   # 例外時も確実に解放

    # ── Phase 4: オリジナル展示タイム（scrape_oriten_for_races 内で完結）──
    all_oriten = list({(v, r, rid) for v, r, rid in oriten_targets + list(oriten_missing)})
    if all_oriten:
        log.info("--- オリジナル展示タイム取得・補完 %d件 ---", len(all_oriten))
        scrape_oriten_for_races(all_oriten)

    log.info("=== ライブスクレイパー 完了 ===")


if __name__ == "__main__":
    main()
