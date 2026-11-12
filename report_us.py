"""
美股每日報告 → report/美股每日報告.pdf（最新）＋ report/archive/YYYY-MM-DD.pdf（每天一份）

順序同台股：大盤（能不能出手）→ 板塊（錢往哪裡去）→ 個股（候選）
規則依美股回測定案（data/backtest_report_us.md）：
  主流＝創 52 週新高 ≥ 3 檔且創高股平均收紅＋板塊成交額比（5 日均 ÷ 50 日均）≥ 1.3
  醞釀＝成交額比 ≥ 1.3、創高 < 2（只列主題板塊；產業板塊回測沒有優勢）
  啟動＝創高 ≥ 3 且收紅、成交額比未達
  尾聲＝前 4 個交易日曾是主流，今天只剩價格或資金其中一項；退潮＝兩項都沒有
  候選股＝第②階段＋RS ≥ 85＋股價 ≥ 12＋50 日均成交額 ≥ 2,000 萬美元，
          且在主流板塊（產業或主題）或 RS 中位數前 20% 的產業；主流優先，再依加分項數、RS 排序
不放 VCP、RS 線、新晉 RS90 等個股訊號（同台股；這些在 RS_美股.xlsx 裡）。
"""

import html
import os
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd

from common import DATA, ROOT

OUT_DIR = os.path.join(ROOT, "report")
OUT_PDF = os.path.join(OUT_DIR, "美股每日報告.pdf")
ARCHIVE = os.path.join(OUT_DIR, "archive")
MAX_CAND = int(os.getenv("REPORT_MAX_CANDIDATES", "15"))
EXT_WARN = 25  # 距 50 日線超過幾 % 標「勿追」
notes = []


def rd(name, **kw):
    p = os.path.join(DATA, name)
    return pd.read_csv(p, encoding="utf-8-sig", **kw) if os.path.exists(p) else None


def e(x):
    return html.escape(str(x))


def num(x, fmt="{:.1f}", na="—"):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return na
        return fmt.format(x)
    except Exception:
        return na


def table(df, cls=""):
    if df is None or df.empty:
        return "<p class='muted'>（無）</p>"
    h = "".join(f"<th>{e(c)}</th>" for c in df.columns)
    rows = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in r) + "</tr>" for r in df.astype(str).values)
    return f"<table class='{cls}'><thead><tr>{h}</tr></thead><tbody>{rows}</tbody></table>"


# ---------- 1. 大盤 ----------

def market():
    b = rd("market_breadth.csv")
    m = rd("market_monitor.csv")
    out = []
    if b is None or b.empty:
        notes.append("沒有大盤廣度資料（market_breadth.csv）")
        return "", None
    t, p10 = b.iloc[-1], b.iloc[-11] if len(b) > 11 else b.iloc[0]
    arrow = "↑" if t["第②階段比例%"] > p10["第②階段比例%"] else "↓" if t["第②階段比例%"] < p10["第②階段比例%"] else "→"
    state = t["市場狀態"]
    warn = []
    if t.get("S&P 25日出貨日數", 0) >= 5:
        warn.append(f"S&P 出貨日 {int(t['S&P 25日出貨日數'])} 天（≥ 5 要提高警覺）")
    if t.get("Nasdaq 25日出貨日數", 0) >= 5:
        warn.append(f"Nasdaq 出貨日 {int(t['Nasdaq 25日出貨日數'])} 天")
    for col in ("全部站上20MA%", "全部站上50MA%", "全部站上200MA%"):
        if col in t and t[col] <= 15:
            warn.append(f"{col.replace('全部', '')} 只有 {t[col]}%（極低）")
        if col in t and t[col] >= 85:
            warn.append(f"{col.replace('全部', '')} 達 {t[col]}%（極高）")
    if isinstance(t.get("指數廣度背離"), str) and t["指數廣度背離"]:
        warn.append(t["指數廣度背離"])

    out.append(f"<p class='big'><b>{e(state)}</b>｜新高 {int(t['52週新高'])}（前 5 日均 {num(t['前5日均'])}）、"
               f"新低 {int(t['52週新低'])}｜第②階段 {t['第②階段比例%']}%（{arrow}）｜"
               f"出貨日 S&P {int(t.get('S&P 25日出貨日數', 0))}、Nasdaq {int(t.get('Nasdaq 25日出貨日數', 0))}（25 日內）</p>")
    out.append(f"<p>S&P 500 {num(t.get('S&P 500'), '{:,.2f}')}（{num(t.get('S&P漲跌%'), '{:+.2f}')}%）｜"
               f"Nasdaq {num(t.get('Nasdaq'), '{:,.2f}')}（{num(t.get('Nasdaq漲跌%'), '{:+.2f}')}%）</p>")
    w = pd.DataFrame({
        "": ["全部", "NYSE", "Nasdaq"],
        "站上 20MA": [f"{t.get(f'{g}站上20MA%', '—')}%" for g in ("全部", "NYSE", "Nasdaq")],
        "站上 50MA": [f"{t.get(f'{g}站上50MA%', '—')}%" for g in ("全部", "NYSE", "Nasdaq")],
        "站上 200MA": [f"{t.get(f'{g}站上200MA%', '—')}%" for g in ("全部", "NYSE", "Nasdaq")],
    })
    out.append(table(w, "narrow"))
    if m is not None and not m.empty:
        r = m.iloc[-1]
        out.append(f"<p><b>Market Monitor：{e(r['state'])}</b>｜漲 ≥ 4% {int(r['up4'])}／跌 ≥ 4% {int(r['dn4'])}｜"
                   f"5 日比 {num(r['ratio5'], '{:.2f}')}／10 日比 {num(r['ratio10'], '{:.2f}')}｜站上 40MA（T2108）{num(r['t2108'])}%｜"
                   f"季漲 ≥ 25% {num(r['q_up25'], '{:.0f}')}／季跌 ≥ 25% {num(r['q_dn25'], '{:.0f}')}</p>")
        if r["up4"] >= 300:
            warn.append(f"廣度突破日：漲 ≥ 4% 有 {int(r['up4'])} 家")
        if r["dn4"] >= 300:
            warn.append(f"廣度崩跌日：跌 ≥ 4% 有 {int(r['dn4'])} 家")
    p = os.path.join(DATA, "utility_note.txt")
    if os.path.exists(p):
        out.append(f"<p class='muted'>{e(open(p, encoding='utf-8').read().strip())}</p>")
    if warn:
        out.append("<p class='warn'>⚠ " + "；".join(e(x) for x in warn) + "</p>")
    return "\n".join(out), t["日期"]


# ---------- 2. 板塊 ----------

def leaders_map(stocks, members):
    s = stocks.set_index("代號")
    out = {}
    for key, codes in members.items():
        x = s.reindex([c for c in codes if c in s.index]).dropna(subset=["RS評分"])
        x = x.sort_values("RS評分", ascending=False).head(3)
        out[key] = "、".join(f"{c}({int(r)})" for c, r in zip(x.index, x["RS評分"]))
    return out


def sectors(gs, stocks, members):
    out = []
    days = sorted(gs["日期"].unique())
    today, prev4 = days[-1], days[-5:-1]
    T = gs[gs["日期"] == today].copy()
    P = gs[gs["日期"].isin(prev4)]
    was_main = set(map(tuple, P.loc[P["主流"], ["類型", "板塊"]].values))
    was_main_yday = set(map(tuple, gs[(gs["日期"] == days[-2]) & gs["主流"]][["類型", "板塊"]].values)) if len(days) > 1 else set()
    lead = leaders_map(stocks, members)

    main = T[T["主流"]].sort_values(["類型", "RS排名"])
    out.append("<h3>主流（創高 ≥ 3 檔且收紅＋成交額比 ≥ 1.3）</h3>")
    if main.empty:
        out.append("<p>今天沒有主流板塊。</p>")
    else:
        d = pd.DataFrame({
            "類型": main["類型"], "板塊": [e(x) + (" 🆕" if (k, x) not in was_main_yday else "") for k, x in zip(main["類型"], main["板塊"])],
            "家數": main["家數"], "創高": main["創高"], "成交額比": main["成交額比"].map("{:.2f}".format),
            "5日漲幅%": main["5日漲幅%"].map("{:+.1f}".format),
            "RS 排名": [f"{int(r)}／{n}" for r, n in zip(main["RS排名"], main["類型"].map(T.groupby("類型").size()))],
            "龍頭（RS）": [e(lead.get((k, x), "")) for k, x in zip(main["類型"], main["板塊"])],
        })
        out.append(table(d))

    # 階段變化
    chg = []
    for _, r in T.iterrows():
        k = (r["類型"], r["板塊"])
        if k in was_main and not r["主流"]:
            stage = "尾聲" if (r["價格訊號"] or r["資金訊號"]) else "退潮"
            chg.append((stage, r))
    brew = T[T["醞釀"] & (T["類型"] == "主題")].sort_values("成交額比", ascending=False)
    start = T[T["啟動"]].sort_values("創高", ascending=False)
    out.append("<h3>階段變化</h3><ul>")
    if len(brew):
        out.append("<li><b>醞釀（主題）</b>：" + "、".join(
            f"{e(x)}（成交額比 {v:.2f}、5 日 {p:+.1f}%）" for x, v, p in zip(brew["板塊"], brew["成交額比"], brew["5日漲幅%"])) + "</li>")
    if len(start):
        out.append("<li><b>啟動</b>（價格先突破、成交額還沒放大）：" + "、".join(
            f"{e(x)}（{k}，創高 {n}）" for x, k, n in zip(start["板塊"].head(10), start["類型"].head(10), start["創高"].head(10)))
            + ("…" if len(start) > 10 else "") + "</li>")
    for stage in ("尾聲", "退潮"):
        xs = sorted([r for s_, r in chg if s_ == stage], key=lambda r: (r["類型"] != "主題", r["RS排名"]))
        if xs:
            out.append(f"<li><b>{stage}</b>：" + "、".join(f"{e(r['板塊'])}（{r['類型']}）" for r in xs[:10])
                       + (f"…等 {len(xs)} 個" if len(xs) > 10 else "") + "</li>")
    if not (len(brew) or len(start) or chg):
        out.append("<li>沒有變化</li>")
    out.append("</ul><p class='muted'>醞釀時成交額放大也可能是賣壓，請搭配 5 日漲幅看；回測裡醞釀只在主題板塊有優勢。</p>")

    # 產業排名（J Law：和 1 週、3 個月、6 個月前比）
    ind = T[T["類型"] == "產業"].sort_values("RS排名")
    n_ind = len(ind)

    def ch(x):
        return "—" if pd.isna(x) else f"{int(x):+d}"
    out.append(f"<h3>產業 RS 排名前 10（共 {n_ind} 個產業；括號＝名次進步）</h3>")
    top = ind.head(10)
    out.append(table(pd.DataFrame({
        "排名": top["RS排名"].astype(int), "產業": top["板塊"].map(e), "RS 中位數": top["RS中位數"],
        "1週": top["排名1週進步"].map(ch), "3個月": top["排名3月進步"].map(ch), "6個月": top["排名6月進步"].map(ch),
        "創高": top["創高"], "龍頭（RS）": [e(lead.get(("產業", x), "")) for x in top["板塊"]],
    })))
    up = ind[ind["RS百分位"] >= 0.5].sort_values("排名3月進步", ascending=False).head(5)
    if len(up):
        out.append("<p><b>3 個月進步最快</b>（目前在前半段）：" + "、".join(
            f"{e(x)}（第 {int(r)}，進步 {int(c)}）" for x, r, c in zip(up["板塊"], up["RS排名"], up["排名3月進步"]) if pd.notna(c)) + "</p>")
    th = T[T["類型"] == "主題"].sort_values("RS排名").head(8)
    out.append("<p><b>主題 RS 前 8</b>：" + "、".join(
        f"{e(x)}（{v:.0f}{'，主流' if m else ''}）" for x, v, m in zip(th["板塊"], th["RS中位數"], th["主流"])) + "</p>")
    return "\n".join(out), T


def etfs():
    d = rd("etf_strength.csv")
    if d is None or d.empty:
        return "<p class='muted'>（沒有 ETF 資料）</p>"
    x = d[d["群組"].isin(["類股", "產業", "主題"])]
    top = x.sort_values("RS(清單內)", ascending=False).head(6)
    hot = x[x["RSI14"] >= 70]
    cold = x[x["RSI14"] <= 30]
    s = "<p><b>最強 ETF</b>：" + "、".join(
        f"{e(c)} {e(n)}（RS {r:.0f}、RSI {rsi:.0f}、1 月 {m:+.1f}%）" for c, n, r, rsi, m in
        zip(top["代號"], top["名稱"], top["RS(清單內)"], top["RSI14"], top["1月%"])) + "</p>"
    if len(hot):
        s += "<p>RSI ≥ 70（偏熱）：" + "、".join(f"{e(c)} {e(n)} {v:.0f}" for c, n, v in zip(hot["代號"], hot["名稱"], hot["RSI14"])) + "</p>"
    if len(cold):
        s += "<p>RSI ≤ 30（偏冷）：" + "、".join(f"{e(c)} {e(n)} {v:.0f}" for c, n, v in zip(cold["代號"], cold["名稱"], cold["RSI14"])) + "</p>"
    return s


# ---------- 3. 候選股 ----------

def candidates(stocks, T, members, data_day):
    from fundamentals_us import update_earnings, update_fundamentals
    s = stocks.copy()
    s = s[s["第②階段"].astype(str).str.lower().eq("true") & (s["RS評分"] >= 85) & (s["收盤"] >= 12)
          & (s["50日均成交額(百萬美元)"] >= 20)]
    main_keys = set(map(tuple, T.loc[T["主流"], ["類型", "板塊"]].values))
    top_ind = set(T[(T["類型"] == "產業") & (T["RS百分位"] >= 0.8)]["板塊"])
    in_main, why = {}, {}
    for (k, g), codes in members.items():
        for c in codes:
            if (k, g) in main_keys:
                in_main.setdefault(c, []).append(g)
    st = pd.read_csv(os.path.join(DATA, "stocks.csv"), dtype=str, keep_default_na=False).set_index("code")
    ind = st["industry"]
    ipo = pd.to_numeric(st.get("ipo_year", pd.Series(dtype=str)), errors="coerce")
    for c in s["代號"]:
        if c in in_main:
            why[c] = "★ " + "、".join(in_main[c])
        elif ind.get(c, "") in top_ind:
            why[c] = ind.get(c, "")
    s = s[s["代號"].isin(why)].copy()
    total = len(s)
    if s.empty:
        return "<p>今天沒有符合條件的候選股。</p>", 0
    try:
        fund = update_fundamentals(list(s["代號"]))
    except Exception as ex:
        notes.append(f"基本面（SEC）更新失敗：{ex}")
        fund = {}
    try:
        earn = update_earnings()
    except Exception as ex:
        notes.append(f"財報日曆更新失敗：{ex}")
        earn = {}
    if not any((fund.get(c, {}).get("flags") or fund.get(c, {}).get("eps_yoy")) for c in s["代號"]):
        notes.append("基本面加分項沒有資料（SEC 連線失敗或被拒；可在 repo Secrets 設 SEC_USER_AGENT＝「名字 email」）")
    if not earn:
        notes.append("財報日曆沒有資料（Nasdaq 連線失敗），財報欄空白")
    yr = date.today().year

    first = s.set_index("代號")["首個交易日"] if "首個交易日" in s else pd.Series(dtype=str)
    hist_start = stocks["首個交易日"].min() if "首個交易日" in stocks else ""

    def flags(c):
        f = [x for x in (fund.get(c, {}).get("flags") or "").split("、") if x]
        y = ipo.get(c, np.nan)
        # Nasdaq 的 IPO 年遇到轉板會被改成轉板年（例如 Etsy 2025）：
        # 我們的資料第一天就有成交、但 IPO 年比那天還晚 → 不算 IPO
        fd = str(first.get(c, "") or "")
        bogus = fd and fd == hist_start and pd.notna(y) and int(y) > int(fd[:4])
        if pd.notna(y) and y >= yr - 3 and not bogus:
            f.append("強勢IPO")
        return f
    s["板塊"] = s["代號"].map(why)
    s["主流"] = s["代號"].isin(in_main)
    s["加分項"] = s["代號"].map(flags)
    s["加分數"] = s["加分項"].map(len)
    s = s.sort_values(["主流", "加分數", "RS評分"], ascending=False)

    d0 = datetime.strptime(data_day, "%Y-%m-%d").date()

    def er(c):
        if c not in earn:
            return ""
        d, t = earn[c]
        n = (date.fromisoformat(d) - d0).days
        if n <= 0:  # 資料日當天或之前公布的，已經反映在股價裡
            return ""
        txt = f"{d[5:]}{t}"
        return f"<span class='warn'>⚠ {txt}</span>" if n <= 7 else txt

    def ext(x):
        return f"<span class='warn'>{x:+.0f}% 勿追</span>" if x >= EXT_WARN else f"{x:+.0f}%"
    top = s.head(MAX_CAND)
    d = pd.DataFrame({
        "代號": top["代號"].map(e), "名稱": top["名稱"].map(lambda x: e(str(x)[:22])),
        "板塊（★＝主流）": top["板塊"].map(lambda x: e(str(x)[:34])), "RS": top["RS評分"].astype(int),
        "距高點": top["距52週高點%"].map("{:+.0f}%".format), "距50日線": top["距50日線%"].map(ext),
        "財報": top["代號"].map(er),
        "加分項": top["加分項"].map(lambda f: e("、".join(f))),
        "EPS年增%": top["代號"].map(lambda c: e((fund.get(c, {}).get("eps_yoy") or "").replace("|", "／"))),
    })
    html_ = table(d, "cand")
    if total > MAX_CAND:
        html_ += f"<p class='muted'>共 {total} 檔符合，只列前 {MAX_CAND} 檔；完整名單見 RS_美股.xlsx → 必要條件候選。</p>"
    return html_, total


def utility():
    u = rd("utility_today.csv")
    if u is None or u.empty or "分級" not in u:
        return ""
    a = u[u["分級"] == "A"].head(8)
    if a.empty:
        return ""
    cols = [c for c in ["代號", "名稱", "自高點RS", "修正期RS", "自低點RS", "距20日線%", "備註"] if c in a.columns]
    a = a[cols].copy()
    for c in ("自高點RS", "修正期RS", "自低點RS"):
        if c in a:
            a[c] = a[c].map(lambda v: "" if pd.isna(v) else int(v))
    if "距20日線%" in a:
        a["距20日線%"] = a["距20日線%"].map(lambda v: "" if pd.isna(v) else f"{v:+.0f}%")
    a = a.fillna("").astype(str).map(e)
    return (f"<h2>大盤修正中：Utility Screen A 級（共 {int((u['分級'] == 'A').sum())} 檔）</h2>"
            "<p class='muted'>修正時撐得住又最先反彈；離 20 日線太遠的等整理再看。</p>" + table(a))


CSS = """
@page { size: A4; margin: 12mm 11mm; @bottom-right { content: counter(page) " / " counter(pages); font-size: 8pt; color: #888; } }
body { font-family: 'Noto Sans CJK TC', 'Noto Sans CJK JP', 'Noto Sans CJK SC', sans-serif; font-size: 8.6pt; color: #1a1a1a; line-height: 1.35; }
h1 { font-size: 15pt; margin: 0 0 2px; color: #1F3864; }
h2 { font-size: 11pt; margin: 10px 0 4px; padding-bottom: 2px; border-bottom: 1.5px solid #1F3864; color: #1F3864; }
h3 { font-size: 9.5pt; margin: 7px 0 3px; color: #333; }
p { margin: 2px 0 4px; } ul { margin: 2px 0 4px 16px; padding: 0; } li { margin: 1px 0; }
.big { font-size: 9.6pt; } .muted { color: #777; font-size: 7.8pt; } .warn { color: #C00000; font-weight: bold; }
table { border-collapse: collapse; width: 100%; margin: 3px 0 6px; font-size: 7.9pt; }
table.narrow { width: auto; }
th { background: #1F3864; color: #fff; padding: 2px 4px; text-align: left; font-weight: bold; }
td { padding: 2px 4px; border-bottom: 0.5px solid #ddd; vertical-align: top; }
tr:nth-child(even) td { background: #F5F7FA; }
.cand td:nth-child(3) { max-width: 50mm; }
.cand td:nth-child(5), .cand td:nth-child(6), .cand td:nth-child(7), .cand td:nth-child(8), .cand th { white-space: nowrap; }
.sub { color: #666; font-size: 8pt; margin-bottom: 6px; }
"""


def main():
    stocks = rd("stocks_today.csv")
    gs = rd("group_signals.csv")
    if stocks is None or gs is None:
        print("缺少 stocks_today.csv 或 group_signals.csv，請先跑 rs_us.py")
        return 1
    members = {}
    st = pd.read_csv(os.path.join(DATA, "stocks.csv"), dtype=str, keep_default_na=False)
    for c, g in zip(st["code"], st["industry"]):
        if g:
            members.setdefault(("產業", g), []).append(c)
    for f in ("sectors_us.csv", "sectors_us_補充.csv"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            t = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
            for c, g in zip(t["代號"], t["板塊"]):
                members.setdefault(("主題", g.strip()), []).append(c.strip())

    mk_html, day = market()
    sec_html, T = sectors(gs, stocks, members)
    data_day = day or gs["日期"].max()
    if stocks["資料日期"].iloc[0] != data_day:
        notes.append(f"個股資料日期 {stocks['資料日期'].iloc[0]} 和大盤 {data_day} 不一致")
    cand_html, n_cand = candidates(stocks, T, members, data_day)

    body = f"""
<h1>美股每日報告　{data_day}</h1>
<div class='sub'>美東收盤資料｜產生於 {datetime.now():%Y-%m-%d %H:%M} UTC｜選股流程：大盤 → 板塊 → 個股</div>
<h2>1. 大盤：能不能出手</h2>
{mk_html}
<h2>2. 板塊：錢往哪裡去</h2>
{sec_html}
<h3>ETF</h3>
{etfs()}
<h2>3. 候選股（{n_cand} 檔）</h2>
<p class='muted'>第②階段＋RS ≥ 85＋股價 ≥ 12 美元＋50 日均成交額 ≥ 2,000 萬美元，且在主流板塊（★）或 RS 前 20% 的產業；
主流優先，再依加分項數、RS 排序。財報 7 天內標 ⚠；距 50 日線 ≥ {EXT_WARN}% 標勿追。</p>
{cand_html}
{utility()}
<h2>注意</h2>
<ul>{''.join(f'<li>{e(n)}</li>' for n in notes) or '<li>無</li>'}
<li class='muted'>規則依美股回測（2014–2026，含已下市股票）：主流板塊之後 20 日勝過大盤 60%（產業）／67%（主題），基準 55%／63%。
報告只負責篩選，不是買賣建議；進場、停損、部位依自己的交易規則。</li></ul>
"""
    doc = f"<!DOCTYPE html><html lang='zh-Hant'><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"
    os.makedirs(ARCHIVE, exist_ok=True)
    from weasyprint import HTML
    HTML(string=doc).write_pdf(OUT_PDF)
    arch = os.path.join(ARCHIVE, f"{data_day}.pdf")
    with open(OUT_PDF, "rb") as src, open(arch, "wb") as dst:
        dst.write(src.read())
    with open(os.path.join(OUT_DIR, "美股每日報告.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已輸出 {OUT_PDF}、{arch}（候選股 {n_cand} 檔）")
    for n in notes:
        print("注意：", n)


if __name__ == "__main__":
    sys.exit(main())
