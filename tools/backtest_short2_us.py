"""
美股做空回測（第二版）：只空大型股、放空 ETF、長空頭裡反彈失敗才空

  HIST_DIR=…/history python tools/backtest_short2_us.py     → data/backtest_short2_us.md

資料
- 個股：Releases 的 us_history_long.tar（2014 起，含已下市股票）
- ETF：data/etf_long.csv.gz（tools/fetch_etf_long.py 產生；etfs_us.csv 的 61 檔）
- 指數：S&P 500（history/index/spx.csv.gz）

排除小型股
- 沒有歷史市值，用「50 日均成交額」每天全市場排名當代理：前 1,000 名 ≈ 大型＋中型股（接近 Russell 1000），
  另測前 500 名；都加股價 ≥ 12 美元。排名每天重算，已下市股票照算，沒有倖存者偏差。

大盤狀態
- 長空頭：S&P 500 < 200MA 且 200MA 比 20 天前低
- 空頭邊緣：S&P 500 < 200MA（不看斜率）

反彈失敗（個股、ETF、指數同一套定義）
- 均線 M（50MA 或 20MA）比 10 天前低（下彎）
- 近 5 日曾反彈到 M 附近：最高價 ≥ M × 0.98
- 今天收黑、收盤 < M，且收盤 < 20MA
- 同一檔 10 個交易日內只算第一次（減少重疊）

衡量：之後 10／20／60 日的報酬、下跌比例、中位數；20 日內曾反彈 ≥ 15%（個股）或 ≥ 8%（ETF）的比例。
報酬用含息還原價（空方要付股息，已反映）。未扣借券費與手續費。
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.getenv("HIST_DIR", os.path.join(ROOT, "history"))
OUT = os.path.join(ROOT, "data", "backtest_short2_us.md")
ETF_LONG = os.path.join(ROOT, "data", "etf_long.csv.gz")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import backtest_us as bt  # noqa: E402

lines = []


def say(s=""):
    print(s, flush=True)
    lines.append(s)


# ---------- 共用 ----------

def first_only(sig, gap=10):
    """訊號在 gap 天內只留第一次。"""
    prev = sig.shift(1).rolling(gap, min_periods=1).max().fillna(0).astype(bool)
    return sig & ~prev


def failed_rally(adj, high, n_ma):
    ma = adj.rolling(n_ma, min_periods=n_ma).mean()
    ma20 = adj.rolling(20, min_periods=20).mean()
    falling = ma < ma.shift(10)
    touched = (high / ma).rolling(5, min_periods=1).max() >= 0.98
    sig = falling & touched & (adj < ma) & (adj < ma20) & (adj < adj.shift(1))
    return first_only(sig.fillna(False))


def fwd(adj, n):
    return adj.shift(-n) / adj - 1


def bounce(adj, th):
    fut_max = adj[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1)
    return (fut_max / adj - 1 >= th).astype("float32").where(fwd(adj, 20).notna())


HEAD = ("| 訊號 | 樣本 | 10 日平均／下跌比例 | 20 日平均（中位） | 20 日下跌比例 | 60 日平均／下跌比例 | 20 日內曾反彈 ≥ {th} |\n"
        "|---|---:|---:|---:|---:|---:|---:|")


def stat_row(name, mask, F10, F20, F60, BO, min_n=30):
    m = mask.values if hasattr(mask, "values") else mask
    m = np.asarray(m, dtype=bool)
    x10, x20, x60, bo = (np.asarray(a)[m] for a in (F10, F20, F60, BO))
    x10, x20, x60, bo = (a[~np.isnan(a)] for a in (x10, x20, x60, bo))
    if len(x20) < min_n:
        return f"| {name} | {len(x20)} | （太少） | | | | |"
    return (f"| {name} | {len(x20)} | {x10.mean()*100:+.2f}%／{(x10 < 0).mean()*100:.0f}% | "
            f"{x20.mean()*100:+.2f}%（{np.median(x20)*100:+.1f}%） | {(x20 < 0).mean()*100:.1f}% | "
            f"{(x60.mean()*100 if len(x60) else np.nan):+.1f}%／{((x60 < 0).mean()*100 if len(x60) else np.nan):.0f}% | "
            f"{bo.mean()*100:.0f}% |")


def load_spx(dates):
    s = pd.read_csv(os.path.join(HIST, "index", "spx.csv.gz"), parse_dates=["date"]).set_index("date")
    adj = s["adjusted_close"].reindex(dates).ffill()
    high = (s["high"] * s["adjusted_close"] / s["close"]).reindex(dates).ffill()
    return adj, high


def regimes(spx):
    s200 = spx.rolling(200).mean()
    return {"長空頭（< 200MA 且下彎）": (spx < s200) & (s200 < s200.shift(20)),
            "S&P 500 < 200MA": spx < s200,
            "多頭（≥ 200MA）": spx >= s200}


# ---------- 一、ETF ----------

def part_etf():
    if not os.path.exists(ETF_LONG):
        say("\n## 一、ETF：沒有 data/etf_long.csv.gz，請先跑 tools/fetch_etf_long.py\n")
        return
    e = pd.read_csv(ETF_LONG, parse_dates=["date"])
    adj = e.pivot(index="date", columns="code", values="adj").sort_index()
    high = e.pivot(index="date", columns="code", values="adj_high").reindex(adj.index)
    adj = adj[adj.index >= "2014-01-02"]
    high = high.reindex(adj.index)
    dates = adj.index
    groups = pd.read_csv(os.path.join(ROOT, "etfs_us.csv"), encoding="utf-8-sig").set_index("代號")["群組"]
    sect = [c for c in adj.columns if groups.get(c) in ("類股", "產業", "主題")]
    spx, _ = load_spx(dates)
    R = regimes(spx)

    F10, F20, F60 = fwd(adj, 10), fwd(adj, 20), fwd(adj, 60)
    BO = bounce(adj, 0.08)

    def r(n):
        return adj / adj.shift(n) - 1
    score = (0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252))[sect]
    rk = score.rank(axis=1, pct=True)
    weak = pd.DataFrame(False, index=dates, columns=adj.columns)
    weak[sect] = (rk <= 0.2) & score.notna()
    ma50 = adj.rolling(50).mean()
    downtrend = (adj < ma50) & (ma50 < ma50.shift(10))
    fr50 = failed_rally(adj, high, 50)
    fr20 = failed_rally(adj, high, 20)

    # 指數反彈失敗（以 SPY 的價格判斷，當天訊號套用到所有 ETF）
    idx_fr = pd.Series(False, index=dates)
    if "SPY" in adj:
        idx_fr = failed_rally(adj[["SPY"]], high[["SPY"]], 50)["SPY"] | failed_rally(adj[["SPY"]], high[["SPY"]], 20)["SPY"]
        idx_fr = first_only(idx_fr)

    n_years = (dates[-1] - dates[0]).days / 365.25
    say(f"\n## 一、放空 ETF（{dates[0]:%Y-%m-%d} ～ {dates[-1]:%Y-%m-%d}，約 {n_years:.0f} 年）\n")
    say(f"類股＋產業＋主題 ETF 共 {len(sect)} 檔（不含大盤、另類、海外）。每天都算（不取樣），"
        "「每天放空」是對照用的基準。反彈門檻 8%。\n")

    def col(c):
        return pd.DataFrame(False, index=dates, columns=adj.columns).assign(**{c: True})

    everyday = pd.DataFrame(True, index=dates, columns=adj.columns) & adj.notna()
    in_sect = pd.DataFrame(False, index=dates, columns=adj.columns)
    in_sect[sect] = True
    in_sect &= adj.notna()

    for rname, rmask in [("全部日子", pd.Series(True, index=dates)), *R.items()]:
        rm = pd.DataFrame(np.repeat(rmask.fillna(False).values[:, None], adj.shape[1], axis=1),
                          index=dates, columns=adj.columns)
        say(f"\n### {rname}（{int(rmask.sum())} 天）\n")
        say(HEAD.format(th="8%"))
        rows = {}
        for c in ("SPY", "QQQ", "IWM"):
            if c in adj:
                rows[f"每天放空 {c}"] = col(c) & rm & adj.notna()
                rows[f"{c} 反彈失敗（50MA 或 20MA）"] = col(c) & rm & (fr50 | fr20)
        rows["每天放空全部類股／產業 ETF"] = in_sect & rm
        rows["弱勢 ETF（RS 後 20%）＋下降趨勢"] = in_sect & rm & weak & downtrend
        rows["ETF 自己反彈到下彎 50MA 失敗"] = in_sect & rm & fr50
        rows["ETF 自己反彈到下彎 20MA 失敗"] = in_sect & rm & fr20
        rows["弱勢 ETF＋自己反彈失敗（50 或 20）"] = in_sect & rm & weak & (fr50 | fr20)
        im = pd.DataFrame(np.repeat(idx_fr.values[:, None], adj.shape[1], axis=1), index=dates, columns=adj.columns)
        rows["SPY 反彈失敗當天 → 空弱勢 ETF"] = in_sect & rm & weak & im
        for k, v in rows.items():
            say(stat_row(k, v.fillna(False), F10, F20, F60, BO, min_n=15))


# ---------- 二、個股 ----------

def part_stocks():
    p = bt.load()
    lines.extend(bt.lines)
    bt.lines.clear()
    adj, close, dv, ah = p["adj"], p["close"], p["dv"], p["ah"]
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
    dv50 = dv.rolling(50, min_periods=40).mean().where(listed)
    dvrank = dv50.rank(axis=1, ascending=False)
    del dv50
    px = close >= 12
    big1000 = listed & px & (dvrank <= 1000)
    big500 = listed & px & (dvrank <= 500)
    del dvrank
    weak = (rs <= 30) | stage4
    fr50 = failed_rally(adj, ah, 50)
    fr20 = failed_rally(adj, ah, 20)
    del ah

    spx, spx_hi = load_spx(dates)
    R = regimes(spx)
    spx_fr = failed_rally(spx.to_frame("s"), spx_hi.to_frame("s"), 50)["s"] | \
        failed_rally(spx.to_frame("s"), spx_hi.to_frame("s"), 20)["s"]
    spx_fr = first_only(spx_fr)

    F10, F20, F60 = (fwd(adj, n).where(valid).astype("float32") for n in (10, 20, 60))
    BO = bounce(adj, 0.15)
    ok = rs.notna().sum(axis=1) > 1000

    say(f"\n## 二、個股（{dates[ok.values][0]:%Y-%m-%d} ～ {dates[-1]:%Y-%m-%d}，含已下市股票，只看大型＋中型股）\n")
    say("大型＋中型股＝每天 50 日均成交額排名前 1,000（另列前 500）且股價 ≥ 12 美元。"
        "弱勢＝RS ≤ 30 或第④階段。反彈門檻 15%。\n")

    def rep(mask_by_day, dmask):
        dm = pd.DataFrame(np.repeat((dmask & ok).fillna(False).values[:, None], adj.shape[1], axis=1),
                          index=dates, columns=adj.columns)
        return (mask_by_day & dm).fillna(False)

    spx_day = pd.DataFrame(np.repeat(spx_fr.fillna(False).values[:, None], adj.shape[1], axis=1),
                           index=dates, columns=adj.columns)
    for rname, rmask in [("全部日子", pd.Series(True, index=dates)), *R.items(),
                         ("2022 空頭", pd.Series((dates >= "2022-01-01") & (dates <= "2022-10-31"), index=dates))]:
        say(f"\n### {rname}（{int((rmask & ok).sum())} 天）\n")
        say(HEAD.format(th="15%"))
        rows = {
            "對照：大型＋中型股，每天（每 5 日取樣）": big1000 & pd.DataFrame(
                np.repeat((np.arange(len(dates)) % 5 == 0)[:, None], adj.shape[1], axis=1), index=dates, columns=adj.columns),
            "大型＋中型 × 弱勢（每 5 日取樣）": big1000 & weak & pd.DataFrame(
                np.repeat((np.arange(len(dates)) % 5 == 0)[:, None], adj.shape[1], axis=1), index=dates, columns=adj.columns),
            "大型＋中型 × 反彈到下彎 50MA 失敗": big1000 & fr50,
            "大型＋中型 × 反彈到下彎 20MA 失敗": big1000 & fr20,
            "大型＋中型 × 弱勢 × 反彈失敗（50 或 20）": big1000 & weak & (fr50 | fr20),
            "前 500 大 × 弱勢 × 反彈失敗（50 或 20）": big500 & weak & (fr50 | fr20),
            "S&P 500 反彈失敗當天 → 空大型＋中型弱勢股": big1000 & weak & spx_day,
        }
        for k, v in rows.items():
            say(stat_row(k, rep(v, rmask), F10, F20, F60, BO))


def main():
    say("# 美股做空回測（第二版）：ETF、只空大型＋中型股、長空頭反彈失敗\n")
    say("做空要賺錢＝**下跌比例 > 50%、平均與中位數為負**。報酬含息還原；未扣借券費與手續費。"
        "表格裡的「下跌比例」就是做空的勝率。\n")
    part_etf()
    part_stocks()
    say("\n> 樣本會重疊（同一段空頭裡多檔股票同一天出訊號），長空頭只有 2015–16、2018 底、2020、2022 幾段，"
        "結果主要由 2022 決定。")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已存 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
