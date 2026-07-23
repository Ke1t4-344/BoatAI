#!/usr/bin/env python3
"""
biyori_scraper.py — 競艇日和から過去レースの展示情報を取得

URL例: https://kyoteibiyori.com/race_shusso.php?place_no=2&race_no=1&hiduke=20240601&slider=4

実行:
    python3 biyori_scraper.py                      # 展示なしレースを全件取得
    python3 biyori_scraper.py --from 20240101      # 指定日以降
    python3 biyori_scraper.py --from 20240101 --to 20241231
    python3 biyori_scraper.py --venue 02           # 特定会場のみ
    python3 biyori_scraper.py --limit 500          # 上限件数
    python3 biyori_scraper.py --dry-run            # DB書き込みなし（動作確認用）
"""

import sqlite3
import time
import argparse
import random
import re
import logging
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

DB_PATH = Path(__file__).parent / "boatai.db"
BASE_URL = "https://kyoteibiyori.com/race_shusso.php"

# venue_code (2桁) → place_no (整数)
VENUE_TO_PLACE = {
    "01": 1,   # 桐生
    "02": 2,   # 戸田
    "03": 3,   # 江戸川
    "04": 4,   # 平和島
    "05": 5,   # 多摩川
    "06": 6,   # 浜名湖
    "07": 7,   # 蒲郡
    "08": 8,   # 常滑
    "09": 9,   # 津
    "10": 10,  # 三国
    "11": 11,  # びわこ
    "12": 12,  # 住之江
    "13": 13,  # 尼崎
    "14": 14,  # 鳴門
    "15": 15,  # 丸亀
    "16": 16,  # 児島
    "17": 17,  # 宮島
    "18": 18,  # 徳山
    "19": 19,  # 下関
    "20": 20,  # 若松
    "21": 21,  # 芦屋
    "22": 22,  # 福岡
    "23": 23,  # 唐津
    "24": 24,  # 大村
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("logs/biyori_scraper.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://kyoteibiyori.com/",
})


def _to_float(s: str) -> float | None:
    if not s:
        return None
    s = s.strip().replace("　", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s: str) -> int | None:
    v = _to_float(s)
    return int(v) if v is not None else None


def fetch_exhibition(place_no: int, race_no: int, hiduke: str, retries=3) -> dict | None:
    """
    1レース分の展示情報を取得。
    戻り値: {boat_no: {exhibit_course, exhibition_time, lap_time, mawariashi_time,
                        straight_time, exhibit_st, weight, tilt}, ...}
    失敗時: None
    """
    params = {"place_no": place_no, "race_no": race_no, "hiduke": hiduke, "slider": 4}

    for attempt in range(retries):
        try:
            resp = SESSION.get(BASE_URL, params=params, timeout=15)
            if resp.status_code == 404:
                return {}   # レース自体がない
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == retries - 1:
                log.warning(f"  取得失敗 {hiduke}/{place_no}/{race_no}: {e}")
                return None
            time.sleep(3 * (attempt + 1))

    soup = BeautifulSoup(resp.text, "html.parser")
    return _parse_exhibition(soup)


def _parse_exhibition(soup: BeautifulSoup) -> dict:
    """
    展示情報テーブルをパースして boat_no → データ の辞書を返す。
    """
    result = {}

    # 「展示情報」セクションを探す
    # 競艇日和のページは複数テーブルがある。
    # 行ラベル「展示」「進入」「ST」などが含まれるテーブルを特定する。
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text()
        if "展示" in text and "進入" in text and "ST" in text:
            target_table = table
            break

    if target_table is None:
        return result

    # 行ラベル → データ行 のマッピングを構築
    row_data = {}
    for tr in target_table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        values = [c.get_text(strip=True) for c in cells[1:]]
        if label and values:
            row_data[label] = values

    # ラベルのマッピング（表記ゆれに対応）
    LABEL_MAP = {
        "進入":   "exhibit_course",
        "展示":   "exhibition_time",
        "周回":   "lap_time",
        "周り足": "mawariashi_time",
        "直線":   "straight_time",
        "ST":     "exhibit_st",
        "体重":   "weight",
        "チルト": "tilt",
    }

    # 各艇のデータを組み立て（最大6艇）
    # 進入コース行が基準（艇番1〜6 は列順ではなく進入コースで対応）
    courses_raw = row_data.get("進入", [])
    if not courses_raw:
        return result

    # 列数（最大6）
    n = min(len(courses_raw), 6)

    for col_idx in range(n):
        # 進入コースが艇番として使われることが多いが、
        # 実際は列番号が艇番（1号艇=列0、2号艇=列1...）
        boat_no = col_idx + 1

        entry = {}
        for label, field in LABEL_MAP.items():
            vals = row_data.get(label, [])
            if col_idx < len(vals):
                raw = vals[col_idx].strip()
                if field == "exhibit_course":
                    entry[field] = _to_int(raw)
                elif field == "exhibit_st":
                    # ST は ".09" のような形式
                    entry[field] = raw if raw else None
                elif field == "weight":
                    entry[field] = _to_float(raw)
                elif field == "tilt":
                    entry[field] = _to_float(raw)
                else:
                    entry[field] = _to_float(raw)
            else:
                entry[field] = None

        result[boat_no] = entry

    return result


def get_target_races(conn: sqlite3.Connection, args) -> list:
    """展示データが未取得のレースを取得"""
    where = [
        "EXISTS (SELECT 1 FROM race_result_entries rre WHERE rre.race_id=r.id AND rre.rank=1)",
        "NOT EXISTS (SELECT 1 FROM before_info bi WHERE bi.race_id=r.id AND bi.exhibition_time IS NOT NULL)",
    ]
    params = []

    if args.from_date:
        where.append("r.date >= ?")
        params.append(args.from_date)
    if args.to_date:
        where.append("r.date <= ?")
        params.append(args.to_date)
    if args.venue:
        where.append("r.venue_code = ?")
        params.append(args.venue)

    limit_sql = f"LIMIT {args.limit}" if args.limit > 0 else ""
    sql = f"""
        SELECT r.id, r.date, r.venue_code, r.race_no
        FROM races r
        WHERE {' AND '.join(where)}
        ORDER BY r.date ASC, r.venue_code, r.race_no
        {limit_sql}
    """
    return conn.execute(sql, params).fetchall()


def save_exhibition(conn: sqlite3.Connection, race_id: int,
                    data: dict, dry_run=False) -> int:
    """
    展示データを before_info テーブルに保存。
    既存行があれば展示関連列だけ UPDATE、なければ INSERT。
    戻り値: 保存した艇数
    """
    saved = 0
    for boat_no, entry in data.items():
        if not any(v is not None for v in entry.values()):
            continue

        existing = conn.execute(
            "SELECT id FROM before_info WHERE race_id=? AND boat_no=?",
            (race_id, boat_no)
        ).fetchone()

        if dry_run:
            saved += 1
            continue

        if existing:
            conn.execute("""
                UPDATE before_info SET
                    exhibit_course   = COALESCE(?, exhibit_course),
                    exhibition_time  = COALESCE(?, exhibition_time),
                    lap_time         = COALESCE(?, lap_time),
                    mawariashi_time  = COALESCE(?, mawariashi_time),
                    straight_time    = COALESCE(?, straight_time),
                    exhibit_st       = COALESCE(?, exhibit_st),
                    weight           = COALESCE(?, weight),
                    tilt             = COALESCE(?, tilt)
                WHERE race_id=? AND boat_no=?
            """, (
                entry.get("exhibit_course"), entry.get("exhibition_time"),
                entry.get("lap_time"), entry.get("mawariashi_time"),
                entry.get("straight_time"), entry.get("exhibit_st"),
                entry.get("weight"), entry.get("tilt"),
                race_id, boat_no,
            ))
        else:
            conn.execute("""
                INSERT INTO before_info
                  (race_id, boat_no, exhibit_course, exhibition_time,
                   lap_time, mawariashi_time, straight_time,
                   exhibit_st, weight, tilt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                race_id, boat_no,
                entry.get("exhibit_course"), entry.get("exhibition_time"),
                entry.get("lap_time"), entry.get("mawariashi_time"),
                entry.get("straight_time"), entry.get("exhibit_st"),
                entry.get("weight"), entry.get("tilt"),
            ))
        saved += 1

    if not dry_run and saved > 0:
        conn.commit()
    return saved


def main():
    parser = argparse.ArgumentParser(description="競艇日和から過去展示データを取得")
    parser.add_argument("--from",  dest="from_date", help="取得開始日 例: 20240101")
    parser.add_argument("--to",    dest="to_date",   help="取得終了日 例: 20241231")
    parser.add_argument("--venue", help="会場コード 例: 02")
    parser.add_argument("--limit", type=int, default=0, help="最大レース数 (0=無制限)")
    parser.add_argument("--dry-run", action="store_true", help="DB書き込みなし（動作確認）")
    parser.add_argument("--sleep",   type=float, default=1.5, help="リクエスト間隔(秒) デフォルト1.5")
    args = parser.parse_args()

    Path("logs").mkdir(exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")

    races = get_target_races(conn, args)
    total = len(races)
    log.info(f"取得対象: {total}レース {'[DRY RUN]' if args.dry_run else ''}")

    if total == 0:
        log.info("対象なし")
        conn.close()
        return

    done = ok = skip = err = 0

    for i, (race_id, date, venue_code, race_no) in enumerate(races, 1):
        place_no = VENUE_TO_PLACE.get(venue_code)
        if not place_no:
            log.warning(f"  [{i}/{total}] venue_code={venue_code} は未対応")
            skip += 1
            continue

        data = fetch_exhibition(place_no, race_no, date)

        if data is None:
            err += 1
            log.warning(f"  [{i}/{total}] {date} {venue_code} {race_no}R — 取得失敗")
        elif len(data) == 0:
            skip += 1
            log.debug(f"  [{i}/{total}] {date} {venue_code} {race_no}R — データなし")
        else:
            saved = save_exhibition(conn, race_id, data, args.dry_run)
            ok += 1
            done += 1

        if i % 50 == 0 or i == total:
            log.info(f"  進捗 [{i}/{total}] 保存:{ok} スキップ:{skip} エラー:{err}")

        # レート制限（ランダムジッター付き）
        time.sleep(args.sleep + random.uniform(0, 0.5))

    conn.close()
    log.info(f"\n=== 完了 === 保存:{ok} スキップ:{skip} エラー:{err} / 合計:{total}")


if __name__ == "__main__":
    main()
