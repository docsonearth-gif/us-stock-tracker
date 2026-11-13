"""
用 EODHD 回補美股歷史（訂閱的那個月用；之後每天改由 fetch_us.py 用 yfinance 抓）

兩種模式（環境變數 MODE）
  working（預設）  近 WORK_DAYS 個日曆日的資料 → data/daily/*.csv.gz（每天跑的 RS 用這份）
                   + 股票清單 data/stocks.csv + 指數 data/index.csv
  long             長歷史（LONG_START 起）＋已下市股票，專門給回測用
                   → history/active/、history/delisted/ 每檔一個 csv.gz（不進 repo，
                     由 workflow 打包成 GitHub Release 附件）

其他環境變數
  WORK_DAYS   working 模式回補幾個日曆日（預設 800，約 550 個交易日）
  LONG_START  long 模式起始日（預設 2014-01-01）
  OVERWRITE   1＝已存在的每日檔也覆蓋（預設只補沒有的日期）
  WORKERS     同時連線數（預設 8）
  PER_MIN     每分鐘最多幾次呼叫（預設 700；EODHD 付費方案上限 1000）
  LIMIT       只抓前 N 檔（測試用）
"""

import csv
import gzip
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from common import (DATA, INDEXES, ROOT, eodhd_get, last_closed_day, eodhd_symbols, have_days, load_index,
                    load_stocks, save_day, save_index, save_stocks)

MODE = os.getenv("MODE", "working")
WORK_DAYS = int(os.getenv("WORK_DAYS", "800"))
LONG_START = os.getenv("LONG_START", "2014-01-01")
OVERWRITE = os.getenv("OVERWRITE") == "1"
WORKERS = int(os.getenv("WORKERS", "8"))
PER_MIN = int(os.getenv("PER_MIN", "700"))
LIMIT = int(os.getenv("LIMIT", "0"))
HIST = os.path.join(ROOT, "history")


class Throttle:
    def __init__(self, per_min):
        self.gap = 60.0 / per_min
        self.lock = threading.Lock()
        self.next = time.time()

    def wait(self):
        with self.lock:
            now = time.time()
            if now < self.next:
                time.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap


THROTTLE = Throttle(PER_MIN)


def fetch_eod(eod_code, start):
    THROTTLE.wait()
    j = eodhd_get(f"eod/{eod_code}", {"from": start, "period": "d"})
    return j or []


def to_rows(bars):
    """EODHD 日線 → 加上 ret（用 adjusted_close 算，含分割與配息）。"""
    out, prev_adj, prev_close = [], None, None
    for b in bars:
        c = b.get("close")
        a = b.get("adjusted_close") or c
        if not c or c <= 0:
            continue
        if prev_adj:
            ret = a / prev_adj - 1 if a and prev_adj > 0 else c / prev_close - 1
        else:
            ret = None
        out.append({"date": b["date"], "open": b.get("open"), "high": b.get("high"),
                    "low": b.get("low"), "close": c, "volume": b.get("volume"),
                    "adj": a, "ret": ret})
        prev_adj, prev_close = a, c
    return out


# ---------- working ----------

def run_working():
    start = (date.today() - timedelta(days=WORK_DAYS)).isoformat()
    print(f"[working] 抓 {start} 之後的日線")

    syms = eodhd_symbols()
    print(f"EODHD 普通股（主要交易所，已排除權證／特別股／SPAC）：{len(syms)} 檔")
    old = load_stocks()
    today = date.today().isoformat()
    for k, v in syms.items():
        o = old.get(k, {})
        v.update({"sector": o.get("sector", ""), "industry": o.get("industry", ""), "ipo_year": o.get("ipo_year", ""), "shares": o.get("shares", ""),
                  "source": "eodhd", "first_seen": o.get("first_seen") or today})
    merged = {**old, **syms}
    save_stocks(merged)

    codes = sorted(syms)
    if LIMIT:
        codes = codes[:LIMIT]

    by_day = defaultdict(list)
    ok = empty = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_eod, f"{syms[c]['eod_code']}.US", start): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            try:
                rows = to_rows(f.result())
            except Exception as e:
                fail += 1
                print(f"  {c} 失敗：{e}")
                continue
            if not rows:
                empty += 1
                continue
            ok += 1
            for r in rows:
                by_day[r["date"]].append({**r, "code": c, "src": "eodhd"})
            if i % 500 == 0:
                print(f"  {i}/{len(codes)}（{(time.time() - t0) / 60:.1f} 分鐘）")
    print(f"個股：成功 {ok}、沒有資料 {empty}、失敗 {fail}")

    # 指數
    idx = load_index()
    for col, (eod, _) in INDEXES.items():
        try:
            for b in fetch_eod(eod, start):
                d = idx.setdefault(b["date"], {})
                d[col] = b.get("close") or ""
                d[f"{col}_vol"] = b.get("volume") or ""
            print(f"指數 {col}：OK")
        except Exception as e:
            print(f"指數 {col} 失敗：{e}")
    save_index(idx)

    # 盤中還沒收盤的那天不寫；指數也一樣
    closed = last_closed_day()
    for d in [d for d in by_day if d > closed]:
        print(f"  {d}: 還沒收盤，略過")
        del by_day[d]
    for d in [d for d in idx if d > closed]:
        del idx[d]
    save_index(idx)

    # 每日檔：家數太少的日期（假日、半天資料）不寫
    have = set(have_days())
    sizes = {d: len(v) for d, v in by_day.items()}
    typical = sorted(sizes.values())[len(sizes) // 2] if sizes else 0
    written = skipped = 0
    for d, rows in sorted(by_day.items()):
        if len(rows) < typical * 0.5:
            print(f"  {d}: 只有 {len(rows)} 檔，略過")
            continue
        if d in have and not OVERWRITE:
            skipped += 1
            continue
        save_day(d, rows)
        written += 1
    print(f"寫入每日檔 {written} 天（已存在略過 {skipped} 天），每天約 {typical} 檔")


# ---------- long ----------

def write_hist(folder, code, bars):
    os.makedirs(folder, exist_ok=True)
    with gzip.open(os.path.join(folder, f"{code}.csv.gz"), "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "adjusted_close", "volume"])
        for b in bars:
            w.writerow([b.get("date"), b.get("open"), b.get("high"), b.get("low"), b.get("close"),
                        b.get("adjusted_close"), b.get("volume")])


def run_long():
    print(f"[long] 抓 {LONG_START} 之後的日線（含已下市）")
    groups = {"active": eodhd_symbols(), "delisted": {}}
    try:
        groups["delisted"] = eodhd_symbols(delisted=True)
    except PermissionError as e:
        print(f"下市股清單無法取得（方案可能不含）：{e}")
    for g, syms in groups.items():
        print(f"{g}：{len(syms)} 檔")

    # 清單（含下市）也存一份
    os.makedirs(HIST, exist_ok=True)
    with open(os.path.join(HIST, "symbols.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group", "code", "eod_code", "name", "exchange"])
        for g, syms in groups.items():
            for c, v in sorted(syms.items()):
                w.writerow([g, c, v["eod_code"], v["name"], v["exchange"]])

    stats = {}
    for g, syms in groups.items():
        codes = sorted(syms)[:LIMIT] if LIMIT else sorted(syms)
        folder = os.path.join(HIST, g)
        ok = empty = fail = 0
        t0 = time.time()
        with ThreadPoolExecutor(WORKERS) as ex:
            futs = {ex.submit(fetch_eod, f"{syms[c]['eod_code']}.US", LONG_START): c for c in codes}
            for i, f in enumerate(as_completed(futs), 1):
                c = futs[f]
                try:
                    bars = f.result()
                except Exception as e:
                    fail += 1
                    print(f"  {c} 失敗：{e}")
                    continue
                if not bars:
                    empty += 1
                    continue
                write_hist(folder, c, bars)
                ok += 1
                if i % 1000 == 0:
                    print(f"  {g} {i}/{len(codes)}（{(time.time() - t0) / 60:.1f} 分鐘）")
        stats[g] = (ok, empty, fail)
        print(f"{g}：成功 {ok}、沒有資料 {empty}、失敗 {fail}")

    for col, (eod, _) in INDEXES.items():
        try:
            write_hist(os.path.join(HIST, "index"), col, fetch_eod(eod, LONG_START))
        except Exception as e:
            print(f"指數 {col} 失敗：{e}")

    with open(os.path.join(HIST, "README.txt"), "w", encoding="utf-8") as f:
        f.write(f"EODHD 美股長歷史，起始 {LONG_START}，下載日 {date.today()}\n")
        for g, (ok, empty, fail) in stats.items():
            f.write(f"{g}: {ok} 檔有資料、{empty} 檔沒資料、{fail} 檔失敗\n")
        f.write("每檔欄位：date, open, high, low, close（未還原）, adjusted_close（含分割與配息還原）, volume\n")


def main():
    os.makedirs(DATA, exist_ok=True)
    if MODE == "long":
        run_long()
    else:
        run_working()


if __name__ == "__main__":
    sys.exit(main())
