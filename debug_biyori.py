#!/usr/bin/env python3
"""
競艇日和のHTMLを確認するデバッグスクリプト
実行: python3 debug_biyori.py
"""
import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
})

url = "https://kyoteibiyori.com/race_shusso.php"
params = {"place_no": 2, "race_no": 1, "hiduke": "20210103", "slider": 4}

print(f"URL: {url}?{'&'.join(f'{k}={v}' for k,v in params.items())}")
resp = SESSION.get(url, params=params, timeout=15)
print(f"Status: {resp.status_code}, Size: {len(resp.text)} bytes")

# HTMLをファイルに保存
with open("debug_biyori.html", "w", encoding=resp.encoding or "utf-8") as f:
    f.write(resp.text)
print("→ debug_biyori.html に保存しました")

# テーブル情報を表示
soup = BeautifulSoup(resp.text, "html.parser")
tables = soup.find_all("table")
print(f"\nテーブル数: {len(tables)}")

for i, tbl in enumerate(tables):
    rows = tbl.find_all("tr")
    if not rows:
        continue
    labels = []
    for tr in rows[:5]:
        cells = tr.find_all(["th", "td"])
        if cells:
            labels.append(cells[0].get_text(strip=True))
    print(f"\n[Table {i}] rows={len(rows)} 先頭ラベル: {labels}")

# キーワード検索
keywords = ["展示", "進入", "ST", "exhibition", "直前"]
print("\n=== キーワード検索 ===")
text = soup.get_text()
for kw in keywords:
    found = kw in text
    print(f"  '{kw}': {'あり' if found else 'なし'}")
