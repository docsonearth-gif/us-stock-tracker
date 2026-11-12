"""
美股做空回測（第三版）：照 Notion「沽空」筆記的規則，一筆一筆模擬交易

  HIST_DIR=…/history python tools/backtest_short3_us.py     → data/backtest_short3_us.md
                                                               data/backtest_short3_trades.csv（交易明細）

資料：Releases 的 us_history_long.tar（2014 起，含已下市股票；開高低收都用還原價）

選股（訊號當天都要符合）
- 大型＋中型股：50 日均成交額每天排名前 1,000、股價 ≥ 12 美元（排除小型股）
- 之前是強勢股：過去一年 RS 曾 ≥ 80
- 做頭至少一個月：52 週高點出現在 21 個交易日以前
- RS 線持續向下至少一個月：RS 線（個股 ÷ S&P 500）比 21 天前低，且 RS 評分比 21 天前低
- 50MA 向下：比 10 天前低
- 已經跌破過 200MA（第一次跌破不空）：過去 5～60 天內曾收在 200MA 之下

進場：反彈到 200MA 過不去、掉頭往下
- 近 5 日最高價曾到 200MA × 0.98 以上；今天收黑、收在 200MA 之下
- 不追空：今天不是跳空大跌（開盤沒有低於前一天最低價 3% 以上）
- 隔天開盤進場

止損：200MA × 1.045 與「近 3 日最高價 × 1.005」取較高者；止損距離 > 10% 不做
回補（主規則）：獲利 20% 回補一半；剩下的收盤站上 50MA 隔天開盤全部回補（進場時股價若已在 50MA 之上，
  也就是 50MA 已跌到 200MA 下面，改成站上 200MA 才回補）；最多持有 120 天
同一檔出場後才能再進場（止損後可以再試）

變化版
- 中段做空：另外要求 200MA 向下
- 50MA 反彈失敗：進場改成反彈到 50MA 過不去（股價已在 200MA 之下）
- 不看「之前是強勢股」：檢查前領導股條件有沒有用
- 回補改成 2.5R 回補一半

成本：每筆扣 0.2%（手續費＋借券費，大型股借券便宜）。股息：還原價已反映（空方要付）。
"""

import csv
import gzip
import os
import pickle
import re
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.getenv("HIST_DIR", os.path.join(ROOT, "history"))
OUT = os.path.join(ROOT, "data", "backtest_short3_us.md")
TRADES = os.path.join(ROOT, "data", "backtest_short3_trades.csv")
CACHE = os.getenv("PANEL_CACHE", "/tmp/us_panels_ohlc.pkl")
BAD = re.compile(r"-(R|RI|RT|W|WS|WI|U|UN)$|^[A-Z]TEST(-|$)")
COST = 0.002
lines = []


def say(s=""):
    print(s, flush=True)
    lines.append(s)


# ---------- 讀資料 ----------

def read_one(args):
    path, key = args
    try:
        d = pd.read_csv(path, usecols=["date", "open", "high", "low", "close", "adjusted_close", "volume"])
    except Exception:
        return key, None
    d = d[(d["close"] > 0) & (d["adjusted_close"] > 0)]
    if len(d) < 250:
        return key, None
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date").set_index("date")
    f = d["adjusted_close"] / d["close"]
    out = pd.DataFrame({"adj": d["adjusted_close"], "ao": d["open"] * f, "ah": d["high"] * f,
                        "al": d["low"] * f, "close": d["close"], "dv": d["close"] * d["volume"]})
    # 開高低不合理（0 或缺）時用收盤補
    for c in ("ao", "ah", "al"):
        out[c] = out[c].where(out[c] > 0, out["adj"])
    out["ah"] = out[["ah", "adj", "ao"]].max(axis=1)
    out["al"] = out[["al", "adj", "ao"]].min(axis=1)
    return key, out.astype("float32")


def load():
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    jobs = []
    with open(os.path.join(HIST, "symbols.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if BAD.search(r["code"]):
                continue
            key = r["code"] if r["group"] == "active" else r["code"] + "~D"
            p = os.path.join(HIST, r["group"], f"{r['code']}.csv.gz")
            if os.path.exists(p):
                jobs.append((p, key))
    with ProcessPoolExecutor(2) as ex:
        res = [x for x in ex.map(read_one, jobs, chunksize=200) if x[1] is not None]
    pan = {}
    for fld in ("adj", "ao", "ah", "al", "close", "dv"):
        pan[fld] = pd.concat({k: d[fld] for k, d in res}, axis=1).sort_index().astype("float32")
    idx = pan["adj"].index
    idx = idx[idx >= "2014-01-02"]
    n = pan["close"].notna().sum(axis=1)
    idx = idx[n.reindex(idx) > n.median() * 0.5]
    pan = {k: v.reindex(idx) for k, v in pan.items()}
    pan["n_active"] = sum(1 for k, _ in res if not k.endswith("~D"))
    pan["n_delisted"] = sum(1 for k, _ in res if k.endswith("~D"))
    with open(CACHE, "wb") as f:
        pickle.dump(pan, f, protocol=4)
    return pan


# ---------- 訊號 ----------

def build(p):
    adj, ao, ah, al, close, dv = (p[k] for k in ("adj", "ao", "ah", "al", "close", "dv"))
    dates = adj.index
    listed = close.notna()
    valid = (close.ffill() >= 1) & listed

    def r(n):
        return adj / adj.shift(n) - 1
    score = (0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252)).where(valid)
    rs = (score.rank(axis=1, pct=True) * 98 + 1).astype("float32")
    del score

    ma50 = adj.rolling(50, min_periods=50).mean().astype("float32")
    ma200 = adj.rolling(200, min_periods=200).mean().astype("float32")
    dv50 = dv.rolling(50, min_periods=40).mean().where(listed)
    big = listed & (close >= 12) & (dv50.rank(axis=1, ascending=False) <= 1000)
    del dv50

    spx = pd.read_csv(os.path.join(HIST, "index", "spx.csv.gz"), parse_dates=["date"]).set_index("date")["adjusted_close"]
    spx = spx.reindex(dates).ffill()
    line = adj.div(spx, axis=0)

    leader = rs.rolling(252, min_periods=120).max() >= 80
    hi = ah.rolling(252, min_periods=120).max()
    lo = al.rolling(252, min_periods=120).min()
    runup = hi / lo >= 1.8   # 之前急漲過：52 週內高點 ≥ 低點 × 1.8
    days_since_hi = ah.rolling(252, min_periods=120).apply(lambda x: len(x) - 1 - np.nanargmax(x), raw=True) \
        if os.getenv("SLOW_HI") else None
    if days_since_hi is None:
        # 「52 週高點在 21 天以前」＝近 21 天的最高價低於 52 週高點
        topping = ah.rolling(21, min_periods=15).max() < hi * 0.999
    else:
        topping = days_since_hi >= 21
    rs_down = (line < line.shift(21)) & (rs < rs.shift(21))
    ma50_down = ma50 < ma50.shift(10)
    ma200_down = ma200 < ma200.shift(20)
    below200 = adj < ma200
    broke_before = below200.shift(5).rolling(56, min_periods=1).max().fillna(0).astype(bool)
    near200 = (ah / ma200).rolling(5, min_periods=1).max() >= 0.98
    near50 = (ah / ma50).rolling(5, min_periods=1).max() >= 0.98
    down_day = adj < adj.shift(1)
    no_gap = ao >= al.shift(1) * 0.97

    base = big & topping & rs_down & ma50_down & broke_before & no_gap & down_day
    sig = {
        "主規則（前領導股＋200MA 反彈失敗）": base & leader & near200 & below200,
        "中段做空（另外要求 200MA 向下）": base & leader & near200 & below200 & ma200_down,
        "主規則＋股價也在 50MA 之下（雙重壓力）": base & leader & near200 & below200 & (adj < ma50),
        "主規則＋之前急漲過（52 週高 ≥ 低 × 1.8）": base & leader & runup & near200 & below200,
        "50MA 反彈失敗（已在 200MA 之下）": base & leader & near50 & (adj < ma50) & below200 & ~near200,
        "不看前領導股（200MA 反彈失敗）": base & near200 & below200,
    }
    s200 = spx.rolling(200).mean()
    regime = pd.Series(np.where(spx < s200, np.where(s200 < s200.shift(20), "長空頭", "跌破200MA"), "多頭"),
                       index=dates)
    return dict(adj=adj, ao=ao, ah=ah, al=al, ma50=ma50, ma200=ma200, sig=sig, regime=regime, dates=dates)


# ---------- 模擬 ----------

def simulate(D, sig, target="pct20", max_risk=0.10, max_hold=120, exit_only_200=False):
    A, O, H, L = (D[k].values for k in ("adj", "ao", "ah", "al"))
    M50, M200 = D["ma50"].values, D["ma200"].values
    S = sig.fillna(False).values
    dates, cols = D["dates"], D["adj"].columns
    regime = D["regime"].values
    T = len(dates)
    trades = []
    skipped = 0
    for j in range(S.shape[1]):
        idx = np.flatnonzero(S[:, j])
        free_from = 0
        for i in idx:
            if i < free_from or i + 2 >= T:
                continue
            e = i + 1
            entry = O[e, j]
            if not np.isfinite(entry) or entry <= 0:
                continue
            stop = max(M200[i, j] * 1.045, np.nanmax(H[max(0, i - 2):i + 1, j]) * 1.005)
            risk = stop / entry - 1
            if not np.isfinite(risk) or risk <= 0 or risk > max_risk:
                skipped += 1
                continue
            tgt = entry * (1 - 0.20) if target == "pct20" else entry * (1 - 2.5 * risk)
            remain, pnl, half_done = 1.0, 0.0, False
            # 出場線：進場時股價在 50MA 之下 → 收盤站上 50MA 回補；在 50MA 之上（50MA 已跌到 200MA 下面）→ 站上 200MA 回補
            exit_ma, exit_name = (M50, "站上50MA") if (A[i, j] < M50[i, j] and not exit_only_200) else (M200, "站上200MA")
            reason, k = "到期", e
            for k in range(e, min(T, e + max_hold)):
                if not np.isfinite(A[k, j]):
                    # 停止交易（下市）：用最後價格出場
                    last = A[k - 1, j] if k > e else entry
                    pnl += remain * (entry - last) / entry
                    remain, reason = 0.0, "下市"
                    break
                if H[k, j] >= stop:
                    px = max(O[k, j], stop) if k > e else stop
                    pnl += remain * (entry - px) / entry
                    remain, reason = 0.0, ("止損" if not half_done else "止損（已回補一半）")
                    break
                if not half_done and L[k, j] <= tgt:
                    px = min(O[k, j], tgt) if k > e else tgt
                    pnl += 0.5 * (entry - px) / entry
                    remain, half_done = 0.5, True
                if A[k, j] > exit_ma[k, j] and k + 1 < T:
                    px = O[k + 1, j] if np.isfinite(O[k + 1, j]) else A[k, j]
                    pnl += remain * (entry - px) / entry
                    remain, reason = 0.0, (exit_name if not half_done else "回補一半＋" + exit_name)
                    k += 1
                    break
            if remain > 0:
                pnl += remain * (entry - A[k, j]) / entry
            pnl -= COST
            trades.append((dates[e], cols[j], regime[i], entry, stop, risk, pnl, pnl / risk, reason, k - e + 1, half_done))
            free_from = k + 1
    t = pd.DataFrame(trades, columns=["進場日", "代號", "大盤", "進場價", "止損價", "風險%", "報酬", "R",
                                      "出場原因", "持有天數", "回補一半"])
    return t, skipped


def max_consec_losses(r):
    best = cur = 0
    for x in r:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def summary(t):
    if len(t) < 10:
        return f"| {len(t)} | （太少） | | | | | | |"
    t = t.sort_values("進場日")
    w, l = t[t["R"] > 0], t[t["R"] <= 0]
    pf = w["報酬"].sum() / -l["報酬"].sum() if len(l) and l["報酬"].sum() < 0 else np.inf
    return (f"| {len(t)} | {(t['R'] > 0).mean()*100:.0f}% | {t['R'].mean():+.2f}R | {t['R'].median():+.2f}R | "
            f"{t['報酬'].mean()*100:+.2f}% | {w['R'].mean():+.2f}R／{l['R'].mean():+.2f}R | {pf:.2f} | "
            f"{t['回補一半'].mean()*100:.0f}% | {t['持有天數'].median():.0f} |")


HEAD = ("| 條件 | 交易數 | 勝率 | 平均 R | 中位 R | 平均每筆報酬 | 平均賺／賠（R） | 獲利因子 | 達 20% 回補一半 | 持有天數中位 |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def main():
    p = load()
    say("# 美股做空回測（第三版）：照 Notion「沽空」筆記，一筆一筆模擬\n")
    say(f"資料：現存 {p['n_active']} 檔＋已下市 {p['n_delisted']} 檔，{p['adj'].index[0]:%Y-%m-%d} ～ "
        f"{p['adj'].index[-1]:%Y-%m-%d}。只做大型＋中型股（成交額前 1,000、股價 ≥ 12 美元）。\n")
    say("**R＝這筆賺賠 ÷ 進場時的止損距離**。平均 R > 0 才是有優勢；勝率低沒關係，只要賺的時候夠大。"
        f"每筆已扣成本 {COST*100:.1f}%。\n")
    D = build(p)
    del p
    allt = {}
    say("## 一、各版本\n")
    say(HEAD)
    for name, s in D["sig"].items():
        t, sk = simulate(D, s)
        allt[name] = t
        say(f"| {name}（止損太遠略過 {sk}） " + summary(t))
    t, sk = simulate(D, D["sig"]["主規則（前領導股＋200MA 反彈失敗）"], target="r25")
    allt["主規則，回補改成 2.5R"] = t
    say(f"| 主規則，回補改成 2.5R 回補一半 " + summary(t))
    t, sk = simulate(D, D["sig"]["主規則（前領導股＋200MA 反彈失敗）"], exit_only_200=True)
    allt["主規則，出場只看站上 200MA"] = t
    say(f"| 主規則，出場放寬：只在站上 200MA 回補 " + summary(t))
    t, sk = simulate(D, D["sig"]["主規則＋之前急漲過（52 週高 ≥ 低 × 1.8）"], exit_only_200=True)
    allt["急漲過＋出場只看 200MA"] = t
    say(f"| 急漲過＋出場只看 200MA " + summary(t))
    t, sk = simulate(D, D["sig"]["主規則（前領導股＋200MA 反彈失敗）"], max_risk=0.05)
    allt["主規則，止損 ≤ 5%"] = t
    say(f"| 主規則，只做止損距離 ≤ 5%（略過 {sk}） " + summary(t))

    main_t = allt["主規則（前領導股＋200MA 反彈失敗）"]
    say("\n## 二、主規則：依進場時大盤狀態\n")
    say(HEAD)
    for g in ("多頭", "跌破200MA", "長空頭"):
        say(f"| S&P 500 {g} " + summary(main_t[main_t["大盤"] == g]))
    say("\n（跌破200MA＝S&P 500 < 200MA 但 200MA 還沒下彎；長空頭＝< 200MA 且 200MA 下彎）\n")

    say("## 三、主規則：依年份\n")
    say(HEAD)
    for y, g in main_t.groupby(main_t["進場日"].dt.year):
        say(f"| {y} " + summary(g))

    say("\n## 四、主規則：出場原因\n")
    say("| 出場原因 | 筆數 | 平均 R |")
    say("|---|---:|---:|")
    for k, g in main_t.groupby("出場原因"):
        say(f"| {k} | {len(g)} | {g['R'].mean():+.2f}R |")

    say("\n## 五、主規則：止損距離\n")
    say(f"- 止損距離中位數 {main_t['風險%'].median()*100:.1f}%（25%～75%：{main_t['風險%'].quantile(.25)*100:.1f}%～"
        f"{main_t['風險%'].quantile(.75)*100:.1f}%）")
    st = main_t.sort_values("進場日")
    say(f"- 最大連續虧損 {max_consec_losses(st['R'].values)} 筆（依進場日排序，多檔同時持有）")
    say(f"- R 分布：≤ −1R {(st['R'] <= -0.95).mean()*100:.0f}%｜−1～0 {((st['R'] > -0.95) & (st['R'] <= 0)).mean()*100:.0f}%｜"
        f"0～2R {((st['R'] > 0) & (st['R'] <= 2)).mean()*100:.0f}%｜> 2R {(st['R'] > 2).mean()*100:.0f}%")
    worst = st["R"].min()
    say(f"- 最差一筆 {worst:+.1f}R（跳空越過止損）")

    say("\n> 限制：同一天多檔一起出訊號，交易之間不獨立；沒有資金與部位上限；止損用盤中最高價判斷，跳空開高時用開盤價出場；"
        "下市股票用最後價格出場（若是被收購通常是跳空上漲，已反映）。")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    out = pd.concat({k: v for k, v in allt.items()}, names=["版本"]).reset_index(level=0)
    out.to_csv(TRADES, index=False, encoding="utf-8-sig", float_format="%.4f")
    print(f"\n已存 {OUT}、{TRADES}")


if __name__ == "__main__":
    sys.exit(main())
