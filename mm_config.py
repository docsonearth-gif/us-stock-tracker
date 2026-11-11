"""美股 Market Monitor 參數設定。改這裡的數字就能調整篩選條件與 Excel 上色門檻。"""

# ---------- 股票篩選 ----------
MIN_PRICE = 5.0              # 收盤價至少幾美元才納入（排除低價股）
MIN_VOLUME_SHARES = 100_000  # 4% 單日突破/跌破：當日成交量至少 10 萬股（Stockbee 原版條件）
MIN_AVG_VOLUME_SHARES = 100_000  # 多日區間指標：近 20 日平均成交量至少 10 萬股

# ---------- 指標計算 ----------
DAILY_MOVE = 0.04           # 單日漲跌 4%
QUARTER_DAYS = 65           # 一季 ≈ 65 個交易日
MONTH_DAYS = 20             # 一個月 ≈ 20 個交易日
DAYS_34 = 34                # Stockbee 的 34 日
MA_DAYS = 40                # T2108：站上 40 日均線的比例

# ---------- Excel 上色門檻 ----------
BIG_DAY_COUNT = 300         # 單日 4% 家數 ≥ 300 → 深綠／深紅（Stockbee 原版門檻）
RATIO_STRONG = 2.0          # 5/10 日比 ≥ 2 → 綠
RATIO_WEAK = 0.5            # 5/10 日比 ≤ 0.5 → 紅
T2108_LOW = 20              # 站上 40MA ≤ 20% → 極度超賣
T2108_WEAK = 40
T2108_STRONG = 60
T2108_HIGH = 80             # ≥ 80% → 極度過熱
OVERHEAT_RATIO5 = 7.0       # 5 日比 ≥ 7 → 短線過熱

OUTPUT_ROWS = 260           # Excel 顯示最近幾個交易日
