"""
美股共用工具：路徑、代號格式、每日檔讀寫、EODHD 連線

每日檔 data/daily/YYYY-MM-DD.csv.gz（美東交易日）
  code     代號（Yahoo 格式，例如 BRK-B）
  open/high/low/close  未還原價格（當天實際成交價）
  volume   成交股數
  ret      當天「含息、含分割」的報酬率（小數）。用同一個來源、同一份還原價算出
           「今天 ÷ 前一個交易日 − 1」，所以之後再發生分割或配息，舊的 ret 都不用改。
           計算時把 ret 連乘回去就是還原股價（跟台股版用漲跌價差還原是同一個概念）。
  src      資料來源：eodhd／yahoo
"""

import csv
import glob
import gzip
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DAILY = os.path.join(DATA, "daily")
STOCKS_CSV = os.path.join(DATA, "stocks.csv")
INDEX_CSV = os.path.join(DATA, "index.csv")
FIELDS = ["code", "open", "high", "low", "close", "volume", "ret", "src"]

EODHD_KEY = os.getenv("EODHD_API_KEY", "").strip()
EODHD = "https://eodhd.com/api"

# 指數：欄位名稱 → (EODHD 代號, Yahoo 代號)
INDEXES = {
    "spx": ("GSPC.INDX", "^GSPC"),     # S&P 500：Utility Screen、RS 線、出貨日
    "ixic": ("IXIC.INDX", "^IXIC"),    # Nasdaq Composite：出貨日另外看
}

# 主要交易所（排除 OTC／粉單）
EXCHANGES = {"NYSE", "NASDAQ", "NYSE MKT", "AMEX", "NYSE ARCA", "BATS", "NYSE AMERICAN"}

# 名稱含這些字的不是普通股（權證、單位、特別股、SPAC…）
NOT_COMMON = re.compile(
    r"\bwarrants?\b|\bunits?\b|\brights?\b|preferred|\bpfd\b|depositary shares? represent|"
    r"\bnotes? due\b|debentures?|acquisition corp|acquisition corporation|\bspac\b|"
    r"\bfixed[- ]to[- ]floating\b|\bperpetual\b",
    re.I,
)


# 代號尾碼：權利（-R、-RI、-RT）、權證（-W、-WS）、發行前交易（-WI）、單位（-U）；測試代號
BAD_SUFFIX = re.compile(r"-(R|RI|RT|W|WS|WI|U|UN)$|^[A-Z]TEST(-|$)")
NY = ZoneInfo("America/New_York")


def last_closed_day():
    """最後一個「已經收盤」的美東日期（YYYY-MM-DD）。16:45 前算前一天，避免把盤中資料當成收盤。"""
    now = datetime.now(NY)
    d = now.date() if now.hour * 60 + now.minute >= 16 * 60 + 45 else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def norm_code(code):
    """統一成 Yahoo 格式：BRK.B／BRK_B／BRK/B → BRK-B。"""
    c = str(code).strip().upper()
    if c.endswith(".US"):
        c = c[:-3]
    return re.sub(r"[./_ ]", "-", c)


def eodhd_code(code):
    return f"{code}.US"


# ---------- 每日檔 ----------

def day_path(key):
    return os.path.join(DAILY, f"{key}.csv.gz")


def have_days():
    return sorted(os.path.basename(p)[:10] for p in glob.glob(os.path.join(DAILY, "*.csv.gz")))


def fmt(v, nd=4):
    if v is None or v == "":
        return ""
    try:
        if v != v:  # NaN
            return ""
    except TypeError:
        return ""
    if nd == 0:
        return str(int(round(float(v))))          # 整數（成交量）：不能去掉尾端的 0
    return f"{float(v):.{nd}f}".rstrip("0").rstrip(".")


def save_day(key, rows):
    """rows：list of dict（欄位見 FIELDS）。"""
    os.makedirs(DAILY, exist_ok=True)
    tmp = day_path(key) + ".tmp"   # 先寫暫存檔再改名，中途出錯也不會留下半個檔案
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for r in sorted(rows, key=lambda x: x["code"]):
            w.writerow([r["code"], fmt(r.get("open")), fmt(r.get("high")), fmt(r.get("low")),
                        fmt(r.get("close")), fmt(r.get("volume"), 0), fmt(r.get("ret"), 6),
                        r.get("src", "")])
    os.replace(tmp, day_path(key))


def load_day(key):
    if not os.path.exists(day_path(key)):
        return {}
    out = {}
    with gzip.open(day_path(key), "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["code"]] = r
    return out


# ---------- 股票清單 ----------

STOCK_FIELDS = ["code", "name", "exchange", "sector", "industry", "ipo_year", "source", "first_seen"]


def load_stocks():
    if not os.path.exists(STOCKS_CSV):
        return {}
    with open(STOCKS_CSV, encoding="utf-8") as f:
        return {r["code"]: r for r in csv.DictReader(f)}


def save_stocks(stocks):
    os.makedirs(DATA, exist_ok=True)
    with open(STOCKS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STOCK_FIELDS, extrasaction="ignore")
        w.writeheader()
        for k in sorted(stocks):
            w.writerow({c: stocks[k].get(c, "") for c in STOCK_FIELDS})


# ---------- 指數 ----------

def load_index():
    """回傳 {date: {spx:..., ixic:...}}。"""
    out = {}
    if os.path.exists(INDEX_CSV):
        with open(INDEX_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["date"]] = {k: v for k, v in r.items() if k != "date"}
    return out


def save_index(idx):
    cols = ["date"] + [f"{k}{s}" for k in INDEXES for s in ("", "_vol")]
    with open(INDEX_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in sorted(idx):
            w.writerow([d] + [idx[d].get(c, "") for c in cols[1:]])


# ---------- EODHD ----------

def eodhd_get(path, params=None, tries=4):
    if not EODHD_KEY:
        raise RuntimeError("沒有設定 EODHD_API_KEY")
    p = {"api_token": EODHD_KEY, "fmt": "json", **(params or {})}
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(f"{EODHD}/{path}", params=p, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code in (401, 402, 403):
                raise PermissionError(f"EODHD 拒絕（{r.status_code}）：{r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except PermissionError:
            raise
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"EODHD {path} 失敗：{last}")


def eodhd_symbols(delisted=False):
    """EODHD 美股清單 → {code: {...}}，只留主要交易所的普通股。"""
    j = eodhd_get("exchange-symbol-list/US", {"delisted": 1} if delisted else None) or []
    out = {}
    for r in j:
        if str(r.get("Type", "")).lower() != "common stock":
            continue
        ex = str(r.get("Exchange", "")).upper()
        if ex not in EXCHANGES:
            continue
        name = str(r.get("Name", "")).strip()
        if NOT_COMMON.search(name):
            continue
        code = norm_code(r.get("Code", ""))
        if not code or BAD_SUFFIX.search(code):
            continue
        out[code] = {"code": code, "name": name, "exchange": ex.replace("NYSE MKT", "AMEX")
                     .replace("NYSE AMERICAN", "AMEX"), "eod_code": r.get("Code")}
    return out
