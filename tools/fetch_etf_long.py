"""
下載 etfs_us.csv 裡 ETF 的長歷史（2014 起，EODHD），給做空回測用。

  python tools/fetch_etf_long.py      → data/etf_long.csv.gz（date, code, adj, adj_high, close）

由 GitHub Actions「ETF 長歷史」執行（金鑰在 repo Secrets）。每檔 1 次 API 呼叫。
"""

import csv
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from common import DATA, eodhd_get, last_closed_day  # noqa: E402

START = os.getenv("LONG_START", "2014-01-01")
OUT = os.path.join(DATA, "etf_long.csv.gz")


def main():
    with open(os.path.join(ROOT, "etfs_us.csv"), encoding="utf-8-sig") as f:
        codes = [r["代號"].strip() for r in csv.DictReader(f) if r["代號"].strip()]
    end = last_closed_day()
    parts = []
    for c in codes:
        try:
            bars = eodhd_get(f"eod/{c}.US", {"from": START, "to": end}) or []
        except Exception as e:
            print(f"{c} 失敗：{e}")
            continue
        if not bars:
            print(f"{c} 沒有資料")
            continue
        d = pd.DataFrame(bars)
        d["adj"] = d["adjusted_close"].fillna(d["close"])
        f_ = d["adj"] / d["close"]
        d["adj_high"] = d["high"] * f_
        d["code"] = c
        parts.append(d[["date", "code", "adj", "adj_high", "close"]])
        print(f"{c}: {d['date'].iloc[0]} ～ {d['date'].iloc[-1]}，{len(d)} 天")
    out = pd.concat(parts, ignore_index=True)
    out.to_csv(OUT, index=False, float_format="%.4f", compression="gzip")
    print(f"已存 {OUT}：{out['code'].nunique()} 檔、{len(out)} 筆")


if __name__ == "__main__":
    main()
