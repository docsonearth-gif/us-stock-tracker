# 美股 RS 排名與大盤廣度（每日自動）

羅傑交易系統的美股版，台股 `tw-stock-tracker` 的移植。每個交易日收盤後自動下載美國主要交易所（NYSE、Nasdaq、AMEX）全部普通股，計算：

- 個股 **RS 評分**（1–99）、短期 RS、Minervini **第②階段**、每日選股清單
- **大盤廣度**（52 週新高／新低、出貨日、市場狀態）與**市場寬度**（站上 20／50／200 日線比例，全部／NYSE／Nasdaq）
- 大盤修正時的 **Utility Screen**（基準 S&P 500）
- **VCP 先決條件**、**52 週新高**名單

已排除：ETF、權證、單位、特別股、SPAC、OTC。

---

## 什麼時候更新

| 台北時間 | 內容 |
|---|---|
| **週二～週六 06:30** | 前一個美股交易日的行情、RS、大盤廣度、`RS_美股.xlsx` |

GitHub 排程常延遲。想馬上跑：**Actions → 美股 RS 每日計算 → Run workflow**（或改 `run.txt` 推上來）。

漏跑幾天沒關係，下一次會自動補齊（最多往回 10 天）。

---

## 資料來源

| 期間 | 來源 | 說明 |
|---|---|---|
| 訂閱 EODHD 的那個月 | EODHD | 第一次執行自動回補約兩年；每天和 Yahoo 對帳（`data/compare.csv`）；Yahoo 資料不完整時自動改用 EODHD |
| 之後 | yfinance（Yahoo） | 股票清單改用 Nasdaq 官方上市清單（免費） |
| 回測 | EODHD 長歷史 | 2014 年起＋已下市股票，存在 **Releases** 附件（`us_history_long.tar`），不放在 repo 裡 |

**還原股價**：每日檔存當天「含分割、含配息」的報酬率（`ret`），計算時連乘回去。之後再分割或配息，舊資料都不用改，Yahoo 和 EODHD 也能直接接在一起。

**取消 EODHD 前要確認**：`data/compare.csv` 最近幾天「收盤差>0.5%檔數」「報酬率差>0.5點檔數」都很少（個位數），「成交量比中位數」接近 1。

---

## 檔案

### `RS_美股.xlsx`（每個工作表 A1 是資料日期）

| 工作表 | 內容 |
|---|---|
| 大盤廣度 | 近 60 天：新高／新低、新高股收紅收黑與「新高大跌≥5%」、第②階段比例、站上 20／50／200MA 比例（全部／NYSE／Nasdaq）、S&P 與 Nasdaq 出貨日、指數廣度背離、市場狀態 |
| 52週新高 | 當天創 52 週新高的股票，依成交額排序 |
| Utility Screen | S&P 500 距高點 21–200 個交易日才有名單 |
| 必要條件候選 | 第②階段＋RS ≥ 80＋股價 ≥ 10 美元＋50 日均成交額 ≥ 2,000 萬美元 |
| VCP先決條件 | 同 TradingView `trend_template.pine`（不看 150MA） |
| 全部股票 | 每檔普通股的 RS、短期 RS、第②階段、距高點、距 50 日線、RSI14、成交額 |

板塊（自訂主題、產業）之後加入。

### 背景資料（`data/`）

`daily/`（每日行情，約 560 個交易日）、`stocks.csv`（股票清單）、`index.csv`（S&P 500、Nasdaq）、`market_breadth.csv`、`compare.csv`（Yahoo vs EODHD 對帳）

---

## 和台股版的差異

| 項目 | 台股 | 美股 |
|---|---|---|
| 市場寬度均線 | 20／60／240 | 20／50／200 |
| 出貨日成交量 | 全市場成交金額 | S&P 用 NYSE 股票成交量；Nasdaq 用 Nasdaq 股票成交量 |
| 出貨警訊 | 新高股收黑 ≥ 40% 或新高跌停 ≥ 2 | 新高股收黑 ≥ 40% 或新高股收跌 ≥ 5% 有 5 檔以上 |
| RS 排名範圍 | 全部普通股 | 股價 ≥ 1 美元的普通股 |
| Utility 自低點 RS | 低點後 5 天 | 低點後 3 天（沒有漲停擠在一起的問題） |

## 參數（在 workflow 的 `env:` 修改）

`MIN_RS`、`MIN_PRICE`（美元）、`MIN_DOLLAR_VOL_M`（百萬美元）、`RS_MIN_PRICE`、`NH_DROP`、`NH_DROP_N`、`UTILITY_*`（同台股版，成交額門檻改 `UTILITY_MIN_DOLLAR_VOL_M`）

## 程式

| 檔案 | 做什麼 |
|---|---|
| `fetch_us.py` | 每日下載（yfinance）、補缺日、EODHD 備援與對帳 |
| `backfill_eodhd.py` | EODHD 回補：`working`（兩年，每日用）／`long`（2014 起＋下市股，回測用） |
| `rs_us.py` | RS、廣度、Utility Screen、Excel |
| `common.py` | 共用工具 |
