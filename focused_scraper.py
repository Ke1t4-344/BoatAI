#!/usr/bin/env python3
"""
focused_scraper.py — 発走直前レースの集中ポーリング（並列処理版）

scheduled_time を使って「今から15分以内に発走 or 発走後15分以内」のレースを対象に
直前情報・オッズ・確定結果を並列取得する。

LaunchAgent StartInterval=30 で常時起動し、
対象レースがなければ即終了（CPU負荷ゼロ）。
"""

import sqlite3
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
import sys

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    parse_odds_3t, parse_odds_tansho, parse_odds_2t,
    parse_before_info, parse_race_result,
    save_odds, save_odds_tansho, save_odds_2t,
    save_before_info, save_weather,
    save_race_result_entries, save_payouts, save_st_history_from_result,
    VENUE_NAMES, BASE_URL, DB_PATH, TODAY, HEADERS, REQ_DELAY,
)
from venue_scraper import scrape_oriten_for_races, ensure_oriten_columns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# 発走の何分前から集中ポーリングするか（展示航走は発走30分前に完了）
FOCUS_MINUTES_BEFORE = 40
# 結果確定後も何分間ポーリングを続けるか
FOLLOW_MINUTES_AFTER = 25
# 並列数（boatrace.jpへの負荷を考慮）
MAX_WORKERS = 8

# スレッドローカルセッション（スレッドごとに独立したHTTPセッション）
_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.session = s
    return _thread_local.session


def _fetch(url: str, params: dict | None = None) -> BeautifulSoup | None:
    """スレッドセーフなfetch（スレッドローカルセッション使用）"""
    try:
        resp = _get_session().get(url, params=params, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        time.sleep(REQ_DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.warning("取得失敗 %s %s: %s", url, params, e)
        return None


def open_db() -> sqlite3.Connection:
    from db_connect import open_db as _open
    return _open()


def find_focused_races(conn) -> list[tuple[str, int, int, str | None]]:
    """
    scheduled_time が (now - FOLLOW_MINUTES_AFTER) ～ (now + FOCUS_MINUTES_BEFORE) の
    未確定レースを返す。
    """
    now = datetime.now()
    t_from = (now - timedelta(minutes=FOLLOW_MINUTES_AFTER)).strftime("%H:%M")
    t_to   = (now + timedelta(minutes=FOCUS_MINUTES_BEFORE)).strftime("%H:%M")

    rows = conn.execute("""
        SELECT r.venue_code, r.race_no, r.id, r.scheduled_time
        FROM races r
        LEFT JOIN race_result_entries rre
               ON rre.race_id = r.id AND rre.rank = 1
        WHERE r.date = ?
          AND rre.id IS NULL
          AND r.scheduled_time IS NOT NULL
          AND r.scheduled_time >= ?
          AND r.scheduled_time <= ?
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
        ORDER BY r.scheduled_time, r.venue_code
    """, (TODAY, t_from, t_to)).fetchall()
    return rows


def needs_before_info(conn, race_id: int) -> bool:
    """6艇分の展示タイムが揃っていない場合に True を返す（部分取得の補完も行う）"""
    row = conn.execute("""
        SELECT COUNT(DISTINCT boat_no) FROM before_info
        WHERE race_id = ? AND exhibition_time IS NOT NULL
    """, (race_id,)).fetchone()
    # エントリ数（最大6艇）と比較
    entry_count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE race_id = ?", (race_id,)
    ).fetchone()[0]
    expected = min(entry_count, 6)
    return row[0] < expected


# ── 並列フェッチ ──────────────────────────────────────────
def fetch_race_data(vcode: str, rno: int, race_id: int,
                    stime: str | None, do_before_info: bool) -> dict:
    """
    1レース分のデータをHTTPで取得（DB書き込みなし）。
    各種ページ（直前情報・3連単・単勝・2連単・結果）を並列取得。
    Returns: {
        'vcode', 'rno', 'race_id', 'stime',
        'before_info': (bi_entries, weather) | None,
        'odds_3t': combo_map | None,
        'odds_tansho': tansho_map | None,
        'odds_2t': combo_2t | None,
        'result': (res_entries, payouts) | None,
    }
    """
    now_str = datetime.now().strftime("%H:%M")
    vname = VENUE_NAMES.get(vcode, vcode)
    log.info("  [fetch開始] %s %dR (発走: %s)", vname, rno, stime or "??:??")

    # ── 各ページ取得関数（スレッドで並列実行） ──────────────────────────
    def _do_before_info():
        if not do_before_info:
            return None
        soup = _fetch(f"{BASE_URL}/beforeinfo",
                      params={"rno": rno, "jcd": vcode, "hd": TODAY})
        if soup:
            bi_entries, weather = parse_before_info(soup)
            if bi_entries:
                return (bi_entries, weather)
        return None

    def _do_odds3t():
        soup = _fetch(f"{BASE_URL}/odds3t",
                      params={"rno": rno, "jcd": vcode, "hd": TODAY})
        if soup:
            combo_map = parse_odds_3t(soup)
            return combo_map if combo_map else None
        return None

    def _do_tansho():
        soup = _fetch(f"{BASE_URL}/oddstkf",
                      params={"rno": rno, "jcd": vcode, "hd": TODAY})
        if soup:
            tansho_map = parse_odds_tansho(soup)
            return tansho_map if tansho_map else None
        return None

    def _do_odds2t():
        soup = _fetch(f"{BASE_URL}/odds2tf",
                      params={"rno": rno, "jcd": vcode, "hd": TODAY})
        if soup:
            combo_2t = parse_odds_2t(soup)
            return combo_2t if combo_2t else None
        return None

    def _do_result():
        if not (stime and stime <= now_str):
            return None
        soup = _fetch(f"{BASE_URL}/raceresult",
                      params={"rno": rno, "jcd": vcode, "hd": TODAY})
        if soup:
            res_entries, payouts = parse_race_result(soup)
            if res_entries:
                return (res_entries, payouts)
        return None

    # ── 並列実行（同一レースの全ページを同時取得） ─────────────────────
    # MAX_WORKERS=3: boatrace.jp への同時接続を抑制しつつ高速化
    with ThreadPoolExecutor(max_workers=3) as inner_pool:
        f_bi     = inner_pool.submit(_do_before_info)
        f_3t     = inner_pool.submit(_do_odds3t)
        f_tansho = inner_pool.submit(_do_tansho)
        f_2t     = inner_pool.submit(_do_odds2t)
        f_result = inner_pool.submit(_do_result)

        before_info  = f_bi.result()
        odds_3t      = f_3t.result()
        odds_tansho  = f_tansho.result()
        odds_2t      = f_2t.result()
        race_result  = f_result.result()

    return {
        "vcode": vcode, "rno": rno, "race_id": race_id, "stime": stime,
        "before_info": before_info,
        "odds_3t":     odds_3t,
        "odds_tansho": odds_tansho,
        "odds_2t":     odds_2t,
        "result":      race_result,
    }


def save_race_data(conn: sqlite3.Connection, data: dict) -> bool:
    """
    fetch_race_data の結果をDBに書き込む。
    Returns True if before_info was newly saved (oriten取得候補)。
    """
    vcode    = data["vcode"]
    rno      = data["rno"]
    race_id  = data["race_id"]
    vname    = VENUE_NAMES.get(vcode, vcode)
    new_bi   = False

    if data["before_info"]:
        bi_entries, weather = data["before_info"]
        save_before_info(conn, race_id, bi_entries)
        save_weather(conn, race_id, weather)
        conn.commit()
        log.info("  %s %dR: 直前情報取得 %d艇", vname, rno, len(bi_entries))
        new_bi = True

    if data["odds_3t"]:
        save_odds(conn, race_id, data["odds_3t"])
        conn.commit()

    if data["odds_tansho"]:
        save_odds_tansho(conn, race_id, data["odds_tansho"])
        conn.commit()

    if data["odds_2t"]:
        save_odds_2t(conn, race_id, data["odds_2t"])
        conn.commit()

    if data["result"]:
        res_entries, payouts = data["result"]
        save_race_result_entries(conn, race_id, res_entries)
        save_payouts(conn, race_id, payouts)
        save_st_history_from_result(
            conn, race_id, TODAY, vcode, rno, res_entries
        )
        conn.commit()
        log.info("  %s %dR: 確定結果保存 %d件", vname, rno, len(res_entries))

    return new_bi


def main() -> None:
    # ── レース開催時間外は即終了（Turso read 節約）──
    # 7:30 未満 / 23:30 超はどの会場もレースなし → DB呼び出し不要
    _now_h = datetime.now()
    if not (7 <= _now_h.hour < 23 or (_now_h.hour == 23 and _now_h.minute <= 30)):
        return
    if _now_h.hour < 7 or (_now_h.hour == 7 and _now_h.minute < 30):
        return

    # ── DB書き込みロック取得（today_scraper等と同時書き込み防止） ──
    # today_scraperがフェッチ中（最大5分）でも待機して衝突を防ぐ
    from db_lock import acquire_write_lock, release_write_lock
    acquire_write_lock(wait=True, timeout=600)  # 最大10分待機

    conn = open_db()
    ensure_oriten_columns(conn)
    targets = find_focused_races(conn)

    if not targets:
        conn.close()
        release_write_lock()
        return

    now_str = datetime.now().strftime("%H:%M")
    log.info("=== 集中ポーリング 開始 (%s) — 対象: %d件 ===", now_str, len(targets))

    # before_info が必要かどうかを事前チェック（DB読み取り、メインスレッドで）
    tasks = [
        (vcode, rno, race_id, stime, needs_before_info(conn, race_id))
        for vcode, rno, race_id, stime in targets
    ]

    # フェッチ中はロックを一時解放（フェッチはDB書き込みなし）
    release_write_lock()

    # 並列フェッチ
    fetch_results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_race_data, vcode, rno, race_id, stime, do_bi): race_id
            for vcode, rno, race_id, stime, do_bi in tasks
        }
        for future in as_completed(futures):
            try:
                fetch_results.append(future.result())
            except Exception as e:
                log.warning("フェッチエラー race_id=%d: %s", futures[future], e)

    # DB書き込み前にロック再取得（odds_scraperの書き込み完了まで最大5分待機）
    acquire_write_lock(wait=True, timeout=300)

    # DB書き込み（シングルスレッド）
    oriten_candidates = []
    for data in fetch_results:
        new_bi = save_race_data(conn, data)
        if new_bi:
            oriten_candidates.append((data["vcode"], data["rno"], data["race_id"]))

    # before_infoあり・mawariashi_time未取得の補完（focused対象レースに限定）
    target_race_ids = {race_id for _, _, race_id, _ in targets}
    oriten_missing = conn.execute("""
        SELECT r.venue_code, r.race_no, r.id
        FROM races r
        WHERE r.date = ?
          AND r.id IN ({})
          AND EXISTS (
              SELECT 1 FROM before_info bi
              WHERE bi.race_id = r.id AND bi.exhibition_time IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM before_info bi
              WHERE bi.race_id = r.id AND bi.mawariashi_time IS NOT NULL
          )
        ORDER BY r.venue_code, r.race_no
    """.format(",".join("?" * len(target_race_ids))),
        (TODAY, *target_race_ids)
    ).fetchall() if target_race_ids else []

    conn.close()
    release_write_lock()

    all_oriten = list({(v, r, rid) for v, r, rid in oriten_candidates + list(oriten_missing)})
    if all_oriten:
        log.info("オリジナル展示タイム取得・補完: %d件", len(all_oriten))
        scrape_oriten_for_races(all_oriten)

    # 自動バックアップは backup_db.py (LaunchAgent, 30分おき) に委任
    # ここで30秒おきに658MBを書くのはディスクI/O過多・ロック占有率過大のため禁止

    log.info("=== 集中ポーリング 完了 ===")


if __name__ == "__main__":
    main()
