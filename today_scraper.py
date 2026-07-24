#!/usr/bin/env python3
"""
today_scraper.py — 指定日の全会場データを高速並列取得

Usage:
    python3 today_scraper.py              # 今日
    python3 today_scraper.py 20260707     # 指定日
    python3 today_scraper.py 20260704 20260707  # 期間指定

特徴:
- HTTPフェッチを ThreadPoolExecutor で並列化（会場・レース横断）
- DB書き込みはメインスレッドでシリアル（WAL競合を防止）
- historical_scraper と scraped_history を共有するため重複取得なし
"""

import argparse
import re
import sqlite3
import sys
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 設定 ──────────────────────────────────────────────
BASE_URL  = "https://www.boatrace.jp/owpc/pc/race"
DB_PATH   = Path(__file__).parent / "boatai.db"
REQ_DELAY = 0.5        # focused_scraper より少し短め（過去データ取得）
MAX_WORKERS = 10       # 同時HTTP接続数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

VENUE_NAMES = {
    "01": "桐生",   "02": "戸田",   "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡",   "08": "常滑",
    "09": "津",     "10": "三国",   "11": "びわこ", "12": "住之江",
    "13": "尼崎",   "14": "鳴門",   "15": "丸亀",   "16": "児島",
    "17": "宮島",   "18": "徳山",   "19": "下関",   "20": "若松",
    "21": "芦屋",   "22": "福岡",   "23": "唐津",   "24": "大村",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# スレッドローカルHTTPセッション
_thread_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_thread_local, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.s = s
    return _thread_local.s


def _fetch(url: str, params: dict | None = None) -> BeautifulSoup | None:
    try:
        resp = _session().get(url, params=params, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        time.sleep(REQ_DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.warning("取得失敗 %s %s: %s", url, params, e)
        return None


# ── パーサー（historical_scraper から流用） ─────────────────────────
_ZEN_NUM = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}


def _float(s):
    try: return float(str(s).strip())
    except: return None


def _int(s):
    try: return int(str(s).strip())
    except: return None


def _zen_to_int(s):
    if s is None: return None
    return _ZEN_NUM.get(str(s).strip(), _int(s))


def parse_entries(soup: BeautifulSoup) -> list[dict]:
    results = []
    for tbody in soup.select("tbody.is-fs12"):
        trs = tbody.find_all("tr")
        if not trs: continue
        tds = trs[0].find_all("td")
        if len(tds) < 8: continue

        raw = tds[0].get_text(strip=True)
        boat_no = _ZEN_NUM.get(raw, _int(raw))
        if boat_no is None: continue

        info_td = tds[2]
        divs = info_td.find_all("div")
        player_no = player_class = player_name = age = weight = None
        if divs:
            m = re.match(r"(\d+)\s*/\s*(\S+)", divs[0].get_text(strip=True))
            if m: player_no, player_class = m.group(1), m.group(2)
            if len(divs) > 1: player_name = divs[1].get_text(strip=True)
            if len(divs) > 2:
                m2 = re.search(r"(\d+)歳/(\d+\.?\d*)kg", divs[2].get_text(strip=True))
                if m2: age, weight = _int(m2.group(1)), _float(m2.group(2))

        fl_texts = [s.strip() for s in tds[3].get_text("\n").split("\n") if s.strip()]
        flying = late = avg_st = None
        if fl_texts:
            m = re.match(r"F(\d+)", fl_texts[0]); flying = _int(m.group(1)) if m else None
        if len(fl_texts) > 1:
            m = re.match(r"L(\d+)", fl_texts[1]); late = _int(m.group(1)) if m else None
        if len(fl_texts) > 2: avg_st = _float(fl_texts[2])

        def _triple(idx):
            vals = [s.strip() for s in tds[idx].get_text("\n").split("\n") if s.strip()]
            return (_float(vals[0]) if vals else None,
                    _float(vals[1]) if len(vals) > 1 else None,
                    None)

        nat_win, nat_2ring, _ = _triple(4)
        loc_win, loc_2ring, _ = _triple(5)
        m_no_r, motor_2ring, _ = _triple(6)
        b_no_r, boat_2ring, _  = _triple(7)

        results.append({
            "boat_no": boat_no, "player_no": player_no, "player_name": player_name,
            "player_class": player_class, "age": age, "weight": weight,
            "flying_count": flying, "late_count": late, "avg_start_timing": avg_st,
            "national_win_rate": nat_win, "national_2ring_rate": nat_2ring,
            "local_win_rate": loc_win, "local_2ring_rate": loc_2ring,
            "motor_no": _int(str(int(m_no_r))) if m_no_r is not None else None,
            "motor_2ring_rate": motor_2ring,
            "boat_no_hull": _int(str(int(b_no_r))) if b_no_r is not None else None,
            "boat_2ring_rate": boat_2ring,
        })
    return results


def parse_race_result(soup: BeautifulSoup) -> tuple[list[dict], list[dict]]:
    tables = soup.find_all("table")
    finish_entries, payouts = [], []
    start_info: dict[int, dict] = {}

    result_table = next((t for t in tables if t.find("th", string="着")), None)
    if result_table:
        for tbody in result_table.find_all("tbody"):
            tr = tbody.find("tr")
            if not tr: continue
            tds = tr.find_all("td")
            if len(tds) < 3: continue
            rank = _zen_to_int(tds[0].get_text(strip=True))
            classes = " ".join(tds[1].get("class", []))
            m_bn = re.search(r"is-boatColor(\d)", classes)
            boat_no = int(m_bn.group(1)) if m_bn else _int(tds[1].get_text(strip=True))
            spans = tds[2].find_all("span") if len(tds) > 2 else []
            player_no   = spans[0].get_text(strip=True) if spans else None
            player_name = spans[1].get_text(strip=True) if len(spans) > 1 else None
            race_time   = tds[3].get_text(strip=True) if len(tds) > 3 else None
            if rank:
                finish_entries.append({
                    "rank": rank, "boat_no": boat_no,
                    "player_no": player_no, "player_name": player_name,
                    "race_time": race_time, "start_course": None,
                    "start_timing": None, "winning_trick": None,
                })

    start_table = next(
        (t for t in tables if "is-h292__3rdadd" in " ".join(t.get("class", []))), None
    )
    if start_table:
        course_pos = 0
        for div in start_table.find_all("div", class_="table1_boatImage1"):
            course_pos += 1
            num_span = div.find("span", class_="table1_boatImage1Number")
            boat_no_s = _int(num_span.get_text(strip=True)) if num_span else None
            inner = div.find("span", class_="table1_boatImage1TimeInner")
            st_timing = trick = None
            if inner:
                parts = inner.get_text(strip=True).split()
                if parts:
                    st_timing = parts[0]
                    trick = parts[1] if len(parts) > 1 else None
            start_info[course_pos] = {"boat_no": boat_no_s, "st": st_timing, "trick": trick}

    winning_trick = None
    for t in tables:
        th = t.find("th")
        if th and "決まり手" in th.get_text():
            td = t.find("td"); winning_trick = td.get_text(strip=True) if td else None; break

    boat_map = {e["boat_no"]: e for e in finish_entries if e["boat_no"]}
    for cpos, info in start_info.items():
        bn = info["boat_no"]
        if bn and bn in boat_map:
            boat_map[bn]["start_course"] = cpos
            boat_map[bn]["start_timing"] = info["st"]
    for e in finish_entries:
        if e["rank"] == 1: e["winning_trick"] = winning_trick; break

    payout_table = next((t for t in tables if t.find("th", string="勝式")), None)
    if payout_table:
        for tbody in payout_table.find_all("tbody"):
            trs = tbody.find_all("tr")
            if not trs: continue
            bet_td = trs[0].find("td", attrs={"rowspan": True})
            bet_type = bet_td.get_text(strip=True) if bet_td else None
            for tr in trs:
                combo_div = tr.find("div", class_="numberSet1")
                if not combo_div: continue
                nums = [s.get_text(strip=True)
                        for s in combo_div.find_all("span", class_="numberSet1_number")]
                seps = combo_div.find_all("span", class_="numberSet1_text")
                sep = "-" if seps and seps[0].get_text(strip=True) == "-" else "="
                combination = sep.join(nums) if nums else None
                payout_span = tr.find("span", class_="is-payout1")
                payout_text = payout_span.get_text(strip=True) if payout_span else ""
                m = re.search(r"[\d,]+", payout_text.replace("¥", ""))
                payout_amount = int(m.group(0).replace(",", "")) if m else None
                pop = None
                for td in tr.find_all("td"):
                    v = td.get_text(strip=True)
                    if v.isdigit() and td != bet_td: pop = int(v); break
                if combination and bet_type:
                    payouts.append({"bet_type": bet_type, "combination": combination,
                                    "payout": payout_amount, "popularity": pop})

    return finish_entries, payouts


# ── HTTP並列フェッチ ───────────────────────────────────────────────
def fetch_venues(date_str: str) -> list[str]:
    soup = _fetch(f"{BASE_URL}/index", params={"hd": date_str})
    if not soup: return []
    pattern = re.compile(r"jcd=(\d{2})&hd=" + date_str)
    seen: set[str] = set()
    for a in soup.find_all("a", href=pattern):
        m = pattern.search(a["href"])
        if m: seen.add(m.group(1))
    return sorted(seen)


def fetch_one_race(date_str: str, vcode: str, rno: int) -> dict:
    """1レース分のデータをHTTPで並列取得"""
    def _do_list():
        soup = _fetch(f"{BASE_URL}/racelist",
                      params={"rno": rno, "jcd": vcode, "hd": date_str})
        return parse_entries(soup) if soup else []

    def _do_result():
        soup = _fetch(f"{BASE_URL}/raceresult",
                      params={"rno": rno, "jcd": vcode, "hd": date_str})
        return parse_race_result(soup) if soup else ([], [])

    # 出走表と結果を並列取得
    with ThreadPoolExecutor(max_workers=2) as p:
        f_list   = p.submit(_do_list)
        f_result = p.submit(_do_result)
        entries       = f_list.result()
        res_entries, payouts = f_result.result()

    return {"vcode": vcode, "rno": rno,
            "entries": entries, "res_entries": res_entries, "payouts": payouts}


# ── DB書き込み ─────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def save_race_data(conn: sqlite3.Connection, date_str: str, data: dict) -> None:
    vcode = data["vcode"]
    rno   = data["rno"]

    # races
    conn.execute("""
        INSERT INTO races (date, venue_code, race_no, race_title)
        VALUES (?,?,?,?)
        ON CONFLICT(date, venue_code, race_no) DO NOTHING
    """, (date_str, vcode, rno, f"{rno}R"))
    race_id = conn.execute(
        "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (date_str, vcode, rno)
    ).fetchone()[0]

    # entries
    for e in data["entries"]:
        conn.execute("""
            INSERT INTO entries
              (race_id,boat_no,player_no,player_name,player_class,age,weight,
               flying_count,late_count,avg_start_timing,
               national_win_rate,national_2ring_rate,local_win_rate,local_2ring_rate,
               motor_no,motor_2ring_rate,boat_no_hull,boat_2ring_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(race_id,boat_no) DO UPDATE SET
              player_no=excluded.player_no, player_name=excluded.player_name,
              motor_no=excluded.motor_no, motor_2ring_rate=excluded.motor_2ring_rate,
              boat_no_hull=excluded.boat_no_hull, boat_2ring_rate=excluded.boat_2ring_rate
        """, (race_id, e["boat_no"], e["player_no"], e["player_name"], e["player_class"],
              e["age"], e["weight"], e["flying_count"], e["late_count"], e["avg_start_timing"],
              e["national_win_rate"], e["national_2ring_rate"],
              e["local_win_rate"], e["local_2ring_rate"],
              e["motor_no"], e["motor_2ring_rate"], e["boat_no_hull"], e["boat_2ring_rate"]))

    # race_result_entries
    for e in data["res_entries"]:
        conn.execute("""
            INSERT INTO race_result_entries
              (race_id,rank,boat_no,player_no,player_name,race_time,
               start_course,start_timing,winning_trick)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(race_id,rank) DO UPDATE SET
              boat_no=excluded.boat_no, player_no=excluded.player_no,
              player_name=excluded.player_name, race_time=excluded.race_time,
              start_course=excluded.start_course, start_timing=excluded.start_timing,
              winning_trick=excluded.winning_trick
        """, (race_id, e["rank"], e["boat_no"], e["player_no"], e["player_name"],
              e["race_time"], e["start_course"], e["start_timing"], e["winning_trick"]))

    # payouts
    for p in data["payouts"]:
        conn.execute("""
            INSERT INTO payouts (race_id,bet_type,combination,payout,popularity)
            VALUES (?,?,?,?,?)
            ON CONFLICT(race_id,bet_type,combination) DO UPDATE SET
              payout=excluded.payout, popularity=excluded.popularity
        """, (race_id, p["bet_type"], p["combination"], p["payout"], p["popularity"]))

    conn.commit()


# ── メイン ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="指定日の全データを高速並列取得")
    parser.add_argument("start", nargs="?", default=date.today().strftime("%Y%m%d"),
                        help="取得開始日 YYYYMMDD（省略時: 今日）")
    parser.add_argument("end", nargs="?",
                        help="取得終了日 YYYYMMDD（省略時: start と同日）")
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, "%Y%m%d").date()
    end_date   = datetime.strptime(args.end, "%Y%m%d").date() if args.end else start_date

    # ── DB書き込みロック取得（他スクレイパーとの同時書き込み防止） ──
    from db_lock import check_and_acquire, acquire_write_lock, release_write_lock
    check_and_acquire("today_scraper.py")

    conn = open_db()
    conn.execute("INSERT OR IGNORE INTO venues (venue_code, venue_name) VALUES (?,?)",
                 ("00", "dummy"))  # venues テーブル初期化保証
    for code, name in VENUE_NAMES.items():
        conn.execute("INSERT OR IGNORE INTO venues (venue_code, venue_name) VALUES (?,?)",
                     (code, name))
    conn.commit()

    # 完了済みペア
    done_pairs = {
        (r[0], r[1])
        for r in conn.execute("SELECT date, venue_code FROM scraped_history").fetchall()
    }

    d = start_date
    while d <= end_date:
        date_str  = d.strftime("%Y%m%d")
        date_disp = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

        # 既に全完了かチェック
        if (date_str, "DONE") in done_pairs:
            log.info("%s: 取得済み（スキップ）", date_disp)
            d += timedelta(days=1)
            continue

        log.info("=== %s 取得開始 ===", date_disp)
        t0 = time.monotonic()

        # 開催会場取得
        venues = fetch_venues(date_str)
        if not venues:
            log.info("%s: 開催なし", date_disp)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO scraped_history VALUES (?,?,?)",
                         (date_str, "NO_RACE", now_str))
            conn.execute("INSERT OR REPLACE INTO scraped_history VALUES (?,?,?)",
                         (date_str, "DONE", now_str))
            conn.commit()
            d += timedelta(days=1)
            continue

        log.info("開催会場: %s (%d場)",
                 " ".join(VENUE_NAMES.get(v, v) for v in venues), len(venues))

        # 全レースのフェッチタスクを生成（スキップ済み会場を除く）
        tasks = []
        for vcode in venues:
            if (date_str, vcode) in done_pairs:
                log.info("  [%s] スキップ（取得済み）", VENUE_NAMES.get(vcode, vcode))
                continue
            for rno in range(1, 13):  # 最大12R（実際のR数は結果で確認）
                tasks.append((date_str, vcode, rno))

        log.info("フェッチタスク: %d件 (MAX_WORKERS=%d)", len(tasks), MAX_WORKERS)

        # フェッチ中はロックを解放（フェッチはDB書き込みなし）
        # → focused_scraperが並行して書き込み可能になる
        release_write_lock()

        # 並列フェッチ
        results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_one_race, ds, vc, rno): (ds, vc, rno)
                for ds, vc, rno in tasks
            }
            done_count = 0
            for future in as_completed(futures):
                ds, vc, rno = futures[future]
                try:
                    data = future.result()
                    results[(vc, rno)] = data
                    done_count += 1
                    if done_count % 12 == 0:
                        log.info("  フェッチ進捗: %d/%d件", done_count, len(tasks))
                except Exception as e:
                    log.warning("フェッチ失敗 %s %dR: %s", vc, rno, e)

        # DB書き込み前にロック再取得
        acquire_write_lock(wait=True, timeout=120)
        log.info("DB書き込み中...")
        saved = 0
        for vcode in venues:
            if (date_str, vcode) in done_pairs:
                continue
            for rno in range(1, 13):
                data = results.get((vcode, rno))
                if data and (data["entries"] or data["res_entries"]):
                    save_race_data(conn, date_str, data)
                    saved += 1
            # 会場完了マーク
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO scraped_history VALUES (?,?,?)",
                         (date_str, vcode, now_str))
            conn.commit()

        # 日付完了マーク
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT OR REPLACE INTO scraped_history VALUES (?,?,?)",
                     (date_str, "DONE", now_str))
        conn.commit()

        elapsed = time.monotonic() - t0
        log.info("=== %s 完了: %d件保存 (%.1f秒) ===", date_disp, saved, elapsed)

        d += timedelta(days=1)

    # WALフラッシュ（PASSIVE: 安全。TRUNCATEは他プロセスの読み取りと競合しDB破損の原因になるため禁止）
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass

    # 自動バックアップ（完了後）
    from db_backup import backup_db
    backup_db(conn, label="today")

    conn.close()
    release_write_lock()
    log.info("全処理完了")


if __name__ == "__main__":
    main()
