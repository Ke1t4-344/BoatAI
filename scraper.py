#!/usr/bin/env python3
"""
boatrace.jp スクレイパー
本日の全会場のレース情報を boatai.db に保存する

取得対象:
  - レース一覧・出走表・3連単オッズ
  - コース別成績（選手ごと）
  - 直前情報（展示タイム・チルト・スタート展示・前走成績）
  - 気象・水面情報
  - 確定結果（着順・決まり手・払戻金）
  - 今節成績
"""

import os
import re
import sqlite3
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone, timedelta

# Railway (UTC) でも正しい JST 日付・時刻を使うために明示
_JST = timezone(timedelta(hours=9))
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from db_lock import acquire_write_lock, release_write_lock

# ── 設定 ──────────────────────────────────────────────
BASE_URL  = "https://www.boatrace.jp/owpc/pc/race"
DATA_URL  = "https://www.boatrace.jp/owpc/pc/data/racersearch"
DB_PATH   = Path(__file__).parent / "boatai.db"
TODAY     = datetime.now(_JST).strftime("%Y%m%d")  # JST 当日日付
REQ_DELAY = 0.8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

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

_ZEN_NUM = {"１":1,"２":2,"３":3,"４":4,"５":5,"６":6}


# ── DB 初期化 ────────────────────────────────────────
def _ensure_column(conn: sqlite3.Connection, table: str, col: str, col_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS venues (
            venue_code TEXT PRIMARY KEY,
            venue_name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS races (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT    NOT NULL,
            venue_code     TEXT    NOT NULL,
            race_no        INTEGER NOT NULL,
            race_title     TEXT,
            scheduled_time TEXT,
            UNIQUE (date, venue_code, race_no),
            FOREIGN KEY (venue_code) REFERENCES venues(venue_code)
        );

        -- 出走表
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

        -- 3連単オッズ
        CREATE TABLE IF NOT EXISTS odds_3t (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL,
            combination TEXT    NOT NULL,
            odds        REAL,
            UNIQUE (race_id, combination),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        -- コース別成績（選手ごと）
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

        -- コース別成績フェッチ試行ログ（成功・失敗問わず記録）
        CREATE TABLE IF NOT EXISTS course_stats_log (
            player_no    TEXT NOT NULL,
            fetched_date TEXT NOT NULL,
            has_data     INTEGER NOT NULL DEFAULT 0,  -- 1=データあり 0=公式サイトにデータなし
            PRIMARY KEY (player_no, fetched_date)
        );

        -- 直前情報（レースごと・艇ごと）
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

        -- 気象・水面情報
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

        -- 確定着順（本番ST・決まり手含む）
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

        -- 払戻金
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

        -- 今節成績
        CREATE TABLE IF NOT EXISTS meet_standings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            venue_code   TEXT NOT NULL,
            standing_rank INTEGER,
            player_no    TEXT,
            player_name  TEXT,
            player_class TEXT,
            points_rate  REAL,
            results_text TEXT,
            total_points INTEGER,
            deductions   INTEGER,
            UNIQUE (date, venue_code, player_no),
            FOREIGN KEY (venue_code) REFERENCES venues(venue_code)
        );

        -- 単勝オッズ
        CREATE TABLE IF NOT EXISTS odds_tansho (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id INTEGER NOT NULL,
            boat_no INTEGER NOT NULL,
            odds    REAL,
            UNIQUE (race_id, boat_no),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        -- 2連単オッズ
        CREATE TABLE IF NOT EXISTS odds_2t (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL,
            combination TEXT    NOT NULL,
            odds        REAL,
            UNIQUE (race_id, combination),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );

        -- 選手ST履歴（STばらつき計算用）
        CREATE TABLE IF NOT EXISTS st_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            player_no    TEXT    NOT NULL,
            race_id      INTEGER NOT NULL,
            race_date    TEXT    NOT NULL,
            venue_code   TEXT,
            race_no      INTEGER,
            start_course INTEGER,
            start_timing TEXT,
            finish_rank  INTEGER,
            UNIQUE (player_no, race_id),
            FOREIGN KEY (race_id) REFERENCES races(id)
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       INTEGER NOT NULL UNIQUE,
            predicted_at  TEXT    NOT NULL,
            top5_combos   TEXT    NOT NULL,
            actual_combo  TEXT,
            hit_top3      INTEGER,
            hit_top5      INTEGER,
            top5_honmei   TEXT,
            top5_chuana   TEXT,
            top5_ana      TEXT,
            hit_honmei    INTEGER,
            hit_chuana    INTEGER,
            hit_ana       INTEGER,
            hit_honmei_5  INTEGER,
            hit_chuana_10 INTEGER,
            hit_ana_10    INTEGER,
            FOREIGN KEY (race_id) REFERENCES races(id)
        );
    """)

    # entries テーブルの新カラムを追加（既存DBへの後付け対応）
    _ensure_column(conn, "entries", "branch",               "TEXT")
    _ensure_column(conn, "entries", "national_3ring_rate",  "REAL")
    _ensure_column(conn, "entries", "nige_rate",            "REAL")
    _ensure_column(conn, "entries", "sashi_rate",           "REAL")
    _ensure_column(conn, "entries", "makuri_rate",          "REAL")
    _ensure_column(conn, "entries", "makuri_sashi_rate",    "REAL")
    _ensure_column(conn, "entries", "teiko_rate",           "REAL")
    _ensure_column(conn, "entries", "megumi_rate",          "REAL")

    # before_info の追加カラム（展示周回・直線タイム）
    _ensure_column(conn, "before_info", "lap_time",      "REAL")
    _ensure_column(conn, "before_info", "straight_time", "REAL")

    # predictions の絞り込み用hit列（Top5/Top10）
    _ensure_column(conn, "predictions", "hit_honmei_5",  "INTEGER")
    _ensure_column(conn, "predictions", "hit_chuana_10", "INTEGER")
    _ensure_column(conn, "predictions", "hit_ana_10",    "INTEGER")

    conn.commit()


# コース別成績の並列フェッチ数（boatrace.jpへの同時接続数を抑制）
COURSE_MAX_WORKERS = 5
MAX_RACE_WORKERS   = 12

# ── HTTP ユーティリティ ──────────────────────────────
_session = requests.Session()
_session.headers.update(HEADERS)

# スレッドローカルセッション（コース別成績の並列フェッチ用）
_tl = threading.local()


def _get_tl_session() -> requests.Session:
    """スレッドローカルなHTTPセッションを返す（並列フェッチ用）"""
    if not hasattr(_tl, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _tl.session = s
    return _tl.session


def _tl_fetch(url: str, params: dict | None = None) -> BeautifulSoup | None:
    """スレッドセーフなfetch（スレッドローカルセッション使用）"""
    try:
        resp = _get_tl_session().get(url, params=params, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        time.sleep(REQ_DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.warning("取得失敗 %s %s: %s", url, params, e)
        return None


def fetch(url: str, params: dict | None = None) -> BeautifulSoup | None:
    try:
        resp = _session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        time.sleep(REQ_DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.warning("取得失敗 %s %s: %s", url, params, e)
        return None


def _float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(str(s).strip())
    except ValueError:
        return None


def _int(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(str(s).strip())
    except ValueError:
        return None


def _zen_to_int(s: str | None) -> int | None:
    if s is None:
        return None
    return _ZEN_NUM.get(s.strip(), _int(s))


# ────────────────────────────────────────────────────
# 既存パーサー: 出走表・3連単
# ────────────────────────────────────────────────────

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


def parse_odds_3t(soup: BeautifulSoup) -> dict[str, float | None]:
    tables = soup.find_all("table")
    if len(tables) < 2:
        return {}

    table  = tables[1]
    thead  = table.find("thead")
    tbody_ = table.find("tbody")
    if not thead or not tbody_:
        return {}

    first_boats: list[int] = []
    for th in thead.find_all("th"):
        classes = th.get("class", [])
        for c in classes:
            m = re.match(r"is-boatColor(\d)", c)
            if m and "borderLeftNone" not in " ".join(classes):
                first_boats.append(int(m.group(1)))
                break

    num_groups = len(first_boats)
    if num_groups == 0:
        return {}

    combo_map: dict[str, float | None] = {}
    current_second = [None] * num_groups

    for tr in table.find("tbody").parent.find_all("tr"):
        tds = tr.find_all("td")
        cell_idx = 0
        for grp in range(num_groups):
            if cell_idx >= len(tds):
                break
            td = tds[cell_idx]
            if td.get("rowspan"):
                current_second[grp] = _int(td.get_text(strip=True))
                cell_idx += 1
            if cell_idx + 1 >= len(tds):
                break
            third_no = _int(tds[cell_idx].get_text(strip=True))
            odds_val = _float(tds[cell_idx + 1].get_text(strip=True))
            cell_idx += 2
            first = first_boats[grp]
            second = current_second[grp]
            if first and second and third_no:
                combo_map[f"{first}-{second}-{third_no}"] = odds_val

    return combo_map


# ────────────────────────────────────────────────────
# 新規パーサー 1: コース別成績
# ────────────────────────────────────────────────────

def parse_course_stats(soup: BeautifulSoup) -> list[dict]:
    tables = soup.find_all("table")
    if len(tables) < 3:
        return []

    entry_rates: dict[int, float | None] = {}
    for tbody in tables[0].find_all("tbody"):
        th = tbody.find("th", class_=re.compile(r"is-boatColor"))
        if not th:
            continue
        m = re.search(r"is-boatColor(\d)", " ".join(th.get("class", [])))
        if not m:
            continue
        course = int(m.group(1))
        label = tbody.find("span", class_="table1_progress2Label")
        entry_rates[course] = _float(label.get_text().replace("%", "").strip()) if label else None

    place_rates: dict[int, dict] = {c: {"w1": None, "w2": None, "w3": None} for c in range(1, 7)}
    for tbody in tables[1].find_all("tbody"):
        th = tbody.find("th", class_=re.compile(r"is-boatColor"))
        if not th:
            continue
        m = re.search(r"is-boatColor(\d)", " ".join(th.get("class", [])))
        if not m:
            continue
        course = int(m.group(1))
        bar = tbody.find("div", class_="table1_progress2Bar")
        if not bar:
            continue
        for span in bar.find_all("span", class_="is-progress"):
            style = span.get("style", "")
            wm = re.search(r"width:\s*(\d+\.?\d*)%", style)
            if not wm:
                continue
            w = float(wm.group(1))
            inner = span.find("span")
            if inner:
                cls = " ".join(inner.get("class", []))
                if "is-progress1" in cls:
                    place_rates[course]["w1"] = w
                elif "is-progress2" in cls:
                    place_rates[course]["w2"] = w
                elif "is-progress3" in cls:
                    place_rates[course]["w3"] = w

    avg_sts: dict[int, float | None] = {}
    for tbody in tables[2].find_all("tbody"):
        th = tbody.find("th", class_=re.compile(r"is-boatColor"))
        if not th:
            continue
        m = re.search(r"is-boatColor(\d)", " ".join(th.get("class", [])))
        if not m:
            continue
        course = int(m.group(1))
        label = tbody.find("span", class_="table1_progress2Label")
        avg_sts[course] = _float(label.get_text(strip=True)) if label else None

    return [
        {
            "course_no":    c,
            "entry_rate":   entry_rates.get(c),
            "win_rate_1st": place_rates[c]["w1"],
            "win_rate_2nd": place_rates[c]["w2"],
            "win_rate_3rd": place_rates[c]["w3"],
            "avg_st":       avg_sts.get(c),
        }
        for c in range(1, 7)
    ]


# ────────────────────────────────────────────────────
# 新規パーサー 2: 直前情報 + 気象
# ────────────────────────────────────────────────────

def _parse_weather(soup: BeautifulSoup) -> dict:
    wb = soup.select_one("div.weather1_body")
    if not wb:
        return {}
    result: dict = {}

    # 気温 + 風向コード
    dir_unit = wb.select_one("div.weather1_bodyUnit.is-direction")
    if dir_unit:
        ds = dir_unit.select_one("span.weather1_bodyUnitLabelData")
        if ds:
            m = re.search(r"(\d+\.?\d*)", ds.get_text())
            result["temperature"] = float(m.group(1)) if m else None
        img = dir_unit.select_one("p[class]")
        if img:
            m = re.search(r"is-direction(\d+)", " ".join(img.get("class", [])))
            result["wind_direction"] = int(m.group(1)) if m else None

    # 天候
    wu = wb.select_one("div.weather1_bodyUnit.is-weather")
    if wu:
        title = wu.select_one("span.weather1_bodyUnitLabelTitle")
        result["weather_desc"] = title.get_text(strip=True) if title else None

    # 風速
    wind = wb.select_one("div.weather1_bodyUnit.is-wind")
    if wind:
        ds = wind.select_one("span.weather1_bodyUnitLabelData")
        if ds:
            m = re.search(r"(\d+\.?\d*)", ds.get_text())
            result["wind_speed"] = float(m.group(1)) if m else None

    # 水温
    water = wb.select_one("div.weather1_bodyUnit.is-waterTemperature")
    if water:
        ds = water.select_one("span.weather1_bodyUnitLabelData")
        if ds:
            m = re.search(r"(\d+\.?\d*)", ds.get_text())
            result["water_temp"] = float(m.group(1)) if m else None

    # 波高
    wave = wb.select_one("div.weather1_bodyUnit.is-wave")
    if wave:
        ds = wave.select_one("span.weather1_bodyUnitLabelData")
        if ds:
            m = re.search(r"(\d+)", ds.get_text())
            result["wave_height"] = float(m.group(1)) if m else None

    return result


def parse_before_info(soup: BeautifulSoup) -> tuple[list[dict], dict]:
    tables = soup.find_all("table")

    entries: list[dict] = []
    exhibit_map: dict[int, dict] = {}  # course_pos → {boat_no, st}

    # メインテーブル (.is-w748)
    main_table = next(
        (t for t in tables if "is-w748" in " ".join(t.get("class", []))),
        None
    )
    if main_table:
        for tbody in main_table.select("tbody.is-fs12"):
            trs = tbody.find_all("tr")
            first_tds = trs[0].find_all("td")

            # 艇番
            raw = first_tds[0].get_text(strip=True) if first_tds else ""
            boat_no = _ZEN_NUM.get(raw, _int(raw))

            # 体重
            weight = None
            for td in first_tds:
                txt = td.get_text(strip=True)
                m = re.search(r"(\d+\.?\d*)kg", txt)
                if m:
                    weight = float(m.group(1))
                    break

            # 展示タイム・チルト・周回タイム・直線タイム（rowspan=4 の数値セル順に取得）
            exhibition_time = tilt = lap_time = straight_time = None
            for td in first_tds:
                if td.get("rowspan") != "4":
                    continue
                td_classes = " ".join(td.get("class", []))
                if ("is-boatColor" in td_classes or "is-fBold" in td_classes
                        or td.find("img") or td.find("ul")):
                    continue
                val = td.get_text(strip=True)
                f = _float(val)
                if f is None:
                    continue
                if f > 5 and exhibition_time is None:
                    exhibition_time = f      # 展示タイム (e.g. 6.78)
                elif exhibition_time is not None and tilt is None:
                    tilt = f                 # チルト角度 (e.g. 0.0)
                elif tilt is not None and lap_time is None:
                    lap_time = f             # 周回タイム / まわり足
                elif lap_time is not None and straight_time is None:
                    straight_time = f        # 直線タイム

            # 前走成績（全行を走査してラベルで取得）
            prev_race_no = prev_course = prev_st = prev_finish = None
            prev_race_date = prev_race_venue = None

            for tr in trs:
                tds_text = [td.get_text(strip=True) for td in tr.find_all("td")]
                for i, txt in enumerate(tds_text):
                    if txt == "R" and i + 1 < len(tds_text):
                        prev_race_no = _int(tds_text[i + 1])
                    elif txt == "進入" and i + 1 < len(tds_text):
                        prev_course = _int(tds_text[i + 1])
                    elif txt == "ST" and i + 1 < len(tds_text):
                        prev_st = tds_text[i + 1]
                    elif txt == "着順" and i + 1 < len(tds_text):
                        prev_finish = _zen_to_int(tds_text[i + 1])

            for a in tbody.find_all("a", href=re.compile(r"raceresult")):
                href = a["href"]
                m_d = re.search(r"hd=(\d{8})", href)
                m_v = re.search(r"jcd=(\d{2})", href)
                if m_d:
                    prev_race_date = m_d.group(1)
                if m_v:
                    prev_race_venue = m_v.group(1)
                break

            if boat_no:
                entries.append({
                    "boat_no":           boat_no,
                    "weight":            weight,
                    "exhibition_time":   exhibition_time,
                    "tilt":              tilt,
                    "lap_time":          lap_time,
                    "straight_time":     straight_time,
                    "exhibit_course":    None,
                    "exhibit_st":        None,
                    "prev_race_venue":   prev_race_venue,
                    "prev_race_date":    prev_race_date,
                    "prev_race_no":      prev_race_no,
                    "prev_entry_course": prev_course,
                    "prev_start_timing": prev_st,
                    "prev_finish":       prev_finish,
                })

    # スタート展示テーブル (.is-w238)
    ex_table = next(
        (t for t in tables if "is-w238" in " ".join(t.get("class", []))),
        None
    )
    if ex_table:
        course_pos = 0
        for div in ex_table.find_all("div", class_="table1_boatImage1"):
            course_pos += 1
            num_span = div.find("span", class_="table1_boatImage1Number")
            boat_no_ex = _int(num_span.get_text(strip=True)) if num_span else None
            time_span = div.find("span", class_="table1_boatImage1Time")
            st_text = time_span.get_text(strip=True) if time_span else None
            exhibit_map[course_pos] = {"boat_no": boat_no_ex, "st": st_text}

    # exhibit_course / exhibit_st を埋める
    boat_to_entry = {e["boat_no"]: e for e in entries}
    for cpos, ex in exhibit_map.items():
        bn = ex["boat_no"]
        if bn and bn in boat_to_entry:
            boat_to_entry[bn]["exhibit_course"] = cpos
            boat_to_entry[bn]["exhibit_st"] = ex["st"]

    weather = _parse_weather(soup)
    return entries, weather


# ────────────────────────────────────────────────────
# 新規パーサー 3: 確定結果（着順・払戻金）
# ────────────────────────────────────────────────────

def parse_race_result(soup: BeautifulSoup) -> tuple[list[dict], list[dict]]:
    tables = soup.find_all("table")
    finish_entries: list[dict] = []
    start_info: dict[int, dict] = {}  # course_pos → {boat_no, st, trick}
    payouts: list[dict] = []

    # table[1]: 着順テーブル
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
            player_no  = spans[0].get_text(strip=True) if spans else None
            player_name = spans[1].get_text(strip=True) if len(spans) > 1 else None
            race_time  = tds[3].get_text(strip=True) if len(tds) > 3 else None
            if rank:
                finish_entries.append({
                    "rank": rank, "boat_no": boat_no,
                    "player_no": player_no, "player_name": player_name,
                    "race_time": race_time, "start_course": None,
                    "start_timing": None, "winning_trick": None,
                })

    # table[2]: スタート情報テーブル
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

    # 決まり手テーブル（単独）
    winning_trick = None
    for t in tables:
        th = t.find("th")
        if th and "決まり手" in th.get_text():
            td = t.find("td")
            winning_trick = td.get_text(strip=True) if td else None
            break

    # スタート情報を着順エントリに統合
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

    # 払戻金テーブル
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


# ────────────────────────────────────────────────────
# 新規パーサー 4: 今節成績
# ────────────────────────────────────────────────────

def parse_meet_standings(soup: BeautifulSoup) -> list[dict]:
    table = soup.find("table")
    if not table:
        return []

    standings: list[dict] = []
    for tbody in table.find_all("tbody"):
        tr = tbody.find("tr")
        if not tr:
            continue
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue

        rank = _int(tds[0].get_text(strip=True))
        a1 = tds[1].find("a")
        player_no = a1.get_text(strip=True) if a1 else tds[1].get_text(strip=True)
        a2 = tds[2].find("a")
        player_name = a2.get_text(strip=True) if a2 else tds[2].get_text(strip=True)
        player_class = tds[3].get_text(strip=True)
        points_rate = _float(tds[4].get_text(strip=True))
        ranking_span = tds[5].find("span", class_="is-ranking")
        results_text = ranking_span.get_text(strip=True) if ranking_span else tds[5].get_text(strip=True)
        total_points = _int(tds[6].get_text(strip=True))
        deductions  = _int(tds[7].get_text(strip=True))

        standings.append({
            "rank": rank, "player_no": player_no, "player_name": player_name,
            "player_class": player_class, "points_rate": points_rate,
            "results_text": results_text, "total_points": total_points,
            "deductions": deductions,
        })

    return standings


# ────────────────────────────────────────────────────
# DB 書き込みヘルパー
# ────────────────────────────────────────────────────

def upsert_venue(conn, code):
    conn.execute(
        "INSERT OR IGNORE INTO venues (venue_code, venue_name) VALUES (?,?)",
        (code, VENUE_NAMES.get(code, code)),
    )


def upsert_race(conn, venue_code, race_no, scheduled_time: str | None = None) -> int:
    conn.execute("""
        INSERT INTO races (date, venue_code, race_no, race_title, scheduled_time)
        VALUES (?,?,?,?,?)
        ON CONFLICT(date, venue_code, race_no) DO UPDATE SET
            scheduled_time = COALESCE(excluded.scheduled_time, scheduled_time)
    """, (TODAY, venue_code, race_no, f"{race_no}R", scheduled_time))
    row = conn.execute(
        "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (TODAY, venue_code, race_no),
    ).fetchone()
    return row[0]


def save_entries(conn, race_id, entries):
    if not entries:
        return
    sql = """
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
    """
    conn.executemany(sql, [
        (race_id, e["boat_no"], e["player_no"], e["player_name"], e["player_class"],
         e["age"], e["weight"], e["flying_count"], e["late_count"], e["avg_start_timing"],
         e["national_win_rate"], e["national_2ring_rate"],
         e["local_win_rate"], e["local_2ring_rate"],
         e["motor_no"], e["motor_2ring_rate"], e["boat_no_hull"], e["boat_2ring_rate"])
        for e in entries
    ])


def save_odds(conn, race_id, combo_map):
    if not combo_map:
        return
    conn.executemany(
        "INSERT INTO odds_3t (race_id, combination, odds) VALUES (?,?,?)"
        " ON CONFLICT(race_id, combination) DO UPDATE SET odds=excluded.odds",
        [(race_id, combo, odds_val) for combo, odds_val in combo_map.items()]
    )


def save_course_stats(conn, player_no, stats):
    if not stats:
        return
    conn.executemany(
        "INSERT INTO course_stats"
        " (player_no,fetched_date,course_no,entry_rate,win_rate_1st,win_rate_2nd,win_rate_3rd,avg_st)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(player_no,fetched_date,course_no) DO UPDATE SET"
        " entry_rate=excluded.entry_rate, win_rate_1st=excluded.win_rate_1st,"
        " win_rate_2nd=excluded.win_rate_2nd, win_rate_3rd=excluded.win_rate_3rd,"
        " avg_st=excluded.avg_st",
        [(player_no, TODAY, s["course_no"], s["entry_rate"],
          s["win_rate_1st"], s["win_rate_2nd"], s["win_rate_3rd"], s["avg_st"])
         for s in stats]
    )


def save_course_stats_log(conn, player_no: str, has_data: bool) -> None:
    """course_stats フェッチ試行を記録（成功・失敗問わず）"""
    conn.execute("""
        INSERT OR REPLACE INTO course_stats_log (player_no, fetched_date, has_data)
        VALUES (?, ?, ?)
    """, (player_no, TODAY, 1 if has_data else 0))


def save_before_info(conn, race_id, entries):
    if not entries:
        return
    conn.executemany(
        "INSERT INTO before_info"
        " (race_id,boat_no,weight,exhibition_time,tilt,lap_time,straight_time,"
        " exhibit_course,exhibit_st,"
        " prev_race_venue,prev_race_date,prev_race_no,prev_entry_course,"
        " prev_start_timing,prev_finish)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(race_id,boat_no) DO UPDATE SET"
        " weight=excluded.weight, exhibition_time=excluded.exhibition_time,"
        " tilt=excluded.tilt, lap_time=excluded.lap_time,"
        " straight_time=excluded.straight_time,"
        " exhibit_course=excluded.exhibit_course,"
        " exhibit_st=excluded.exhibit_st, prev_race_venue=excluded.prev_race_venue,"
        " prev_race_date=excluded.prev_race_date, prev_race_no=excluded.prev_race_no,"
        " prev_entry_course=excluded.prev_entry_course,"
        " prev_start_timing=excluded.prev_start_timing, prev_finish=excluded.prev_finish",
        [(race_id, e["boat_no"], e["weight"], e["exhibition_time"], e["tilt"],
          e.get("lap_time"), e.get("straight_time"),
          e["exhibit_course"], e["exhibit_st"], e["prev_race_venue"],
          e["prev_race_date"], e["prev_race_no"], e["prev_entry_course"],
          e["prev_start_timing"], e["prev_finish"])
         for e in entries]
    )


def _save_live_prediction(conn, race_id: int, vcode: str, rno: int) -> None:
    """XGBoost予想を実行して predictions テーブルに保存（upsert）。"""
    import json as _json
    try:
        from ml_predict import predict_ml
        result = predict_ml(TODAY, vcode, rno, conn=conn)
    except Exception as e:
        log.warning("    ML予想スキップ (%s-%sR): %s", vcode, rno, e)
        # フォールバック: ルールベース
        try:
            from predict import predict as _predict
            result = _predict(TODAY, vcode, rno)
        except Exception as e2:
            log.warning("    ルールベース予想もスキップ: %s", e2)
            return

    top5        = [d["combo"] for d in result.get("recommended_3t_detail", [])[:5]]
    honmei_list = [d["combo"] for d in result.get("honmei_detail", [])]
    chuana_list = [d["combo"] for d in result.get("chuana_detail", [])]
    ana_list    = [d["combo"] for d in result.get("ana_detail", [])]
    now         = datetime.now(_JST).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        INSERT INTO predictions
          (race_id, predicted_at, top5_combos, top5_honmei, top5_chuana, top5_ana)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(race_id) DO UPDATE SET
            predicted_at = excluded.predicted_at,
            top5_combos  = excluded.top5_combos,
            top5_honmei  = excluded.top5_honmei,
            top5_chuana  = excluded.top5_chuana,
            top5_ana     = excluded.top5_ana
    """, (race_id, now,
          _json.dumps(top5, ensure_ascii=False),
          _json.dumps(honmei_list, ensure_ascii=False),
          _json.dumps(chuana_list, ensure_ascii=False),
          _json.dumps(ana_list, ensure_ascii=False)))
    log.info("    予想保存: Top5=%s", top5[:3])


def _update_prediction_result(conn, race_id: int) -> None:
    """確定結果保存後に actual_combo と hit フィールドを更新する。"""
    import json as _json

    # predictionsレコードが存在しない場合はスキップ
    pred = conn.execute(
        "SELECT top5_combos, top5_honmei, top5_chuana, top5_ana FROM predictions WHERE race_id=?",
        (race_id,)
    ).fetchone()
    if not pred:
        return

    # 実際の3連単を取得
    rows = conn.execute("""
        SELECT rank, boat_no FROM race_result_entries
        WHERE race_id=? AND rank IN (1,2,3) AND boat_no IS NOT NULL ORDER BY rank
    """, (race_id,)).fetchall()
    if len(rows) < 3:
        return
    rank_map = {r[0]: r[1] for r in rows}
    actual = f"{rank_map[1]}-{rank_map[2]}-{rank_map[3]}"

    def _hit(json_str, n=None):
        if not json_str:
            return None
        combos = _json.loads(json_str)
        if n is not None:
            combos = combos[:n]
        return 1 if actual in combos else 0

    top5    = _json.loads(pred[0]) if pred[0] else []
    hit_t3  = 1 if actual in top5[:3] else 0
    hit_t5  = 1 if actual in top5     else 0

    conn.execute("""
        UPDATE predictions
           SET actual_combo=?, hit_top3=?, hit_top5=?,
               hit_honmei=?, hit_chuana=?, hit_ana=?,
               hit_honmei_5=?, hit_chuana_10=?, hit_ana_10=?
         WHERE race_id=?
    """, (actual, hit_t3, hit_t5,
          _hit(pred[1]),     _hit(pred[2]),     _hit(pred[3]),
          _hit(pred[1], 5),  _hit(pred[2], 10), _hit(pred[3], 10),
          race_id))
    log.info("    的中判定: actual=%s hit_top3=%d hit_top5=%d", actual, hit_t3, hit_t5)


def save_weather(conn, race_id, w):
    if not w:
        return
    conn.execute("""
        INSERT INTO weather
          (race_id,temperature,weather_desc,wind_speed,wind_direction,water_temp,wave_height)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(race_id) DO UPDATE SET
          temperature=excluded.temperature, weather_desc=excluded.weather_desc,
          wind_speed=excluded.wind_speed, wind_direction=excluded.wind_direction,
          water_temp=excluded.water_temp, wave_height=excluded.wave_height
    """, (race_id, w.get("temperature"), w.get("weather_desc"), w.get("wind_speed"),
          w.get("wind_direction"), w.get("water_temp"), w.get("wave_height")))


def save_race_result_entries(conn, race_id, entries):
    if not entries:
        return
    conn.executemany(
        "INSERT INTO race_result_entries"
        " (race_id,rank,boat_no,player_no,player_name,race_time,"
        " start_course,start_timing,winning_trick)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(race_id,rank) DO UPDATE SET"
        " boat_no=excluded.boat_no, player_no=excluded.player_no,"
        " player_name=excluded.player_name, race_time=excluded.race_time,"
        " start_course=excluded.start_course, start_timing=excluded.start_timing,"
        " winning_trick=excluded.winning_trick",
        [(race_id, e["rank"], e["boat_no"], e["player_no"], e["player_name"],
          e["race_time"], e["start_course"], e["start_timing"], e["winning_trick"])
         for e in entries]
    )


def save_payouts(conn, race_id, payouts):
    if not payouts:
        return
    conn.executemany(
        "INSERT INTO payouts (race_id,bet_type,combination,payout,popularity)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(race_id,bet_type,combination) DO UPDATE SET"
        " payout=excluded.payout, popularity=excluded.popularity",
        [(race_id, p["bet_type"], p["combination"], p["payout"], p["popularity"])
         for p in payouts]
    )


def save_meet_standings(conn, venue_code, standings):
    if not standings:
        return
    conn.executemany(
        "INSERT INTO meet_standings"
        " (date,venue_code,standing_rank,player_no,player_name,player_class,"
        " points_rate,results_text,total_points,deductions)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(date,venue_code,player_no) DO UPDATE SET"
        " standing_rank=excluded.standing_rank,"
        " points_rate=excluded.points_rate, results_text=excluded.results_text,"
        " total_points=excluded.total_points, deductions=excluded.deductions",
        [(TODAY, venue_code, s["rank"], s["player_no"], s["player_name"],
          s["player_class"], s["points_rate"], s["results_text"],
          s["total_points"], s["deductions"])
         for s in standings]
    )


# ────────────────────────────────────────────────────
# 新規パーサー 5: 単勝オッズ (oddstkf)
# ────────────────────────────────────────────────────

def parse_odds_tansho(soup: BeautifulSoup) -> dict[int, float | None]:
    """単勝オッズ: boat_no → odds"""
    result: dict[int, float | None] = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            # boat color class から艇番特定
            boat_no = None
            for td in tds:
                classes = " ".join(td.get("class", []))
                m = re.search(r"is-boatColor(\d)", classes)
                if m:
                    boat_no = int(m.group(1))
                    break
            if boat_no is None:
                # 艇番が数字セルとして格納されているケース
                txt = tds[0].get_text(strip=True)
                if txt.isdigit() and 1 <= int(txt) <= 6:
                    boat_no = int(txt)
            if boat_no is None:
                continue
            # 数値セルからオッズを取得（最初の小数が単勝オッズ）
            for td in tds[1:]:
                val = _float(td.get_text(strip=True).replace(",", ""))
                if val is not None:
                    result[boat_no] = val
                    break
    return result


# ────────────────────────────────────────────────────
# 新規パーサー 6: 2連単オッズ (odds2tf)
# ────────────────────────────────────────────────────

def parse_odds_2t(soup: BeautifulSoup) -> dict[str, float | None]:
    """2連単オッズ: 'a-b' → odds"""
    tables = soup.find_all("table")
    if len(tables) < 2:
        return {}

    # 3連単と同様のテーブル構造: 1着艇がヘッダ、2着艇が行
    table = tables[1]
    thead = table.find("thead")
    if not thead:
        return {}

    first_boats: list[int] = []
    for th in thead.find_all("th"):
        classes = " ".join(th.get("class", []))
        m = re.search(r"is-boatColor(\d)", classes)
        if m and "borderLeftNone" not in classes:
            first_boats.append(int(m.group(1)))

    if not first_boats:
        return {}

    combo_map: dict[str, float | None] = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cell_idx = 0
        for grp, first in enumerate(first_boats):
            if cell_idx >= len(tds):
                break
            second_no = _int(tds[cell_idx].get_text(strip=True))
            if second_no is None or cell_idx + 1 >= len(tds):
                cell_idx += 1
                continue
            odds_val = _float(tds[cell_idx + 1].get_text(strip=True))
            cell_idx += 2
            if second_no and first != second_no:
                combo_map[f"{first}-{second_no}"] = odds_val

    return combo_map


# ────────────────────────────────────────────────────
# 新規パーサー 7: 選手シーズン成績（決まり手率・3連対率・支部）
# ────────────────────────────────────────────────────

def parse_player_season(soup: BeautifulSoup) -> dict:
    """
    /data/racersearch/season?toban=XXXX から
    branch, national_3ring_rate, 決まり手各率 を取得
    """
    result: dict = {
        "branch": None,
        "national_3ring_rate": None,
        "nige_rate": None,
        "sashi_rate": None,
        "makuri_rate": None,
        "makuri_sashi_rate": None,
        "teiko_rate": None,
        "megumi_rate": None,
    }

    # 支部: テキスト「支部」の近くにあるセル
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        ths = tr.find_all("th")
        row_text = tr.get_text()
        if "支部" in row_text:
            # th=支部ラベル, td=支部名
            for td in tds:
                t = td.get_text(strip=True)
                if t and "支部" not in t and len(t) <= 8:
                    result["branch"] = t
                    break

    # 3連対率: ページに「3連対率」というラベルがある箇所
    all_text = soup.get_text()
    m = re.search(r"3連対率[\s\S]*?(\d+\.\d+)", all_text[:3000])
    if m:
        result["national_3ring_rate"] = float(m.group(1))

    # 決まり手: 「逃げ」「差し」「まくり」「まくり差し」「抵抗」「恵まれ」の順で出現するパターン
    TRICKS = [
        ("nige_rate",        "逃げ"),
        ("sashi_rate",       "差し"),
        ("makuri_rate",      "まくり"),
        ("makuri_sashi_rate","まくり差し"),
        ("teiko_rate",       "抵抗"),
        ("megumi_rate",      "恵まれ"),
    ]
    for key, label in TRICKS:
        idx = all_text.find(label)
        if idx >= 0:
            # ラベルの直後100文字以内で最初の数値(小数も可)を取得
            snippet = all_text[idx: idx + 80]
            m2 = re.search(r"(\d+\.?\d*)\s*%?", snippet[len(label):])
            if m2:
                result[key] = float(m2.group(1))

    return result


# ────────────────────────────────────────────────────
# 新規保存: 単勝・2連単オッズ
# ────────────────────────────────────────────────────

def save_odds_tansho(conn, race_id: int, tansho_map: dict[int, float | None]) -> None:
    if not tansho_map:
        return
    conn.executemany(
        "INSERT INTO odds_tansho (race_id, boat_no, odds) VALUES (?,?,?)"
        " ON CONFLICT(race_id, boat_no) DO UPDATE SET odds=excluded.odds",
        [(race_id, boat_no, odds_val) for boat_no, odds_val in tansho_map.items()]
    )


def save_odds_2t(conn, race_id: int, combo_map: dict[str, float | None]) -> None:
    if not combo_map:
        return
    conn.executemany(
        "INSERT INTO odds_2t (race_id, combination, odds) VALUES (?,?,?)"
        " ON CONFLICT(race_id, combination) DO UPDATE SET odds=excluded.odds",
        [(race_id, combo, odds_val) for combo, odds_val in combo_map.items()]
    )


def calc_trick_rates_from_db(conn, player_no: str) -> dict:
    """
    race_result_entries の winning_trick 実績から決まり手率を計算する。
    boatrace.jp の選手検索ページには決まり手率データが存在しないため、
    DB に蓄積した確定結果データを使って算出する。
    「抜き」は「逃げ」に近い動きなので nige_rate に合算する。
    """
    rows = conn.execute("""
        SELECT winning_trick, COUNT(*) cnt
        FROM race_result_entries
        WHERE player_no=? AND rank=1
        GROUP BY winning_trick
    """, (player_no,)).fetchall()

    total = sum(r[1] for r in rows)
    if total == 0:
        return {}

    trick_map = {r[0]: r[1] for r in rows}
    nige  = trick_map.get("逃げ", 0) + trick_map.get("抜き", 0)
    return {
        "nige_rate":         round(nige                          / total * 100, 1),
        "sashi_rate":        round(trick_map.get("差し", 0)      / total * 100, 1),
        "makuri_rate":       round(trick_map.get("まくり", 0)    / total * 100, 1),
        "makuri_sashi_rate": round(trick_map.get("まくり差し", 0)/ total * 100, 1),
        "teiko_rate":        round(trick_map.get("抵抗", 0)      / total * 100, 1),
        "megumi_rate":       round(trick_map.get("恵まれ", 0)    / total * 100, 1),
    }


def save_player_season(conn, race_id: int, player_no: str, data: dict) -> None:
    """決まり手率・支部・3連対率を entries テーブルに更新"""
    conn.execute("""
        UPDATE entries SET
          branch=?, national_3ring_rate=?,
          nige_rate=?, sashi_rate=?, makuri_rate=?,
          makuri_sashi_rate=?, teiko_rate=?, megumi_rate=?
        WHERE race_id=? AND player_no=?
    """, (
        data.get("branch"), data.get("national_3ring_rate"),
        data.get("nige_rate"), data.get("sashi_rate"), data.get("makuri_rate"),
        data.get("makuri_sashi_rate"), data.get("teiko_rate"), data.get("megumi_rate"),
        race_id, player_no,
    ))


def save_st_history_from_result(
    conn, race_id: int, race_date: str, venue_code: str, race_no: int,
    result_entries: list[dict]
) -> None:
    """確定結果からST履歴を保存"""
    for e in result_entries:
        if not e.get("player_no"):
            continue
        conn.execute("""
            INSERT INTO st_history
              (player_no, race_id, race_date, venue_code, race_no,
               start_course, start_timing, finish_rank)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(player_no, race_id) DO UPDATE SET
              start_timing=excluded.start_timing,
              finish_rank=excluded.finish_rank
        """, (
            e["player_no"], race_id, race_date, venue_code, race_no,
            e.get("start_course"), e.get("start_timing"), e.get("rank"),
        ))


# ────────────────────────────────────────────────────
# 開催一覧・レース数取得
# ────────────────────────────────────────────────────

def fetch_today_venues() -> list[str]:
    soup = fetch(f"{BASE_URL}/index")
    if soup is None:
        return []
    pattern = re.compile(r"jcd=(\d{2})&hd=" + TODAY)
    seen: set[str] = set()
    for a in soup.find_all("a", href=pattern):
        m = pattern.search(a["href"])
        if m:
            seen.add(m.group(1))
    venues = sorted(seen)
    log.info("本日の開催会場: %s", venues)
    return venues


def fetch_race_schedule(venue_code: str) -> tuple[int, dict[int, str]]:
    """
    raceindex ページからレース数とレース別開始時刻を取得する。
    Returns: (max_race_no, {race_no: "HH:MM"})
    """
    soup = fetch(f"{BASE_URL}/raceindex", params={"jcd": venue_code, "hd": TODAY})
    if soup is None:
        return 12, {}

    race_nos: set[int] = set()
    schedule: dict[int, str] = {}

    # raceindex の各行: <a href="...rno=N...">NR</a> の近くに時刻テキストがある
    for a in soup.find_all("a", href=re.compile(r"rno=\d+")):
        m = re.search(r"rno=(\d+)", a["href"])
        if not m:
            continue
        rno = int(m.group(1))
        race_nos.add(rno)

        # 親セル or 親行から時刻を探す（HH:MM 形式）
        cell = a.find_parent("td") or a.find_parent("li") or a.find_parent("div")
        if cell:
            row = cell.find_parent("tr") or cell.find_parent("ul") or cell.parent
            text = row.get_text(" ", strip=True) if row else cell.get_text(" ", strip=True)
            tm = re.search(r"\b(\d{1,2}:\d{2})\b", text)
            if tm:
                schedule[rno] = tm.group(1)

    max_rno = max(race_nos) if race_nos else 12
    return max_rno, schedule


def fetch_max_race_no(venue_code: str) -> int:
    max_rno, _ = fetch_race_schedule(venue_code)
    return max_rno


# ────────────────────────────────────────────────────
# メイン処理
# ────────────────────────────────────────────────────

def main(force: bool = False) -> None:
    # ── 多重起動防止（PIDファイルロック）──────────────────
    PID_FILE = Path(__file__).parent / ".scraper_running.pid"
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)   # プロセスが存在するか確認
            log.warning("別インスタンスが実行中 (PID=%d)。二重起動を防止して終了します。", old_pid)
            return
        except (ProcessLookupError, PermissionError):
            pass   # 古いPIDファイルが残っているだけ → 上書きして続行
    PID_FILE.write_text(str(os.getpid()))
    try:
        acquire_write_lock(wait=True, timeout=600)
        try:
            _main_body(force=force)
        finally:
            release_write_lock()
    finally:
        PID_FILE.unlink(missing_ok=True)


def _main_body(force: bool = False) -> None:
    # 23:30〜07:30 は historical_scraper と競合するため実行しない
    # --force オプション指定時はこの制限をスキップ（手動補完用）
    now = datetime.now(_JST)  # Railway (UTC) でも JST を使用
    now_minutes = now.hour * 60 + now.minute
    if not force and not (7 * 60 + 30 <= now_minutes < 23 * 60 + 30):
        log.info("23:30〜07:30 は historical_scraper 専用時間帯のため終了します (JST=%02d:%02d)", now.hour, now.minute)
        return

    log.info("=== boatrace.jp スクレイパー 開始 (日付: %s) ===", TODAY)

    from db_connect import open_db
    conn = open_db()
    init_db(conn)

    venue_codes = fetch_today_venues()
    if not venue_codes:
        log.error("本日の開催会場が取得できませんでした。")
        conn.close()
        return

    for vcode in venue_codes:
        vname = VENUE_NAMES.get(vcode, vcode)
        log.info("═══ %s (%s) ═══", vname, vcode)
        upsert_venue(conn, vcode)
        conn.commit()

        max_rno, schedule = fetch_race_schedule(vcode)
        log.info("  レース数: %d", max_rno)

        # 今節成績
        soup_rank = fetch(f"{BASE_URL}/pointrank", params={"jcd": vcode, "hd": TODAY})
        if soup_rank:
            standings = parse_meet_standings(soup_rank)
            save_meet_standings(conn, vcode, standings)
            conn.commit()
            log.info("  今節成績: %d名", len(standings))

        # ── レースごとのデータ取得（3フェーズ並列版）────────────────
        # Phase 1: 全レースのDB状態確認（ロック保持中・高速）
        # Phase 2: 全レースを並列HTTPフェッチ（ロック解放中）
        # Phase 3: DB一括書き込み（ロック再取得）
        collected_player_nos: set[str] = set()

        # ── Phase 1: DB状態確認 ───────────────────────────────────────
        race_states: list[dict] = []
        for rno in range(1, max_rno + 1):
            race_id = upsert_race(conn, vcode, rno, schedule.get(rno))
            conn.commit()

            has_result = conn.execute(
                "SELECT COUNT(*) FROM race_result_entries WHERE race_id=?", (race_id,)
            ).fetchone()[0]
            if has_result > 0:
                for row in conn.execute(
                    "SELECT player_no FROM entries WHERE race_id=? AND player_no IS NOT NULL",
                    (race_id,)
                ).fetchall():
                    collected_player_nos.add(row[0])
                # actual_combo未更新の予測があれば的中判定を補完
                needs_update = conn.execute(
                    "SELECT COUNT(*) FROM predictions WHERE race_id=? AND actual_combo IS NULL",
                    (race_id,)
                ).fetchone()[0]
                if needs_update > 0:
                    _update_prediction_result(conn, race_id)
                    conn.commit()
                log.info("  ── %dR (race_id=%d) スキップ（確定結果取得済み）", rno, race_id)
                race_states.append({"rno": rno, "race_id": race_id, "skip": True})
                continue

            has_entries = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE race_id=?", (race_id,)
            ).fetchone()[0]
            if has_entries > 0:
                for row in conn.execute(
                    "SELECT player_no FROM entries WHERE race_id=? AND player_no IS NOT NULL",
                    (race_id,)
                ).fetchall():
                    collected_player_nos.add(row[0])
            has_before = conn.execute(
                "SELECT COUNT(*) FROM before_info WHERE race_id=?", (race_id,)
            ).fetchone()[0]
            race_states.append({
                "rno": rno, "race_id": race_id, "skip": False,
                "has_entries": has_entries, "has_before": has_before,
            })

        # ── Phase 2: 並列HTTPフェッチ（ロック解放中） ─────────────────
        active_states = [s for s in race_states if not s["skip"]]
        fetched_races: dict[int, dict] = {}

        if active_states:
            release_write_lock()

            def _fetch_race_pages(state: dict) -> dict:
                """1レース分の全ページを取得・パースして返す（スレッドセーフ）"""
                rno         = state["rno"]
                has_entries = state["has_entries"]
                has_before  = state["has_before"]

                soup_list   = _tl_fetch(f"{BASE_URL}/racelist",
                                        params={"rno": rno, "jcd": vcode, "hd": TODAY}) \
                              if has_entries == 0 else None
                soup_odds   = _tl_fetch(f"{BASE_URL}/odds3t",
                                        params={"rno": rno, "jcd": vcode, "hd": TODAY})
                soup_tansho = _tl_fetch(f"{BASE_URL}/oddstkf",
                                        params={"rno": rno, "jcd": vcode, "hd": TODAY})
                soup_2t     = _tl_fetch(f"{BASE_URL}/odds2tf",
                                        params={"rno": rno, "jcd": vcode, "hd": TODAY})
                soup_before = _tl_fetch(f"{BASE_URL}/beforeinfo",
                                        params={"rno": rno, "jcd": vcode, "hd": TODAY}) \
                              if has_before == 0 else None
                soup_result = _tl_fetch(f"{BASE_URL}/raceresult",
                                        params={"rno": rno, "jcd": vcode, "hd": TODAY})

                return {
                    "rno":         rno,
                    "race_id":     state["race_id"],
                    "has_entries": has_entries,
                    "has_before":  has_before,
                    "entries":     parse_entries(soup_list)       if soup_list   else [],
                    "combo_map":   parse_odds_3t(soup_odds)       if soup_odds   else {},
                    "tansho_map":  parse_odds_tansho(soup_tansho) if soup_tansho else {},
                    "combo_2t":    parse_odds_2t(soup_2t)         if soup_2t     else {},
                    "bi_weather":  parse_before_info(soup_before) if soup_before else ([], {}),
                    "result":      parse_race_result(soup_result) if soup_result else ([], []),
                }

            with ThreadPoolExecutor(max_workers=MAX_RACE_WORKERS) as pool:
                futures = {
                    pool.submit(_fetch_race_pages, s): s["rno"]
                    for s in active_states
                }
                for future in as_completed(futures):
                    rno = futures[future]
                    try:
                        fetched_races[rno] = future.result()
                    except Exception as e:
                        log.warning("  %dR: フェッチ失敗 %s", rno, e)

            # ── Phase 3: DB一括書き込み（ロック再取得） ─────────────────
            acquire_write_lock(wait=True, timeout=60)

        for state in active_states:
            rno     = state["rno"]
            race_id = state["race_id"]
            data    = fetched_races.get(rno)
            log.info("  ── %dR (race_id=%d) ──", rno, race_id)

            if not data:
                log.warning("    フェッチデータなし（スキップ）")
                continue

            if data["entries"]:
                save_entries(conn, race_id, data["entries"])
                conn.commit()
                for e in data["entries"]:
                    if e["player_no"]:
                        collected_player_nos.add(e["player_no"])
                log.info("    出走表: %d艇", len(data["entries"]))
            elif data["has_entries"] > 0:
                log.info("    出走表: スキップ（取得済み）")

            if data["combo_map"]:
                save_odds(conn, race_id, data["combo_map"])
                conn.commit()
                log.info("    3連単オッズ: %d件", len(data["combo_map"]))

            if data["tansho_map"]:
                save_odds_tansho(conn, race_id, data["tansho_map"])
                conn.commit()
                log.info("    単勝オッズ: %d件", len(data["tansho_map"]))

            if data["combo_2t"]:
                save_odds_2t(conn, race_id, data["combo_2t"])
                conn.commit()
                log.info("    2連単オッズ: %d件", len(data["combo_2t"]))

            bi_entries, weather = data["bi_weather"]
            if bi_entries:
                save_before_info(conn, race_id, bi_entries)
                save_weather(conn, race_id, weather)
                conn.commit()
                log.info("    直前情報: %d艇 / 気象: %s", len(bi_entries), bool(weather))
                _save_live_prediction(conn, race_id, vcode, rno)
                conn.commit()
            elif data["has_before"] > 0:
                log.info("    直前情報: スキップ（取得済み）")
            else:
                log.info("    直前情報: 0艇（展示前）")

            res_entries, payouts = data["result"]
            if res_entries:
                save_race_result_entries(conn, race_id, res_entries)
                save_payouts(conn, race_id, payouts)
                save_st_history_from_result(conn, race_id, TODAY, vcode, rno, res_entries)
                conn.commit()
                log.info("    確定結果: 着順%d件 / 払戻%d件",
                         len(res_entries), len(payouts))
                _update_prediction_result(conn, race_id)
                conn.commit()
            else:
                log.info("    確定結果: 着順0件 / 払戻0件")

        # コース別成績 + シーズン成績（選手ごと・本日未取得分のみ）
        # already: 本日すでにフェッチ試行済みの選手（成功・失敗問わず）
        # + 直近7日以内にデータなし判定された選手（クールダウン中）
        already = {row[0] for row in conn.execute("""
            SELECT player_no FROM course_stats WHERE fetched_date = ?
            UNION
            SELECT player_no FROM course_stats_log  WHERE fetched_date = ?
            UNION
            SELECT player_no FROM course_stats_log
             WHERE has_data = 0
               AND fetched_date >= date(?, '-7 days')
               AND fetched_date < ?
        """, (TODAY, TODAY, TODAY, TODAY)).fetchall()}
        to_fetch = sorted(collected_player_nos - already)
        log.info("  コース別成績・シーズン成績: %d名取得予定 (スキップ:%d名)",
                 len(to_fetch), len(collected_player_nos) - len(to_fetch))

        if to_fetch:
            # ── HTTPフェッチ（ロック解放中・並列） ──────────────────────
            # ロックを解放してfocused_scraperが割り込めるようにする（最大16分→3分に短縮）
            release_write_lock()

            def _fetch_player_stats(pno: str) -> dict:
                """コース別成績 + シーズン成績をHTTP取得（スレッドセーフ）"""
                result: dict = {"player_no": pno, "course": None, "season": None}
                soup_cs = _tl_fetch(f"{DATA_URL}/course", params={"toban": pno})
                if soup_cs:
                    result["course"] = parse_course_stats(soup_cs)
                soup_season = _tl_fetch(f"{DATA_URL}/season", params={"toban": pno})
                if soup_season:
                    result["season"] = parse_player_season(soup_season)
                return result

            fetched_player_data: dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=COURSE_MAX_WORKERS) as pool:
                futures = {pool.submit(_fetch_player_stats, pno): pno for pno in to_fetch}
                done_count = 0
                for future in as_completed(futures):
                    pno = futures[future]
                    try:
                        fetched_player_data[pno] = future.result()
                        done_count += 1
                        if done_count % 10 == 0 or done_count == len(to_fetch):
                            log.info("  コース別成績取得進捗: %d/%d名", done_count, len(to_fetch))
                    except Exception as e:
                        log.warning("  %s: フェッチ失敗 %s", pno, e)

            # ── DB書き込み（ロック再取得） ────────────────────────────
            acquire_write_lock(wait=True, timeout=120)

            for player_no in to_fetch:
                data = fetched_player_data.get(player_no)
                if not data:
                    continue

                # コース別成績保存
                cs = data.get("course")
                if cs is not None:
                    save_course_stats(conn, player_no, cs)
                    save_course_stats_log(conn, player_no, bool(cs))
                    if not cs:
                        log.debug("  %s: コース別成績なし（公式サイトにデータなし）", player_no)
                    conn.commit()

                # シーズン成績保存
                season_data = data.get("season")
                if season_data is not None:
                    # 決まり手率: DB実績から計算して上書き
                    trick_rates = calc_trick_rates_from_db(conn, player_no)
                    season_data.update(trick_rates)
                    rows = conn.execute("""
                        SELECT e.race_id FROM entries e
                        JOIN races r ON r.id = e.race_id
                        WHERE e.player_no=? AND r.date=?
                    """, (player_no, TODAY)).fetchall()
                    for (rid,) in rows:
                        save_player_season(conn, rid, player_no, season_data)
                    conn.commit()
                    log.info("    %s: 決まり手率 逃げ=%.1f%% 差し=%.1f%% まくり=%.1f%% (DB実績%d件)",
                             player_no,
                             season_data.get("nige_rate") or 0,
                             season_data.get("sashi_rate") or 0,
                             season_data.get("makuri_rate") or 0,
                             sum(1 for _ in conn.execute(
                                 "SELECT 1 FROM race_result_entries WHERE player_no=? AND rank=1",
                                 (player_no,)
                             )))

    # WALのパッシブチェックポイント（他プロセスをブロックしない安全な方式）
    # 注意: TRUNCATE は他のバックアッププロセスのread snapshotを無効化しDB破損を招く可能性があるため使用禁止
    # SQLiteの自動チェックポイント(1000ページ)に任せる方針。手動ではPASSIVEのみ許可。
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    conn.close()
    log.info("=== 完了: %s に保存しました ===", DB_PATH)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="時間制限(22:00〜08:00)を無視して強制実行（手動補完用）")
    args = parser.parse_args()
    main(force=args.force)
