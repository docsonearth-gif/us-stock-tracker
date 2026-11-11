"""
美股個股分類（每週更新一次；FORCE_CLASSIFY=1 可強制）

1. 產業別：Nasdaq 官方股票篩選器（一次抓全部美股的 sector／industry）→ 寫進 data/stocks.csv
   → rs_us.py 的「產業強度」工作表
2. 主題板塊：主題 ETF 的成分股 → sectors_us.csv（代號,板塊,名稱,來源）
   → rs_us.py 的「自訂板塊強度」、板塊 RSI 熱圖
   - 主題與來源 ETF 在 themes_us.csv（板塊,來源,ETF）；來源支援 ssga／globalx／ark／ishares／ftrust
   - 某個主題這次抓不到，就沿用上一版的成分股
   - 只保留在股票清單裡的美股（外國掛牌、ETF 都會被排除）
3. 手動補充：sectors_us_補充.csv（格式同 sectors_us.csv），自動更新不會動它

ETF 發行商偶爾會改網址；抓不到的會列在 log，對應主題沿用舊資料，不會中斷每日流程。
"""

import csv
import io
import os
import re
import sys
from datetime import date, timedelta

import pandas as pd
import requests

from common import DATA, ROOT, load_stocks, norm_code, save_stocks

THEMES_CSV = os.path.join(ROOT, "themes_us.csv")
OUT_CSV = os.path.join(ROOT, "sectors_us.csv")
STAMP = os.path.join(DATA, "classify_stamp.txt")
EVERY_DAYS = int(os.getenv("CLASSIFY_EVERY_DAYS", "7"))
FORCE = os.getenv("FORCE_CLASSIFY") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9"}

ISHARES = {  # iShares 的商品頁 id 與網址名稱
    "SOXX": ("239705", "ishares-phlx-semiconductor-etf"),
    "IGV": ("239771", "ishares-north-american-techsoftware-etf"),
    "IHI": ("239516", "ishares-us-medical-devices-etf"),
    "ITA": ("239502", "ishares-us-aerospace-defense-etf"),
    "ITB": ("239512", "ishares-us-home-construction-etf"),
}
ARK = {
    "ARKX": "ARK_SPACE_EXPLORATION_&_INNOVATION_ETF_ARKX_HOLDINGS.csv",
    "ARKG": "ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKQ": "ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKW": "ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
}


# ---------- 1. 產業別 ----------

def nasdaq_industry():
    r = requests.get("https://api.nasdaq.com/api/screener/stocks",
                     params={"tableonly": "true", "download": "true"}, headers=UA, timeout=60)
    r.raise_for_status()
    rows = (r.json().get("data") or {}).get("rows") or []
    out = {}
    for x in rows:
        sym = str(x.get("symbol", "")).strip()
        if not sym or "^" in sym:
            continue
        out[norm_code(sym)] = (str(x.get("sector") or "").strip(), str(x.get("industry") or "").strip())
    return out


def update_industry():
    try:
        ind = nasdaq_industry()
    except Exception as e:
        print(f"產業別（Nasdaq）失敗，沿用舊資料：{e}")
        return
    st = load_stocks()
    hit = 0
    for c, v in st.items():
        if c in ind and (ind[c][0] or ind[c][1]):
            v["sector"], v["industry"] = ind[c]
            hit += 1
    save_stocks(st)
    n_ind = len({v["industry"] for v in st.values() if v.get("industry")})
    print(f"產業別：Nasdaq 共 {len(ind)} 檔；股票清單 {len(st)} 檔中 {hit} 檔有分類，{n_ind} 個產業")


# ---------- 2. 主題 ETF 成分股 ----------

def tickers_from(df, col_candidates):
    for c in df.columns:
        if str(c).strip().lower() in col_candidates:
            return [str(t).strip() for t in df[c].dropna()]
    raise ValueError(f"找不到代號欄位：{list(df.columns)[:8]}")


def h_ssga(etf):
    url = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
           f"holdings-daily-us-en-{etf.lower()}.xlsx")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    hdr = raw.index[raw.apply(lambda x: x.astype(str).str.strip().eq("Ticker").any(), axis=1)][0]
    df = pd.read_excel(io.BytesIO(r.content), skiprows=hdr)
    return tickers_from(df, {"ticker"})


def h_globalx(etf):
    last = None
    for back in range(0, 10):
        d = date.today() - timedelta(days=back)
        url = f"https://assets.globalxetfs.com/funds/holdings/{etf.lower()}_full-holdings_{d:%Y%m%d}.csv"
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code != 200 or "Ticker" not in r.text[:2000]:
            last = r.status_code
            continue
        lines = r.text.splitlines()
        start = next(i for i, l in enumerate(lines) if "Ticker" in l)
        df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
        return tickers_from(df, {"ticker"})
    raise RuntimeError(f"Global X 近 10 天都沒有檔案（最後狀態 {last}）")


def h_ark(etf):
    url = f"https://assets.ark-funds.com/fund-documents/funds-etf-csv/{ARK[etf]}"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    return tickers_from(df, {"ticker"})


def h_ishares(etf):
    pid, slug = ISHARES[etf]
    url = (f"https://www.ishares.com/us/products/{pid}/{slug}/1467271812596.ajax"
           f"?fileType=csv&fileName={etf}_holdings&dataType=fund")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    lines = r.text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker"))
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    return tickers_from(df, {"ticker"})


def h_ftrust(etf):
    url = f"https://www.ftportfolios.com/Retail/Etf/EtfHoldings.aspx?Ticker={etf}"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        cols = [str(c).strip().lower() for c in t.columns]
        if "identifier" in cols or "ticker" in cols:
            col = t.columns[cols.index("identifier" if "identifier" in cols else "ticker")]
            return [str(x).strip() for x in t[col].dropna()]
    raise ValueError("找不到持股表")


HANDLERS = {"ssga": h_ssga, "globalx": h_globalx, "ark": h_ark, "ishares": h_ishares, "ftrust": h_ftrust}


def load_old():
    old = {}
    if os.path.exists(OUT_CSV):
        with open(OUT_CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                old.setdefault(r["板塊"], []).append(r)
    return old


def update_themes():
    with open(THEMES_CSV, encoding="utf-8-sig") as f:
        themes = [r for r in csv.DictReader(f) if r["板塊"].strip()]
    stocks = load_stocks()
    old = load_old()
    rows, report = [], []
    by_theme = {}
    for t in themes:
        by_theme.setdefault(t["板塊"].strip(), []).append((t["來源"].strip().lower(), t["ETF"].strip().upper()))
    for theme, srcs in by_theme.items():
        got, used, errs = set(), [], []
        for src, etf in srcs:
            try:
                tick = HANDLERS[src](etf)
                codes = {norm_code(x) for x in tick if re.fullmatch(r"[A-Za-z][A-Za-z.\-/ ]{0,7}", x or "")}
                codes &= set(stocks)
                if codes:
                    got |= codes
                    used.append(f"{etf}({len(codes)})")
                else:
                    errs.append(f"{etf}:沒有美股成分股")
            except Exception as e:
                errs.append(f"{etf}:{str(e)[:80]}")
        if got:
            src_txt = "+".join(used)
            rows += [{"代號": c, "板塊": theme, "名稱": stocks[c].get("name", ""), "來源": src_txt} for c in sorted(got)]
            report.append(f"  {theme}：{len(got)} 檔（{src_txt}）" + (f"｜失敗 {'; '.join(errs)}" if errs else ""))
        elif theme in old:
            rows += old[theme]
            report.append(f"  {theme}：這次抓不到，沿用舊的 {len(old[theme])} 檔｜{'; '.join(errs)}")
        else:
            report.append(f"  {theme}：抓不到，也沒有舊資料｜{'; '.join(errs)}")
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["代號", "板塊", "名稱", "來源"])
        w.writeheader()
        w.writerows(rows)
    print(f"主題板塊：{len(by_theme)} 個主題、{len(rows)} 筆")
    print("\n".join(report))


def main():
    if not FORCE and os.path.exists(STAMP):
        last = open(STAMP).read().strip()
        if last and (date.today() - date.fromisoformat(last)).days < EVERY_DAYS:
            print(f"分類上次更新 {last}，未滿 {EVERY_DAYS} 天，略過")
            return
    update_industry()
    update_themes()
    with open(STAMP, "w") as f:
        f.write(date.today().isoformat())


if __name__ == "__main__":
    sys.exit(main())
