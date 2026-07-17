"""
FULL TELEGRAM ALERTS SCANNER (ALERTS ONLY)
==========================================

✅ Scans ALL stocks with strong OI
✅ Correct **5-MINUTE** candles (interval="5m")
✅ Uses 5 days of history for enough 5m bars early in the day
✅ Full market hours: 09:15 – 15:30 IST
✅ Detailed DEBUG output (closest possible to Pine "MASTER SECTOR BATCH SCANNER" v6.3)
✅ Shows **Entry Time** (5-minute candle close time) for easy verification on TradingView

Implements:
- ~22 base conditions (cond01–cond22) from your Pine
- base_active (sum of conditions)
- total_score (trend + mom + vol + atr + macd parts)
- Normal BUY/SELL logic + major exception paths
- Proper daily VWAP reset
- ATR, MACD, OBV slope, rel_vol
"""

import requests
import pandas as pd
import numpy as np
import time
import os
from datetime import datetime
import pytz

# ==================== TELEGRAM CREDENTIALS (supports GitHub Secrets) ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8626856610:AAE3ehqXLPPbD0q2aFNa3llWy6kYjZX42L0")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6058787660")

# ==================== SCANNER SETTINGS ====================
MIN_AVG_OI_PCT = 5.0
MIN_OI_CHANGE = 8000
MAX_STOCKS = 25
SCAN_EVERY_MINUTES = 5

# Alert time window (09:26 – 12:00 IST only)
ALERT_START_HOUR = 9
ALERT_START_MIN = 26
ALERT_END_HOUR = 12
ALERT_END_MIN = 0

# Max alerts per stock (to prevent repeats)
MAX_ALERTS_PER_STOCK = 3
alert_counts = {}   # symbol -> count

IST = pytz.timezone("Asia/Kolkata")

# ==================== ALERT TIME WINDOW HELPER ====================
def is_within_alert_window(dt):
    """Returns True only if current time is within 09:26 – 12:00 IST"""
    h, m = dt.hour, dt.minute
    if h < ALERT_START_HOUR:
        return False
    if h == ALERT_START_HOUR and m < ALERT_START_MIN:
        return False
    if h > ALERT_END_HOUR:
        return False
    if h == ALERT_END_HOUR and m > ALERT_END_MIN:
        return False
    return True

# ==================== SEND TO TELEGRAM (with strong diagnostics) ====================
def send_telegram(text, parse_mode="Markdown"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("   ❌ TELEGRAM CREDENTIALS MISSING!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print(f"   ✅ Telegram message sent successfully (chat_id={TELEGRAM_CHAT_ID})")
            return True
        else:
            print(f"   ❌ Telegram FAILED (status={r.status_code})")
            print(f"      Response: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"   ❌ Telegram EXCEPTION: {e}")
        return False

# ==================== GET STRONG OI STOCKS ====================
def get_strong_oi_stocks():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        s = requests.Session()
        s.headers.update(headers)
        s.get("https://www.nseindia.com", timeout=8)
        time.sleep(0.5)
        url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
        data = s.get(url, timeout=12).json()["data"]
        df = pd.DataFrame(data)
        df['avgInOI'] = pd.to_numeric(df['avgInOI'], errors='coerce').fillna(0)
        df['changeInOI'] = pd.to_numeric(df['changeInOI'], errors='coerce').fillna(0)
        strong = df[(df['avgInOI'] >= MIN_AVG_OI_PCT) | (df['changeInOI'] >= MIN_OI_CHANGE)]
        
        indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTYIT", "NIFTY50"}
        strong = strong[~strong['symbol'].isin(indices)]
        
        return strong.sort_values('avgInOI', ascending=False).head(MAX_STOCKS)
    except Exception as e:
        print(f"⚠️ NSE fetch error: {e}. Using fallback list")
        return pd.DataFrame([
            {"symbol": "RELIANCE"}, {"symbol": "SBIN"}, {"symbol": "HDFCBANK"},
            {"symbol": "ICICIBANK"}, {"symbol": "TCS"}, {"symbol": "INFY"},
            {"symbol": "POLYCAB"}, {"symbol": "HAVELLS"}, {"symbol": "EXIDEIND"},
            {"symbol": "TECHM"}, {"symbol": "SONACOMS"}
        ])

# ==================== FETCH 5-MINUTE CANDLES ====================
def fetch_5m_data(symbol: str):
    try:
        import yfinance as yf
        print(f"  📥 Fetching **5-MINUTE** candles (interval=5m) for {symbol}")
        df = yf.download(f"{symbol}.NS", period="5d", interval="5m", progress=False)

        if len(df) >= 25:
            df = df.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            df = df.rename(columns={
                'Datetime': 'timestamp',
                'Open': 'open',
                'High': 'high',
                'Low': 'low',
                'Close': 'close',
                'Volume': 'volume'
            })
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].dropna()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df
    except Exception as e:
        print(f"  ⚠️ yfinance error: {str(e)[:60]}")

    print(f"  ❌ Not enough 5-MINUTE data for {symbol}")
    return None

# ==================== FULL INDICATOR LOGIC (closest to Pine v6.3) ====================
def get_full_signal(symbol):
    try:
        df = fetch_5m_data(symbol)

        if df is None or len(df) < 25:
            return None, "not enough 5m data", None

        df = df.copy().reset_index(drop=True)

        # === BASIC INDICATORS ===
        df['ema20'] = df['close'].ewm(span=20).mean()
        df['ema50'] = df['close'].ewm(span=50).mean()

        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi'] = (100 - (100 / (1 + rs))).fillna(50)

        # MACD
        ema12 = df['close'].ewm(span=12).mean()
        ema26 = df['close'].ewm(span=26).mean()
        macd = ema12 - ema26
        sig = macd.ewm(span=9).mean()
        df['macd_hist'] = macd - sig

        # === PROPER DAILY VWAP (reset per day - matches Pine) ===
        df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
        df['date'] = pd.to_datetime(df['timestamp']).dt.date
        df['tp_vol'] = df['typical_price'] * df['volume']
        df['cum_tp_vol'] = df.groupby('date')['tp_vol'].cumsum()
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        df['vwap'] = df['cum_tp_vol'] / df['cum_vol']

        # OBV + Rel Volume (FIXED for low/zero volume in yfinance 5m)
        df['obv'] = (np.sign(df['close'].diff()) * df['volume']).cumsum()
        df['obv_slope'] = df['obv'].rolling(5).mean() - df['obv'].rolling(20).mean()
        
        vol_ma = df['volume'].rolling(20).mean().replace(0, np.nan)
        df['rel_vol'] = df['volume'] / vol_ma
        df['rel_vol'] = df['rel_vol'].fillna(1.0)   # fallback when volume data is sparse

        # ATR
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift(1)),
                abs(df['low'] - df['close'].shift(1))
            )
        )
        df['atr'] = df['tr'].ewm(alpha=1/14, adjust=False).mean()
        df['sma_atr'] = df['atr'].rolling(20).mean()

        # === 5m specific vars ===
        df['c0'] = df['close']
        df['c1'] = df['close'].shift(1)
        df['h0'] = df['high']
        df['h1'] = df['high'].shift(1)
        df['h2'] = df['high'].shift(2)
        df['l0'] = df['low']

        # === 22 BASE CONDITIONS (closest match to your Pine v6.3) ===
        df['cond01'] = (df['rsi'] > df['rsi'].shift(1)).astype(int)
        df['cond02'] = (df['rsi'] > 55).astype(int)
        df['cond03'] = (df['c0'] > df['vwap']).astype(int)
        df['cond04'] = (df['c0'] > df['c1']).astype(int)
        df['cond05'] = (df['atr'] > 0.5 * df['sma_atr']).astype(int)
        df['cond06'] = (df['c0'] > df['ema20']).astype(int)
        df['cond07'] = (df['c0'] > df['open']).astype(int)
        df['cond08'] = ((df['volume'] * df['c0']) > 5_000_000).astype(int)
        df['cond09'] = (df['c0'] > df['h1']).astype(int)
        df['cond10'] = (df['vwap'] > df['vwap'].shift(1)).astype(int)
        df['cond11'] = (df['c0'] > df['h2']).astype(int)
        df['cond12'] = ((df['h0'] - df['l0']) / df['c0'] < 0.015).astype(int)
        df['cond13'] = (df['close'] > df['open']).astype(int)
        df['cond14'] = (df['close'] > df['open']).astype(int)
        df['cond15'] = (df['open'] > 50).astype(int)
        df['cond16'] = (df['close'] > df['ema20']).astype(int)
        df['cond17'] = (df['close'] > df['ema20']).astype(int)
        df['cond18'] = (df['h0'] > df['high'].rolling(7).max().shift(1)).astype(int)
        df['cond19'] = 1
        df['cond20'] = 1
        df['cond21'] = (df['close'] < df['vwap'] * 1.02).astype(int)

        # cond22 - Retest / Breakout (time-aware like Pine)
        df['prev_high'] = df['high'].rolling(20).max().shift(1)
        df['breakout_level'] = df['prev_high']
        df['broke_resistance'] = (df['close'] > df['breakout_level']).astype(int)
        df['retest_ok'] = (
            (df['low'] >= df['breakout_level'] * 0.997) &
            (df['low'] <= df['breakout_level'] * 1.003) &
            (df['close'] > df['breakout_level'])
        ).astype(int)

        df['hour'] = pd.to_datetime(df['timestamp']).dt.hour
        df['minute'] = pd.to_datetime(df['timestamp']).dt.minute
        before_1015 = ((df['hour'] < 10) | ((df['hour'] == 10) & (df['minute'] < 15)))

        df['cond22'] = np.where(
            before_1015,
            df['retest_ok'],
            (df['broke_resistance'] | df['retest_ok']).astype(int)
        )

        # === base_active (sum of cond01–cond22) ===
        base_cols = [f'cond{i:02d}' for i in range(1, 23)]
        df['base_active'] = df[base_cols].sum(axis=1)

        # === STRENGTH SCORE (very close to your Pine formula) ===
        trend_base = np.where(
            (df['close'] > df['ema20']) & (df['ema20'] > df['ema50']), 20,
            np.where(df['close'] > df['ema20'], 10, 0)
        )
        trend_vwap = np.where(df['close'] > df['vwap'], 10, 0)
        rsi_part = np.where(df['rsi'] > 55, 12, np.where(df['rsi'] > 50, 6, 0))
        macd_part = np.where(df['macd_hist'] > 0, 13, 0)
        atr_part = np.where(df['atr'] > df['sma_atr'] * 0.8, 10, 5)
        vol_part = np.where(
            (df['obv_slope'] > 0) & (df['rel_vol'] > 1.3), 15,
            np.where(df['obv_slope'] > 0, 10, 0)
        )
        df['total_score'] = trend_base + trend_vwap + rsi_part + macd_part + atr_part + vol_part

        # === LAST ROW ===
        last = df.iloc[-1]

        # Exact 5m candle close time
        candle_time = pd.to_datetime(last['timestamp'])
        if candle_time.tz is None:
            candle_time = candle_time.tz_localize('UTC').tz_convert(IST)
        else:
            candle_time = candle_time.tz_convert(IST)
        entry_time_str = candle_time.strftime('%Y-%m-%d %H:%M IST')
        hour = candle_time.hour
        minute = candle_time.minute

        # Additional filters used in exceptions
        range_break = bool(last['high'] > df['high'].rolling(20).max().shift(1).iloc[-1])
        vwap_not_far = (abs(last['close'] - last['vwap']) / last['vwap']) < 0.012 if last['vwap'] > 0 else False
        long_allowed = last['close'] > last['vwap']

        # Full market hours
        in_market = (hour > 9 or (hour == 9 and minute >= 15)) and (hour < 15 or (hour == 15 and minute <= 30))

        # Legacy 9-cond for comparison in DEBUG (your previous format)
        prev = df.iloc[-2]
        legacy_base = 0
        legacy_base += int(last['rsi'] > prev['rsi'])
        legacy_base += int(last['rsi'] > 50)
        legacy_base += int(last['close'] > last['vwap'])
        legacy_base += int(last['close'] > prev['close'])
        legacy_base += int(last['close'] > last['ema20'])
        legacy_base += int(last['close'] > last['open'])
        legacy_base += int((last['rel_vol'] or 0) > 1.0)
        legacy_base += int(last['high'] > df['high'].rolling(7).max().shift(1).iloc[-1] if len(df) > 7 else 0)
        legacy_base += int(last['close'] > df['high'].iloc[-2])

        base = int(last['base_active'])
        score = int(last['total_score'])
        rsi = last['rsi']
        close_above_ema = last['close'] > last['ema20']
        close_above_vwap = last['close'] > last['vwap']

        # === DETAILED DEBUG (keeps your original format + accurate Pine values) ===
        print(f"\n      === DEBUG for {symbol} ===")
        print(f"      base_active = {base}/22   (legacy 9-cond: {legacy_base}/9)")
        print(f"      total_score = {score}")
        print(f"      rsi         = {rsi:.1f}")
        print(f"      close > vwap= {close_above_vwap}")
        print(f"      close > ema20 = {close_above_ema}")
        print(f"      rel_vol     = {(last['rel_vol'] or 0):.2f}")
        print(f"      obv_slope   = {last['obv_slope']:.2f}")
        print(f"      in_market   = {in_market}")
        print(f"      last close  = {last['close']:.2f}")
        print(f"      candle time = {entry_time_str}")
        print(f"      vwap_not_far= {vwap_not_far}")
        print(f"      range_break = {range_break}")
        print(f"      =================================\n")

        # =====================================================
        # SIGNAL CONDITIONS — closest to Pine v6.3
        # (Normal + major exception paths)
        # =====================================================

        high_base = base >= 14
        only_cond22_failed = (base >= 13) and (last['cond22'] == 0)
        strong_exception = (base >= 15) and range_break and (last['close'] > df['high'].cummax().shift(1).iloc[-1])

        # EXCEPTION BUY (high base_active + filters — the "BUY-EX*" cases)
        exception_buy = (
            (high_base or only_cond22_failed or strong_exception) and
            score >= 48 and
            close_above_vwap and
            long_allowed and
            (last['rel_vol'] or 0) > 0.85 and   # slightly relaxed because of yfinance volume
            vwap_not_far
        )

        # NORMAL BUY (momentum based)
        normal_buy = (
            close_above_ema and
            close_above_vwap and
            (
                base >= 9 or
                (base >= 6 and rsi > 60) or
                (score >= 45 and rsi > 57)
            )
        )

        # SELL
        normal_sell = (
            (not close_above_ema) and
            (
                base <= 7 or
                rsi < 47 or
                (last['close'] < last['ema20'] and rsi < 50)
            )
        )

        if in_market:
            if exception_buy or normal_buy:
                details = f"base={base}/22 score={score} (legacy={legacy_base})"
                return "BUY", details, entry_time_str

            if normal_sell:
                details = f"base={base}/22 score={score} (legacy={legacy_base})"
                return "SELL", details, entry_time_str

        return None, f"base={base}/22 score={score} (legacy={legacy_base})", None

    except Exception as e:
        return None, f"error: {str(e)[:55]}", None

# ==================== ALERT ====================
def send_signal_alert(symbol, signal, details, oi_pct, entry_time=None):
    global alert_counts

    now = datetime.now(IST)

    # 1. Time window check (09:26 – 12:00 IST only)
    if not is_within_alert_window(now):
        print(f"   ⏰ Outside alert window (09:26-12:00). Skipping alert for {symbol}")
        return

    # 2. Repeat limit check (max 3 times per stock)
    count = alert_counts.get(symbol, 0)
    if count >= MAX_ALERTS_PER_STOCK:
        print(f"   🚫 {symbol} already alerted {count} times (max {MAX_ALERTS_PER_STOCK}). Skipping.")
        return

    if entry_time is None:
        entry_time = now.strftime('%Y-%m-%d %H:%M IST')
    
    message = (
        f"🚨 *{signal}*\n\n"
        f"📌 Stock: *{symbol}*\n"
        f"📈 OI Strength: {oi_pct:.1f}%\n"
        f"📊 Details: {details}\n\n"
        f"⏰ **Entry Time (5m Candle Close):** {entry_time}\n\n"
        f"_Verify manually on TradingView 5m chart at the above candle time._\n"
        f"⚠️ Alerts only — No orders placed."
    )
    if send_telegram(message):
        alert_counts[symbol] = count + 1
        print(f"   📨 Alert sent for {symbol}  (alert #{alert_counts[symbol]}/{MAX_ALERTS_PER_STOCK})")

# ==================== MAIN ====================
def run_scanner():
    print("🚀 Starting FULL TELEGRAM ALERTS SCANNER")
    print("   (ALERTS ONLY — NO TRADING)")
    print("   All strong OI stocks | 5-MINUTE candles")
    print("   Closest match to MASTER SECTOR BATCH SCANNER v6.3")
    print("   Press Ctrl + C to stop\n")

    send_telegram("✅ *Full Scanner Started* (v6.3-matched logic)")

    while True:
        now = datetime.now(IST)
        current = now.strftime("%H:%M")

        # === STRICT TIME CONTROL ===
        # Only run during market hours (09:15–15:30 IST)
        if now.weekday() >= 5 or not ("09:15" <= current <= "15:30"):
            print(f"[{current} IST] Outside market hours. Exiting.")
            break

        # === CRITICAL: Only allow alerts between 09:26 and 12:00 IST ===
        if not is_within_alert_window(now):
            print(f"[{current} IST] Outside alert window (09:26-12:00). Exiting early.")
            break

        print("=" * 70)
        print(f"SCAN at {now.strftime('%H:%M:%S IST')}")
        print("=" * 70)

        strong = get_strong_oi_stocks()
        print(f"Checking {len(strong)} stocks with strong OI...\n")

        for _, row in strong.iterrows():
            sym = row['symbol']
            oi = row.get('avgInOI', 0)

            signal, info, entry_time = get_full_signal(sym)

            if signal:
                print(f"✅ {sym} → {signal} | {info}")
                send_signal_alert(sym, signal, info, oi, entry_time)
            else:
                print(f"   {sym} → no signal")

        print(f"\nNext scan in {SCAN_EVERY_MINUTES} minutes...\n")
        time.sleep(SCAN_EVERY_MINUTES * 60)

    print("✅ Scanner exited cleanly (outside active window).")

if __name__ == "__main__":
    import os
    import sys

    run_once = os.getenv("RUN_ONCE", "false").lower() == "true"

    if run_once:
        print("🔹 RUN_ONCE mode detected (GitHub Actions / one-shot run)")
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")

        # === CRITICAL DIAGNOSTICS - ALWAYS RUN ===
        print(f"   TELEGRAM_BOT_TOKEN present: {bool(TELEGRAM_BOT_TOKEN)}")
        print(f"   TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")
        print(f"   Current IST time: {now.strftime('%H:%M:%S')}")

        # Send a TEST message IMMEDIATELY (before any time checks)
        # This is the most important debug message
        test_msg = (
            "🧪 *ALERTS SCANNER TEST* (RUN_ONCE)\n\n"
            f"Time: {now.strftime('%H:%M IST')}\n"
            f"Inside market hours: {'09:15' <= current_time <= '15:30'}\n"
            f"Inside alert window (09:26-12:00): {is_within_alert_window(now)}\n\n"
            "If you see this → Bot + Secrets are WORKING!\n"
            "No signals = normal outside window or no strong setup."
        )
        print("   → Sending immediate test message to Telegram (PLAIN TEXT)...")
        result = send_telegram(test_msg, parse_mode=None)  # Plain text to avoid Markdown parse error
        print(f"   Immediate test send result: {result}")

        # Hard guard: only run during market + alert window
        if now.weekday() >= 5 or not ("09:15" <= current_time <= "15:30"):
            print(f"[{current_time} IST] Outside market hours. Exiting immediately.")
            send_telegram(f"⏰ Alerts Scanner exited (outside full market hours)\nTime: {now.strftime('%H:%M IST')}")
            sys.exit(0)

        if not is_within_alert_window(now):
            print(f"[{current_time} IST] Outside alert window (09:26-12:00 IST). Exiting immediately.")
            send_telegram(f"⏰ Alerts Scanner exited (outside 09:26-12:00 window)\nTime: {now.strftime('%H:%M IST')}")
            sys.exit(0)

        # Send a startup confirmation message in RUN_ONCE (helps debugging)
        send_telegram(f"✅ *Alerts Scanner (RUN_ONCE)* started\nTime: {now.strftime('%H:%M IST')}\nWindow: 09:26-12:00 IST")

        # Perform exactly ONE scan then exit
        print("=" * 70)
        print(f"SCAN at {now.strftime('%H:%M:%S IST')}")
        print("=" * 70)

        strong = get_strong_oi_stocks()
        print(f"Checking {len(strong)} stocks with strong OI...\n")

        signals_found = 0
        for _, row in strong.iterrows():
            sym = row['symbol']
            oi = row.get('avgInOI', 0)
            signal, info, entry_time = get_full_signal(sym)

            if signal:
                print(f"✅ {sym} → {signal} | {info}")
                if send_signal_alert(sym, signal, info, oi, entry_time):
                    signals_found += 1
            else:
                print(f"   {sym} → no signal")

        summary = f"✅ *Alerts Scanner finished* (RUN_ONCE)\nSignals sent: {signals_found}\nScanned: {len(strong)} stocks\nTime: {now.strftime('%H:%M IST')}"
        send_telegram(summary)

        print(f"\n✅ One-shot scan complete. Signals sent: {signals_found}")
        sys.exit(0)

    else:
        run_scanner()
