"""
每天下載美股行情（EODHD Bulk API：一次呼叫抓整個美國市場某一天），存成 data/daily/YYYY-MM-DD.csv.gz

流程
1. 更新股票清單（EODHD；金鑰失效時改用 Nasdaq 官方上市清單）
2. 第一次執行、歷史不夠時：自動回補（backfill_eodhd.py 的 working 模式）
3. 用 S&P 500 的日線找出「最後一個已存日期」之後、已收盤的交易日，逐日用 Bulk API 補上
   （排程漏跑幾天，下一次會自動補齊）
   ret＝(收盤＋當天除息的股利) ÷ (前一日收盤 ÷ 分割比例) − 1
4. 家數太少（資料還沒出齊）就先不寫，等下次
5. 只保留最近 HIST_DAYS 個交易日

環境變數
  HIST_DAYS     保留幾個交易日（預設 560）
  SOURCE        eodhd（預設）／yahoo（備用：yfinance）
  MIN_COVERAGE  當天家數至少要有前一天的幾成才寫入（預設 0.85）
  VERIFY_DATE   YYYY-MM-DD：只驗證，不寫檔。用 Bulk API 重算那一天，和已存的資料（回補時用還原價算的）比對
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
SOURCE = os.getenv("SOURCE", "eodhd")
COMPARE = os.getenv("COMPARE", "0") == "1"
VERIFY_DATE = os.getenv("VERIFY_DATE", "")
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
        new[k] = {**v, "sector": o.get("sector", ""), "industry": o.get("industry", ""), "ipo_year": o.get("ipo_year", ""), "shares": o.get("shares", ""),
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

def bulk_bars(day):
    """EODHD Bulk：某一天整個美國市場的日線 → {code: bar}。"""
    return {norm_code(b.get("code", "")): b for b in (eodhd_get("eod-bulk-last-day/US", {"date": day}) or [])}


def eodhd_bulk(day, prev_rows):
    """EODHD 一次抓整個美國市場某一天。ret＝(收盤＋股利) ÷ (前一日收盤 ÷ 分割比例) − 1。
    有分割、或漲跌超過 ±35% 的股票（反分割常漏在分割清單外），改用該檔自己的還原價重算。"""
    bars = bulk_bars(day)
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
    for c, b in bars.items():
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
    fix_returns(day, rows, prev_rows, set(splits))
    return rows


def fix_returns(day, rows, prev_rows, split_codes):
    """分割或大漲大跌的股票：抓該檔近 10 天日線，用 adjusted_close 算當天報酬（和回補時同一個方法）。"""
    stocks = load_stocks()
    # 分割清單上的股票，公式已經用了分割比例；只有算出來仍超過 ±35%（或算不出來）才重算。
    # 分割當天 EODHD 的還原價有時還沒調整，所以兩種算法取變動較小的那個。
    cand = [c for c, r in rows.items() if c in stocks and c in prev_rows and
            (r["ret"] is None or abs(r["ret"]) > 0.35)]
    if not cand:
        return
    start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=12)).date().isoformat()
    fixed = []
    for c in cand[:150]:
        try:
            bars = [b for b in (eodhd_get(f"eod/{c}.US", {"from": start, "to": day}) or []) if b.get("adjusted_close")]
        except Exception:
            continue
        if len(bars) >= 2 and bars[-1]["date"] == day and bars[-2]["adjusted_close"] > 0:
            new = bars[-1]["adjusted_close"] / bars[-2]["adjusted_close"] - 1
            if rows[c]["ret"] is not None and abs(new) >= abs(rows[c]["ret"]):
                continue
            if rows[c]["ret"] is None or abs(new - rows[c]["ret"]) > 0.005:
                fixed.append(f"{c} {(rows[c]['ret'] or 0) * 100:+.1f}%→{new * 100:+.1f}%")
            rows[c]["ret"] = new
    if fixed:
        print(f"  {day} 用還原價重算報酬 {len(fixed)} 檔：{'、'.join(fixed[:20])}")


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


def eodhd_index(start, end):
    out = {}
    for col, (eod, _) in INDEXES.items():
        try:
            for b in eodhd_get(f"eod/{eod}", {"from": start, "to": end}) or []:
                d = out.setdefault(b["date"], {})
                d[col] = b.get("close") or ""
                d[f"{col}_vol"] = b.get("volume") or ""
        except Exception as e:
            print(f"指數 {col} 失敗：{e}")
    return out


def as_float_rows(day_rows):
    out = {}
    for c, r in day_rows.items():
        rr = dict(r)
        for k in ("open", "high", "low", "close", "volume", "ret"):
            rr[k] = float(r[k]) if r.get(k) not in (None, "") else None
        out[c] = rr
    return out


def verify(day):
    """用 Bulk API 重算某一天，和回補時（用 adjusted_close 算）存的資料比對。"""
    days = have_days()
    if day not in days or days.index(day) == 0:
        print(f"沒有 {day} 或前一天的資料，無法驗證")
        return
    prev = load_day(days[days.index(day) - 1])
    stored = as_float_rows(load_day(day))
    e = eodhd_bulk(day, prev)
    e = {c: r for c, r in e.items() if c in stored or c in load_stocks()}
    global COMPARE_CSV
    COMPARE_CSV = os.path.join(LOGS, "verify.csv")
    compare(day, stored, e)
    both = [c for c in stored if c in e and stored[c]["ret"] is not None and e[c]["ret"] is not None]
    bad = sorted(both, key=lambda c: -abs(stored[c]["ret"] - e[c]["ret"]))[:15]
    print("報酬率差最大的 15 檔（已存 vs Bulk，%）：")
    for c in bad:
        print(f"  {c:8s} {stored[c]['ret'] * 100:8.2f} {e[c]['ret'] * 100:8.2f}")


# ---------- 主程式 ----------

def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    if VERIFY_DATE:
        return verify(VERIFY_DATE)
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
    closed = last_closed_day()
    print(f"已有資料到 {last}；最後一個已收盤的交易日：{closed}；來源 {SOURCE}")

    # 交易日：以 S&P 500 有收盤的日子為準
    if SOURCE == "yahoo":
        idx_new = yahoo_index(start, (date.today() + timedelta(days=1)).isoformat())
    else:
        idx_new = eodhd_index(start, closed)
    idx_new = {d: v for d, v in idx_new.items() if d <= closed}
    trade_days = sorted(d for d, v in idx_new.items() if v.get("spx") and d > last)
    idx = load_index()
    for d, v in idx_new.items():
        idx.setdefault(d, {}).update(v)
    save_index(idx)
    if not trade_days:
        print("沒有新的交易日（假日或資料尚未更新）。")

    codes = sorted(stocks)
    prev = load_day(last) if days else {}
    prev_n = len(prev) or len(codes) * 0.6

    frames = {}
    if trade_days and SOURCE == "yahoo":
        t0 = time.time()
        frames = yahoo_rows(yahoo_window(codes, start, (date.today() + timedelta(days=1)).isoformat()), trade_days)
        print(f"Yahoo 下載完成（{(time.time() - t0) / 60:.1f} 分鐘）")

    for d in trade_days:
        if SOURCE == "yahoo":
            rows = frames.get(d, [])
            rmap = {r["code"]: r for r in rows}
            if COMPARE and EODHD_KEY:
                try:
                    compare(d, rmap, {c: r for c, r in eodhd_bulk(d, prev).items() if c in stocks})
                except Exception as e:
                    print(f"  對帳失敗：{e}")
        else:
            try:
                rmap = {c: r for c, r in eodhd_bulk(d, prev).items() if c in stocks}
            except Exception as e:
                print(f"  EODHD Bulk {d} 失敗：{e}")
                break
            rows = list(rmap.values())
        if len(rows) < prev_n * MIN_COVERAGE:
            print(f"  {d}: 只有 {len(rows)} 檔（前一天 {int(prev_n)} 檔），資料可能還沒出齊，這天先不寫，下次再補")
            break  # 後面的日子也先不寫，避免中間留洞
        save_day(d, rows)
        print(f"  {d}: 寫入 {len(rows)} 檔（{SOURCE}）")
        prev, prev_n = rmap, len(rows)

    days = have_days()
    for k in days[:-HIST_DAYS] if len(days) > HIST_DAYS else []:
        os.remove(day_path(k))
    print(f"目前共有 {len(have_days())} 個交易日的資料。")


if __name__ == "__main__":
    sys.exit(main())
