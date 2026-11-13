"""
美股 RS 排名、第②階段、板塊強度與大盤廣度（讀 fetch_us.py 存的 data/daily/*.csv.gz）
台股版 rs_taiwan.py 的移植；算法相同，差異如下：

- 還原股價：每日檔的 ret（含分割、配息的日報酬）連乘回去
- RS 評分：近 3 個月 40%＋6／9／12 個月各 20%，只和「股價 ≥ RS_MIN_PRICE（預設 1 美元）」的股票比
- 必要條件候選：第②階段＋RS ≥ 85＋股價 ≥ 12 美元＋50 日均成交額 ≥ 2,000 萬美元（J Law 美股條件）
- 市場寬度：站上 20／50／200 日線（美股慣用；TradingView S5TH 等同一概念）
- 出貨日：S&P 500 跌 ≥ 0.2% 且 NYSE 股票總成交量比前一天大；Nasdaq 另外算（用 Nasdaq 股票成交量）
- 沒有漲跌停：台股的「新高跌停」改成「新高股收跌 ≥ 5%」
- Utility Screen：基準改 S&P 500；低點後 3 天就算自低點 RS（美股沒有連續漲停把排名擠在一起的問題）

輸出
- RS_美股.xlsx：大盤廣度／52週新高／Utility Screen／自訂板塊強度／必要條件候選／VCP先決條件／產業強度／全部股票
- data/sector_history.csv、data/market_breadth.csv
"""

import glob
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT_XLSX = os.path.join(ROOT, "RS_美股.xlsx")
HIST_CSV = os.path.join(DATA, "sector_history.csv")
BREADTH_CSV = os.path.join(DATA, "market_breadth.csv")
SECTOR_FILES = ["sectors_us.csv", "sectors_us_補充.csv"]

MIN_RS = int(os.getenv("MIN_RS", "85"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "12"))                       # 美元（J Law 美股：≥ 12）
MIN_TURNOVER = float(os.getenv("MIN_DOLLAR_VOL_M", "20")) * 1e6       # 50 日均成交額（美元）
RS_MIN_PRICE = float(os.getenv("RS_MIN_PRICE", "1"))                  # 參與 RS 排名的最低股價
NH_DROP = float(os.getenv("NH_DROP", "5"))                            # 新高股收跌幾 % 算「新高大跌」
NH_DROP_N = int(os.getenv("NH_DROP_N", "5"))                          # 新高大跌幾檔算出貨警訊
HISTORY_DAYS = 81

# Utility Screen 參數
UT_MIN_DAYS = int(os.getenv("UTILITY_MIN_DAYS", "20"))
UT_MAX_DAYS = int(os.getenv("UTILITY_MAX_DAYS", "200"))
UT_MIN_RS = int(os.getenv("UTILITY_MIN_RS", "85"))
UT_MIN_TURNOVER = float(os.getenv("UTILITY_MIN_DOLLAR_VOL_M", "20")) * 1e6  # 20 日均成交額（美元）
UT_MAX_OFF_HIGH = float(os.getenv("UTILITY_MAX_OFF_HIGH", "25"))
UT_MIN_LOW_DAYS = int(os.getenv("UTILITY_MIN_LOW_DAYS", "3"))
UT_SMOOTH = int(os.getenv("UTILITY_SMOOTH", "5"))
UT_MAX_EXT = float(os.getenv("UTILITY_MAX_EXT", "15"))
UT_MIN_HELD = int(os.getenv("UTILITY_MIN_HELD", "80"))
UT_DAYS = os.getenv("UTILITY_DAYS")
UT_FORCE = os.getenv("UTILITY_FORCE") == "1"


# ---------- 讀資料 ----------

def load_prices():
    files = sorted(glob.glob(os.path.join(DATA, "daily", "*.csv.gz")))
    if not files:
        raise SystemExit("data/daily 沒有資料，請先執行 fetch_us.py")
    parts = []
    for p in files:
        df = pd.read_csv(p, dtype={"code": str}, keep_default_na=False, na_values=[""])
        df["date"] = os.path.basename(p)[:10]
        parts.append(df[["code", "date", "close", "high", "low", "volume", "ret"]])
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df["turnover"] = df["close"] * df["volume"]
    piv = {c: df.pivot_table(index="date", columns="code", values=c, aggfunc="last", dropna=False)
           for c in ("close", "high", "low", "ret", "turnover", "volume")}
    cols = piv["close"].columns
    for c in piv:
        piv[c] = piv[c].reindex(columns=cols)
    st = load_stocks()
    info = pd.DataFrame(index=cols)
    info["name"] = st["name"].reindex(cols).fillna("") if st is not None else ""
    info["market"] = st["exchange"].reindex(cols).fillna("其他") if st is not None else "其他"
    return piv, info


def load_stocks():
    p = os.path.join(DATA, "stocks.csv")
    if not os.path.exists(p):
        return None
    return pd.read_csv(p, dtype=str, keep_default_na=False).set_index("code")


def load_index_df(index):
    p = os.path.join(DATA, "index.csv")
    if not os.path.exists(p):
        return None
    t = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
    return t.reindex(index).ffill()


def load_sectors():
    parts = []
    for name in SECTOR_FILES:
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            s = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
            s.columns = [c.strip() for c in s.columns]
            parts.append(s[["代號", "板塊"]])
    if not parts:
        return None
    s = pd.concat(parts)
    s["代號"] = s["代號"].str.strip().str.upper().str.replace(".", "-", regex=False)
    s["板塊"] = s["板塊"].str.strip()
    s = s.dropna().drop_duplicates()
    print(f"自訂板塊：{s['板塊'].nunique()} 個板塊、{s['代號'].nunique()} 檔股票")
    return s


def load_industry():
    st = load_stocks()
    if st is None or "industry" not in st or not (st["industry"] != "").any():
        return None
    return st["industry"].replace("", np.nan)


# ---------- 計算 ----------

def adjusted_prices(piv):
    """用每日檔的 ret（含分割、配息）連乘還原；ret 缺值時用收盤價比，超過 ±50% 當作資料問題記 0。"""
    close, r = piv["close"], piv["ret"]
    raw = close / close.ffill().shift(1) - 1
    r = r.fillna(raw.where(raw.abs() <= 0.5, 0.0))
    listed = close.notna().cummax()
    growth = (1 + r.fillna(0)).cumprod().where(listed)
    last_close = close.ffill().iloc[-1]
    adj = growth / growth.iloc[-1] * last_close
    factor = (adj / close)
    adj_high = (piv["high"] * factor)
    adj_low = (piv["low"] * factor)
    adj = adj.where(listed).ffill().where(listed)
    return adj, adj_high, adj_low


def wilder_rsi(adj, period=14):
    d = adj.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = up / dn
    return 100 - 100 / (1 + rs)


def rs_rating(adj, valid=None):
    """每一天的 RS 評分（1–99）；資料不滿 252 天、或當天股價 < RS_MIN_PRICE 的股票為空白。"""
    def ret(n):
        return adj / adj.shift(n) - 1
    score = 0.4 * ret(63) + 0.2 * ret(126) + 0.2 * ret(189) + 0.2 * ret(252)
    if valid is not None:
        score = score.where(valid)
    pct = score.rank(axis=1, pct=True)
    return (pct * 98 + 1).round()


def period_rating(adj, n, valid=None):
    """單一期間的 RS（1–99）：近 n 個交易日報酬在全市場的百分位。歷史不滿 n 天為空白。"""
    x = adj / adj.shift(n) - 1
    if valid is not None:
        x = x.where(valid)
    pct = x.rank(axis=1, pct=True)
    return (pct * 98 + 1).round()


# 短期 RS：欄位名稱 → 交易日數
PERIOD_RS = {"RS 1月": 21, "RS 3月": 63, "RS 6月": 126}


def compute(piv, info, spx):
    adj, ahigh, alow = adjusted_prices(piv)
    close = piv["close"]
    valid = close.ffill() >= RS_MIN_PRICE
    rs = rs_rating(adj, valid)
    rsi = wilder_rsi(adj)

    ma50, ma150, ma200 = (adj.rolling(n, min_periods=n).mean() for n in (50, 150, 200))
    hi52 = ahigh.rolling(250, min_periods=60).max()
    lo52 = alow.rolling(250, min_periods=60).min()
    prev_hi = ahigh.shift(1).rolling(249, min_periods=59).max()
    # 碰到前高也算（與 TradingView 一致）；容差避免還原價格的小數誤差
    new_high = (ahigh >= prev_hi * 0.9995) & prev_hi.notna()
    prev_lo = alow.shift(1).rolling(249, min_periods=59).min()
    new_low = (alow <= prev_lo * 1.0005) & prev_lo.notna()
    pct = (piv["ret"] * 100).where(close.notna())

    stage2 = ((adj > ma50) & (ma50 > ma150) & (ma150 > ma200) & (ma200 > ma200.shift(21))
              & (adj >= lo52 * 1.3) & (adj >= hi52 * 0.75))

    T = adj.index[-1]
    out = pd.DataFrame(index=adj.columns)
    out["名稱"] = info["name"]
    out["市場"] = info["market"]
    out["收盤"] = close.ffill().loc[T]
    out["RS評分"] = rs.loc[T]
    out["RS 5日前"] = rs.shift(5).loc[T]
    out["RS 5日變化"] = out["RS評分"] - out["RS 5日前"]
    out["新晉RS90"] = (out["RS評分"] >= 90) & (out["RS 5日前"] < 90)
    for col, n in PERIOD_RS.items():
        out[col] = period_rating(adj, n, valid).loc[T]
    out["第②階段"] = stage2.loc[T]
    out["今日創52週新高"] = new_high.loc[T]
    out["近5日創52週新高"] = new_high.tail(5).any()
    out["近10日創高次數"] = new_high.tail(10).sum()
    out["距52週高點%"] = (adj.loc[T] / hi52.loc[T] - 1) * 100
    # 股價距 50 日線：正數＝在均線上方幾 %，太大代表離均線很遠（延伸）
    out["距50日線%"] = ((adj.loc[T] / ma50.loc[T] - 1) * 100).round(1)
    # RS 連續 ≥ 80 的天數（到今天為止；今天 < 80 就是 0）
    s80 = (rs >= 80).astype(int)
    c = s80.cumsum()
    streak = c - c.where(s80 == 0).ffill().fillna(0)
    out["RS≥80連續天數"] = streak.loc[T].where(rs.loc[T].notna())
    out["RSI14"] = rsi.loc[T]
    out["50日均成交額(百萬美元)"] = (piv["turnover"].tail(50).mean() / 1e6).round(1)
    out["今日成交"] = close.loc[T].notna()
    out["今日漲跌%"] = pct.loc[T].round(2)
    out["今日成交額(百萬美元)"] = (piv["turnover"].loc[T] / 1e6).round(1)

    if spx is not None and spx.notna().any():
        line = adj.div(spx, axis=0)
        line_hi = line.rolling(250, min_periods=60).max()
        out["RS線近5日創新高"] = (line.tail(5) >= line_hi.tail(5) * 0.9995).any()
    else:
        out["RS線近5日創新高"] = False

    out.index.name = "代號"
    has_hist = prev_hi.notna() & close.notna()
    panels = {"rs": rs, "rsi": rsi, "new_high": new_high, "new_low": new_low, "stage2": stage2,
              "pct": pct, "has_hist": has_hist, "ma200_ok": ma200.shift(21).notna() & close.notna(),
              "adj": adj.where(close.notna()), "market": info["market"].reindex(adj.columns),
              "adj_ff": adj, "ahigh": ahigh, "alow": alow, "ma50": ma50, "ma200": ma200,
              "hi52": hi52, "lo52": lo52}
    return out.reset_index(), panels


# ---------- Utility Screen ----------

def market_correction(spx):
    """S&P 500 目前離高點幾天、修正低點在哪。回傳 dict；資料不足回傳 None。"""
    if spx is None or spx.notna().sum() < 30:
        return None
    t = spx.dropna()
    idx = t.index
    peak = t.idxmax()            # 資料期間內最高收盤（同值取最早那天）
    d_high = len(t) - 1 - idx.get_loc(peak)
    after = t.loc[peak:]
    low = after.idxmin()
    d_low = len(t) - 1 - idx.get_loc(low)
    info = {
        "peak_date": peak, "low_date": low, "d_high": d_high, "d_low": d_low,
        "dd_now": (t.iloc[-1] / t.max() - 1) * 100,
        "dd_low": (after.min() / t.max() - 1) * 100,
    }
    if UT_DAYS:
        info["d_high"] = int(UT_DAYS)
        info["peak_date"] = idx[-1 - int(UT_DAYS)]
        info["manual"] = True
    info["active"] = UT_MIN_DAYS < info["d_high"] <= UT_MAX_DAYS
    return info


def anchored_rating(adj, anchor):
    """以 anchor 那天的收盤為起點，之後每一天的區間報酬在全市場的百分位（1–99）。"""
    base = adj.loc[anchor]
    sub = adj.loc[adj.index > anchor]
    pct = (sub / base - 1).rank(axis=1, pct=True)
    return (pct * 98 + 1).round()


def utility_screen(full, panels, piv, mc):
    adj, ahigh = panels["adj_ff"], panels["ahigh"]
    T = adj.index[-1]
    ma20 = adj.rolling(20, min_periods=20).mean()
    hi200 = ahigh.rolling(200, min_periods=60).max()
    turn20 = piv["turnover"].tail(20).mean()

    r_hi = anchored_rating(adj, mc["peak_date"])
    u = pd.DataFrame(index=adj.columns)
    u["自高點RS"] = r_hi.iloc[-1]
    u[f"自高點RS {UT_SMOOTH}日均"] = r_hi.tail(UT_SMOOTH).mean().round(1)
    ok = (r_hi >= UT_MIN_RS).astype(int)
    c = ok.cumsum()
    u[f"自高點RS≥{UT_MIN_RS}連續天數"] = (c - c.where(ok == 0).ffill().fillna(0)).iloc[-1]
    u["自高點漲跌%"] = ((adj.loc[T] / adj.loc[mc["peak_date"]] - 1) * 100).round(1)

    # 修正期 RS：指數高點 → 修正低點，這段誰撐得住（區間固定，不受最近幾天急漲影響）
    if mc["low_date"] > mc["peak_date"]:
        u["修正期RS"] = anchored_rating(adj.loc[:mc["low_date"]], mc["peak_date"]).iloc[-1]
    else:
        u["修正期RS"] = np.nan
    if mc["d_low"] >= UT_MIN_LOW_DAYS and mc["low_date"] > mc["peak_date"]:
        r_lo = anchored_rating(adj, mc["low_date"])
        u["自低點RS"] = r_lo.iloc[-1]
        u["自低點漲跌%"] = ((adj.loc[T] / adj.loc[mc["low_date"]] - 1) * 100).round(1)
    else:
        u["自低點RS"], u["自低點漲跌%"] = np.nan, np.nan

    u["距200日高點%"] = ((adj.loc[T] / hi200.loc[T] - 1) * 100).round(1)
    u["距20日線%"] = ((adj.loc[T] / ma20.loc[T] - 1) * 100).round(1)
    u["站上20MA"] = adj.loc[T] > ma20.loc[T]
    u["20MA上揚"] = ma20.loc[T] > ma20.shift(5).loc[T]
    u["收盤>200MA"] = adj.loc[T] > panels["ma200"].loc[T]
    u["50MA>200MA"] = panels["ma50"].loc[T] > panels["ma200"].loc[T]
    u["20日均成交額(百萬美元)"] = (turn20 / 1e6).round(1)
    u.index.name = "代號"

    keep = ["代號", "名稱", "市場", "產業別", "自訂板塊", "收盤", "RS評分", "RS線近5日創新高"]
    df = full[[c for c in keep if c in full.columns]].merge(u.reset_index(), on="代號", how="left")
    df = df.rename(columns={"RS評分": "年RS"})

    cond = ((df["自高點RS"] >= UT_MIN_RS) & df["收盤>200MA"] & df["50MA>200MA"]
            & (df["20日均成交額(百萬美元)"] * 1e6 >= UT_MIN_TURNOVER)
            & (df["距200日高點%"] > -UT_MAX_OFF_HIGH))
    df = df[cond].copy()

    held = df["修正期RS"].fillna(df["自高點RS"]) >= UT_MIN_HELD
    stable = df[f"自高點RS {UT_SMOOTH}日均"] >= UT_MIN_RS
    trend = df["站上20MA"]
    both = df["自低點RS"] >= UT_MIN_RS
    extended = df["距20日線%"] > UT_MAX_EXT
    base = held & stable & trend
    df["分級"] = np.select([base & both, base], ["A", "B"], "C")
    notes = []
    low_na = df["自低點RS"].isna()
    for h, s, t, b, e, na in zip(held, stable, trend, both, extended, low_na):
        n = []
        if not h:
            n.append("修正期沒撐住（近期急拉？）")
        if not s:
            n.append("自高點RS不穩")
        if not t:
            n.append("跌破20MA")
        if na:
            n.append(f"自低點RS未計算（低點後未滿{UT_MIN_LOW_DAYS}天）")
        elif not b:
            n.append("反彈偏慢（防禦型？）")
        if e:
            n.append(f"離20MA超過{UT_MAX_EXT:.0f}%，勿追")
        notes.append("、".join(n))
    df["備註"] = notes
    df["綜合分"] = df[[f"自高點RS {UT_SMOOTH}日均", "自低點RS"]].mean(axis=1, skipna=True).round(1)
    df = df.sort_values(["分級", "綜合分"], ascending=[True, False])

    cols = ["分級", "代號", "名稱", "市場", "產業別", "自訂板塊", "綜合分", "自高點RS", f"自高點RS {UT_SMOOTH}日均",
            f"自高點RS≥{UT_MIN_RS}連續天數", "修正期RS", "自低點RS", "年RS", "自高點漲跌%", "自低點漲跌%", "距200日高點%",
            "站上20MA", "20MA上揚", "距20日線%", "RS線近5日創新高", "收盤", "20日均成交額(百萬美元)", "備註"]
    return df[[c for c in cols if c in df.columns]]


# VCP 先決條件（TradingView trend_template.pine 的版本，不看 150MA）
VCP_OFF_HIGH = float(os.getenv("VCP_OFF_HIGH", "25"))     # 距 52 週高點 % 以內
VCP_ABOVE_LOW = float(os.getenv("VCP_ABOVE_LOW", "30"))   # 高於 52 週低點至少 %
VCP_MA50_TOL = float(os.getenv("VCP_MA50_TOL", "8"))      # 可低於 50MA 最多 %
VCP_MA50_UP = int(os.getenv("VCP_MA50_UP", "10"))         # 50MA 比幾天前高


def vcp_prereq(full, panels, piv):
    """VCP 先決條件：股價 ≥ 52 週高點 × 0.75、≥ 52 週低點 × 1.3、200MA 比一個月前高、
    50MA > 200MA、收盤 > 200MA、50MA 比 10 天前高、收盤 ≥ 50MA × 0.92。依 RS 評分排序。"""
    adj, ma50, ma200 = panels["adj_ff"], panels["ma50"], panels["ma200"]
    T = adj.index[-1]
    c, m50, m200 = adj.loc[T], ma50.loc[T], ma200.loc[T]
    hi, lo = panels["hi52"].loc[T], panels["lo52"].loc[T]
    ok = ((c >= hi * (1 - VCP_OFF_HIGH / 100)) & (c >= lo * (1 + VCP_ABOVE_LOW / 100))
          & (m200 > ma200.shift(21).loc[T]) & (m50 > m200) & (c > m200)
          & (m50 > ma50.shift(VCP_MA50_UP).loc[T]) & (c >= m50 * (1 - VCP_MA50_TOL / 100)))
    v = pd.DataFrame(index=adj.columns)
    v["高於52週低點%"] = ((c / lo - 1) * 100).round(1)
    v["距200日線%"] = ((c / m200 - 1) * 100).round(1)
    # 收斂程度：近 10 日最高到最低的振幅；量縮：近 10 日 ÷ 近 50 日平均成交金額
    v["近10日振幅%"] = ((panels["ahigh"].tail(10).max() / panels["alow"].tail(10).min() - 1) * 100).round(1)
    t = piv["turnover"]
    v["成交額量比(10/50日)"] = (t.tail(10).mean() / t.tail(50).mean()).round(2)
    v.index.name = "代號"
    keep = ok[ok.fillna(False)].index
    d = full[full["代號"].isin(keep)].merge(v.reset_index(), on="代號", how="left")
    d["距52週高點%"] = d["距52週高點%"].round(1)
    cols = ["代號", "名稱", "市場", "自訂板塊", "產業別", "收盤", "RS評分", "距52週高點%", "高於52週低點%",
            "距50日線%", "距200日線%", "近10日振幅%", "成交額量比(10/50日)", "50日均成交額(百萬美元)",
            "第②階段", "近10日創高次數"]
    return d[[c for c in cols if c in d.columns]].sort_values("RS評分", ascending=False)


def new_high_list(full):
    """今天創 52 週新高的股票（碰到前高也算），依成交金額由大到小排。"""
    cols = ["代號", "名稱", "市場", "自訂板塊", "產業別", "收盤", "今日漲跌%", "今日成交額(百萬美元)",
            "RS評分", "第②階段", "近10日創高次數"]
    d = full[full["今日創52週新高"]]
    return d[[c for c in cols if c in d.columns]].sort_values("今日成交額(百萬美元)", ascending=False)


def utility_note(mc):
    if mc is None:
        return "Utility Screen：沒有 S&P 500 資料（data/index.csv），無法判斷大盤修正"
    s = (f"S&P 500 高點 {mc['peak_date']:%Y-%m-%d}，距今 {mc['d_high']} 個交易日（目前 {mc['dd_now']:.1f}%）；"
         f"修正低點 {mc['low_date']:%Y-%m-%d}（{mc['dd_low']:.1f}%），距今 {mc['d_low']} 天")
    if mc.get("manual"):
        s += f"｜手動指定區間 {mc['d_high']} 天"
    if not mc["active"]:
        s += f"｜未啟動（需距高點 {UT_MIN_DAYS + 1}–{UT_MAX_DAYS} 天），請看一般 RS"
    return s


def group_strength(members, df, panels, key):
    """members：DataFrame[代號, key]。依 RS 中位數排名。"""
    m = members.merge(df.drop(columns=[key], errors="ignore"), on="代號", how="inner")
    m = m[m["今日成交"]].copy()
    g = m.groupby(key).agg(
        家數=("代號", "count"),
        RS中位數=("RS評分", "median"),
        RS中位數5日前=("RS 5日前", "median"),
        **{"RS 1月中位數": ("RS 1月", "median")},
        板塊RSI=("RSI14", "median"),
        第二階段家數=("第②階段", "sum"),
        今日創新高家數=("今日創52週新高", "sum"),
        近5日創新高家數=("近5日創52週新高", "sum"),
    )
    # 板塊 RSI 5 日前
    rsi5 = panels["rsi"].shift(5).iloc[-1]
    m["RSI 5日前"] = m["代號"].map(rsi5)
    g["板塊RSI 5日變化"] = g["板塊RSI"] - m.groupby(key)["RSI 5日前"].median()
    g["RS中位數5日變化"] = g["RS中位數"] - g["RS中位數5日前"]
    g["第二階段比例%"] = g["第二階段家數"] / g["家數"] * 100
    g["近10日創高比例%"] = m.groupby(key)["近10日創高次數"].apply(lambda x: (x > 0).mean() * 100)
    g = g.drop(columns=["RS中位數5日前"])

    # 龍頭候選：RS 最高、近 10 日創高次數多的前 3 檔
    def leaders(x):
        x = x.dropna(subset=["RS評分"]).sort_values(["RS評分", "近10日創高次數"], ascending=False).head(3)
        return "、".join(f"{n}({int(r)})" for n, r in zip(x["名稱"], x["RS評分"]))
    g["龍頭候選(RS)"] = m.groupby(key).apply(leaders, include_groups=False)

    g = g.sort_values("RS中位數", ascending=False)
    g.insert(0, "排名", range(1, len(g) + 1))
    return g.reset_index()


def sector_history(members, panels, info_df):
    """最近 HISTORY_DAYS 個交易日，每個自訂板塊的 RS 中位數、排名、RSI 中位數、創高家數。"""
    dates = panels["rs"].index[-HISTORY_DAYS:]
    rows = []
    mem = members.groupby("板塊")["代號"].apply(list)
    for s, codes in mem.items():
        codes = [c for c in codes if c in panels["rs"].columns]
        if not codes:
            continue
        rs = panels["rs"].loc[dates, codes].median(axis=1)
        rsi = panels["rsi"].loc[dates, codes].median(axis=1)
        nh = panels["new_high"].loc[dates, codes].sum(axis=1)
        st = panels["stage2"].loc[dates, codes].sum(axis=1)
        for d in dates:
            rows.append([d.strftime("%Y-%m-%d"), s, round(rs[d], 1) if pd.notna(rs[d]) else "",
                         round(rsi[d], 1) if pd.notna(rsi[d]) else "", int(nh[d]), int(st[d])])
    h = pd.DataFrame(rows, columns=["日期", "板塊", "RS中位數", "板塊RSI", "創新高家數", "第二階段家數"])
    h["RS中位數"] = pd.to_numeric(h["RS中位數"], errors="coerce")
    h["排名"] = h.groupby("日期")["RS中位數"].rank(ascending=False, method="min")
    h.to_csv(HIST_CSV, index=False, encoding="utf-8-sig")
    return h


def rank_changes(h):
    """板塊 RS 排名的 5 日、20 日變化（正數 = 名次進步）。"""
    piv = h.pivot(index="日期", columns="板塊", values="排名").sort_index()
    out = pd.DataFrame({"排名5日進步": piv.shift(5).iloc[-1] - piv.iloc[-1],
                        "排名20日進步": piv.shift(20).iloc[-1] - piv.iloc[-1]})
    return out.reset_index()


# ---------- 板塊訊號（美股回測定案，見 data/backtest_report_us.md） ----------
SIG_NH = int(os.getenv("SIG_NEW_HIGHS", "3"))         # 主流：創 52 週新高 ≥ 3 檔且創高股平均收紅
SIG_DVR = float(os.getenv("SIG_DOLLAR_VOL_RATIO", "1.3"))  # 資金：板塊 5 日均成交額 ÷ 50 日均 ≥ 1.3
SIG_DAYS = 130
GROUP_SIG_CSV = os.path.join(DATA, "group_signals.csv")


def group_signals(groups, panels, piv):
    """groups：{(類型, 板塊): [代號]}。最近 SIG_DAYS 天每個板塊的訊號，存 data/group_signals.csv。
    主流＝創高 ≥ SIG_NH 且收紅＋成交額比 ≥ SIG_DVR；醞釀＝成交額比 ≥ SIG_DVR、創高 < 2；啟動＝創高 ≥ SIG_NH 且收紅、成交額比未達。"""
    nh_all, pct, rs = panels["new_high"], panels["pct"], panels["rs"]
    dv = piv["turnover"]
    dv5, dv50 = dv.rolling(5, min_periods=3).mean(), dv.rolling(50, min_periods=40).mean()
    dates = rs.index[-SIG_DAYS:]
    rows = []
    for (kind, g), codes in groups.items():
        c = [x for x in set(codes) if x in rs.columns]
        if len(c) < 5:
            continue
        nh = nh_all.loc[dates, c]
        listed = piv["close"].loc[dates, c].notna()
        rows.append(pd.DataFrame({
            "日期": dates.strftime("%Y-%m-%d"), "類型": kind, "板塊": g,
            "家數": listed.sum(axis=1).values,
            "創高": nh.sum(axis=1).values,
            "創高股平均漲跌%": pct.loc[dates, c].where(nh).mean(axis=1).round(2).values,
            "成交額比": (dv5.loc[dates, c].sum(axis=1) / dv50.loc[dates, c].sum(axis=1)).round(2).values,
            "RS中位數": rs.loc[dates, c].median(axis=1).round(1).values,
            "5日漲幅%": (panels["adj"].loc[dates, c] / panels["adj"].shift(5).loc[dates, c] - 1)
            .mean(axis=1).mul(100).round(2).values,
        }))
    if not rows:
        return None
    df = pd.concat(rows, ignore_index=True)
    df["RS排名"] = df.groupby(["日期", "類型"])["RS中位數"].rank(ascending=False, method="min")
    df["RS百分位"] = df.groupby(["日期", "類型"])["RS中位數"].rank(pct=True).round(3)
    price = (df["創高"] >= SIG_NH) & (df["創高股平均漲跌%"] > 0)
    money = df["成交額比"] >= SIG_DVR
    df["價格訊號"], df["資金訊號"] = price, money
    df["主流"] = price & money
    df["醞釀"] = money & (df["創高"] < 2)
    df["啟動"] = price & ~money
    # 排名變化（正數＝進步）：1 週、1 個月、3 個月、6 個月前（J Law：和上週、3 個月前、6 個月前比）
    piv_rank = df.pivot_table(index="日期", columns=["類型", "板塊"], values="RS排名").sort_index()
    for n, name in ((5, "排名1週進步"), (21, "排名1月進步"), (63, "排名3月進步"), (126, "排名6月進步")):
        ch = (piv_rank.shift(n) - piv_rank).stack(["類型", "板塊"], future_stack=True).rename(name)
        df = df.merge(ch.reset_index(), on=["日期", "類型", "板塊"], how="left")
    df.to_csv(GROUP_SIG_CSV, index=False, encoding="utf-8-sig")
    return df


MA_BREADTH = (20, 50, 200)
BREADTH_GROUPS = {"全部": None, "NYSE": ("NYSE", "AMEX", "NYSE ARCA", "BATS"), "Nasdaq": ("NASDAQ",)}


def distribution(idx_close, vol):
    """出貨日：指數跌 ≥ 0.2% 且成交量比前一天大；回傳 (每日是否出貨日, 25 日出貨日數)。"""
    chg = idx_close.pct_change() * 100
    d = (chg <= -0.2) & (vol > vol.shift(1)) & (vol > 0)
    return d, d.rolling(25, min_periods=1).sum().astype(int), chg.round(2)


def market_breadth(piv, panels, idx):
    """每日大盤廣度。市場狀態規則同台股，但「新高跌停」改成「新高股收跌 ≥ NH_DROP%」。"""
    nh, nl, st, pct = panels["new_high"], panels["new_low"], panels["stage2"], panels["pct"]
    valid = panels["has_hist"]
    df = pd.DataFrame(index=nh.index)
    df["52週新高"] = nh.sum(axis=1)
    df["52週新低"] = nl.sum(axis=1)
    df["新高減新低"] = df["52週新高"] - df["52週新低"]
    df["新高減新低10日均"] = df["新高減新低"].rolling(10).mean()
    p = pct.where(nh)
    df["新高收紅"] = (p > 0).sum(axis=1)
    df["新高收黑"] = (p < 0).sum(axis=1)
    df[f"新高大跌≥{NH_DROP:g}%"] = (p <= -NH_DROP).sum(axis=1)
    df["收黑比例%"] = (df["新高收黑"] / df["52週新高"].replace(0, np.nan) * 100).round(1)
    df["前5日均"] = df["52週新高"].shift(1).rolling(5).mean()
    df["相對前5日均"] = (df["52週新高"] / df["前5日均"]).round(2)
    df["第②階段比例%"] = (st.sum(axis=1) / panels["ma200_ok"].sum(axis=1) * 100).round(1)
    adj, mkt = panels["adj"], panels["market"]
    for n in MA_BREADTH:
        ma = adj.rolling(n, min_periods=n).mean()
        ok, above = ma.notna() & adj.notna(), adj > ma
        for g, exs in BREADTH_GROUPS.items():
            cols = adj.columns if exs is None else mkt.index[mkt.isin(exs)]
            df[f"{g}站上{n}MA%"] = (above[cols].sum(axis=1) / ok[cols].sum(axis=1).replace(0, np.nan) * 100).round(1)
    if idx is not None and "spx" in idx:
        vol, mkt_ = piv["volume"], panels["market"]
        nyse_vol = vol[mkt_.index[mkt_.isin(BREADTH_GROUPS["NYSE"])]].sum(axis=1)
        nq_vol = vol[mkt_.index[mkt_.isin(BREADTH_GROUPS["Nasdaq"])]].sum(axis=1)
        df["S&P 500"] = idx["spx"]
        dd, n25, chg = distribution(idx["spx"], nyse_vol)
        df["S&P漲跌%"], df["S&P出貨日"], df["S&P 25日出貨日數"] = chg, dd, n25
        if "ixic" in idx and idx["ixic"].notna().any():
            df["Nasdaq"] = idx["ixic"]
            dd, n25, chg = distribution(idx["ixic"], nq_vol)
            df["Nasdaq漲跌%"], df["Nasdaq出貨日"], df["Nasdaq 25日出貨日數"] = chg, dd, n25
        spx = idx["spx"]
        near_hi = spx >= spx.rolling(20).max() * 0.99
        near_lo = spx <= spx.rolling(20).min() * 1.01
        weak = (df["新高減新低10日均"] < df["新高減新低10日均"].shift(10)) | \
               (df["第②階段比例%"] < df["第②階段比例%"].shift(10) - 2)
        strong = (df["52週新低"].rolling(5).mean() < df["52週新低"].rolling(5).mean().shift(10))
        df["指數廣度背離"] = np.where(near_hi & weak, "指數近高點、廣度轉弱",
                              np.where(near_lo & strong, "指數近低點、新低減少", ""))

    def state(r):
        if pd.isna(r["前5日均"]):
            return "資料不足"
        if r["收黑比例%"] >= 40 or r[f"新高大跌≥{NH_DROP:g}%"] >= NH_DROP_N:
            return "出貨警訊"
        if r["相對前5日均"] <= 0.8:
            return "收縮"
        if r["相對前5日均"] >= 1.2 and r["收黑比例%"] < 30:
            return "擴張"
        return "持平"
    df["市場狀態"] = df.apply(state, axis=1)
    df = df[valid.sum(axis=1) > 1500]
    df.index = df.index.strftime("%Y-%m-%d")
    df.index.name = "日期"
    df.to_csv(BREADTH_CSV, encoding="utf-8-sig")
    return df


# ---------- 做空觀察（Notion「沽空」筆記的選股條件；只篩選，進場時機自己判斷） ----------

SW_TOP_N = int(os.getenv("SHORT_TOP_N", "1500"))          # 排除小型股：50 日均成交額排名前幾名
SW_MIN_PRICE = float(os.getenv("SHORT_MIN_PRICE", "12"))  # 美元
SW_LEADER_RS = int(os.getenv("SHORT_LEADER_RS", "80"))    # 過去一年 RS 曾達到
SW_BREAK_DAYS = int(os.getenv("SHORT_BREAK_DAYS", "60"))  # 幾天內曾收在 200MA 之下


def index_failed_rally(s):
    """指數反彈到下彎 50MA 或 20MA 失敗（只有收盤價，用收盤判斷）。回傳每天 True/False。"""
    ma20 = s.rolling(20).mean()
    out = pd.Series(False, index=s.index)
    for n in (50, 20):
        ma = s.rolling(n).mean()
        touched = (s / ma).rolling(5, min_periods=1).max() >= 0.98
        out |= ((ma < ma.shift(10)) & touched & (s < ma) & (s < ma20) & (s < s.shift(1))).fillna(False)
    return out


def index_short_note(s, name):
    if s is None or s.notna().sum() < 210:
        return f"{name}：資料不足"
    s = s.dropna()
    ma200 = s.rolling(200).mean()
    T = s.index[-1]
    pos = "在 200MA 之下" if s[T] < ma200[T] else "在 200MA 之上"
    slope = "200MA 下彎" if ma200[T] < ma200.shift(20)[T] else "200MA 上揚"
    fr = index_failed_rally(s)
    recent = fr.tail(5)
    tag = ""
    if recent.any():
        tag = f"｜⚠ 近 5 日出現指數反彈失敗（{recent[recent].index[-1]:%m-%d}）"
        if s[T] >= ma200[T]:
            tag += "，但指數仍在 200MA 之上（回測無優勢）"
    return f"{name} {pos}（{(s[T] / ma200[T] - 1) * 100:+.1f}%）、{slope}{tag}"


def load_earnings():
    p = os.path.join(DATA, "earnings.csv")
    if not os.path.exists(p):
        return None
    e = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    e["date"] = pd.to_datetime(e["date"], errors="coerce")
    return e.dropna(subset=["date"]).sort_values("date").groupby("code")["date"].first()


def short_watch(full, panels, piv, spx):
    """前領導股做頭、跌破 200MA 的大型＋中型股。只篩選，不給進場訊號。"""
    adj, ah, rs = panels["adj_ff"], panels["ahigh"], panels["rs"]
    ma50, ma200 = panels["ma50"], panels["ma200"]
    close = piv["close"]
    T = adj.index[-1]
    dv50 = piv["turnover"].tail(50).mean()
    rank = dv50.where(close.loc[T].notna()).rank(ascending=False)
    hi252 = ah.tail(252).max()
    c = adj.loc[T]

    w = pd.DataFrame(index=adj.columns)
    w["一年RS最高"] = rs.tail(252).max()
    w["RS 21日前"] = rs.shift(21).loc[T]
    w["成交額排名"] = rank
    w["52週高點距今(天)"] = ah.tail(252).apply(lambda x: len(x) - 1 - int(np.nanargmax(x.values)) if x.notna().any() else np.nan)
    w["距52週高點%"] = ((c / hi252 - 1) * 100).round(1)
    w["距200日線%"] = ((c / ma200.loc[T] - 1) * 100).round(1)
    w["距50日線%"] = ((c / ma50.loc[T] - 1) * 100).round(1)
    w["50MA向下"] = ma50.loc[T] < ma50.shift(10).loc[T]
    w["200MA向下"] = ma200.loc[T] < ma200.shift(20).loc[T]
    w["50MA<200MA"] = ma50.loc[T] < ma200.loc[T]
    below = (adj < ma200).tail(SW_BREAK_DAYS)
    w["近期收在200MA下天數"] = below.sum()
    # 從下面反彈上來：近 15 日曾收在 200MA × 0.97 以下，近 5 日最高價又碰到 200MA × 0.98
    w["近5日反彈到200MA"] = (((ah / ma200).tail(5).max() >= 0.98)
                          & ((adj / ma200).tail(15).min() < 0.97) & (c < ma200.loc[T]))
    # 反彈到 50MA：股價在 50MA 之下，近 5 日最高價碰到 50MA × 0.98
    w["近5日反彈到50MA"] = ((ah / ma50).tail(5).max() >= 0.98) & (c < ma50.loc[T])
    w["MA200"] = ma200.loc[T]
    w["MA50"] = ma50.loc[T]
    w["收盤還原"] = c
    if spx is not None and spx.notna().any():
        line = adj.div(spx, axis=0)
        # RS 線持續往下一個月：RS 線在自己的 21 日均線之下，且 21 日均線比 10 天前低（不設 RS 分數門檻）
        lma = line.rolling(21, min_periods=15).mean()
        w["RS線持續往下"] = (line.loc[T] < lma.loc[T]) & (lma.loc[T] < lma.shift(10).loc[T])
    else:
        w["RS線持續往下"] = True
    w.index.name = "代號"

    keep = ["代號", "名稱", "市場", "產業別", "自訂板塊", "收盤", "RS評分", "50日均成交額(百萬美元)"]
    df = full[[k for k in keep if k in full.columns]].merge(w.reset_index(), on="代號", how="left")
    cond = ((df["成交額排名"] <= SW_TOP_N) & (df["收盤"] >= SW_MIN_PRICE)
            & (df["一年RS最高"] >= SW_LEADER_RS)
            & (df["52週高點距今(天)"] >= 21)
            & df["RS線持續往下"]
            & df["50MA向下"]
            # 已跌破 200MA（目前在 200MA 下）；或還在 200MA 之上但已跌破下彎的 50MA（早期）
            & ((df["收盤還原"] < df["MA200"]) | (df["收盤還原"] < df["MA50"])))
    df = df[cond.fillna(False)].copy()

    earn = load_earnings()
    if earn is not None:
        df["下次財報"] = df["代號"].map(earn).dt.strftime("%Y-%m-%d")
        soon = df["代號"].map(earn) <= T + pd.Timedelta(days=10)
    else:
        df["下次財報"], soon = "", pd.Series(False, index=df.index)


    def status(r):
        if r["收盤還原"] >= r["MA200"]:
            return "早期（未跌破200MA）"
        if r["近5日反彈到200MA"]:
            return "反彈到200MA附近"
        if r["近5日反彈到50MA"]:
            return "反彈到50MA附近"
        if r["近期收在200MA下天數"] <= 3:
            return "剛跌破200MA"
        return "在200MA之下"
    df["狀態"] = df.apply(status, axis=1)
    # 參考止損：200MA 相關看 200MA × 1.045；反彈到 50MA、早期看 50MA × 1.045（筆記：後期入場以 50MA 當止損參考）
    use50 = df["狀態"].isin(["反彈到50MA附近", "早期（未跌破200MA）"])
    df["止損依據"] = np.where(use50, "50MA×1.045", "200MA×1.045")
    df["參考止損"] = (np.where(use50, df["MA50"], df["MA200"]) * 1.045 * df["收盤"] / df["收盤還原"]).round(2)
    df["止損距離%"] = ((np.where(use50, df["MA50"], df["MA200"]) * 1.045 / df["收盤還原"] - 1) * 100).round(1)
    notes = []
    for _, r in df.iterrows():
        n = []
        if r["狀態"] == "剛跌破200MA":
            n.append("筆記：第一次跌破不空，等反彈")
        if r["狀態"] == "早期（未跌破200MA）":
            n.append("筆記：未跌破200MA就空，止損難設、成功率較低")
        if r["距50日線%"] <= -15:
            n.append("已大幅延伸，勿追空")
        if r["止損距離%"] > 10:
            n.append("離止損太遠")
        if r["200MA向下"]:
            n.append("200MA向下（中段做空）")
        notes.append("、".join(n))
    df["備註"] = notes
    sm = soon.reindex(df.index).fillna(False).astype(bool)
    df.loc[sm, "備註"] = ("⚠ 10天內財報、" + df.loc[sm, "備註"]).str.rstrip("、")
    order = {"反彈到200MA附近": 0, "反彈到50MA附近": 1, "在200MA之下": 2, "剛跌破200MA": 3, "早期（未跌破200MA）": 4}
    df = df.sort_values(["狀態", "止損距離%"], key=lambda s: s.map(order) if s.name == "狀態" else s)
    cols = ["狀態", "代號", "名稱", "產業別", "自訂板塊", "收盤", "RS評分", "RS 21日前", "一年RS最高",
            "距200日線%", "距50日線%", "50MA<200MA", "200MA向下", "近期收在200MA下天數", "52週高點距今(天)",
            "距52週高點%", "參考止損", "止損依據", "止損距離%", "下次財報", "成交額排名", "50日均成交額(百萬美元)", "備註"]
    return df[[k for k in cols if k in df.columns]]


# ---------- Excel ----------

RS_COLOR_COLS = ("RS評分", "RS中位數", "板塊RSI", "RSI14", "RS 1月中位數", *PERIOD_RS,
                 "綜合分", "自高點RS", f"自高點RS {UT_SMOOTH}日均", "修正期RS", "自低點RS", "年RS")


def write_excel(sec, cand, ind, full, latest, breadth=None, util=None, util_note="", newhigh=None, vcp=None,
                short=None, short_note=""):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(name="Arial", bold=True, color="FFFFFF")
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        sheets = []
        if util is not None:
            sheets.append(("Utility Screen", util))
        if sec is not None:
            sheets.append(("自訂板塊強度", sec))
        sheets += [("必要條件候選", cand)]
        if vcp is not None:
            sheets.append(("VCP先決條件", vcp))
        if ind is not None:
            sheets.append(("產業強度", ind))
        if short is not None:
            sheets.append(("做空觀察", short))
        sheets.append(("全部股票", full))
        if newhigh is not None:
            sheets.insert(0, ("52週新高", newhigh))
        if breadth is not None:
            sheets.insert(0, ("大盤廣度", breadth.iloc[::-1].head(60).reset_index()))
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name, index=False, startrow=1)
            ws = xw.sheets[name]
            title = f"資料日期 {latest}"
            if name == "Utility Screen" and util_note:
                title += f"｜{util_note}"
            if name == "做空觀察" and short_note:
                title += f"｜{short_note}"
            ws.cell(row=1, column=1, value=title).font = Font(name="Arial", bold=True)
            for c in ws[2]:
                c.fill, c.font = head_fill, head_font
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.freeze_panes = "D3" if name in ("Utility Screen", "做空觀察") else "C3"
            for i, col in enumerate(df.columns, 1):
                q = df[col].map(lambda v: len(str(v))).quantile(0.9) if len(df) else 4
                width = max(8, min(36, int(q * 1.8) + 2, len(str(col)) * 2 + 2))
                ws.column_dimensions[get_column_letter(i)].width = width
                if pd.api.types.is_float_dtype(df[col]):
                    for cell in ws.iter_cols(min_col=i, max_col=i, min_row=3):
                        for c in cell:
                            c.number_format = "0.0"
                if col in RS_COLOR_COLS and len(df):
                    L = get_column_letter(i)
                    ws.conditional_formatting.add(
                        f"{L}3:{L}{len(df) + 2}",
                        ColorScaleRule(start_type="num", start_value=20, start_color="F8696B",
                                       mid_type="num", mid_value=50, mid_color="FFFFFF",
                                       end_type="num", end_value=90, end_color="63BE7B"))


# ---------- 主程式 ----------

def main():
    piv, info = load_prices()
    idx = load_index_df(piv["close"].index)
    spx = idx["spx"] if idx is not None and "spx" in idx else None
    n_days = len(piv["close"].index)
    latest = piv["close"].index[-1].strftime("%Y-%m-%d")
    print(f"資料：{n_days} 個交易日、{piv['close'].shape[1]} 檔，最新 {latest}")
    if n_days < 253:
        print("注意：資料不滿 253 天，RS 評分會是空白（還在補歷史）。")

    full, panels = compute(piv, info, spx)
    industry = load_industry()
    full["產業別"] = full["代號"].map(industry).fillna("未分類") if industry is not None else "未分類"

    ind = None
    if industry is not None:
        ind = group_strength(full[["代號", "產業別"]], full, panels, "產業別").rename(columns={"產業別": "產業"})

    sectors = load_sectors()
    sec = None
    if sectors is not None:
        sec = group_strength(sectors, full, panels, "板塊")
        rank = sectors.merge(sec[["板塊", "排名"]], on="板塊").sort_values("排名")
        agg = rank.groupby("代號").agg(自訂板塊=("板塊", "、".join), 最佳板塊排名=("排名", "min"))
        full = full.merge(agg, on="代號", how="left")
        hist = sector_history(sectors, panels, full)
        sec = sec.merge(rank_changes(hist), on="板塊", how="left")
    else:
        full["自訂板塊"], full["最佳板塊排名"] = "", np.nan

    full = full[full["今日成交"]].drop(columns=["今日成交", "RS 5日前"])
    newhigh = new_high_list(full)
    cols = ["代號", "名稱", "市場", "產業別", "自訂板塊", "最佳板塊排名", "RS評分", "RS 5日變化",
            *PERIOD_RS, "新晉RS90", "RS≥80連續天數", "收盤", "今日漲跌%", "距52週高點%", "距50日線%",
            "今日創52週新高", "近5日創52週新高", "近10日創高次數", "RS線近5日創新高", "第②階段", "RSI14",
            "50日均成交額(百萬美元)"]
    full = full[cols].sort_values("RS評分", ascending=False)
    vcp = vcp_prereq(full, panels, piv)

    cand = full[full["第②階段"] & (full["RS評分"] >= MIN_RS) & (full["收盤"] >= MIN_PRICE)
                & (full["50日均成交額(百萬美元)"] * 1e6 >= MIN_TURNOVER)]

    mc = market_correction(spx)
    note = utility_note(mc)
    util = None
    if mc is not None and (mc["active"] or (UT_FORCE and mc["d_high"] >= 5)):
        util = utility_screen(full, panels, piv, mc)
    elif mc is not None:
        util = pd.DataFrame(columns=["分級", "代號", "名稱"])

    breadth = market_breadth(piv, panels, idx)

    # 報告用的資料檔
    groups = {}
    if industry is not None:
        for c, g in industry.dropna().items():
            groups.setdefault(("產業", g), []).append(c)
    if sectors is not None:
        for c, g in zip(sectors["代號"], sectors["板塊"]):
            groups.setdefault(("主題", g), []).append(c)
    gs = group_signals(groups, panels, piv)
    if gs is not None:
        today = gs[gs["日期"] == gs["日期"].max()]
        print(f"\n板塊訊號（{today['日期'].iloc[0]}）：主流 {int(today['主流'].sum())}、醞釀 {int(today['醞釀'].sum())}、"
              f"啟動 {int(today['啟動'].sum())}")
        print(today[today["主流"]][["類型", "板塊", "家數", "創高", "成交額比", "RS排名"]].to_string(index=False))
    first_day = piv["close"].apply(lambda x: x.first_valid_index()).dt.strftime("%Y-%m-%d")
    full.assign(資料日期=latest, 首個交易日=full["代號"].map(first_day)).to_csv(
        os.path.join(DATA, "stocks_today.csv"), index=False, encoding="utf-8-sig")
    if util is not None:
        util.to_csv(os.path.join(DATA, "utility_today.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(DATA, "utility_note.txt"), "w", encoding="utf-8") as f:
        f.write(note)
    short = short_watch(full, panels, piv, spx)
    short_note = (index_short_note(spx, "S&P 500") + "｜只篩選（前領導股做頭＋跌破 200MA，或未跌破 200MA 但跌破下彎 50MA＝早期），進場時機自己判斷；"
                  "回測：個股做空自動化無優勢，見 Project 反向RS做空.md")
    short.to_csv(os.path.join(DATA, "short_watch.csv"), index=False, encoding="utf-8-sig")
    write_excel(sec, cand, ind, full, latest, breadth, util, note, newhigh, vcp, short, short_note)

    print(f"計算 RS 的股票：{full['RS評分'].notna().sum()} 檔；符合必要條件：{len(cand)} 檔")
    show = [c for c in ["52週新高", "52週新低", "收黑比例%", "第②階段比例%", "全部站上50MA%",
                        "S&P 25日出貨日數", "Nasdaq 25日出貨日數", "市場狀態", "指數廣度背離"] if c in breadth]
    print("\n大盤廣度（最近 5 日）：")
    print(breadth.tail(5)[show].to_string())
    if sec is not None:
        print("\n自訂板塊強度前 10 名：")
        print(sec.head(10)[["排名", "板塊", "家數", "RS中位數", "板塊RSI", "第二階段家數", "近5日創新高家數"]]
              .round(1).to_string(index=False))
    print("\n必要條件候選前 15 名：")
    print(cand.head(15)[["代號", "名稱", "RS評分", "距52週高點%", "近10日創高次數"]]
          .round(1).to_string(index=False))
    print(f"\n今日創 52 週新高：{len(newhigh)} 檔；符合 VCP 先決條件：{len(vcp)} 檔")
    print(f"\n{note}")
    print(f"\n做空觀察：{len(short)} 檔（反彈到200MA附近 {int((short['狀態'] == '反彈到200MA附近').sum())}）｜{short_note}")
    if util is not None and len(util):
        print(f"Utility Screen 符合 {len(util)} 檔（A {int((util['分級'] == 'A').sum())}、"
              f"B {int((util['分級'] == 'B').sum())}、C {int((util['分級'] == 'C').sum())}），前 15 名：")
        print(util.head(15)[["分級", "代號", "名稱", "綜合分", "自高點RS", "修正期RS", "自低點RS", "年RS", "距20日線%"]]
              .to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
