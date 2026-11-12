"""
美股回測：反向 RS 做空（2014 起，含已下市股票）

  HIST_DIR=…/history python tools/backtest_short_us.py     → data/backtest_short_us.md

資料：Releases 的 us_history_long.tar（同 backtest_us.py）。已下市股票都在裡面，
破產、下市這些「最好的空單」有算進去，沒有倖存者偏差。

做空訊號（和台股 tools/backtest_short.py 同一套定義）
- 天真版：RS ≤ 10、RS ≤ 30
- B 類（弱勢延續）：第④階段（收盤 < 50MA < 150MA < 200MA、200MA 比一個月前低）＋ RS ≤ 30
- A 類（前領導股轉弱）：60 日內 RS 曾 ≥ 80、現在 ≤ 50、收盤 < 50MA
- 加條件：所屬產業（Nasdaq 產業別）RS 中位數在後 20%；距 50MA 不超過 −25%
- 流動性：股價 ≥ 12 美元、50 日均成交額 ≥ 2,000 萬美元（全部訊號都套用，包含基準）

大盤狀態：全部／S&P 500 < 50MA／< 50MA 且 50MA 下彎／排除過度超賣（近 5 日跌 ≥ 4% 家數前 10%）／
過度超賣時；另列 2018Q4、2020 疫情、2022 空頭

衡量（每 5 個交易日取樣）：之後 20／60 日的絕對報酬與下跌比例、跑輸大盤比例（減全市場中位數）、
之後 20 日內收盤曾比進場高 ≥ 15% 的比例（軋空風險）。報酬用還原價（含配息；空方要付股息，已反映）。
未扣借券費與手續費。
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.getenv("HIST_DIR", os.path.join(ROOT, "history"))
OUT = os.path.join(ROOT, "data", "backtest_short_us.md")
STEP = 5
lines = []

sys.path.insert(0, os.path.join(ROOT, "tools"))
import backtest_us as bt  # noqa: E402


def say(s=""):
    print(s, flush=True)
    lines.append(s)


def main():
    p = bt.load()
    lines.extend(bt.lines)
    bt.lines.clear()
    adj, close, dv = p["adj"], p["close"], p["dv"]
    del p
    dates = adj.index
    listed = close.notna()
    valid = (close.ffill() >= 1) & listed

    def r(n):
        return adj / adj.shift(n) - 1
    score = (0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252)).where(valid)
    rs = (score.rank(axis=1, pct=True) * 98 + 1).astype("float32")
    del score
    ma50 = adj.rolling(50, min_periods=50).mean().astype("float32")
    ma150 = adj.rolling(150, min_periods=150).mean().astype("float32")
    ma200 = adj.rolling(200, min_periods=200).mean().astype("float32")
    stage4 = (adj < ma50) & (ma50 < ma150) & (ma150 < ma200) & (ma200 < ma200.shift(21))
    del ma150, ma200
    dv50 = dv.rolling(50, min_periods=40).mean()
    liquid = listed & (close >= 12) & (dv50 >= 20e6)
    del dv50
    ret1 = (adj / adj.ffill().shift(1) - 1).where(listed)
    not_ext = (adj / ma50 - 1) > -0.25
    rs_max60 = rs.rolling(60, min_periods=40).max()
    A = (rs_max60 >= 80) & (rs <= 50) & (adj < ma50)
    del rs_max60
    ma50_down = ma50 < ma50.shift(10)
    B = stage4 & (rs <= 30)

    # 產業 RS 中位數後 20%（只有現存股票有產業別）
    st = pd.read_csv(os.path.join(ROOT, "data", "stocks.csv"), dtype=str, keep_default_na=False)
    ind = st.set_index("code")["industry"]
    ind = ind[ind != ""]
    cols = [c for c in adj.columns if c in ind.index]
    imap = ind.reindex(cols)
    counts = imap.value_counts()
    imap = imap[imap.isin(counts[counts >= 5].index)]
    med = rs[imap.index].T.groupby(imap.values).median().T
    pr = med.rank(axis=1, pct=True)
    weak_sec = pd.DataFrame(False, index=dates, columns=adj.columns)
    wk = (pr <= 0.2)
    weak_sec[imap.index] = wk[imap.values].values

    # 之後報酬
    f20 = (adj.shift(-20) / adj - 1).where(valid)
    f60 = (adj.shift(-60) / adj - 1).where(valid)
    e20 = f20.sub(f20.median(axis=1), axis=0)
    fut_max = adj[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)
    squeeze = (fut_max / adj - 1 >= 0.15).astype("float32").where(f20.notna())
    del fut_max

    # 大盤
    spx = pd.read_csv(os.path.join(HIST, "index", "spx.csv.gz"), parse_dates=["date"]).set_index("date")
    spx = spx["adjusted_close" if "adjusted_close" in spx.columns else "close"].reindex(dates).ffill()
    sma = spx.rolling(50).mean()
    below = spx < sma
    down = below & (sma < sma.shift(10))
    s200 = spx.rolling(200).mean()
    bear = (spx < s200) & (s200 < s200.shift(20))
    drop4 = (ret1 <= -0.04).where(liquid).sum(axis=1).rolling(5).sum()
    ok_days = rs.notna().sum(axis=1) > 1000
    oversold = drop4 >= drop4[ok_days].quantile(0.9)

    sample = dates[ok_days.values][::STEP]
    sample = sample[f20.loc[sample].notna().sum(axis=1).values > 0]
    say(f"\n# 反向 RS 做空回測（美股，{sample[0]:%Y-%m-%d} ～ {sample[-1]:%Y-%m-%d}，每 {STEP} 日取樣 {len(sample)} 天）\n")
    say("流動性：股價 ≥ 12 美元、50 日均成交額 ≥ 2,000 萬美元。報酬含配息還原。做空要賺錢＝**下跌比例高、平均報酬為負**。"
        "未扣借券費與手續費。產業用 Nasdaq 產業別（已下市股票沒有產業，不會進「板塊後 20%」）。\n")
    say(f"取樣天數：全部 {len(sample)}｜S&P 500 < 50MA {int(below.reindex(sample).sum())}｜"
        f"< 50MA 且 50MA 下彎 {int(down.reindex(sample).sum())}｜長空頭 {int(bear.reindex(sample).sum())}｜過度超賣 {int(oversold.reindex(sample).sum())}\n")

    sigs = {
        "基準：全部股票": liquid,
        "天真：RS ≤ 10": liquid & (rs <= 10),
        "天真：RS ≤ 30": liquid & (rs <= 30),
        "B：第④階段＋RS ≤ 30": liquid & B,
        "B＋產業後 20%": liquid & B & weak_sec,
        "B＋產業後 20%＋不追空": liquid & B & weak_sec & not_ext,
        "A：前領導股轉弱": liquid & A,
        "A＋產業後 20%": liquid & A & weak_sec,
        "A＋50MA 下彎": liquid & A & ma50_down,
    }
    S = {k: v.loc[sample].fillna(False).astype(bool) for k, v in sigs.items()}
    F20, F60, E20, SQ = (x.loc[sample] for x in (f20, f60, e20, squeeze))

    def row(name, dd):
        mk = S[name].loc[dd]

        def pick(p):
            return p.loc[dd].where(mk).stack().dropna()
        x20, x60, y20, sq = pick(F20), pick(F60), pick(E20), pick(SQ)
        if len(x20) < 30:
            return f"| {name} | {len(x20)} | （太少） | | | | |"
        return (f"| {name} | {len(x20)} | {x20.mean()*100:+.2f}%（中位 {x20.median()*100:+.1f}%） | "
                f"{(x20 < 0).mean()*100:.1f}% | {(y20 < 0).mean()*100:.1f}%（{y20.mean()*100:+.2f}%） | "
                f"{x60.mean()*100:+.1f}%／{(x60 < 0).mean()*100:.0f}% | {sq.mean()*100:.0f}% |")

    def block(label, dsel):
        dd = sample[dsel.reindex(sample).fillna(False).values.astype(bool)]
        say(f"\n## {label}（{len(dd)} 天）\n")
        if len(dd) == 0:
            say("沒有樣本")
            return
        say("| 訊號 | 樣本 | 20 日平均報酬 | 20 日下跌比例 | 20 日跑輸大盤（平均超額） | 60 日平均／下跌比例 | 20 日內曾反彈 ≥ 15% |")
        say("|---|---:|---:|---:|---:|---:|---:|")
        for name in sigs:
            say(row(name, dd))

    T = pd.Series(True, index=dates)
    block("全部日子", T)
    block("S&P 500 < 50MA", below)
    block("S&P 500 < 50MA 且 50MA 下彎", down)
    block("S&P 500 < 50MA 且 50MA 下彎，排除過度超賣", down & ~oversold)
    block("過度超賣時（近 5 日跌 ≥ 4% 家數前 10%）", oversold)
    block("S&P 500 < 200MA 且 200MA 下彎（長空頭）", bear)
    block("長空頭，排除過度超賣", bear & ~oversold)
    block("S&P 500 ≥ 50MA（多頭）", ~below)
    for a, b, name in [("2018-10-01", "2018-12-31", "2018Q4 空頭"), ("2020-02-15", "2020-04-30", "2020 疫情"),
                       ("2022-01-01", "2022-10-31", "2022 空頭")]:
        block(name, (dates >= a) & (dates <= b) & T)

    say("\n> 解讀：做空看「下跌比例」與「平均報酬」，和同一大盤狀態下的全部股票比。"
        "樣本每 5 日取一次，同一檔股票連續幾週的訊號仍會重疊。")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已存 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
