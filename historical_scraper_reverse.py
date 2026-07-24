#!/usr/bin/env python3
"""
boatrace.jp 過去データスクレイパー（逆順版）
対象期間 : 2025-12-31 〜 2021-01-01（新しい日付から収集）
取得データ: 出走表 / 確定着順 / 決まり手 / 払戻金
"""

import argparse
import re
import sqlite3
import sys
import time
import random
import logging
import signal
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from db_lock import acquire_write_lock, release_write_lock

# ── 設定 ──────────────────────────────────────────────
BASE_URL      = "https://www.boatrace.jp/owpc/pc/race"
DB_PATH       = Path(__file__).parent / "boatai.db"
LOG_DIR       = Path(__file__).parent / "logs"
START_DATE    = date.today() - timedelta(days=1)  # 常に昨日から開始（逆順）
END_DATE      = date(2021,  1,  1)   # 逆順なので古い日付が終了
REQ_DELAY_MIN = 1.0
REQ_DELAY_MAX = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

VENUE_NAMES: dict[str, str] = {
    "01": "桐生",   "02": "戸田",   "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡",   "08": "常滑",
    "09": "津",     "10": "三国",   "11": "びわこ", "12": "住之江",
    "13": "尼崎",   "14": "鳴門",   "15": "丸亀",   "16": "児島",
    "17": "宮島",   "18": "徳山",   "19": "下関",   "20": "若松",
    "21": "芦屋",   "22": "福岡",   "23": "唐津",   "24": "大村",
}

_ZEN_NUM = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}

# ── グローバル状態 ─────────────────────────────────────
_stop = False
_session = requests.Session()
_session.headers.update(HEADERS)
log: logging.Logger = logging.getLogger(__name__)


# ── シグナルハンドラ ──────────────────────────────────
def _signal_handler(sig, frame):
    global _stop
    _stop = True
    print("\n[中断受信] 現在の会場処理が完了したら停止します。次回実行で続きから再開できます。",
          flush=True)


signal.signal(signal.SIGINT, _signal_handler)


# ── DB ロックリトライ ─────────────────────────────────
def _retry(fn, max_wait: int = 60):
    """database is locked エラー時に最大max_wait秒リトライする"""
    deadline = time.monotonic() + max_wait
    delay = 1.0
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e) or time.monotonic() >= deadline:
                raise
            log.warning("DB locked — %.0f秒後リトライ...", delay)
            time.sleep(delay)
            delay = min(delay * 2, 10)


# ── ロギング設定 ──────────────────────────────────────
def _setup_logging() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"historical_rev_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return log_file


# ── HTTP ─────────────────────────────────────────────
def fetch(url: str, params: dict | None = None) -> BeautifulSoup | None:
    if _stop:
        return None
    try:
        resp = _session.get(url, params=params, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        time.sleep(random.uniform(REQ_DELAY_MIN, REQ_DELAY_MAX))
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.warning("取得失敗 %s %s: %s", url, params, e)
        time.sleep(REQ_DELAY_MIN)
        return None


# ── 変換ユーティリティ ────────────────────────────────
def _float(s) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).strip())
    except (ValueError, AttributeError):
        return None


def _int(s) -> int | None:
    if s is None:
        return None
    try:
        return int(str(s).strip())
    except (ValueError, AttributeError):
        return None


def _zen_to_int(s) -> int | None:
    if s is None:
        return None
    return _ZEN_NUM.get(str(s).strip(), _int(s))


# ── DB 初期化 ─────────────────────────────────────────
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS venues (
            venue_code TEXT PRIMARY KEY,
            venue_name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS races (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT    NOT NULL,
            venue_code TEXT    NOT NULL,
            race_no    INTEGER NOT NULL,
            race_title TEXT,
            UNIQUE (date, venue_code, race_no),
            FOREIGN KEY (venue_code) REFERENCES venues(venue_code)
        );

        CREATE TABLE IF NOT EXISTS entries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id             INTEGER NOT NULL,
            boat_no             INTEGER NOT NULL,
            player_no           TEXT,
            player_name         TEXT,
            player_class        TEXT,
            age                 INTEGER,
            weight              REAL,
            flying_count        INTEGER,
            late_count          INTEGER,
            avg_start_timing    REAL,
            national_win_rate   REAL,
            national_2ring_rate REAL,
            local_win_rate      REAL,
            local_2ring_rate    REAL,
            motor_no            INTEGER,
            motor_2ring_rate    REAL,
            boat_no_hull        INTEGER,
            boat_2ring_rate     REAL,
            UNIQUE (race_id, boat_no),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        CREATE TABLE IF NOT EXISTS odds_3t (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL,
            combination TEXT    NOT NULL,
            odds        REAL,
            UNIQUE (race_id, combination),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        CREATE TABLE IF NOT EXISTS course_stats (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            player_no    TEXT    NOT NULL,
            fetched_date TEXT    NOT NULL,
            course_no    INTEGER NOT NULL,
            entry_rate   REAL,
            win_rate_1st REAL,
            win_rate_2nd REAL,
            win_rate_3rd REAL,
            avg_st       REAL,
            UNIQUE (player_no, fetched_date, course_no)
        );

        CREATE TABLE IF NOT EXISTS before_info (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id            INTEGER NOT NULL,
            boat_no            INTEGER NOT NULL,
            weight             REAL,
            exhibition_time    REAL,
            tilt               REAL,
            exhibit_course     INTEGER,
            exhibit_st         TEXT,
            prev_race_venue    TEXT,
            prev_race_date     TEXT,
            prev_race_no       INTEGER,
            prev_entry_course  INTEGER,
            prev_start_timing  TEXT,
            prev_finish        INTEGER,
            UNIQUE (race_id, boat_no),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        CREATE TABLE IF NOT EXISTS weather (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       INTEGER NOT NULL,
            temperature   REAL,
            weather_desc  TEXT,
            wind_speed    REAL,
            wind_direction INTEGER,
            water_temp    REAL,
            wave_height   REAL,
            UNIQUE (race_id),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        CREATE TABLE IF NOT EXISTS race_result_entries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       INTEGER NOT NULL,
            rank          INTEGER NOT NULL,
            boat_no       INTEGER,
            player_no     TEXT,
            player_name   TEXT,
            race_time     TEXT,
            start_course  INTEGER,
            start_timing  TEXT,
            winning_trick TEXT,
            UNIQUE (race_id, rank),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        CREATE TABLE IF NOT EXISTS payouts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL,
            bet_type    TEXT    NOT NULL,
            combination TEXT,
            payout      INTEGER,
            popularity  INTEGER,
            UNIQUE (race_id, bet_type, combination),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        CREATE TABLE IF NOT EXISTS meet_standings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,
            venue_code    TEXT NOT NULL,
            standing_rank INTEGER,
            player_no     TEXT,
            player_name   TEXT,
            player_class  TEXT,
            points_rate   REAL,
            results_text  TEXT,
            total_points  INTEGER,
            deductions    INTEGER,
            UNIQUE (date, venue_code, player_no),
            FOREIGN KEY (venue_code) REFERENCES venues(venue_code)
        );

        CREATE TABLE IF NOT EXISTS scraped_history (
            date       TEXT NOT NULL,
            venue_code TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            PRIMARY KEY (date, venue_code)
        );
    """)
    conn.commit()


# ── DB 書き込みヘルパー ──────────────────────────────
def _upsert_venue(conn: sqlite3.Connection, code: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO venues (venue_code, venue_name) VALUES (?,?)",
        (code, VENUE_NAMES.get(code, code)),
    )


def _upsert_race(conn: sqlite3.Connection, date_str: str,
                 venue_code: str, race_no: int) -> int:
    conn.execute("""
        INSERT INTO races (date, venue_code, race_no, race_title)
        VALUES (?,?,?,?)
        ON CONFLICT(date, venue_code, race_no) DO NOTHING
    """, (date_str, venue_code, race_no, f"{race_no}R"))
    row = conn.execute(
        "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (date_str, venue_code, race_no),
    ).fetchone()
    return row[0]


def _save_entries(conn: sqlite3.Connection, race_id: int, entries: list[dict]) -> None:
    for e in entries:
        conn.execute("""
            INSERT INTO entries
              (race_id,boat_no,player_no,player_name,player_class,age,weight,
               flying_count,late_count,avg_start_timing,
               national_win_rate,national_2ring_rate,local_win_rate,local_2ring_rate,
               motor_no,motor_2ring_rate,boat_no_hull,boat_2ring_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(race_id,boat_no) DO UPDATE SET
              player_no=excluded.player_no, player_name=excluded.player_name,
              player_class=excluded.player_class, age=excluded.age, weight=excluded.weight,
              flying_count=excluded.flying_count, late_count=excluded.late_count,
              avg_start_timing=excluded.avg_start_timing,
              national_win_rate=excluded.national_win_rate,
              national_2ring_rate=excluded.national_2ring_rate,
              local_win_rate=excluded.local_win_rate, local_2ring_rate=excluded.local_2ring_rate,
              motor_no=excluded.motor_no, motor_2ring_rate=excluded.motor_2ring_rate,
              boat_no_hull=excluded.boat_no_hull, boat_2ring_rate=excluded.boat_2ring_rate
        """, (race_id, e["boat_no"], e["player_no"], e["player_name"], e["player_class"],
              e["age"], e["weight"], e["flying_count"], e["late_count"], e["avg_start_timing"],
              e["national_win_rate"], e["national_2ring_rate"],
              e["local_win_rate"], e["local_2ring_rate"],
              e["motor_no"], e["motor_2ring_rate"], e["boat_no_hull"], e["boat_2ring_rate"]))


def _save_race_result_entries(conn: sqlite3.Connection, race_id: int,
                               entries: list[dict]) -> None:
    for e in entries:
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


def _save_payouts(conn: sqlite3.Connection, race_id: int, payouts: list[dict]) -> None:
    for p in payouts:
        conn.execute("""
            INSERT INTO payouts (race_id,bet_type,combination,payout,popularity)
            VALUES (?,?,?,?,?)
            ON CONFLICT(race_id,bet_type,combination) DO UPDATE SET
              payout=excluded.payout, popularity=excluded.popularity
        """, (race_id, p["bet_type"], p["combination"], p["payout"], p["popularity"]))


# ── パーサー ─────────────────────────────────────────
def parse_entries(soup: BeautifulSoup) -> list[dict]:
    results = []
    for tbody in soup.select("tbody.is-fs12"):
        trs = tbody.find_all("tr")
        if not trs:
            continue
        first_row_tds = trs[0].find_all("td")
        if len(first_row_tds) < 8:
            continue

        raw = first_row_tds[0].get_text(strip=True)
        boat_no = _ZEN_NUM.get(raw, _int(raw))
        if boat_no is None:
            continue

        info_td = first_row_tds[2]
        divs = info_td.find_all("div")
        player_no = player_class = player_name = age = weight = None
        if divs:
            m = re.match(r"(\d+)\s*/\s*(\S+)", divs[0].get_text(strip=True))
            if m:
                player_no, player_class = m.group(1), m.group(2)
            if len(divs) > 1:
                player_name = divs[1].get_text(strip=True)
            if len(divs) > 2:
                m2 = re.search(r"(\d+)歳/(\d+\.?\d*)kg", divs[2].get_text(strip=True))
                if m2:
                    age, weight = _int(m2.group(1)), _float(m2.group(2))

        fl_texts = [s.strip() for s in first_row_tds[3].get_text("\n").split("\n") if s.strip()]
        flying = late = avg_st = None
        if fl_texts:
            m = re.match(r"F(\d+)", fl_texts[0])
            flying = _int(m.group(1)) if m else None
        if len(fl_texts) > 1:
            m = re.match(r"L(\d+)", fl_texts[1])
            late = _int(m.group(1)) if m else None
        if len(fl_texts) > 2:
            avg_st = _float(fl_texts[2])

        def _triple(idx):
            vals = [s.strip() for s in first_row_tds[idx].get_text("\n").split("\n") if s.strip()]
            return (_float(vals[0]) if len(vals) > 0 else None,
                    _float(vals[1]) if len(vals) > 1 else None,
                    _float(vals[2]) if len(vals) > 2 else None)

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
    finish_entries: list[dict] = []
    start_info: dict[int, dict] = {}
    payouts: list[dict] = []

    result_table = next(
        (t for t in tables if t.find("th", string="着")),
        None
    )
    if result_table:
        for tbody in result_table.find_all("tbody"):
            tr = tbody.find("tr")
            if not tr:
                continue
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
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
        (t for t in tables if "is-h292__3rdadd" in " ".join(t.get("class", []))),
        None
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
            td = t.find("td")
            winning_trick = td.get_text(strip=True) if td else None
            break

    boat_map = {e["boat_no"]: e for e in finish_entries if e["boat_no"]}
    for cpos, info in start_info.items():
        bn = info["boat_no"]
        if bn and bn in boat_map:
            boat_map[bn]["start_course"] = cpos
            boat_map[bn]["start_timing"] = info["st"]

    for e in finish_entries:
        if e["rank"] == 1:
            e["winning_trick"] = winning_trick
            break

    payout_table = next(
        (t for t in tables if t.find("th", string="勝式")),
        None
    )
    if payout_table:
        for tbody in payout_table.find_all("tbody"):
            trs = tbody.find_all("tr")
            if not trs:
                continue
            bet_td = trs[0].find("td", attrs={"rowspan": True})
            bet_type = bet_td.get_text(strip=True) if bet_td else None

            for tr in trs:
                combo_div = tr.find("div", class_="numberSet1")
                if not combo_div:
                    continue
                nums = [s.get_text(strip=True)
                        for s in combo_div.find_all("span", class_="numberSet1_number")]
                seps = combo_div.find_all("span", class_="numberSet1_text")
                sep = "-" if seps and seps[0].get_text(strip=True) == "-" else "="
                combination = sep.join(nums) if nums else None

                payout_span = tr.find("span", class_="is-payout1")
                payout_text = payout_span.get_text(strip=True) if payout_span else ""
                payout_amount = None
                m = re.search(r"[\d,]+", payout_text.replace("¥", ""))
                if m:
                    payout_amount = int(m.group(0).replace(",", ""))

                pop = None
                for td in tr.find_all("td"):
                    v = td.get_text(strip=True)
                    if v.isdigit() and td != bet_td:
                        pop = int(v)
                        break

                if combination and bet_type:
                    payouts.append({
                        "bet_type": bet_type, "combination": combination,
                        "payout": payout_amount, "popularity": pop,
                    })

    return finish_entries, payouts


# ── 開催会場・レース数取得 ────────────────────────────
def fetch_venues_for_date(date_str: str) -> list[str]:
    soup = fetch(f"{BASE_URL}/index", params={"hd": date_str})
    if not soup:
        return []
    pattern = re.compile(r"jcd=(\d{2})&hd=" + date_str)
    seen: set[str] = set()
    for a in soup.find_all("a", href=pattern):
        m = pattern.search(a["href"])
        if m:
            seen.add(m.group(1))
    return sorted(seen)


def fetch_max_race_no(venue_code: str, date_str: str) -> int:
    soup = fetch(f"{BASE_URL}/raceindex", params={"jcd": venue_code, "hd": date_str})
    if not soup:
        return 12
    race_nos: set[int] = set()
    for a in soup.find_all("a", href=re.compile(r"rno=\d+")):
        m = re.search(r"rno=(\d+)", a["href"])
        if m:
            race_nos.add(int(m.group(1)))
    return max(race_nos) if race_nos else 12


# ── 1会場処理 ──────────────────────────────────────
def process_venue(conn: sqlite3.Connection, date_str: str, vcode: str) -> bool:
    # venue登録後すぐコミットしてロックを解放（HTTPリクエスト前に必ず解放）
    _retry(lambda: _upsert_venue(conn, vcode))
    _retry(lambda: conn.commit())

    max_rno = fetch_max_race_no(vcode, date_str)  # HTTP（ロックなし）
    if _stop:
        return False

    vname = VENUE_NAMES.get(vcode, vcode)

    for rno in range(1, max_rno + 1):
        if _stop:
            return False

        # race登録後すぐコミット
        race_id = _retry(lambda: _upsert_race(conn, date_str, vcode, rno))
        _retry(lambda: conn.commit())

        # 出走表（HTTP → 書き込み → コミット）
        soup = fetch(f"{BASE_URL}/racelist",
                     params={"rno": rno, "jcd": vcode, "hd": date_str})
        if soup:
            entries = parse_entries(soup)
            _retry(lambda: _save_entries(conn, race_id, entries))
            _retry(lambda: conn.commit())

        if _stop:
            return False

        # 確定結果 + 払戻金（HTTP → 書き込み → コミット）
        soup = fetch(f"{BASE_URL}/raceresult",
                     params={"rno": rno, "jcd": vcode, "hd": date_str})
        if soup:
            res_entries, payouts = parse_race_result(soup)
            _retry(lambda: _save_race_result_entries(conn, race_id, res_entries))
            _retry(lambda: _save_payouts(conn, race_id, payouts))
            _retry(lambda: conn.commit())

        log.debug("    %s %dR: 出走表+結果 完了", vname, rno)

    return True


# ── 進捗表示ヘルパー ─────────────────────────────────
def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f}分"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}時間"
    return f"{seconds / 86400:.1f}日"


# ── メイン ────────────────────────────────────────────
def _main_body() -> None:
    global log
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="時間制限（8:00停止）をスキップして即時実行")
    args = parser.parse_args()

    log_file = _setup_logging()
    log = logging.getLogger(__name__)

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    init_db(conn)

    # 全日付リストを逆順で生成（新しい日付 → 古い日付）
    all_dates: list[str] = []
    d = START_DATE
    while d >= END_DATE:
        all_dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    total = len(all_dates)

    # 完了済み日付をロード
    done_pairs: set[tuple[str, str]] = {
        (row[0], row[1])
        for row in conn.execute("SELECT date, venue_code FROM scraped_history")
    }
    completed_dates: set[str] = {
        row[0]
        for row in conn.execute(
            "SELECT date FROM scraped_history WHERE venue_code='DONE'"
        )
    }
    done_count = len(completed_dates)

    log.info("=" * 55)
    log.info("boatrace.jp 過去データスクレイパー（逆順版）")
    log.info("期間   : %s → %s (%d日)", START_DATE, END_DATE, total)
    log.info("取得済み: %d日 / 残り: %d日", done_count, total - done_count)
    log.info("ログ   : %s", log_file)
    log.info("Ctrl+C  : 安全に中断 → 次回実行で続きから再開")
    log.info("=" * 55)

    start_time = time.monotonic()
    done_this_run = 0

    for i, date_str in enumerate(all_dates, 1):
        if _stop:
            break

        # 8:00 以降は live_scraper / odds_scraper と DB 競合するため自動停止
        # --force フラグで強制実行中はスキップ
        if not args.force and 8 <= datetime.now().hour < 22:
            log.info("08:00〜22:00 のため自動停止します（live系スクレイパーとの競合防止）")
            log.info("手動実行時は --force オプションで継続できます")
            break

        if date_str in completed_dates:
            continue

        date_disp = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

        venues = fetch_venues_for_date(date_str)
        if _stop:
            break

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not venues:
            conn.execute(
                "INSERT OR REPLACE INTO scraped_history (date, venue_code, scraped_at) VALUES (?,?,?)",
                (date_str, "NO_RACE", now_str)
            )
            conn.execute(
                "INSERT OR REPLACE INTO scraped_history (date, venue_code, scraped_at) VALUES (?,?,?)",
                (date_str, "DONE", now_str)
            )
            conn.commit()
            done_count += 1
            done_this_run += 1
            completed_dates.add(date_str)
            log.info("%s 開催なし  (%d/%d日)", date_disp, done_count, total)
            continue

        interrupted = False
        for vcode in venues:
            if _stop:
                interrupted = True
                break
            if (date_str, vcode) in done_pairs:
                continue

            vname = VENUE_NAMES.get(vcode, vcode)
            log.info("%s [%s] 取得開始...", date_disp, vname)

            success = process_venue(conn, date_str, vcode)

            if success and not _stop:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT OR REPLACE INTO scraped_history (date, venue_code, scraped_at) VALUES (?,?,?)",
                    (date_str, vcode, now_str)
                )
                conn.commit()
                done_pairs.add((date_str, vcode))
            elif _stop:
                interrupted = True
                break

        if interrupted:
            break

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO scraped_history (date, venue_code, scraped_at) VALUES (?,?,?)",
            (date_str, "DONE", now_str)
        )
        conn.commit()
        done_count += 1
        done_this_run += 1
        completed_dates.add(date_str)

        elapsed = time.monotonic() - start_time
        remaining = total - done_count
        if done_this_run > 0:
            sec_per_day = elapsed / done_this_run
            eta_sec = sec_per_day * remaining
            eta_str = _fmt_eta(eta_sec)
        else:
            eta_str = "計算中"

        pct = done_count / total * 100
        log.info(
            "%s 完了  %d/%d日 (%.1f%%)  残り約%s",
            date_disp, done_count, total, pct, eta_str
        )

    # WALをメインDBにフラッシュしてから終了
    # TRUNCATE はDB破損の原因になるため使用禁止 → PASSIVE に変更
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        log.info("WALチェックポイント(PASSIVE)完了")
    except Exception as e:
        log.warning("WALチェックポイント失敗: %s", e)
    conn.close()
    elapsed_total = time.monotonic() - start_time

    if _stop:
        log.info("")
        log.info("中断しました。次回実行時に続きから再開されます。")
        log.info("進捗: %d/%d日 完了 (%.1f%%)", done_count, total, done_count / total * 100)
    else:
        log.info("")
        log.info("=== 全期間の取得が完了しました ===")
        log.info("総処理: %d日  所要時間: %s", done_count, _fmt_eta(elapsed_total))

    log.info("DB: %s", DB_PATH)


def main() -> None:
    acquire_write_lock(wait=True, timeout=600)
    try:
        _main_body()
    finally:
        release_write_lock()


if __name__ == "__main__":
    main()
