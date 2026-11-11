"""
RSI 熱圖（格式同台股 板塊RSI_熱圖.xlsx／tw-sector-rsi 的 RSI_板塊表.xlsx）

版面：
  上方 RSI Chg：1-Day / 5-Day / 20-Day 變化（Excel 公式，引用下方表格）
  下方：Date + 各欄 RSI(14)，最新日期在最上面
顏色：
  底色 = 與前一交易日相比的 RSI 變化（綠＝上升、紅＝下降，越深變化越大，±6 最深）
  RSI ≥ 70 藍色粗體、RSI ≤ 30 紫色粗體

用法：
  write_heatmap(ws, data, columns, note)   data = {日期: {欄名: RSI}}
  python heatmap.py   → 讀 data/sector_history.csv，輸出 板塊RSI_熱圖_美股.xlsx（自訂主題板塊）
"""

import csv
import os

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST_CSV = os.path.join(ROOT, "data", "sector_history.csv")
OUT = os.path.join(ROOT, "板塊RSI_熱圖_美股.xlsx")
SECTOR_FILES = ["sectors_us.csv", "sectors_us_補充.csv"]
MAX_ROWS = int(os.getenv("TABLE_DAYS", "60"))
SATURATE = 6.0

FONT = "Arial"
thin = Side(style="thin", color="BFBFBF")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
HEADER_FILL = PatternFill("solid", fgColor="1F3864")


def change_fill(delta):
    if delta is None or abs(delta) < 0.05:
        return None
    t = min(abs(delta) / SATURATE, 1.0)
    if delta > 0:
        r, g, b = 255 - t * (255 - 99), 255 - t * (255 - 190), 255 - t * (255 - 123)
    else:
        r, g, b = 255 - t * (255 - 230), 255 - t * (255 - 124), 255 - t * (255 - 115)
    return PatternFill("solid", fgColor=f"{int(r):02X}{int(g):02X}{int(b):02X}")


def sector_order():
    order = []
    for name in SECTOR_FILES:
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    s = r["板塊"].strip()
                    if s not in order:
                        order.append(s)
    return [s for s in order if "・其他" not in s] + [s for s in order if "・其他" in s]


def write_heatmap(ws, data, columns, note, tag="us-stock-tracker"):
    """data = {日期: {欄名: RSI}}；columns = 欄位順序。回傳畫了幾天。"""
    all_dates = sorted(data, reverse=True)
    dates = all_dates[: MAX_ROWS + 21]
    sectors = list(columns)
    shown = min(len(dates), MAX_ROWS)
    ncol = len(sectors) + 1
    first = 7

    ws.cell(1, 1, "RSI Chg")
    for j, s in enumerate(sectors, start=2):
        ws.cell(1, j, s)
    for i, (label, n) in enumerate([("1-Day", 1), ("5-Day", 5), ("20-Day", 20)], start=2):
        ws.cell(i, 1, label)
        for j in range(2, ncol + 1):
            col = get_column_letter(j)
            c = ws.cell(i, j)
            if n < len(dates):
                c.value = f'=IFERROR({col}{first}-{col}{first + n},"")'
                a, b = data[dates[0]].get(sectors[j - 2]), data[dates[n]].get(sectors[j - 2])
                if a is not None and b is not None:
                    fill = change_fill(a - b)
                    if fill:
                        c.fill = fill
            c.number_format = '+0.0;-0.0;0.0'

    ws.cell(5, 1, "Date")
    for j, s in enumerate(sectors, start=2):
        ws.cell(5, j, s)
    ws.cell(6, 1, "RSI(14)")
    rows = min(len(dates), max(shown, 21))
    for k in range(rows):
        d = dates[k]
        row = first + k
        ws.cell(row, 1, d)
        prev = data[dates[k + 1]] if k + 1 < len(dates) else {}
        for j, s in enumerate(sectors, start=2):
            v = data[d].get(s)
            c = ws.cell(row, j, v)
            c.number_format = "0.0"
            if v is None:
                continue
            fill = change_fill(v - prev[s]) if prev.get(s) is not None else None
            if fill:
                c.fill = fill
            if v >= 70:
                c.font = Font(name=FONT, size=10, bold=True, color="1F4FD8")
            elif v <= 30:
                c.font = Font(name=FONT, size=10, bold=True, color="7030A0")

    last = first + rows - 1
    for row in ws.iter_rows(min_row=1, max_row=last, max_col=ncol):
        for c in row:
            c.alignment = CENTER
            if c.row != 6:
                c.border = BORDER
            if c.font is None or c.font.name != FONT:
                c.font = Font(name=FONT, size=10, bold=c.font.bold, color=c.font.color)
    for r in (1, 5):
        for j in range(1, ncol + 1):
            c = ws.cell(r, j)
            c.fill = HEADER_FILL
            c.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
        ws.row_dimensions[r].height = 42
    for r in list(range(2, 5)) + list(range(first, last + 1)):
        ws.cell(r, 1).font = Font(name=FONT, size=10, bold=True)
    ws.cell(6, 1).font = Font(name=FONT, size=9, italic=True, color="808080")
    ws.cell(1, 1).comment = Comment(
        "1-Day／5-Day／20-Day = 最新一日 RSI 減去 N 個交易日前（公式）。", tag)
    ws.cell(5, 1).comment = Comment(note + "\n底色：與前一交易日相比，綠＝上升、紅＝下降，越深變化越大（±6 最深）。\n"
                                    "藍色粗體 = RSI ≥ 70；紫色粗體 = RSI ≤ 30。", tag)
    ws.column_dimensions["A"].width = 12
    for j in range(2, ncol + 1):
        ws.column_dimensions[get_column_letter(j)].width = 10
    ws.row_dimensions[6].height = 12
    ws.freeze_panes = "B7"
    return shown


def main():
    if not os.path.exists(HIST_CSV):
        print("沒有 data/sector_history.csv（還沒有自訂板塊），略過板塊熱圖")
        return
    data = {}
    with open(HIST_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["板塊RSI"] != "":
                data.setdefault(r["日期"], {})[r["板塊"]] = float(r["板塊RSI"])
    if not data:
        print("沒有板塊 RSI 資料")
        return
    found = {s for d in data for s in data[d]}
    order = sector_order()
    sectors = [s for s in order if s in found] + sorted(found - set(order))
    wb = Workbook()
    ws = wb.active
    ws.title = "板塊RSI"
    shown = write_heatmap(ws, data, sectors,
                          "板塊 RSI = 板塊內個股 14 日 RSI（Wilder，已還原分割與配息）的中位數。\n"
                          "板塊依 sectors_us.csv（主題 ETF 成分股）＋ sectors_us_補充.csv（手動）。")
    wb.save(OUT)
    print(f"已輸出 {OUT}（{shown} 個交易日 × {len(sectors)} 個板塊）")


if __name__ == "__main__":
    main()
