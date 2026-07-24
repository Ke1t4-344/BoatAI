# 競艇会場 オリジナル展示タイム URL一覧

調査日: 2026-06-28  
参考: 競艇日和 `IsOriginalTenji()` + 各会場サイト調査

## 非対応会場（スキップ）
- **03 江戸川**: オリジナル展示なし
- **09 津**: オリジナル展示なし
- **24 大村**: 準備中（2026-06-28時点）

---

## タイプA: `group-cyokuzen.php` 型（10会場）

URL: `https://{domain}/modules/yosou/group-cyokuzen.php?day={YYYYMMDD}&race={R}&kind=2`

| place_no | 会場名 | ドメイン |
|---|---|---|
| 06 | 浜名湖 | www.boatrace-hamanako.jp |
| 08 | 常滑 | www.boatrace-tokoname.jp |
| 10 | 三国 | www.boatrace-mikuni.jp ← 動作確認済み |
| 13 | 尼崎 | www.boatrace-amagasaki.jp |
| 14 | 鳴門 | www.n14.jp |
| 19 | 下関 | www.boatrace-shimonoseki.jp |
| 20 | 若松 | www.wmb.jp |
| 21 | 芦屋 | www.boatrace-ashiya.com |
| 23 | 唐津 | www.boatrace-karatsu.jp |

HTMLパース対象: `data-kind=2` タブ内の展示データテーブル

---

## タイプB: `cyokuzen.php` 型（group なし）（5会場）

URL: `https://{domain}/modules/yosou/cyokuzen.php?day={YYYYMMDD}&race={R}&kind=2`

| place_no | 会場名 | ドメイン |
|---|---|---|
| 01 | 桐生 | www.kiryu-kyotei.com |
| 05 | 多摩川 | www.boatrace-tamagawa.com |
| 11 | びわこ | www.boatrace-biwako.jp |
| 18 | 徳山 | www.boatrace-tokuyama.jp |
| 22 | 福岡 | www.boatrace-fukuoka.com |

※多摩川・福岡・鳴門は展示タイムのみの可能性あり（shukai/mawariashi/chokusen が取れない場合あり）

---

## タイプC/D: 個別対応が必要な会場

| place_no | 会場名 | パターン | 備考 |
|---|---|---|---|
| 02 | 戸田 | XML API | `/xml/kaisai/{YYYYMMDD}/race_table_original_{RR}.xml` (RR=2桁) |
| 04 | 平和島 | 静的HTML | `/asp/kyogi/04/sp/yoso05{RR}.htm` (日付指定不可) |
| 07 | 蒲郡 | URL直埋め込み | `/asp/gamagori/sp/kyogi/kyogihtml/recomend/recomend{YYYYMMDD}07{RR}.htm` |
| 12 | 住之江 | 静的HTML | `/asp/kyogi/12/pc/st0201.htm` (固定URL, 常に最新) |
| 15 | 丸亀 | 静的HTML | 節番号が必要、要再確認 |
| 16 | 児島 | 静的HTML | `/asp/kyogi/16/sp/yoso0501.htm` (固定URL) |
| 17 | 宮島 | POST API | `POST /race_common/require/kaisai_reload.php?race=R&date=YYYYMMDD` |
| 24 | 大村 | 独自 | `/yosou/chokuzen.php?day={YYYYMMDD}&race={RR}&if=1` (準備中) |

---

## 実装優先度

### Phase 1（タイプA/B: 14会場、共通パーサーで対応可）
桐生・浜名湖・常滑・三国・びわこ・尼崎・鳴門・徳山・下関・若松・芦屋・福岡・唐津

### Phase 2（個別実装: 5会場）
戸田(XML)・蒲郡・宮島・住之江・児島

### Phase 3（要調査: 3会場）
平和島・丸亀・大村

---

## HTMLパース構造（タイプA/B 共通）

三国で確認済みの構造:
```html
<!-- data-kind=2 タブ (オリジナル展示データ) -->
<div data-kind="2">
  <table>
    <!-- 各艇の行: boat_no, 周回タイム, 周り足, 直線 -->
  </table>
</div>
```

値のフォーマット: 小数点付き秒（例: 37.52, 5.71, 6.89）
