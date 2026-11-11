"""
每天下載美股行情（yfinance），存成 data/daily/YYYY-MM-DD.csv.gz

流程
1. 更新股票清單：有 EODHD 金鑰時用 EODHD；沒有（訂閱到期後）改用 Nasdaq 官方的上市清單
2. 第一次執行、歷史不夠時：自動用 EODHD 回補（backfill_eodhd.py 的 working 模式）
3. 用 yfinance 下載「最後一個已存日期往前 10 天」到今天，補上所有缺的交易日
   （排程漏跑幾天，下一次會自動補齊）
4. Yahoo 資料不完整（家數太少）時：有 EODHD 金鑰就改用 EODHD Bulk API；沒有就先不寫，等下次
5. 對帳（有 EODHD 金鑰時自動）：同一天 Yahoo 與 EODHD 的收盤、成交量、報酬率比對，
   摘要寫進 data/compare.csv
6. 只保留最近 HIST_DAYS 個交易日

環境變數
  HIST_DAYS     保留幾個交易日（預設 560）
  SOURCE        yahoo（預設）／eodhd（強制用 EODHD Bulk API）
  COMPARE       1／0：要不要和 EODHD 對帳（預設：有金鑰就對帳）
  MIN_COVERAGE  當天家數至少要有前一天的幾成才寫入（預設 0.85）
"""

import csv
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from common import (BAD_SUFFIX, DATA, EODHD_KEY, INDEXES, ROOT, NOT_COMMON, day_path, eodhd_get, eodhd_symbols,
                    have_days, last_closed_day, load_day, load_index, load_stocks, norm_code, save_day, save_index,
                    save_stocks)

HIST_DAYS = int(os.getenv("HIST_DAYS", "560"))
SOURCE = os.getenv("SOURCE", "yahoo")
COMPARE = os.getenv("COMPARE", "1" if EODHD_KEY else "0") == "1"
MIN_COVERAGE = float(os.getenv("MIN_COVERAGE", "0.85"))
CHUNK = 200
COMPARE_CSV = os.path.join(DATA, "compare.csv")
LOGS = os.path.join(ROOT, "logs")


# ---------- 1. 股票清單 ----------

def nasdaq_trader_symbols():
    """Nasdaq 官方每天更新的上市清單（免費）。回傳 {code: {...}}。"""
    out = {}
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
    for fname, sym_col, ex_col in (("nasdaqlisted.txt", "Symbol", None),
                                   ("otherlisted.txt", "ACT Symbol", "Exchange")):
        txt = requests.get(base + fname, timeout=60, headers={"User-Agent": "Mozilla/5.0"}).text
        rows = list(csv.DictReader(txt.splitlines(), delimiter="|"))
        for r in rows:
            sym = (r.get(sym_col) or "").strip()
            if not sym or sym.startswith("File Creation"):
                continue
            if r.get("ETF") == "Y" or r.get("Test Issue") == "Y":
                continue
            name = (r.get("Security Name") or "").strip()
            if NOT_COMMON.search(name):
                continue
            ex = {"N": "NYSE", "A": "AMEX", "P": "NYSE ARCA", "Z": "BATS", "V": "IEX"}.get(
                (r.get(ex_col) or "").strip(), "NASDAQ") if ex_col else "NASDAQ"
            code = norm_code(sym)
            if BAD_SUFFIX.search(code):
                continue
            out[code] = {"code": code, "name": name, "exchange": ex}
    return out


def update_universe():
    old = load_stocks()
    today = date.today().isoformat()
    try:
        if EODHD_KEY:
            cur, src = eodhd_symbols(), "eodhd"
        else:
            cur, src = nasdaq_trader_symbols(), "nasdaqtrader"
    except Exception as e:
        print(f"股票清單更新失敗，沿用舊清單：{e}")
        return old
    if len(cur) < 3000:
        print(f"股票清單只有 {len(cur)} 檔，看起來不完整，沿用舊清單")
        return old
    new = {}
    for k, v in cur.items():
        o = old.get(k, {})
        new[k] = {**v, "sector": o.get("sector", ""), "industry": o.get("industry", ""),
                  "source": src, "first_seen": o.get("first_seen") or today}
    new = {k: v for k, v in new.items() if not BAD_SUFFIX.search(k)}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    if old:
        print(f"股票清單（{src}）：{len(new)} 檔；新增 {len(added)}"
              f"{'：' + '、'.join(added[:15]) if added else ''}；移除 {len(removed)}")
    save_stocks(new)
    return new


# ---------- 3. yfinance ----------

def yf_download(tickers, start, end):
    import yfinance as yf
    df = yf.download(tickers, start=start, end=end, interval="1d", auto_adjust=False, actions=False,
                     group_by="ticker", threads=True, progress=False, timeout=30)
    if df is None or df.empty:
        return {}
    out = {}
    if not isinstance(df.columns, pd.MultiIndex):
        df.columns = pd.MultiIndex.from_product([[tickers[0]], df.columns])
    for t in df.columns.get_level_values(0).unique():
        d = df[t].dropna(subset=["Close"])
        if len(d):
            out[t] = d
    return out


def yahoo_window(codes, start, end):
    got = {}
    for i in range(0, len(codes), CHUNK):
        part = codes[i:i + CHUNK]
        try:
            got.update(yf_download(part, start, end))
        except Exception as e:
            print(f"  第 {i // CHUNK + 1} 批失敗：{e}")
        time.sleep(1.5)
    missing = [c for c in codes if c not in got]
    if missing and len(missing) < len(codes) * 0.5:
        # 漏掉的再試一次（小批）
        time.sleep(10)
        for i in range(0, len(missing), 50):
            try:
                got.update(yf_download(missing[i:i + 50], start, end))
            except Exception:
                pass
            time.sleep(2)
    return got


def yahoo_rows(frames, keep_days):
    """{ticker: DataFrame} → {day: [row]}，ret 用同一次下載的 Adj Close 計算。"""
    by_day = {d: [] for d in keep_days}
    for t, d in frames.items():
        d = d.sort_index()
        adj = d["Adj Close"] if "Adj Close" in d else d["Close"]
        adj = adj.where(adj > 0)
        ret = adj / adj.shift(1) - 1
        for ts, r in d.iterrows():
            k = ts.strftime("%Y-%m-%d")
            if k not in by_day:
                continue
            by_day[k].append({"code": t, "open": r.get("Open"), "high": r.get("High"),
                              "low": r.get("Low"), "close": r.get("Close"), "volume": r.get("Volume"),
                              "ret": ret.loc[ts], "src": "yahoo"})
    return by_day


def yahoo_index(start, end):
    out = {}
    for col, (_, ysym) in INDEXES.items():
        try:
            f = yf_download([ysym], start, end).get(ysym)
        except Exception as e:
            print(f"指數 {ysym} 失敗：{e}")
            continue
        if f is None:
            continue
        for ts, r in f.iterrows():
            d = out.setdefault(ts.strftime("%Y-%m-%d"), {})
            d[col] = r["Close"]
            d[f"{col}_vol"] = r.get("Volume", "")
    return out


# ---------- 4. EODHD Bulk（備援、對帳） ----------

def eodhd_bulk(day, prev_rows):
    """EODHD 一次抓整個美國市場某一天。ret＝(收盤＋股利) ÷ (前一日收盤 ÷ 分割比例) − 1。"""
    bars = eodhd_get("eod-bulk-last-day/US", {"date": day}) or []
    splits, divs = {}, {}
    try:
        for s in eodhd_get("eod-bulk-last-day/US", {"date": day, "type": "splits"}) or []:
            a, b = str(s.get("split", "1/1")).split("/")
            splits[norm_code(s["code"])] = float(a) / float(b)
        for s in eodhd_get("eod-bulk-last-day/US", {"date": day, "type": "dividends"}) or []:
            divs[norm_code(s["code"])] = float(s.get("value") or s.get("dividend") or 0)
    except Exception as e:
        print(f"  EODHD 分割／股利資料失敗（報酬率可能少算除息）：{e}")
    rows = {}
    for b in bars:
        c = norm_code(b.get("code", ""))
        close = b.get("close")
        if not close:
            continue
        p = prev_rows.get(c)
        ret = None
        if p and p.get("close"):
            pc = float(p["close"]) / splits.get(c, 1.0)
            ret = (close + divs.get(c, 0.0)) / pc - 1 if pc > 0 else None
        rows[c] = {"code": c, "open": b.get("open"), "high": b.get("high"), "low": b.get("low"),
                   "close": close, "volume": b.get("volume"), "ret": ret, "src": "eodhd"}
    return rows


def compare(day, yahoo, eod):
    """Yahoo vs EODHD 同一天的差異摘要。"""
    both = sorted(set(yahoo) & set(eod))
    if not both:
        return
    rows = []
    for c in both:
        y, e = yahoo[c], eod[c]
        yc, ec = float(y["close"]), float(e["close"])
        cd = (yc / ec - 1) * 100 if ec else None
        vr = (float(y["volume"]) / float(e["volume"])) if y.get("volume") and e.get("volume") else None
        rd = ((y["ret"] - e["ret"]) * 100) if y.get("ret") is not None and e.get("ret") is not None \
            and y["ret"] == y["ret"] else None
        rows.append((c, cd, vr, rd))
    df = pd.DataFrame(rows, columns=["code", "收盤差%", "成交量比(Y/E)", "報酬率差(百分點)"])
    s = {
        "日期": day, "兩邊都有": len(both), "只有Yahoo": len(set(yahoo) - set(eod)),
        "只有EODHD": len(set(eod) - set(yahoo)),
        "收盤差>0.5%檔數": int((df["收盤差%"].abs() > 0.5).sum()),
        "報酬率差>0.5點檔數": int((df["報酬率差(百分點)"].abs() > 0.5).sum()),
        "成交量比中位數": round(df["成交量比(Y/E)"].median(), 3),
        "成交量差>20%檔數": int(((df["成交量比(Y/E)"] - 1).abs() > 0.2).sum()),
    }
    new = not os.path.exists(COMPARE_CSV)
    with open(COMPARE_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(s))
        if new:
            w.writeheader()
        w.writerow(s)
    os.makedirs(LOGS, exist_ok=True)
    worst = df.reindex(df["收盤差%"].abs().sort_values(ascending=False).index).head(40)
    worst.to_csv(os.path.join(LOGS, f"compare_{day}.csv"), index=False, encoding="utf-8-sig")
    print(f"  對帳 {day}：{s}")


# ---------- 主程式 ----------

def main():
    os.makedirs(DATA, exist_ok=True)
    stocks = update_universe()

    if len(have_days()) < 300:
        if EODHD_KEY:
            print("歷史不足 300 天，先用 EODHD 回補…")
            import backfill_eodhd
            backfill_eodhd.run_working()
            stocks = load_stocks()
        else:
            print("注意：歷史不足 300 天，而且沒有 EODHD 金鑰，RS 會是空白")

    days = have_days()
    last = days[-1] if days else (date.today() - timedelta(days=30)).isoformat()
    start = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=10)).date().isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    print(f"已有資料到 {last}；下載 {start} 之後")

    # 交易日：以 S&P 500 有收盤的日子為準
    closed = last_closed_day()
    idx_new = {d: v for d, v in yahoo_index(start, end).items() if d <= closed}
    trade_days = sorted(d for d, v in idx_new.items() if v.get("spx") and d > last)
    print(f"最後一個已收盤的交易日：{closed}")
    if not trade_days and SOURCE != "eodhd":
        print("沒有新的交易日（假日或 Yahoo 尚未更新）。")
    idx = load_index()
    for d, v in idx_new.items():
        idx.setdefault(d, {}).update(v)
    save_index(idx)

    codes = sorted(stocks)
    prev = load_day(last) if days else {}
    prev_n = len(prev) or len(codes) * 0.6

    by_day = {}
    if trade_days and SOURCE == "yahoo":
        t0 = time.time()
        frames = yahoo_window(codes, start, end)
        print(f"Yahoo：{len(frames)}/{len(codes)} 檔有資料（{(time.time() - t0) / 60:.1f} 分鐘）")
        by_day = yahoo_rows(frames, trade_days)
    elif SOURCE == "eodhd":
        trade_days = trade_days or [os.getenv("TARGET_DATE", closed)]

    for d in trade_days:
        rows = by_day.get(d, [])
        ymap = {r["code"]: r for r in rows}
        need_eod = SOURCE == "eodhd" or len(rows) < prev_n * MIN_COVERAGE
        emap = {}
        if EODHD_KEY and (need_eod or COMPARE):
            try:
                emap = eodhd_bulk(d, prev)
                emap = {c: r for c, r in emap.items() if c in stocks}
            except Exception as e:
                print(f"  EODHD Bulk {d} 失敗：{e}")
        if need_eod:
            if emap and len(emap) >= prev_n * MIN_COVERAGE:
                print(f"  {d}: Yahoo 只有 {len(rows)} 檔，改用 EODHD（{len(emap)} 檔）")
                rows, ymap = list(emap.values()), emap
            else:
                print(f"  {d}: 資料不完整（Yahoo {len(rows)} 檔、EODHD {len(emap)} 檔，"
                      f"前一天 {int(prev_n)} 檔），這天先不寫，下次再補")
                break  # 後面的日子也先不寫，避免中間留洞
        elif emap and COMPARE:
            compare(d, ymap, emap)
        save_day(d, rows)
        print(f"  {d}: 寫入 {len(rows)} 檔（{rows[0]['src'] if rows else ''}）")
        prev, prev_n = ymap, len(rows)

    # 6. 刪掉超出保留期的舊檔
    days = have_days()
    for k in days[:-HIST_DAYS] if len(days) > HIST_DAYS else []:
        os.remove(day_path(k))
    print(f"目前共有 {len(have_days())} 個交易日的資料。")


if __name__ == "__main__":
    sys.exit(main())
