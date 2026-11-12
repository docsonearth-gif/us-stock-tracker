"""
美股回測：每日報告「主流板塊」與「候選股」規則（2014 起，含已下市股票）

  HIST_DIR=…/history python tools/backtest_us.py     → data/backtest_report_us.md

資料：Releases 的 us_history_long.tar（EODHD；active＋delisted，每檔 adjusted_close 與成交量）

驗證
A. 板塊「價格訊號」：52 週新高 ≥ N 檔且創高股平均收紅（台股用 N＝2）。美股板塊大小差很多，另測「創高比例」
B. Tide「法人資金」的替代：板塊 5 日等權漲幅排前 10、板塊成交額放大（5 日均 ÷ 50 日均），以及兩者組合
C. 主流＝價格訊號＋資金訊號；醞釀＝只有資金；啟動＝只有價格
D. 選股：必要條件候選（第②階段＋RS ≥ 85＋股價 ≥ 12＋50 日均成交額 ≥ 2,000 萬美元）
   在主流板塊裡 vs 不在；在 RS 中位數排名前 20% 的產業 vs 不在
E. 分期間：2015–19、2020–22（含 2020、2022 空頭）、2023 起

衡量：之後 20 個交易日的超額報酬（減去同一天全市場中位數）與「勝過大盤比例」，和台股回測同一個口徑。
每 5 個交易日取樣一次，減少重疊。板塊分類是「現在」的（產業別、主題 ETF 成分股），已下市股票不屬於任何板塊，
所以板塊報酬整體偏高；重點看「有訊號 vs 沒訊號」的差距，而不是絕對值。未扣交易成本。
"""

import csv
import glob
import gzip
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.getenv("HIST_DIR", os.path.join(ROOT, "history"))
OUT = os.path.join(ROOT, "data", "backtest_report_us.md")
BAD = re.compile(r"-(R|RI|RT|W|WS|WI|U|UN)$|^[A-Z]TEST(-|$)")
H = int(os.getenv("HORIZON", "20"))
STEP = 5
lines = []


def say(s=""):
    print(s, flush=True)
    lines.append(s)


# ---------- 讀資料 ----------

def read_one(args):
    path, key = args
    try:
        d = pd.read_csv(path, usecols=["date", "high", "close", "adjusted_close", "volume"])
    except Exception:
        return key, None
    d = d[(d["close"] > 0) & (d["adjusted_close"] > 0)]
    if len(d) < 60:
        return key, None
    d["date"] = pd.to_datetime(d["date"])
    d = d.drop_duplicates("date").set_index("date")
    f = d["adjusted_close"] / d["close"]
    return key, pd.DataFrame({"adj": d["adjusted_close"], "ah": d["high"] * f,
                              "close": d["close"], "dv": d["close"] * d["volume"]}).astype("float32")


def load():
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
    say(f"讀入 {len(res)} 檔（現存 {sum(1 for k, _ in res if not k.endswith('~D'))}、"
        f"已下市 {sum(1 for k, _ in res if k.endswith('~D'))}）")
    pan = {}
    for fld in ("adj", "ah", "close", "dv"):
        pan[fld] = pd.concat({k: d[fld] for k, d in res}, axis=1).sort_index().astype("float32")
    idx = pan["adj"].index
    idx = idx[idx >= "2014-01-02"]
    # 只留美股交易日（家數夠多的日子）
    n = pan["close"].notna().sum(axis=1)
    idx = idx[n.reindex(idx) > n.median() * 0.5]
    return {k: v.reindex(idx) for k, v in pan.items()}


# ---------- 指標 ----------

def indicators(p):
    adj, ah, close, dv = p["adj"], p["ah"], p["close"], p["dv"]
    listed = close.notna()
    valid = close.ffill() >= 1
    ret1 = (adj / adj.ffill().shift(1) - 1).where(listed)
    prev_hi = ah.shift(1).rolling(249, min_periods=59).max()
    nh = (ah >= prev_hi * 0.9995) & prev_hi.notna() & listed
    ma50, ma150, ma200 = (adj.rolling(n, min_periods=n).mean() for n in (50, 150, 200))
    hi52 = ah.rolling(250, min_periods=60).max()
    lo52 = adj.rolling(250, min_periods=60).min()
    st2 = ((adj > ma50) & (ma50 > ma150) & (ma150 > ma200) & (ma200 > ma200.shift(21))
           & (adj >= lo52 * 1.3) & (adj >= hi52 * 0.75))
    del ma50, ma150, ma200, hi52, lo52

    def r(n):
        return adj / adj.shift(n) - 1
    score = (0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252)).where(valid & listed)
    rs = (score.rank(axis=1, pct=True) * 98 + 1).astype("float32")
    del score
    dv50 = dv.rolling(50, min_periods=40).mean()
    fwd = (adj.shift(-H) / adj - 1).where(valid & listed)
    mkt = fwd.median(axis=1)
    exc = fwd.sub(mkt, axis=0).astype("float32")
    fwd60 = (adj.shift(-60) / adj - 1).where(valid & listed)
    exc60 = fwd60.sub(fwd60.median(axis=1), axis=0).astype("float32")
    r5 = r(5).where(listed).astype("float32")
    cand = st2 & (rs >= 85) & (close >= 12) & (dv50 >= 20e6)
    return dict(ret1=ret1, nh=nh, st2=st2, rs=rs, exc=exc, exc60=exc60, r5=r5, dv=dv, cand=cand, listed=listed)


# ---------- 板塊 ----------

def load_groups():
    groups = {}
    st = pd.read_csv(os.path.join(ROOT, "data", "stocks.csv"), dtype=str, keep_default_na=False)
    ind = {}
    for c, i in zip(st["code"], st["industry"]):
        if i:
            ind.setdefault("產業:" + i, []).append(c)
    th = {}
    for f in ("sectors_us.csv", "sectors_us_補充.csv"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            for r in csv.DictReader(open(p, encoding="utf-8-sig")):
                th.setdefault("主題:" + r["板塊"].strip(), []).append(r["代號"].strip())
    return ind, th


def group_frame(members, I, cols):
    """每個板塊每天的：成分股數、創高數、創高股平均漲跌、5 日等權漲幅、成交額比、RS 中位數、之後 20 日超額。"""
    rows = {}
    dv5_all = I["dv"].rolling(5, min_periods=3).mean()
    dv50_all = I["dv"].rolling(50, min_periods=40).mean()
    for g, codes in members.items():
        c = [x for x in set(codes) if x in cols]
        if len(c) < 5:
            continue
        L = I["listed"][c]
        nh = I["nh"][c]
        n_mem = L.sum(axis=1)
        rows[g] = pd.DataFrame({
            "n": n_mem,
            "nh": nh.sum(axis=1),
            "nh_ret": I["ret1"][c].where(nh).mean(axis=1),
            "r5": I["r5"][c].mean(axis=1),
            "dvr": dv5_all[c].sum(axis=1) / dv50_all[c].sum(axis=1),
            "rs_med": I["rs"][c].median(axis=1),
            "exc": I["exc"][c].mean(axis=1),
        })
    df = pd.concat(rows, names=["group", "date"]).reset_index()
    df = df[df["n"] >= 5]
    df["r5_rank"] = df.groupby("date")["r5"].rank(ascending=False)
    df["rs_pct"] = df.groupby("date")["rs_med"].rank(pct=True)
    return df


def stat(x):
    x = x.dropna()
    if len(x) < 30:
        return f"樣本 {len(x):>6}｜（太少）"
    return (f"樣本 {len(x):>6}｜平均 {x.mean() * 100:+5.2f}%｜中位數 {x.median() * 100:+5.2f}%｜"
            f"勝過大盤 {(x > 0).mean() * 100:4.1f}%")


def table(df, sigs, title):
    say(f"\n### {title}\n")
    say("| 訊號 | 樣本 | 平均超額 | 勝過大盤 |")
    say("|---|---:|---:|---:|")
    base = df["exc"].dropna()
    say(f"| （全部板塊‧基準） | {len(base)} | {base.mean() * 100:+.2f}% | {(base > 0).mean() * 100:.1f}% |")
    for name, m in sigs.items():
        x = df.loc[m, "exc"].dropna()
        if len(x) < 30:
            say(f"| {name} | {len(x)} | — | — |")
        else:
            say(f"| {name} | {len(x)} | {x.mean() * 100:+.2f}% | {(x > 0).mean() * 100:.1f}% |")


def signals(df):
    up = df["nh_ret"] > 0
    P = {n: (df["nh"] >= n) & up for n in (2, 3, 4, 6)}
    Pp = {p: (df["nh"] >= 2) & (df["nh"] / df["n"] >= p) & up for p in (0.03, 0.05, 0.10)}
    Mr = df["r5_rank"] <= 10
    Mv = {x: df["dvr"] >= x for x in (1.2, 1.3, 1.5)}
    return P, Pp, Mr, Mv


def section_groups(df, label):
    P, Pp, Mr, Mv = signals(df)
    table(df, {**{f"價格：創高 ≥ {n} 檔＋收紅": m for n, m in P.items()},
               **{f"價格：創高 ≥ 2 檔且 ≥ {int(p * 100)}% 成分股＋收紅": m for p, m in Pp.items()}},
          f"{label}－A. 價格訊號")
    table(df, {"資金A：5 日等權漲幅前 10": Mr,
               **{f"資金B：成交額比 ≥ {x}": m for x, m in Mv.items()},
               **{f"資金A＋B（≥ {x}）": Mr & m for x, m in Mv.items()}},
          f"{label}－B. 資金訊號（Tide 替代）")
    sig = {}
    for pn, pm in [("創高≥2", P[2]), ("創高≥3", P[3]), ("創高≥2且≥5%", Pp[0.05])]:
        for mn, mm in [("漲幅前10", Mr), ("成交額≥1.3", Mv[1.3]), ("漲幅前10＋成交額≥1.3", Mr & Mv[1.3])]:
            sig[f"主流：{pn}＋{mn}"] = pm & mm
    sig["醞釀：漲幅前10、創高<2"] = Mr & ~P[2]
    sig["啟動：創高≥2、不在漲幅前10"] = P[2] & ~Mr
    table(df, sig, f"{label}－C. 主流／醞釀／啟動")
    return P, Pp, Mr, Mv


def main():
    say(f"# 美股回測報告（之後 {H} 個交易日）\n")
    p = load()
    dates = p["adj"].index
    say(f"資料 {dates[0]:%Y-%m-%d} ～ {dates[-1]:%Y-%m-%d}，{len(dates)} 個交易日\n")
    say("報酬都是**超額報酬**（減去同一天全市場中位數），每 5 個交易日取樣；板塊用現在的分類，"
        "已下市股票不屬於任何板塊，所以看「有訊號 vs 基準」的差距。未扣交易成本。")
    I = indicators(p)
    del p
    cols = set(I["rs"].columns)
    sample = dates[::STEP]
    ind, th = load_groups()

    gdf = {}
    for label, members in (("產業", ind), ("主題", th)):
        df = group_frame(members, I, cols)
        df = df[df["date"].isin(sample) & df["exc"].notna()]
        gdf[label] = df
        say(f"\n## {label}（{df['group'].nunique()} 個板塊、{len(df)} 筆板塊‧日）")
        section_groups(df, label)

    # 分期間（產業）
    say("\n## E. 分期間（產業，主流＝創高≥2＋收紅＋5 日漲幅前 10）\n")
    say("| 期間 | 基準勝率 | 主流樣本 | 主流平均超額 | 主流勝率 |")
    say("|---|---:|---:|---:|---:|")
    df = gdf["產業"]
    P, Pp, Mr, Mv = signals(df)
    main_sig = P[2] & Mr
    for a, b, name in [("2014", "2019-12-31", "2014–2019"), ("2020", "2022-12-31", "2020–2022"),
                       ("2023", "2026-12-31", "2023–2026")]:
        w = (df["date"] >= a) & (df["date"] <= b)
        base = df.loc[w, "exc"]
        x = df.loc[w & main_sig, "exc"]
        say(f"| {name} | {(base > 0).mean() * 100:.1f}% | {len(x)} | {x.mean() * 100:+.2f}% | {(x > 0).mean() * 100:.1f}% |")
    for a, b, name in [("2018-10-01", "2018-12-31", "2018Q4 空頭"), ("2020-02-15", "2020-04-30", "2020 疫情"),
                       ("2022-01-01", "2022-10-31", "2022 空頭")]:
        w = (df["date"] >= a) & (df["date"] <= b)
        base = df.loc[w, "exc"]
        x = df.loc[w & main_sig, "exc"]
        say(f"| {name} | {(base > 0).mean() * 100:.1f}% | {len(x)} | "
            f"{(x.mean() * 100 if len(x) else float('nan')):+.2f}% | {((x > 0).mean() * 100 if len(x) else float('nan')):.1f}% |")

    # D. 選股
    say(f"\n## D. 選股：必要條件候選（第②階段＋RS≥85＋股價≥12＋50日均成交額≥2,000萬美元）\n")
    cand = I["cand"].loc[sample]
    exc = I["exc"].loc[sample]
    exc60 = I["exc60"].loc[sample]
    allx = exc.where(I["listed"].loc[sample]).stack()
    say(f"- 全部股票（基準）：{stat(allx)}")
    cx = exc.where(cand).stack()
    say(f"- 全部候選股：{stat(cx)}｜60 日：{stat(exc60.where(cand).stack())}")
    for label, members in (("產業", ind), ("主題", th)):
        df = gdf[label]
        P, Pp, Mr, Mv = signals(df)
        defs = {"主流（創高≥2＋漲幅前10）": P[2] & Mr,
                "主流（創高≥2＋漲幅前10＋成交額≥1.3）": P[2] & Mr & Mv[1.3],
                "主流或觀察（創高≥2 或 漲幅前10）": P[2] | Mr,
                "RS 中位數排名前 20%": df["rs_pct"] >= 0.8}
        say(f"\n**{label}**\n")
        for name, m in defs.items():
            on = df.loc[m, ["group", "date"]]
            flag = pd.DataFrame(False, index=sample, columns=cand.columns)
            for d, gs in on.groupby("date")["group"]:
                codes = {c for g in gs for c in members.get(g, [])}
                codes = [c for c in codes if c in flag.columns]
                flag.loc[d, codes] = True
            inn = exc.where(cand & flag).stack()
            out = exc.where(cand & ~flag).stack()
            say(f"- 候選股 × {name}：在 → {stat(inn)}")
            say(f"  　不在 → {stat(out)}")
            say(f"  　60 日：在 {stat(exc60.where(cand & flag).stack())}｜不在 {stat(exc60.where(cand & ~flag).stack())}")


    # F. 定案候選：主流＝創高≥3＋收紅＋成交額比≥1.3（不用 5 日漲幅排名）
    say("\n## F. 定案候選（主流＝創高≥3＋收紅＋成交額比≥1.3；醞釀＝成交額比≥1.3、創高<2；啟動＝創高≥3、成交額比<1.3）\n")
    for label, members in (("產業", ind), ("主題", th)):
        df = gdf[label]
        P, Pp, Mr, Mv = signals(df)
        sig = {"主流": P[3] & Mv[1.3], "醞釀": Mv[1.3] & (df["nh"] < 2), "啟動": P[3] & ~Mv[1.3],
               "主流（且 RS 中位數前 20%）": P[3] & Mv[1.3] & (df["rs_pct"] >= 0.8),
               "RS 中位數前 20%（不看訊號）": df["rs_pct"] >= 0.8}
        table(df, sig, f"{label}－F. 定案候選")
        say("\n| 期間 | 基準勝率 | 主流樣本 | 主流平均超額 | 主流勝率 |")
        say("|---|---:|---:|---:|---:|")
        ms = sig["主流"]
        for a, b, name in [("2014", "2019-12-31", "2014–2019"), ("2020", "2022-12-31", "2020–2022"),
                           ("2023", "2026-12-31", "2023–2026"), ("2018-10-01", "2018-12-31", "2018Q4 空頭"),
                           ("2020-02-15", "2020-04-30", "2020 疫情"), ("2022-01-01", "2022-10-31", "2022 空頭")]:
            w = (df["date"] >= a) & (df["date"] <= b)
            base = df.loc[w, "exc"]
            x = df.loc[w & ms, "exc"]
            say(f"| {name} | {(base > 0).mean() * 100:.1f}% | {len(x)} | "
                f"{(x.mean() * 100 if len(x) else float('nan')):+.2f}% | {((x > 0).mean() * 100 if len(x) else float('nan')):.1f}% |")
        say(f"\n**{label}：候選股**\n")
        for name, m in {"在主流板塊": sig["主流"], "在主流或醞釀": sig["主流"] | sig["醞釀"],
                        "在 RS 中位數前 20% 板塊": sig["RS 中位數前 20%（不看訊號）"],
                        "在主流 或 RS 中位數前 20%": sig["主流"] | sig["RS 中位數前 20%（不看訊號）"]}.items():
            on = df.loc[m, ["group", "date"]]
            flag = pd.DataFrame(False, index=sample, columns=cand.columns)
            for d, gs in on.groupby("date")["group"]:
                codes = [c for c in {c for g in gs for c in members.get(g, [])} if c in flag.columns]
                flag.loc[d, codes] = True
            say(f"- {name}：20 日 在 {stat(exc.where(cand & flag).stack())}｜不在 {stat(exc.where(cand & ~flag).stack())}")
            say(f"  　60 日 在 {stat(exc60.where(cand & flag).stack())}｜不在 {stat(exc60.where(cand & ~flag).stack())}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已存 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
