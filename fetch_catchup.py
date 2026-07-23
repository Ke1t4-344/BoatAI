#!/usr/bin/env python3
"""
キャッチアップスクリプト — スリープ等で取得漏れた日付のデータを一括補完

- 実在するレースのみDB登録（空レコードを作らない）
- 既取得はスキップ
- スクレイパー稼働中でも使用可（write lock 使用）

使い方:
    python3 fetch_catchup.py 20260719            # 7/19 補完
    python3 fetch_catchup.py 20260719 20260720   # 複数日
"""
import sqlite3
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    DB_PATH, BASE_URL, _tl_fetch,
    parse_entries, save_entries,
    parse_before_info, save_before_info, save_weather,
    parse_race_result, save_race_result_entries, save_payouts,
    parse_odds_3t, parse_odds_tansho, parse_odds_2t,
    save_odds, save_odds_tansho, save_odds_2t,
    save_st_history_from_result,
    VENUE_NAMES,
)
from db_lock import acquire_write_lock, release_write_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MAX_WORKERS = 6
RACE_NOS    = list(range(1, 13))   # 1〜12R


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def get_venue_list(date_str: str) -> list[str]:
    """boatrace.jp の当日TOPから開催会場コード一覧を取得"""
    soup = _tl_fetch(f"{BASE_URL}/index", params={"hd": date_str})
    if not soup:
        log.warning("  会場一覧取得失敗 (%s)", date_str)
        return []
    vcodes = []
    for a in soup.select("a[href*='jcd=']"):
        href = a.get("href", "")
        for part in href.split("&"):
            if part.startswith("jcd="):
                code = part[4:].zfill(2)
                if code not in vcodes:
                    vcodes.append(code)
    log.info("  開催会場: %s",
             " ".join(VENUE_NAMES.get(v, v) for v in vcodes) or "（なし）")
    return vcodes


def upsert_race_for_date(conn, date_str: str, vcode: str, rno: int,
                          scheduled_time=None) -> int:
    """日付指定でrace upsert（scraper.pyのupsert_raceはTODAY固定なので独自実装）"""
    conn.execute("""
        INSERT INTO races (date, venue_code, race_no, race_title, scheduled_time)
        VALUES (?,?,?,?,?)
        ON CONFLICT(date, venue_code, race_no) DO UPDATE SET
            scheduled_time = COALESCE(excluded.scheduled_time, scheduled_time)
    """, (date_str, vcode, rno, f"{rno}R", scheduled_time))
    row = conn.execute(
        "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (date_str, vcode, rno),
    ).fetchone()
    return row[0]


def fetch_one_race(date_str: str, vcode: str, rno: int,
                   has_entries: bool, has_before: bool,
                   has_result: bool, race_id_or_none) -> dict:
    """1レース分をHTTP取得。出走表がなければ実在しないレースとしてNoneを返す"""
    params = {"rno": rno, "jcd": vcode, "hd": date_str}

    # 出走表は必ずフェッチ（race_id確定のため & 実在確認）
    soup_list = _tl_fetch(f"{BASE_URL}/racelist", params=params) if not has_entries else None
    entries = parse_entries(soup_list) if soup_list else []

    # 出走表なし & 既取得もなし → レース自体が存在しない
    if not entries and not has_entries:
        return {"exists": False, "vcode": vcode, "rno": rno}

    soup_before = _tl_fetch(f"{BASE_URL}/beforeinfo", params=params) if not has_before else None
    soup_result = _tl_fetch(f"{BASE_URL}/raceresult", params=params) if not has_result  else None
    soup_odds3  = _tl_fetch(f"{BASE_URL}/odds3t",     params=params)
    soup_tansho = _tl_fetch(f"{BASE_URL}/oddstkf",    params=params)
    soup_2t     = _tl_fetch(f"{BASE_URL}/odds2tf",    params=params)

    return {
        "exists": True,
        "vcode": vcode, "rno": rno,
        "race_id": race_id_or_none,
        "entries":     entries,
        "before":      parse_before_info(soup_before)   if soup_before else ([], {}),
        "result":      parse_race_result(soup_result)   if soup_result else ([], []),
        "odds_3t":     parse_odds_3t(soup_odds3)        if soup_odds3  else {},
        "odds_tansho": parse_odds_tansho(soup_tansho)   if soup_tansho else {},
        "odds_2t":     parse_odds_2t(soup_2t)           if soup_2t     else {},
    }


def process_date(date_str: str):
    log.info("=" * 55)
    log.info("日付: %s", date_str)

    # ── Phase 1: 会場一覧 ──────────────────────────────────────────
    vcodes = get_venue_list(date_str)
    if not vcodes:
        log.warning("  開催会場なし。スキップ")
        return

    # ── Phase 2: DB現状確認（ロック保持・読み取りのみ）─────────────
    acquire_write_lock(wait=True, timeout=300)
    conn = open_db()

    fetch_targets = []     # (vcode, rno, race_id_or_none, has_e, has_b, has_r)
    skip_count = 0

    for vcode in vcodes:
        for rno in RACE_NOS:
            row = conn.execute(
                "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
                (date_str, vcode, rno)
            ).fetchone()
            race_id = row[0] if row else None

            if race_id:
                has_e = conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE race_id=?", (race_id,)
                ).fetchone()[0] > 0
                has_b = conn.execute(
                    "SELECT COUNT(*) FROM before_info WHERE race_id=?", (race_id,)
                ).fetchone()[0] > 0
                has_r = conn.execute(
                    "SELECT COUNT(*) FROM race_result_entries WHERE race_id=?", (race_id,)
                ).fetchone()[0] > 0
            else:
                has_e = has_b = has_r = False

            if has_e and has_b and has_r:
                skip_count += 1
            else:
                fetch_targets.append((vcode, rno, race_id, has_e, has_b, has_r))

    conn.close()
    release_write_lock()

    log.info("  取得対象: %d レース / スキップ（取得済み）: %d", len(fetch_targets), skip_count)
    if not fetch_targets:
        log.info("  全データ取得済み")
        return

    # ── Phase 3: 並列HTTPフェッチ（ロック解放中）──────────────────
    fetched: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fetch_one_race, date_str, vcode, rno, has_e, has_b, has_r, race_id): (vcode, rno)
            for vcode, rno, race_id, has_e, has_b, has_r in fetch_targets
        }
        done = 0
        for future in as_completed(futures):
            try:
                result = future.result()
                fetched.append(result)
            except Exception as e:
                vcode, rno = futures[future]
                log.warning("  フェッチ失敗 %s %dR: %s",
                            VENUE_NAMES.get(vcode, vcode), rno, e)
            done += 1
            if done % 24 == 0:
                log.info("  HTTPフェッチ進捗: %d/%d", done, len(fetch_targets))

    # ── Phase 4: DB一括書き込み（ロック再取得）────────────────────
    acquire_write_lock(wait=True, timeout=300)
    conn = open_db()

    saved = {"races": 0, "entries": 0, "before": 0, "result": 0, "odds": 0}
    not_exist = 0

    for data in fetched:
        if not data.get("exists"):
            not_exist += 1
            continue

        vcode   = data["vcode"]
        rno     = data["rno"]
        vname   = VENUE_NAMES.get(vcode, vcode)
        race_id = data.get("race_id")

        # 出走表があれば race レコードを確定（scheduled_timeは出走表から取れないのでNULL）
        if data["entries"] and race_id is None:
            race_id = upsert_race_for_date(conn, date_str, vcode, rno)
            conn.commit()
            saved["races"] += 1

        if race_id is None:
            # 既存race_idもなく出走表も取れなかった → スキップ
            continue

        if data["entries"]:
            save_entries(conn, race_id, data["entries"])
            conn.commit()
            saved["entries"] += 1
            log.info("  %s %dR: 出走表 %d艇", vname, rno, len(data["entries"]))

        bi_entries, weather = data["before"]
        if bi_entries:
            save_before_info(conn, race_id, bi_entries)
            save_weather(conn, race_id, weather)
            conn.commit()
            saved["before"] += 1

        res_entries, payouts = data["result"]
        if res_entries:
            save_race_result_entries(conn, race_id, res_entries)
            save_payouts(conn, race_id, payouts)
            save_st_history_from_result(conn, race_id, date_str, vcode, rno, res_entries)
            conn.commit()
            saved["result"] += 1
            log.info("  %s %dR: 結果 %s",
                     vname, rno,
                     "-".join(str(e["boat_no"]) for e in res_entries[:3]))

        if data["odds_3t"]:
            save_odds(conn, race_id, data["odds_3t"])
            conn.commit()
            saved["odds"] += 1
        if data["odds_tansho"]:
            save_odds_tansho(conn, race_id, data["odds_tansho"])
            conn.commit()
        if data["odds_2t"]:
            save_odds_2t(conn, race_id, data["odds_2t"])
            conn.commit()

    conn.close()
    release_write_lock()

    log.info("  保存 → レース登録:%d 出走表:%d 前情:%d 結果:%d オッズ:%d (存在しないR:%d)",
             saved["races"], saved["entries"], saved["before"],
             saved["result"], saved["odds"], not_exist)


def main():
    if len(sys.argv) < 2:
        print("使い方: python3 fetch_catchup.py YYYYMMDD [YYYYMMDD ...]")
        sys.exit(1)

    for date_str in sys.argv[1:]:
        if len(date_str) != 8 or not date_str.isdigit():
            log.error("日付形式エラー: %s", date_str)
            continue
        process_date(date_str)

    log.info("=== 全日付キャッチアップ完了 ===")


if __name__ == "__main__":
    main()
