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

import re
import sqlite3
import time
import logging
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 設定 ──────────────────────────────────────────────
BASE_URL  = "https://www.boatrace.jp/owpc/pc/race"
DATA_URL  = "https://www.boatrace.jp/owpc/pc/data/racersearch"
DB_PATH   = Path(__file__).parent / "boatai.db"
TODAY     = date.today().strftime("%Y%m%d")
REQ_DELAY = 1.2

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
    """)
    conn.commit()


# ── HTTP ユーティリティ ──────────────────────────────
_session = requests.Session()
_session.headers.update(HEADERS)


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

            # 展示タイム・チルト（rowspan=4 の数値セル）
            # is-boatColor / img / is-fBold / labelGroup のセルを除外して判定
            exhibition_time = tilt = None
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
                    exhibition_time = f
                elif exhibition_time is not None and tilt is None:
                    tilt = f

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


def upsert_race(conn, venue_code, race_no) -> int:
    conn.execute("""
        INSERT INTO races (date, venue_code, race_no, race_title)
        VALUES (?,?,?,?)
        ON CONFLICT(date, venue_code, race_no) DO NOTHING
    """, (TODAY, venue_code, race_no, f"{race_no}R"))
    row = conn.execute(
        "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
        (TODAY, venue_code, race_no),
    ).fetchone()
    return row[0]


def save_entries(conn, race_id, entries):
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


def save_odds(conn, race_id, combo_map):
    for combo, odds_val in combo_map.items():
        conn.execute("""
            INSERT INTO odds_3t (race_id, combination, odds) VALUES (?,?,?)
            ON CONFLICT(race_id, combination) DO UPDATE SET odds=excluded.odds
        """, (race_id, combo, odds_val))


def save_course_stats(conn, player_no, stats):
    for s in stats:
        conn.execute("""
            INSERT INTO course_stats
              (player_no,fetched_date,course_no,entry_rate,win_rate_1st,win_rate_2nd,win_rate_3rd,avg_st)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(player_no,fetched_date,course_no) DO UPDATE SET
              entry_rate=excluded.entry_rate, win_rate_1st=excluded.win_rate_1st,
              win_rate_2nd=excluded.win_rate_2nd, win_rate_3rd=excluded.win_rate_3rd,
              avg_st=excluded.avg_st
        """, (player_no, TODAY, s["course_no"], s["entry_rate"],
              s["win_rate_1st"], s["win_rate_2nd"], s["win_rate_3rd"], s["avg_st"]))


def save_before_info(conn, race_id, entries):
    for e in entries:
        conn.execute("""
            INSERT INTO before_info
              (race_id,boat_no,weight,exhibition_time,tilt,exhibit_course,exhibit_st,
               prev_race_venue,prev_race_date,prev_race_no,prev_entry_course,
               prev_start_timing,prev_finish)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(race_id,boat_no) DO UPDATE SET
              weight=excluded.weight, exhibition_time=excluded.exhibition_time,
              tilt=excluded.tilt, exhibit_course=excluded.exhibit_course,
              exhibit_st=excluded.exhibit_st, prev_race_venue=excluded.prev_race_venue,
              prev_race_date=excluded.prev_race_date, prev_race_no=excluded.prev_race_no,
              prev_entry_course=excluded.prev_entry_course,
              prev_start_timing=excluded.prev_start_timing, prev_finish=excluded.prev_finish
        """, (race_id, e["boat_no"], e["weight"], e["exhibition_time"], e["tilt"],
              e["exhibit_course"], e["exhibit_st"], e["prev_race_venue"],
              e["prev_race_date"], e["prev_race_no"], e["prev_entry_course"],
              e["prev_start_timing"], e["prev_finish"]))


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


def save_payouts(conn, race_id, payouts):
    for p in payouts:
        conn.execute("""
            INSERT INTO payouts (race_id,bet_type,combination,payout,popularity)
            VALUES (?,?,?,?,?)
            ON CONFLICT(race_id,bet_type,combination) DO UPDATE SET
              payout=excluded.payout, popularity=excluded.popularity
        """, (race_id, p["bet_type"], p["combination"], p["payout"], p["popularity"]))


def save_meet_standings(conn, venue_code, standings):
    for s in standings:
        conn.execute("""
            INSERT INTO meet_standings
              (date,venue_code,standing_rank,player_no,player_name,player_class,
               points_rate,results_text,total_points,deductions)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date,venue_code,player_no) DO UPDATE SET
              standing_rank=excluded.standing_rank,
              points_rate=excluded.points_rate, results_text=excluded.results_text,
              total_points=excluded.total_points, deductions=excluded.deductions
        """, (TODAY, venue_code, s["rank"], s["player_no"], s["player_name"],
              s["player_class"], s["points_rate"], s["results_text"],
              s["total_points"], s["deductions"]))


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


def fetch_max_race_no(venue_code: str) -> int:
    soup = fetch(f"{BASE_URL}/raceindex", params={"jcd": venue_code, "hd": TODAY})
    if soup is None:
        return 12
    race_nos: set[int] = set()
    for a in soup.find_all("a", href=re.compile(r"rno=\d+")):
        m = re.search(r"rno=(\d+)", a["href"])
        if m:
            race_nos.add(int(m.group(1)))
    return max(race_nos) if race_nos else 12


# ────────────────────────────────────────────────────
# メイン処理
# ────────────────────────────────────────────────────

def main() -> None:
    log.info("=== boatrace.jp スクレイパー 開始 (日付: %s) ===", TODAY)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
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

        max_rno = fetch_max_race_no(vcode)
        log.info("  レース数: %d", max_rno)

        # 今節成績
        soup_rank = fetch(f"{BASE_URL}/pointrank", params={"jcd": vcode, "hd": TODAY})
        if soup_rank:
            standings = parse_meet_standings(soup_rank)
            save_meet_standings(conn, vcode, standings)
            conn.commit()
            log.info("  今節成績: %d名", len(standings))

        # レースごとのデータ取得
        collected_player_nos: set[str] = set()

        for rno in range(1, max_rno + 1):
            race_id = upsert_race(conn, vcode, rno)
            conn.commit()
            log.info("  ── %dR (race_id=%d) ──", rno, race_id)

            # 出走表
            soup_list = fetch(f"{BASE_URL}/racelist",
                              params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup_list:
                entries = parse_entries(soup_list)
                save_entries(conn, race_id, entries)
                conn.commit()
                for e in entries:
                    if e["player_no"]:
                        collected_player_nos.add(e["player_no"])
                log.info("    出走表: %d艇", len(entries))

            # 3連単オッズ
            soup_odds = fetch(f"{BASE_URL}/odds3t",
                              params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup_odds:
                combo_map = parse_odds_3t(soup_odds)
                save_odds(conn, race_id, combo_map)
                conn.commit()
                log.info("    3連単オッズ: %d件", len(combo_map))

            # 直前情報 + 気象
            soup_before = fetch(f"{BASE_URL}/beforeinfo",
                                params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup_before:
                bi_entries, weather = parse_before_info(soup_before)
                save_before_info(conn, race_id, bi_entries)
                save_weather(conn, race_id, weather)
                conn.commit()
                log.info("    直前情報: %d艇 / 気象: %s",
                         len(bi_entries), bool(weather))

            # 確定結果
            soup_result = fetch(f"{BASE_URL}/raceresult",
                                params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup_result:
                res_entries, payouts = parse_race_result(soup_result)
                save_race_result_entries(conn, race_id, res_entries)
                save_payouts(conn, race_id, payouts)
                conn.commit()
                log.info("    確定結果: 着順%d件 / 払戻%d件",
                         len(res_entries), len(payouts))

        # コース別成績（選手ごと・本日未取得分のみ）
        already = {row[0] for row in
                   conn.execute("SELECT player_no FROM course_stats WHERE fetched_date=?",
                                (TODAY,)).fetchall()}
        to_fetch = collected_player_nos - already
        log.info("  コース別成績: %d名取得予定", len(to_fetch))

        for player_no in sorted(to_fetch):
            soup_cs = fetch(f"{DATA_URL}/course", params={"toban": player_no})
            if soup_cs:
                cs = parse_course_stats(soup_cs)
                save_course_stats(conn, player_no, cs)
                conn.commit()

    conn.close()
    log.info("=== 完了: %s に保存しました ===", DB_PATH)


if __name__ == "__main__":
    main()
