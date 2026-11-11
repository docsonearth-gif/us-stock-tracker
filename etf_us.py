"""
類股／產業／主題 ETF 的強度與 RSI 熱圖 → ETF強度_美股.xlsx
（台股 RSI_板塊表.xlsx 的美股版：台股用證交所類股指數，美股用可以直接交易的 ETF）

清單在 etfs_us.csv（代號,名稱,群組），要加減 ETF 改那個檔就好。
每次重抓每檔 ETF 約兩年的日線（EODHD，用 adjusted_close，含配息），一檔一次呼叫。

工作表
- ETF強度：群組、名稱、收盤、RSI14 與 5 日變化、1 週／1／3／6 個月報酬、RS（相對 SPY，近 3 個月 40%＋6／9／12 個月各 20%，
  在清單內排名 1–99）、距 52 週高點、距 50／200 日線、趨勢（收盤 > 50MA > 200MA）
- RSI熱圖：同台股熱圖格式；欄位依群組排列
"""

import csv
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from common import DATA, ROOT, eodhd_get, last_closed_day
from heatmap import write_heatmap

LIST_CSV = os.path.join(ROOT, "etfs_us.csv")
OUT = os.path.join(ROOT, "ETF強度_美股.xlsx")
CLOSE_CSV = os.path.join(DATA, "etf_close.csv")
DAYS = int(os.getenv("ETF_DAYS", "800"))


def wilder_rsi(s, period=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return 100 - 100 / (1 + up / dn)


def load_list():
    with open(LIST_CSV, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r["代號"].strip()]


def fetch_all(items):
    start = (date.today() - timedelta(days=DAYS)).isoformat()
    end = last_closed_day()
    adj, close = {}, {}
    for it in items:
        c = it["代號"].strip()
        try:
            bars = eodhd_get(f"eod/{c}.US", {"from": start, "to": end}) or []
        except Exception as e:
            print(f"  {c} 失敗：{e}")
            continue
        if not bars:
            print(f"  {c} 沒有資料")
            continue
        idx = pd.to_datetime([b["date"] for b in bars])
        adj[c] = pd.Series([b.get("adjusted_close") or b.get("close") for b in bars], index=idx, dtype=float)
        close[c] = pd.Series([b.get("close") for b in bars], index=idx, dtype=float)
    return pd.DataFrame(adj).sort_index(), pd.DataFrame(close).sort_index()


def strength(items, adj, close):
    T = adj.index[-1]
    rsi = adj.apply(wilder_rsi)
    ma50, ma200 = adj.rolling(50).mean(), adj.rolling(200).mean()

    def ret(n):
        return (adj.iloc[-1] / adj.iloc[-1 - n] - 1) * 100 if len(adj) > n else pd.Series(np.nan, index=adj.columns)

    score = 0.4 * ret(63) + 0.2 * ret(126) + 0.2 * ret(189) + 0.2 * ret(252)
    rs = (score.rank(pct=True) * 98 + 1).round()
    spy = adj["SPY"] if "SPY" in adj else None
    rows = []
    for it in items:
        c = it["代號"].strip()
        if c not in adj or adj[c].dropna().empty:
            continue
        a = adj[c]
        rows.append({
            "群組": it["群組"], "代號": c, "名稱": it["名稱"],
            "收盤": round(close[c].iloc[-1], 2),
            "RSI14": round(rsi[c].iloc[-1], 1),
            "RSI 5日變化": round(rsi[c].iloc[-1] - rsi[c].iloc[-6], 1),
            "1週%": round(ret(5)[c], 1), "1月%": round(ret(21)[c], 1),
            "3月%": round(ret(63)[c], 1), "6月%": round(ret(126)[c], 1),
            "3月相對SPY(點)": round(ret(63)[c] - ret(63)["SPY"], 1) if spy is not None else np.nan,
            "RS(清單內)": rs[c],
            "距52週高點%": round((a.iloc[-1] / a.tail(252).max() - 1) * 100, 1),
            "距50日線%": round((a.iloc[-1] / ma50[c].iloc[-1] - 1) * 100, 1),
            "距200日線%": round((a.iloc[-1] / ma200[c].iloc[-1] - 1) * 100, 1),
            "多頭排列": bool(a.iloc[-1] > ma50[c].iloc[-1] > ma200[c].iloc[-1]),
        })
    df = pd.DataFrame(rows)
    return df, rsi, T


def write(df, rsi, items, T):
    wb = Workbook()
    ws = wb.active
    ws.title = "ETF強度"
    head_fill = PatternFill("solid", fgColor="1F3864")
    ws.cell(1, 1, f"資料日期 {T:%Y-%m-%d}｜RS(清單內)＝ETF 之間互相比較（近 3 月 40%＋6／9／12 月各 20%）").font = Font(name="Arial", bold=True)
    cols = list(df.columns)
    for j, c in enumerate(cols, 1):
        cell = ws.cell(2, j, c)
        cell.fill, cell.font = head_fill, Font(name="Arial", bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = 18 if c == "名稱" else 10
    for i, r in enumerate(df.itertuples(index=False), 3):
        for j, v in enumerate(r, 1):
            ws.cell(i, j, None if (isinstance(v, float) and np.isnan(v)) else v)
    n = len(df) + 2
    for name in ("RSI14", "RS(清單內)"):
        L = get_column_letter(cols.index(name) + 1)
        lo, mid, hi = (30, 50, 70) if name == "RSI14" else (20, 50, 90)
        ws.conditional_formatting.add(f"{L}3:{L}{n}", ColorScaleRule(
            start_type="num", start_value=lo, start_color="F8696B", mid_type="num", mid_value=mid,
            mid_color="FFFFFF", end_type="num", end_value=hi, end_color="63BE7B"))
    for name in ("1週%", "1月%", "3月%", "6月%", "3月相對SPY(點)"):
        L = get_column_letter(cols.index(name) + 1)
        ws.conditional_formatting.add(f"{L}3:{L}{n}", ColorScaleRule(
            start_type="percentile", start_value=5, start_color="F8696B", mid_type="num", mid_value=0,
            mid_color="FFFFFF", end_type="percentile", end_value=95, end_color="63BE7B"))
    ws.freeze_panes = "D3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(cols))}{n}"

    ws2 = wb.create_sheet("RSI熱圖")
    order = [it["代號"].strip() for it in items if it["代號"].strip() in rsi]
    label = {it["代號"].strip(): f"{it['代號'].strip()}\n{it['名稱']}" for it in items}
    tail = rsi[order].tail(90)
    data = {d.strftime("%Y-%m-%d"): {label[c]: (None if np.isnan(v) else round(float(v), 1))
                                     for c, v in row.items()} for d, row in tail.iterrows()}
    write_heatmap(ws2, data, [label[c] for c in order],
                  "ETF 14 日 RSI（Wilder，用含配息的還原價）。欄位依 etfs_us.csv 的順序：大盤 → 類股 → 產業 → 主題 → 另類 → 海外。")
    wb.save(OUT)
    print(f"已輸出 {OUT}（{len(df)} 檔 ETF）")


def main():
    items = load_list()
    adj, close = fetch_all(items)
    if adj.empty:
        print("沒有抓到任何 ETF 資料")
        return 1
    adj.round(4).to_csv(CLOSE_CSV, index_label="date")
    df, rsi, T = strength(items, adj, close)
    write(df, rsi, items, T)
    show = df.sort_values("RS(清單內)", ascending=False)
    print(show.head(12)[["群組", "代號", "名稱", "RSI14", "1月%", "3月%", "RS(清單內)"]].to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
