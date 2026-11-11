"""讀取 data/daily/*.csv.gz，計算美股市場廣度（仿 Stockbee Market Monitor），輸出 Market_Monitor_美股.xlsx
台股 tw-market-monitor 的移植：指標與上色規則相同，門檻改用 Stockbee 美股原版（見 mm_config.py）"""
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import mm_config as config

ROOT = Path(__file__).parent
DAILY = ROOT / "data" / "daily"
OUT = ROOT / "Market_Monitor_美股.xlsx"
HIST = ROOT / "data" / "market_monitor.csv"


# ---------------- 讀資料 ----------------
def load_raw():
    frames = []
    for f in sorted(DAILY.glob("*.csv.gz")):
        d = pd.read_csv(f, usecols=["code", "close", "volume", "ret"], keep_default_na=False, na_values=[""])
        d["date"] = pd.to_datetime(f.name[:10])
        frames.append(d)
    if not frames:
        raise SystemExit("data/daily 裡沒有資料，請先執行 fetch_us.py")
    return pd.concat(frames, ignore_index=True)


# ---------------- 計算指標 ----------------
def compute(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    close = df.pivot_table(index="date", columns="code", values="close")
    ret = df.pivot_table(index="date", columns="code", values="ret").reindex_like(close)
    vol = df.pivot_table(index="date", columns="code", values="volume").reindex_like(close).fillna(0)
    idx = pd.read_csv(ROOT / "data" / "index.csv", parse_dates=["date"]).set_index("date")
    taiex = idx["spx"].reindex(close.index)

    # 每日檔的 ret 已含分割與配息，連乘成還原價
    traded = close.notna()
    adj = (1 + ret.fillna(0)).cumprod().where(traded)
    # 第一次出現前沒有資料；中間停牌日沿用前值
    adj = adj.ffill().where(traded.cummax())

    price_ok = close >= config.MIN_PRICE
    vol_prev = vol.shift(1)
    liquid_day = (vol >= config.MIN_VOLUME_SHARES) & (vol > vol_prev)
    avg_vol20 = vol.rolling(20, min_periods=20).mean()
    liquid_avg = avg_vol20 >= config.MIN_AVG_VOLUME_SHARES

    universe = traded & price_ok
    m = pd.DataFrame(index=close.index)
    m["universe"] = universe.sum(axis=1)

    up4 = universe & liquid_day & (ret >= config.DAILY_MOVE)
    dn4 = universe & liquid_day & (ret <= -config.DAILY_MOVE)
    m["up4"] = up4.sum(axis=1)
    m["dn4"] = dn4.sum(axis=1)
    for n in (5, 10):
        u = m["up4"].rolling(n, min_periods=n).sum()
        d = m["dn4"].rolling(n, min_periods=n).sum()
        m[f"ratio{n}"] = (u / d.replace(0, np.nan)).round(2)

    base = universe & liquid_avg

    def window(n, th, name_up, name_dn):
        r = adj / adj.shift(n) - 1
        ok = base & r.notna()
        m[name_up] = (ok & (r >= th)).sum(axis=1)
        m[name_dn] = (ok & (r <= -th)).sum(axis=1)
        enough = pd.Series(np.arange(len(m)) >= n, index=m.index)
        m.loc[~enough, [name_up, name_dn]] = np.nan

    window(config.QUARTER_DAYS, 0.25, "q_up25", "q_dn25")
    window(config.MONTH_DAYS, 0.25, "m_up25", "m_dn25")
    window(config.MONTH_DAYS, 0.50, "m_up50", "m_dn50")
    window(config.DAYS_34, 0.13, "d34_up13", "d34_dn13")

    ma = adj.rolling(config.MA_DAYS, min_periods=config.MA_DAYS).mean()
    has_ma = universe & ma.notna()
    above = (has_ma & (adj > ma)).sum(axis=1)
    m["t2108"] = (above / has_ma.sum(axis=1).replace(0, np.nan) * 100).round(1)

    m["taiex"] = taiex
    m["taiex_chg"] = (taiex.pct_change() * 100).round(2)
    m["state"] = classify(m)
    m.to_csv(HIST, encoding="utf-8-sig", index_label="date")
    return m


def classify(m: pd.DataFrame) -> pd.Series:
    """市場狀態分類（依 Stockbee 對極端廣度的解讀）"""
    c = config
    t, r5, r10 = m["t2108"], m["ratio5"], m["ratio10"]
    state = pd.Series("中性", index=m.index)
    state[r10 < 1] = "偏空"
    state[r10 >= c.RATIO_STRONG] = "偏多"
    state[(r5 >= c.OVERHEAT_RATIO5) | (t >= c.T2108_HIGH)] = "短線過熱"
    state[(t <= c.T2108_LOW) | (r10 <= c.RATIO_WEAK)] = "極度超賣"
    state[r10.isna()] = ""
    return state


# ---------------- 輸出 Excel ----------------
COLS = [
    # key, 表頭, 群組, 寬度, 格式
    ("date", "日期", "", 12, "yyyy-mm-dd"),
    ("state", "市場狀態", "", 11, "@"),
    ("up4", "今日\n漲≥4%", "主要廣度指標", 9, "0"),
    ("dn4", "今日\n跌≥4%", "主要廣度指標", 9, "0"),
    ("ratio5", "5日比", "主要廣度指標", 8, "0.00"),
    ("ratio10", "10日比", "主要廣度指標", 8, "0.00"),
    ("q_up25", "季漲\n≥25%", "主要廣度指標", 9, "0"),
    ("q_dn25", "季跌\n≥25%", "主要廣度指標", 9, "0"),
    ("t2108", "站上40MA\n比例%", "主要廣度指標", 10, "0.0"),
    ("m_up25", "月漲\n≥25%", "次要指標", 9, "0"),
    ("m_dn25", "月跌\n≥25%", "次要指標", 9, "0"),
    ("m_up50", "月漲\n≥50%", "次要指標", 9, "0"),
    ("m_dn50", "月跌\n≥50%", "次要指標", 9, "0"),
    ("d34_up13", "34日漲\n≥13%", "次要指標", 9, "0"),
    ("d34_dn13", "34日跌\n≥13%", "次要指標", 9, "0"),
    ("universe", "納入\n家數", "大盤", 9, "0"),
    ("taiex", "S&P 500", "大盤", 11, "#,##0.00"),
    ("taiex_chg", "漲跌%", "大盤", 8, "0.00"),
]


def fill(hex_):
    return PatternFill("solid", fgColor=hex_)


# 色票（深→淺）
DARK_GREEN, GREEN, LIGHT_GREEN = fill("00A651"), fill("8ED973"), fill("D9F2D0")
DARK_RED, PINK, LIGHT_PINK = fill("E0301E"), fill("F4A6A6"), fill("FADADD")
ORANGE, GREY = fill("F4B183"), fill("E7E6E6")
WHITE_BOLD, BOLD = Font(color="FFFFFF", bold=True), Font(bold=True)

STATE_STYLE = {
    "極度超賣": (DARK_RED, WHITE_BOLD),
    "偏空": (PINK, BOLD),
    "中性": (GREY, BOLD),
    "偏多": (GREEN, BOLD),
    "短線過熱": (ORANGE, BOLD),
}
GROUP_FILL = {"主要廣度指標": "1F4E78", "次要指標": "385723", "大盤": "595959", "": "404040"}


def write_excel(m: pd.DataFrame):
    m = m.dropna(subset=["ratio10"]) if m["ratio10"].notna().any() else m
    m = m.sort_index(ascending=False).head(config.OUTPUT_ROWS).reset_index()
    m = m.rename(columns={"index": "date"})

    wb = Workbook()
    ws = wb.active
    ws.title = "Market Monitor"
    col = {k: get_column_letter(i + 1) for i, (k, *_r) in enumerate(COLS)}
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # 第 1 列：群組；第 2 列：欄名
    i = 0
    while i < len(COLS):
        g = COLS[i][2]
        j = i
        while j + 1 < len(COLS) and COLS[j + 1][2] == g:
            j += 1
        if j > i:
            ws.merge_cells(start_row=1, start_column=i + 1, end_row=1, end_column=j + 1)
        ws.cell(1, i + 1, g)
        for k in range(i, j + 1):
            cc = ws.cell(1, k + 1)
            cc.fill = fill(GROUP_FILL[g])
            cc.font = Font(bold=True, color="FFFFFF")
            cc.alignment = center
        i = j + 1
    for k, (key, title, g, width, fmt) in enumerate(COLS, start=1):
        c = ws.cell(2, k, title)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill(GROUP_FILL[g])
        c.alignment = center
        c.border = border
        ws.column_dimensions[get_column_letter(k)].width = width
    ws.row_dimensions[2].height = 32

    for r, row in enumerate(m.itertuples(index=False), start=3):
        rec = row._asdict()
        for k, (key, _t, _g, _w, fmt) in enumerate(COLS, start=1):
            v = rec[key]
            if key == "date":
                v = v.to_pydatetime().date()
            elif key == "state":
                v = v or None
            elif pd.isna(v):
                v = None
            else:
                v = float(v) if fmt.startswith(("0.", "#")) else int(v)
            c = ws.cell(r, k, v)
            c.number_format = fmt
            c.alignment = Alignment(horizontal="center")
            c.border = border

    last = 2 + len(m)
    if last >= 3:
        add_colors(ws, col, m)

    ws.freeze_panes = "C3"
    ws.print_title_rows = "1:2"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    write_notes(wb)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"已輸出 {OUT}（{len(m)} 個交易日）")


def add_colors(ws, col, m):
    """直接把顏色塗進每一格（不用條件式格式，手機預覽也看得到）"""
    c = config
    idx = {k: i + 1 for i, (k, *_r) in enumerate(COLS)}

    def num(v):
        return None if v is None or pd.isna(v) else float(v)

    def paint(r, key, f_=None, fo=None):
        cell = ws.cell(r, idx[key])
        if f_ is not None:
            cell.fill = f_
        if fo is not None:
            cell.font = fo

    for r, row in enumerate(m.itertuples(index=False), start=3):
        x = row._asdict()
        up, dn = num(x["up4"]) or 0, num(x["dn4"]) or 0
        big = c.BIG_DAY_COUNT
        # 1) 日期與 4% 欄：上漲日／下跌日，≥ 門檻用深色
        if up > dn:
            if up >= big:
                paint(r, "date", DARK_GREEN, WHITE_BOLD); paint(r, "up4", DARK_GREEN, WHITE_BOLD)
            else:
                paint(r, "date", LIGHT_GREEN); paint(r, "up4", GREEN)
        elif dn > up:
            if dn >= big:
                paint(r, "date", DARK_RED, WHITE_BOLD); paint(r, "dn4", DARK_RED, WHITE_BOLD)
            else:
                paint(r, "date", LIGHT_PINK); paint(r, "dn4", PINK)
        # 2) 5/10 日比
        for k in ("ratio5", "ratio10"):
            v = num(x[k])
            if v is None:
                continue
            if v >= c.RATIO_STRONG:
                paint(r, k, GREEN)
            elif v >= 1:
                paint(r, k, LIGHT_GREEN)
            elif v <= c.RATIO_WEAK:
                paint(r, k, DARK_RED, WHITE_BOLD)
            else:
                paint(r, k, LIGHT_PINK)
        # 3) 區間漲跌家數
        pairs = [("q_up25", "q_dn25", GREEN, PINK), ("m_up25", "m_dn25", LIGHT_GREEN, LIGHT_PINK),
                 ("m_up50", "m_dn50", LIGHT_GREEN, LIGHT_PINK), ("d34_up13", "d34_dn13", LIGHT_GREEN, LIGHT_PINK)]
        for u, d, fu, fd in pairs:
            a_, b_ = num(x[u]), num(x[d])
            if a_ is None or b_ is None:
                continue
            if a_ > b_:
                paint(r, u, fu)
            elif b_ > a_:
                paint(r, d, fd)
        # 4) 站上 40MA 比例
        t = num(x["t2108"])
        if t is not None:
            if t <= c.T2108_LOW:
                paint(r, "t2108", DARK_RED, WHITE_BOLD)
            elif t < c.T2108_WEAK:
                paint(r, "t2108", PINK)
            elif t >= c.T2108_HIGH:
                paint(r, "t2108", ORANGE)
            elif t >= c.T2108_STRONG:
                paint(r, "t2108", GREEN)
        # 5) 市場狀態
        if x["state"] in STATE_STYLE:
            paint(r, "state", *STATE_STYLE[x["state"]])
        # 6) 加權指數漲跌（紅漲綠跌）
        v = num(x["taiex_chg"])
        if v is not None and v != 0:
            paint(r, "taiex_chg", fo=Font(color="C00000" if v > 0 else "008000", bold=True))


def write_notes(wb):
    ws = wb.create_sheet("說明")
    c = config
    lines = [
        ("美股 Market Monitor（仿 Stockbee MM）", None, ""),
        ("", None, ""),
        ("欄位", None, "定義"),
        ("市場狀態", None, "依 10 日比、5 日比、站上 40MA 比例自動分類，見下方「市場狀態」"),
        ("今日漲≥4%", None, f"當日漲幅 ≥ {c.DAILY_MOVE:.0%}，且成交量 ≥ {c.MIN_VOLUME_SHARES:,} 股、量大於前一日"),
        ("今日跌≥4%", None, f"當日跌幅 ≥ {c.DAILY_MOVE:.0%}，量的條件同上"),
        ("5日比 / 10日比", None, "近 5／10 日「漲≥4% 家數總和 ÷ 跌≥4% 家數總和」"),
        ("季漲/跌 ≥25%", None, f"近 {c.QUARTER_DAYS} 個交易日（還原價）漲／跌 ≥ 25% 的家數"),
        ("站上40MA比例%", None, f"收盤（還原價）在 {c.MA_DAYS} 日均線之上的股票比例，即 Stockbee 的 T2108"),
        ("月漲/跌 ≥25%、≥50%", None, f"近 {c.MONTH_DAYS} 個交易日漲／跌 ≥ 25%、≥ 50% 的家數"),
        ("34日漲/跌 ≥13%", None, f"近 {c.DAYS_34} 個交易日漲／跌 ≥ 13% 的家數"),
        ("納入家數", None, f"NYSE、Nasdaq、AMEX 普通股（不含 ETF、特別股、權證、SPAC），收盤價 ≥ {c.MIN_PRICE:g} 美元；多日指標另要求 20 日均量 ≥ {c.MIN_AVG_VOLUME_SHARES:,} 股"),
        ("還原價", None, "每日報酬已含分割與配息，再連乘；分割不會被誤判成大跌"),
        ("", None, ""),
        ("市場狀態", None, "判斷順序（先符合者優先）"),
        ("極度超賣", "極度超賣", f"站上40MA ≤ {c.T2108_LOW}% 或 10日比 ≤ {c.RATIO_WEAK:g}。Stockbee：極度悲觀的廣度＝反彈或底部的開始"),
        ("短線過熱", "短線過熱", f"5日比 ≥ {c.OVERHEAT_RATIO5:g} 或 站上40MA ≥ {c.T2108_HIGH}%。Stockbee：短線極度樂觀之後常回檔"),
        ("偏多", "偏多", f"10日比 ≥ {c.RATIO_STRONG:g}"),
        ("偏空", "偏空", "10日比 < 1"),
        ("中性", "中性", "其他"),
        ("", None, ""),
        ("顏色", None, "意義"),
        ("日期／漲4%：深綠", DARK_GREEN, f"大漲日：漲4% 家數多於跌4%，且 ≥ {c.BIG_DAY_COUNT} 家"),
        ("日期／漲4%：綠", GREEN, f"一般上漲日：漲4% 家數多於跌4%，未達 {c.BIG_DAY_COUNT} 家（日期欄用淺綠）"),
        ("日期／跌4%：深紅", DARK_RED, f"大跌日：跌4% 家數多於漲4%，且 ≥ {c.BIG_DAY_COUNT} 家"),
        ("日期／跌4%：粉紅", PINK, f"一般下跌日：未達 {c.BIG_DAY_COUNT} 家（Stockbee 的粉紅）（日期欄用淺粉紅）"),
        ("5/10日比：綠", GREEN, f"≥ {c.RATIO_STRONG:g}，多方明顯佔優"),
        ("5/10日比：淺綠", LIGHT_GREEN, f"1 ～ {c.RATIO_STRONG:g}"),
        ("5/10日比：淺粉紅", LIGHT_PINK, f"{c.RATIO_WEAK:g} ～ 1"),
        ("5/10日比：深紅", DARK_RED, f"≤ {c.RATIO_WEAK:g}，空方極端（常接近短線底部）"),
        ("區間漲跌欄", GREEN, "漲的家數多於跌的家數 → 漲那欄綠；反之跌那欄紅。季 25% 用較深的顏色，其餘次要指標用淺色"),
        ("站上40MA：深紅", DARK_RED, f"≤ {c.T2108_LOW}%：極度超賣"),
        ("站上40MA：粉紅", PINK, f"{c.T2108_LOW}–{c.T2108_WEAK}%：偏弱"),
        ("站上40MA：綠", GREEN, f"{c.T2108_STRONG}–{c.T2108_HIGH}%：偏強"),
        ("站上40MA：橘", ORANGE, f"≥ {c.T2108_HIGH}%：極度過熱"),
        ("S&P 500 漲跌%", None, "紅字上漲、綠字下跌（沿用台股習慣）"),
        ("", None, ""),
        ("門檻調整", None, "修改 repo 裡的 mm_config.py，下次自動更新就會套用"),
    ]
    for r, (a, style, b) in enumerate(lines, start=1):
        ca, cb = ws.cell(r, 1, a), ws.cell(r, 2, b)
        if isinstance(style, str):
            f_, fo = STATE_STYLE[style]
            ca.fill, ca.font = f_, fo
        elif style is not None:
            ca.fill = style
            ca.font = WHITE_BOLD if style in (DARK_GREEN, DARK_RED) else BOLD
        elif r == 1 or a in ("欄位", "顏色") or (a == "市場狀態" and b.startswith("判斷")):
            ca.font = Font(bold=True, size=13 if r == 1 else 11)
            cb.font = Font(bold=True)
        elif a:
            ca.font = BOLD
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90


if __name__ == "__main__":
    write_excel(compute(load_raw()))
