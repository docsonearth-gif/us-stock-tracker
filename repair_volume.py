"""
用 EODHD Bulk API 重抓每日檔的成交量與開高低（一次性修正）

原因：回補時用的單檔日線 API，美股成交量不可靠（例如 JPM 某天只有 793 股），
但收盤價與報酬率都正確（已驗證）。Bulk API 是之後每天抓資料用的來源，成交量正確。
這支程式只換 open／high／low／volume，close 與 ret 不動；做完的日期記在 data/volume_repaired.txt，
中斷後重跑會接著做。
"""

import os
import sys
import time

from common import DATA, have_days, load_day, save_day
from fetch_us import bulk_bars

DONE = os.path.join(DATA, "volume_repaired.txt")
GAP = float(os.getenv("BULK_GAP_SEC", "7"))     # Bulk 一次算 100 次呼叫；每分鐘上限 1000 → 間隔 ≥ 6 秒
MAX_MIN = float(os.getenv("MAX_MINUTES", "130"))
WATCH = ["AAPL", "TSLA", "NVDA", "JPM", "F", "KO"]


def main():
    done = set(open(DONE).read().split()) if os.path.exists(DONE) else set()
    todo = [d for d in have_days() if d not in done]
    print(f"要修正 {len(todo)} 天（已完成 {len(done)} 天）")
    t0 = time.time()
    for i, d in enumerate(todo, 1):
        if (time.time() - t0) / 60 > MAX_MIN:
            print("時間到，先停，下次接著做")
            break
        t1 = time.time()
        try:
            bars = bulk_bars(d)
        except Exception as e:
            print(f"  {d} 失敗：{e}")
            time.sleep(30)
            continue
        rows = load_day(d)
        hit = close_diff = 0
        before = {c: rows[c]["volume"] for c in WATCH if c in rows}
        for c, r in rows.items():
            b = bars.get(c)
            if not b or not b.get("close"):
                continue
            hit += 1
            if r.get("close") and abs(float(r["close"]) / b["close"] - 1) > 0.005:
                close_diff += 1
            for k in ("open", "high", "low", "volume"):
                if b.get(k) is not None:
                    r[k] = b[k]
        save_day(d, list(rows.values()))
        with open(DONE, "a") as f:
            f.write(d + "\n")
        if i <= 3 or i % 25 == 0:
            after = {c: int(float(rows[c]["volume"])) for c in WATCH if c in rows}
            print(f"  {d}: {hit}/{len(rows)} 檔更新、收盤不一致 {close_diff}｜修正前 {before}｜修正後 {after}")
        time.sleep(max(0, GAP - (time.time() - t1)))
    print(f"完成，共 {len(open(DONE).read().split())} 天")


if __name__ == "__main__":
    sys.exit(main())
