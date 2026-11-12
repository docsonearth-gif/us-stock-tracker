"""
候選股的基本面加分項與財報日（只查候選股，結果快取在 data/）

加分項（J Law 美股：先過必要條件，再用加分項排序）
  EPS≥20%     最近一季 EPS（稀釋）年增 ≥ 20%
  EPS加速     最近 3 季 EPS 年增率一季比一季高
  轉盈        最近一季 EPS 為正、去年同季為負
  營收≥20%加速 最近一季營收年增 ≥ 20%，且連 2 季加速
  強勢IPO     近 3 年內上市（Nasdaq 篩選器的 IPO 年）

資料來源
  SEC EDGAR（免費）：company_tickers.json（代號 → CIK）、companyfacts（XBRL 財報數字）
    - 只用季度數字（frame＝CYyyyyQn）；外國公司（20-F、IFRS）通常沒有，會顯示空白
    - 每檔快取 7 天（data/fundamentals.csv）
    - SEC 要求 User-Agent 帶聯絡方式：可在 repo Secrets 設 SEC_USER_AGENT（例如「名字 email」）
  Nasdaq 財報日曆（免費）：未來 21 天每天一次 → data/earnings.csv（每天更新）
"""

import csv
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import requests

from common import DATA, norm_code

FUND_CSV = os.path.join(DATA, "fundamentals.csv")
EARN_CSV = os.path.join(DATA, "earnings.csv")
CIK_JSON = os.path.join(DATA, "sec_cik.json")
SEC_UA = os.getenv("SEC_USER_AGENT", "").strip() or "us-stock-tracker research-bot@users.noreply.github.com"
FUND_DAYS = int(os.getenv("FUND_CACHE_DAYS", "7"))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept": "application/json, text/plain, */*"}
FIELDS = ["code", "updated", "q", "eps", "eps_yoy", "rev_yoy", "flags"]

EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"]
REV_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"]


# ---------- SEC ----------

def sec_get(url):
    for i in range(3):
        r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"SEC {r.status_code}：{url}")


def cik_map():
    if os.path.exists(CIK_JSON) and (time.time() - os.path.getmtime(CIK_JSON)) < 7 * 86400:
        return json.load(open(CIK_JSON))
    j = sec_get("https://www.sec.gov/files/company_tickers.json") or {}
    m = {norm_code(v["ticker"]): int(v["cik_str"]) for v in j.values()}
    json.dump(m, open(CIK_JSON, "w"))
    return m


def quarterly(facts, tags, unit_pred):
    """回傳 {(年, 季): 數值}，只取單季（frame＝CYyyyyQn）。多個 tag 時依序補缺。"""
    out = {}
    g = (facts or {}).get("facts", {}).get("us-gaap", {})
    for t in tags:
        units = g.get(t, {}).get("units", {})
        for u, vals in units.items():
            if not unit_pred(u):
                continue
            for v in vals:
                m = re.fullmatch(r"CY(\d{4})Q([1-4])", v.get("frame", ""))
                if m:
                    out.setdefault((int(m.group(1)), int(m.group(2))), v["val"])
    return out


def yoy(series, n=4):
    """最近 n 季的年增率（新到舊），基期 ≤ 0 時為 None。"""
    if not series:
        return [], []
    keys = sorted(series)[::-1]
    res, qs = [], []
    for (y, q) in keys[:n]:
        base = series.get((y - 1, q))
        cur = series[(y, q)]
        res.append((cur / base - 1) if base and base > 0 else None)
        qs.append(f"{y}Q{q}")
    return res, qs


def analyze(facts):
    eps = quarterly(facts, EPS_TAGS, lambda u: "/shares" in u)
    rev = quarterly(facts, REV_TAGS, lambda u: u == "USD")
    eg, qs = yoy(eps)
    rg, _ = yoy(rev)
    flags = []
    if eg and eg[0] is not None and eg[0] >= 0.2:
        flags.append("EPS≥20%")
    if len(eg) >= 3 and None not in eg[:3] and eg[0] > eg[1] > eg[2]:
        flags.append("EPS加速")
    if eps:
        (y, q) = max(eps)
        if eps[(y, q)] > 0 and eps.get((y - 1, q), 1) < 0:
            flags.append("轉盈")
    if len(rg) >= 3 and None not in rg[:3] and rg[0] >= 0.2 and rg[0] > rg[1] > rg[2]:
        flags.append("營收≥20%加速")
    last_q = qs[0] if qs else ""
    last_eps = eps[max(eps)] if eps else None
    return {"q": last_q, "eps": last_eps,
            "eps_yoy": "|".join("" if x is None else f"{x * 100:.0f}" for x in eg[:3]),
            "rev_yoy": "|".join("" if x is None else f"{x * 100:.0f}" for x in rg[:3]),
            "flags": "、".join(flags)}


def load_cache():
    if not os.path.exists(FUND_CSV):
        return {}
    with open(FUND_CSV, encoding="utf-8-sig") as f:
        return {r["code"]: r for r in csv.DictReader(f)}


def update_fundamentals(codes):
    cache = load_cache()
    today = date.today()
    todo = [c for c in codes if c not in cache or
            (today - date.fromisoformat(cache[c]["updated"])).days >= FUND_DAYS]
    if todo:
        try:
            ciks = cik_map()
        except Exception as e:
            print(f"SEC 代號對照失敗：{e}")
            ciks = {}
        ok = 0
        if not ciks:
            todo = []  # 連不上 SEC：這次不更新、也不寫入空白快取
        for c in todo:
            cik = ciks.get(c)
            row = {"code": c, "updated": today.isoformat(), "q": "", "eps": "", "eps_yoy": "", "rev_yoy": "", "flags": ""}
            if cik:
                try:
                    facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
                    if facts:
                        row.update(analyze(facts))
                        ok += 1
                except Exception as e:
                    print(f"  {c} SEC 失敗：{e}")
                    continue  # 失敗的不寫入快取，下次再試
            cache[c] = row
            time.sleep(0.15)  # SEC 上限每秒 10 次
        print(f"基本面：更新 {len(todo)} 檔（{ok} 檔有 SEC 季報數字）")
    with open(FUND_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for k in sorted(cache):
            w.writerow({x: cache[k].get(x, "") for x in FIELDS})
    return cache


# ---------- 財報日 ----------

def update_earnings(days=21):
    """Nasdaq 財報日曆：未來 days 天 → {code: (日期, 時段)}。當天已更新就沿用。"""
    if os.path.exists(EARN_CSV) and date.fromtimestamp(os.path.getmtime(EARN_CSV)) == date.today():
        return load_earnings()
    out = {}
    d0 = date.today()
    for i in range(days):
        d = d0 + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            r = requests.get("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()},
                             headers=UA, timeout=30)
            rows = ((r.json().get("data") or {}).get("rows")) or []
        except Exception as e:
            print(f"  財報日曆 {d} 失敗：{e}")
            continue
        for x in rows:
            c = norm_code(x.get("symbol", ""))
            if c and c not in out:
                t = str(x.get("time", ""))
                out[c] = (d.isoformat(), "盤前" if "pre" in t else "盤後" if "after" in t else "")
        time.sleep(0.5)
    if out:
        with open(EARN_CSV, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "date", "time"])
            for c, (d, t) in sorted(out.items()):
                w.writerow([c, d, t])
        print(f"財報日曆：未來 {days} 天 {len(out)} 檔")
    return out or load_earnings()


def load_earnings():
    if not os.path.exists(EARN_CSV):
        return {}
    with open(EARN_CSV, encoding="utf-8-sig") as f:
        return {r["code"]: (r["date"], r["time"]) for r in csv.DictReader(f)}
