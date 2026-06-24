#!/usr/bin/env python3
"""
boatrace.jp スクレイパー
本日の全会場のレース一覧・出走表・3連単オッズを boatai.db に保存する
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
BASE_URL   = "https://www.boatrace.jp/owpc/pc/race"
DB_PATH    = Path(__file__).parent / "boatai.db"
TODAY      = date.today().strftime("%Y%m%d")
REQ_DELAY  = 1.2  # サーバー負荷軽減

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
        return float(s.strip())
    except ValueError:
        return None


def _int(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s.strip())
    except ValueError:
        return None


# ── 開催会場の取得 ────────────────────────────────────
def fetch_today_venues() -> list[str]:
    """本日開催中の会場コードリストを返す"""
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


# ── レース一覧（最大レース数）取得 ───────────────────
def fetch_max_race_no(venue_code: str) -> int:
    """raceindex ページから当日の最大レース番号を取得"""
    soup = fetch(f"{BASE_URL}/raceindex", params={"jcd": venue_code, "hd": TODAY})
    if soup is None:
        return 12

    race_nos: set[int] = set()
    for a in soup.find_all("a", href=re.compile(r"rno=\d+")):
        m = re.search(r"rno=(\d+)", a["href"])
        if m:
            race_nos.add(int(m.group(1)))

    return max(race_nos) if race_nos else 12


# ── 出走表のパース ────────────────────────────────────
def parse_entries(soup: BeautifulSoup) -> list[dict]:
    """tbody.is-fs12 を解析して6艇分のデータを返す"""
    results = []

    for tbody in soup.select("tbody.is-fs12"):
        trs = tbody.find_all("tr")
        if not trs:
            continue

        first_row_tds = trs[0].find_all("td")
        if len(first_row_tds) < 8:
            continue

        # 艇番: is-boatColorX クラスから取得
        boat_td = first_row_tds[0]
        boat_no = _int(boat_td.get_text(strip=True).replace("１","1").replace("２","2")
                       .replace("３","3").replace("４","4").replace("５","5").replace("６","6"))
        # 全角数字→半角変換
        raw = boat_td.get_text(strip=True)
        boat_no = _int(str(int(raw)) if raw.isdigit() else
                       {"１":1,"２":2,"３":3,"４":4,"５":5,"６":6}.get(raw))
        if boat_no is None:
            continue

        # 選手情報 (td[2])
        info_td = first_row_tds[2]
        divs = info_td.find_all("div")

        player_no = player_class = player_name = age = weight = None

        if divs:
            # div[0]: "4566 / A1"
            no_div = divs[0].get_text(strip=True)
            m = re.match(r"(\d+)\s*/\s*(\S+)", no_div)
            if m:
                player_no    = m.group(1)
                player_class = m.group(2)

            # div[1]: 選手名 (リンク)
            if len(divs) > 1:
                player_name = divs[1].get_text(strip=True)

            # div[2]: "XX/XX\n年齢/体重"
            if len(divs) > 2:
                age_weight = divs[2].get_text(strip=True)
                m2 = re.search(r"(\d+)歳/(\d+\.?\d*)kg", age_weight)
                if m2:
                    age    = _int(m2.group(1))
                    weight = _float(m2.group(2))

        # F数/L数/平均ST (td[3], <br> 区切り)
        fl_td     = first_row_tds[3]
        fl_texts  = [s.strip() for s in fl_td.get_text("\n").split("\n") if s.strip()]
        flying    = late = avg_st = None
        if fl_texts:
            m = re.match(r"F(\d+)", fl_texts[0])
            flying = _int(m.group(1)) if m else None
        if len(fl_texts) > 1:
            m = re.match(r"L(\d+)", fl_texts[1])
            late = _int(m.group(1)) if m else None
        if len(fl_texts) > 2:
            avg_st = _float(fl_texts[2])

        def parse_triple(td_idx: int) -> tuple:
            td = first_row_tds[td_idx]
            vals = [s.strip() for s in td.get_text("\n").split("\n") if s.strip()]
            a = _float(vals[0]) if len(vals) > 0 else None
            b = _float(vals[1]) if len(vals) > 1 else None
            c = _float(vals[2]) if len(vals) > 2 else None
            return a, b, c

        nat_win, nat_2ring, _nat3  = parse_triple(4)
        loc_win, loc_2ring, _loc3  = parse_triple(5)
        motor_no_raw, motor_2ring, _m3 = parse_triple(6)
        boat_hull_raw, boat_2ring, _b3 = parse_triple(7)

        motor_no   = _int(str(int(motor_no_raw))) if motor_no_raw is not None else None
        boat_hull  = _int(str(int(boat_hull_raw))) if boat_hull_raw is not None else None

        results.append({
            "boat_no":             boat_no,
            "player_no":           player_no,
            "player_name":         player_name,
            "player_class":        player_class,
            "age":                 age,
            "weight":              weight,
            "flying_count":        flying,
            "late_count":          late,
            "avg_start_timing":    avg_st,
            "national_win_rate":   nat_win,
            "national_2ring_rate": nat_2ring,
            "local_win_rate":      loc_win,
            "local_2ring_rate":    loc_2ring,
            "motor_no":            motor_no,
            "motor_2ring_rate":    motor_2ring,
            "boat_no_hull":        boat_hull,
            "boat_2ring_rate":     boat_2ring,
        })

    return results


# ── 3連単オッズのパース ──────────────────────────────
def parse_odds_3t(soup: BeautifulSoup) -> dict[str, float | None]:
    """odds3t ページから { '1-2-3': 8.8, ... } を返す"""
    tables = soup.find_all("table")
    if len(tables) < 2:
        return {}

    table = tables[1]
    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        return {}

    # 1着ボート番号を列順に取得（theadの奇数列ヘッダー）
    first_boats: list[int] = []
    for th in thead.find_all("th"):
        classes = th.get("class", [])
        for c in classes:
            m = re.match(r"is-boatColor(\d)", c)
            if m and "borderLeftNone" not in " ".join(classes):
                first_boats.append(int(m.group(1)))
                break

    num_groups = len(first_boats)  # 通常6
    if num_groups == 0:
        return {}

    combo_map: dict[str, float | None] = {}
    current_second = [None] * num_groups  # 各グループの現在の2着

    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        cell_idx = 0

        for grp in range(num_groups):
            if cell_idx >= len(tds):
                break

            # rowspan セルがあれば 2着を更新
            td = tds[cell_idx]
            if td.get("rowspan"):
                current_second[grp] = _int(td.get_text(strip=True))
                cell_idx += 1

            if cell_idx + 1 >= len(tds):
                break

            third_no  = _int(tds[cell_idx].get_text(strip=True))
            odds_val  = _float(tds[cell_idx + 1].get_text(strip=True))
            cell_idx += 2

            first  = first_boats[grp]
            second = current_second[grp]
            if first and second and third_no:
                combo_map[f"{first}-{second}-{third_no}"] = odds_val

    return combo_map


# ── DB 書き込みヘルパー ──────────────────────────────
def upsert_venue(conn: sqlite3.Connection, code: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO venues (venue_code, venue_name) VALUES (?,?)",
        (code, VENUE_NAMES.get(code, code)),
    )


def upsert_race(conn: sqlite3.Connection, venue_code: str, race_no: int) -> int:
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


def save_entries(conn: sqlite3.Connection, race_id: int, entries: list[dict]) -> None:
    for e in entries:
        conn.execute("""
            INSERT INTO entries
              (race_id, boat_no, player_no, player_name, player_class,
               age, weight, flying_count, late_count, avg_start_timing,
               national_win_rate, national_2ring_rate,
               local_win_rate, local_2ring_rate,
               motor_no, motor_2ring_rate, boat_no_hull, boat_2ring_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(race_id, boat_no) DO UPDATE SET
              player_no=excluded.player_no, player_name=excluded.player_name,
              player_class=excluded.player_class, age=excluded.age, weight=excluded.weight,
              flying_count=excluded.flying_count, late_count=excluded.late_count,
              avg_start_timing=excluded.avg_start_timing,
              national_win_rate=excluded.national_win_rate,
              national_2ring_rate=excluded.national_2ring_rate,
              local_win_rate=excluded.local_win_rate,
              local_2ring_rate=excluded.local_2ring_rate,
              motor_no=excluded.motor_no, motor_2ring_rate=excluded.motor_2ring_rate,
              boat_no_hull=excluded.boat_no_hull, boat_2ring_rate=excluded.boat_2ring_rate
        """, (
            race_id, e["boat_no"], e["player_no"], e["player_name"], e["player_class"],
            e["age"], e["weight"], e["flying_count"], e["late_count"], e["avg_start_timing"],
            e["national_win_rate"], e["national_2ring_rate"],
            e["local_win_rate"], e["local_2ring_rate"],
            e["motor_no"], e["motor_2ring_rate"], e["boat_no_hull"], e["boat_2ring_rate"],
        ))


def save_odds(conn: sqlite3.Connection, race_id: int, combo_map: dict) -> None:
    for combo, odds_val in combo_map.items():
        conn.execute("""
            INSERT INTO odds_3t (race_id, combination, odds)
            VALUES (?,?,?)
            ON CONFLICT(race_id, combination) DO UPDATE SET odds=excluded.odds
        """, (race_id, combo, odds_val))


# ── メイン処理 ───────────────────────────────────────
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
        log.info("─── %s (%s) ───", vname, vcode)
        upsert_venue(conn, vcode)
        conn.commit()

        max_rno = fetch_max_race_no(vcode)
        log.info("  レース数: %d", max_rno)

        for rno in range(1, max_rno + 1):
            race_id = upsert_race(conn, vcode, rno)
            conn.commit()

            # 出走表
            soup_list = fetch(f"{BASE_URL}/racelist",
                              params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup_list:
                entries = parse_entries(soup_list)
                save_entries(conn, race_id, entries)
                conn.commit()
                log.info("  %dR 出走表: %d艇", rno, len(entries))
            else:
                log.warning("  %dR 出走表取得失敗", rno)

            # 3連単オッズ
            soup_odds = fetch(f"{BASE_URL}/odds3t",
                              params={"rno": rno, "jcd": vcode, "hd": TODAY})
            if soup_odds:
                combo_map = parse_odds_3t(soup_odds)
                save_odds(conn, race_id, combo_map)
                conn.commit()
                log.info("  %dR 3連単: %d件", rno, len(combo_map))
            else:
                log.warning("  %dR オッズ取得失敗", rno)

    conn.close()
    log.info("=== 完了: %s に保存しました ===", DB_PATH)


if __name__ == "__main__":
    main()
