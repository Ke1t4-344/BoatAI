#!/usr/bin/env python3
"""
biyori_scraper_pw.py — 競艇日和から過去レースの展示情報を取得（Playwright版）

競艇日和はJavaScriptで展示データを動的ロードするため、
ヘッドレスブラウザ（Playwright）でm_Chokuzen変数を取得する。

事前準備:
    pip install playwright
    playwright install chromium

実行:
    python3 biyori_scraper_pw.py                       # 全件
    python3 biyori_scraper_pw.py --from 20210101 --to 20211231
    python3 biyori_scraper_pw.py --venue 02 --limit 100
    python3 biyori_scraper_pw.py --dry-run             # DB書き込みなし
"""

import sqlite3
import time
import argparse
import logging
import json
import random
from pathlib import Path

DB_PATH = Path(__file__).parent / "boatai.db"
BASE_URL = "https://kyoteibiyori.com/race_shusso.php"

VENUE_TO_PLACE = {
    "01": 1, "02": 2, "03": 3, "04": 4, "05": 5, "06": 6,
    "07": 7, "08": 8, "09": 9, "10": 10, "11": 11, "12": 12,
    "13": 13, "14": 14, "15": 15, "16": 16, "17": 17, "18": 18,
    "19": 19, "20": 20, "21": 21, "22": 22, "23": 23, "24": 24,
}

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("logs/biyori_pw.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)


def _to_float(s) -> float | None:
    if s is None: return None
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None


def parse_chokuzen(chokuzen_list: list) -> dict:
    """
    m_Chokuzen の配列から boat_no → データ の辞書を構築。
    列は course 順（1コース=1号艇 とは限らない）。
    """
    result = {}
    for i, entry in enumerate(chokuzen_list):
        boat_no = i + 1  # 列順が艇番（1〜6）
        result[boat_no] = {
            "exhibit_course":   entry.get("course"),
            "exhibition_time":  _to_float(entry.get("display")),
            "exhibit_st":       str(entry.get("start", "")).strip() or None,
            "weight":           _to_float(str(entry.get("taiju", "")).replace("kg", "")),
            "tilt":             _to_float(entry.get("chiruto")),
            "lap_time":         _to_float(entry.get("shukai")),
            "mawariashi_time":  _to_float(entry.get("mawariashi")),
            "straight_time":    _to_float(entry.get("chokusen")),
        }
    return result


def save_exhibition(conn: sqlite3.Connection, race_id: int, data: dict, dry_run=False) -> int:
    saved = 0
    for boat_no, entry in data.items():
        if not any(v is not None for v in entry.values()):
            continue
        if dry_run:
            saved += 1
            continue
        existing = conn.execute(
            "SELECT id FROM before_info WHERE race_id=? AND boat_no=?",
            (race_id, boat_no)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE before_info SET
                    exhibit_course  = COALESCE(?, exhibit_course),
                    exhibition_time = COALESCE(?, exhibition_time),
                    exhibit_st      = COALESCE(?, exhibit_st),
                    weight          = COALESCE(?, weight),
                    tilt            = COALESCE(?, tilt),
                    lap_time        = COALESCE(?, lap_time),
                    mawariashi_time = COALESCE(?, mawariashi_time),
                    straight_time   = COALESCE(?, straight_time)
                WHERE race_id=? AND boat_no=?
            """, (
                entry["exhibit_course"], entry["exhibition_time"],
                entry["exhibit_st"], entry["weight"], entry["tilt"],
                entry["lap_time"], entry["mawariashi_time"], entry["straight_time"],
                race_id, boat_no,
            ))
        else:
            conn.execute("""
                INSERT INTO before_info
                  (race_id, boat_no, exhibit_course, exhibition_time,
                   exhibit_st, weight, tilt, lap_time, mawariashi_time, straight_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                race_id, boat_no,
                entry["exhibit_course"], entry["exhibition_time"],
                entry["exhibit_st"], entry["weight"], entry["tilt"],
                entry["lap_time"], entry["mawariashi_time"], entry["straight_time"],
            ))
        saved += 1
    if not dry_run and saved > 0:
        conn.commit()
    return saved


def get_target_races(conn, args):
    where = [
        "EXISTS (SELECT 1 FROM race_result_entries rre WHERE rre.race_id=r.id AND rre.rank=1)",
        "NOT EXISTS (SELECT 1 FROM before_info bi WHERE bi.race_id=r.id AND bi.exhibition_time IS NOT NULL)",
    ]
    params = []
    if args.from_date:
        where.append("r.date >= ?"); params.append(args.from_date)
    if args.to_date:
        where.append("r.date <= ?"); params.append(args.to_date)
    if args.venue:
        where.append("r.venue_code = ?"); params.append(args.venue)
    limit_sql = f"LIMIT {args.limit}" if args.limit > 0 else ""
    return conn.execute(f"""
        SELECT r.id, r.date, r.venue_code, r.race_no
        FROM races r WHERE {' AND '.join(where)}
        ORDER BY r.date ASC, r.venue_code, r.race_no {limit_sql}
    """, params).fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from",    dest="from_date")
    parser.add_argument("--to",      dest="to_date")
    parser.add_argument("--venue")
    parser.add_argument("--limit",   type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep",   type=float, default=2.0, help="リクエスト間隔秒 (デフォルト2.0)")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="ヘッドレスモード（デフォルトON）")
    parser.add_argument("--show-browser", action="store_true",
                        help="ブラウザを表示して実行")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")

    races = get_target_races(conn, args)
    total = len(races)
    log.info(f"取得対象: {total}レース {'[DRY RUN]' if args.dry_run else ''}")

    if total == 0:
        log.info("対象なし"); conn.close(); return

    done = ok = skip = err = 0
    headless = not args.show_browser

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            locale="ja-JP",
        )
        page = context.new_page()

        for i, (race_id, date, venue_code, race_no) in enumerate(races, 1):
            place_no = VENUE_TO_PLACE.get(venue_code)
            if not place_no:
                skip += 1; continue

            url = f"{BASE_URL}?place_no={place_no}&race_no={race_no}&hiduke={date}&slider=4"
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                # m_Chokuzen が配列として設定されるまで待つ（最大10秒）
                # null の場合もあるので Array.isArray で安全にチェック
                try:
                    page.wait_for_function(
                        "Array.isArray(m_Chokuzen) && m_Chokuzen.length > 0",
                        timeout=10000
                    )
                    chokuzen = page.evaluate("m_Chokuzen")
                except Exception:
                    # タイムアウト or null → データなしとしてスキップ
                    chokuzen = None
            except Exception as e:
                err += 1
                log.warning(f"  [{i}/{total}] {date} {venue_code} {race_no}R — エラー: {e}")
                time.sleep(3)
                continue

            if not chokuzen:
                skip += 1
                log.debug(f"  [{i}/{total}] {date} {venue_code} {race_no}R — データなし")
            else:
                data = parse_chokuzen(chokuzen)
                saved = save_exhibition(conn, race_id, data, args.dry_run)
                ok += 1
                done += 1

            if i % 50 == 0 or i == total:
                log.info(f"  進捗 [{i}/{total}] 保存:{ok} スキップ:{skip} エラー:{err}")

            time.sleep(args.sleep + random.uniform(0, 0.5))

        browser.close()

    conn.close()
    log.info(f"\n=== 完了 === 保存:{ok} スキップ:{skip} エラー:{err} / 合計:{total}")


if __name__ == "__main__":
    main()
