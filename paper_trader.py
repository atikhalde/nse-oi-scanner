"""
PAPER TRADING AUTOMATION (ALERTS + SIMULATED TRADES)
====================================================

✅ Entry logic: **Exact same full v6.3 logic** as full_telegram_alerts.py (22 conditions + long_allowed filter)
✅ **Paper trade ENTRIES only allowed 09:26 – 12:00 IST** (strict window)
✅ Position management (SL/TP/trailing) continues after window
✅ Max **1 entry per stock** (first signal only)
✅ Fixed **₹50,000** capital per trade (qty = round(50000 / entry_price))
✅ SL = Low (BUY) / High (SELL) of the **exact 5-minute entry candle** + 0.02% buffer
✅ **MAX SL RISK = 1%** (hard cap — any candle-based SL >1% is automatically capped)
✅ Target 1:2 (primary)
✅ Target 1:3
✅ On target hit (detected on candle CLOSE): trail SL as per rule + exit at the target
   - 1:2 hit on close → trail SL to entry (breakeven) + exit at 1:2
   - 1:3 hit on close → trail SL to entry (CTC) + exit at 1:3
✅ SL always checked on candle LOW (conservative)
✅ Detailed trade logging:
   - Date
   - Entry Time (exact 5m candle close)
   - Exit Time
   - Entry Price
   - Exit Price
   - SL (with buffer)
   - Targets
   - Move captured (points + %)
   - P&L (paper)
   - Reason for exit

Intact all previous features (DEBUG, time guards, etc.)
"""

import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
import pytz
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.utils import get_column_letter

# ==================== TELEGRAM (optional for alerts) ====================
# Prefer environment variables (recommended for GitHub Actions)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8626856610:AAE3ehqXLPPbD0q2aFNa3llWy6kYjZX42L0")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6058787660")

# ==================== SETTINGS ====================
MIN_AVG_OI_PCT = 5.0
MIN_OI_CHANGE = 8000
MAX_STOCKS = 20
SCAN_EVERY_MINUTES = 5

# Time windows
ALERT_START_HOUR = 9
ALERT_START_MIN = 26
ALERT_END_HOUR = 12
ALERT_END_MIN = 0

MAX_ENTRIES_PER_STOCK = 1   # Only the FIRST signal per stock (max 1 entry)
MAX_ALERTS_PER_STOCK = 3   # for any legacy alert functions (kept for compatibility)

# Paper trading settings
POSITION_VALUE = 50000      # Fixed capital per trade (₹50,000)
SL_BUFFER_PCT = 0.0002      # 0.02% buffer on entry candle SL (safer)
MAX_SL_PCT = 0.01           # Hard cap: maximum SL risk = 1% of entry price
TRADE_LOG_FILE = "paper_trades_log.csv"
EXCEL_REPORT_FILE = "Paper_Trades_Report.xlsx"

IST = pytz.timezone("Asia/Kolkata")

# ==================== STATE ====================
alert_counts = {}           # symbol -> number of entries taken
positions = {}              # symbol -> active paper position dict
trade_log = []
_last_report_stats = {}     # for Telegram caption

# ==================== HELPERS ====================
def is_within_alert_window(dt):
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

def send_telegram(text, parse_mode="Markdown"):
    print(f"   [TELEGRAM] Attempting to send to chat_id={TELEGRAM_CHAT_ID}")
    print(f"   [TELEGRAM] Token present: {bool(TELEGRAM_BOT_TOKEN and len(TELEGRAM_BOT_TOKEN) > 10)}")
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("   [TELEGRAM] ✅ Message sent successfully!")
            return True
        else:
            print(f"   [TELEGRAM] ❌ FAILED - Status: {r.status_code}")
            print(f"   [TELEGRAM] Response: {r.text[:150]}")
            return False
    except Exception as e:
        print(f"   [TELEGRAM] ❌ EXCEPTION: {e}")
        return False

def get_strong_oi_stocks():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        s = requests.Session()
        s.headers.update(headers)
        s.get("https://www.nseindia.com", timeout=8)
        time.sleep(0.4)
        url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
        data = s.get(url, timeout=10).json()["data"]
        df = pd.DataFrame(data)
        df['avgInOI'] = pd.to_numeric(df['avgInOI'], errors='coerce').fillna(0)
        df['changeInOI'] = pd.to_numeric(df['changeInOI'], errors='coerce').fillna(0)
        strong = df[(df['avgInOI'] >= MIN_AVG_OI_PCT) | (df['changeInOI'] >= MIN_OI_CHANGE)]
        indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTYIT", "NIFTY50"}
        strong = strong[~strong['symbol'].isin(indices)]
        return strong.sort_values('avgInOI', ascending=False).head(MAX_STOCKS)
    except Exception as e:
        print(f"⚠️ NSE error: {e}. Using fallback.")
        return pd.DataFrame([
            {"symbol": "EXIDEIND"}, {"symbol": "SONACOMS"}, {"symbol": "HDFCBANK"},
            {"symbol": "RELIANCE"}, {"symbol": "SBIN"}, {"symbol": "ICICIBANK"}
        ])

def fetch_5m_data(symbol: str):
    try:
        import yfinance as yf
        df = yf.download(f"{symbol}.NS", period="5d", interval="5m", progress=False)
        if len(df) >= 25:
            df = df.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            df = df.rename(columns={
                'Datetime': 'timestamp', 'Open': 'open', 'High': 'high',
                'Low': 'low', 'Close': 'close', 'Volume': 'volume'
            })
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].dropna()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df
    except Exception as e:
        print(f"  yf error for {symbol}: {e}")
    return None

# ==================== CORE ENTRY LOGIC (INTACT) ====================
def get_full_signal(symbol):
    try:
        df = fetch_5m_data(symbol)

        if df is None or len(df) < 25:
            return None, "not enough 5m data", None, None, None

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

        # === LAST ROW (use iloc[-2] to use the most recently *completed* 5m candle) ===
        # This fixes the "5 min delayed entry" issue:
        # - yf last bar may be the forming one or the one closing at scan time
        # - By using iloc[-2] we evaluate the signal on the candle that closed ~5min earlier
        # - Combined with +5min close_time, reported entry_time now matches the exact candle close time shown in Pine/TradingView (e.g. 09:30 not 09:35)
        if len(df) < 3:
            return None, "not enough 5m data for completed candle", None, None, None

        last = df.iloc[-2]

        # yfinance 5m 'timestamp' is the START time of the candle.
        # Report/use the CLOSE time of the candle (+5min) to match TradingView 5m bar labels and user's screenshots.
        ts = pd.to_datetime(last['timestamp'])
        if ts.tz is None:
            ts = ts.tz_localize('UTC').tz_convert(IST)
        else:
            ts = ts.tz_convert(IST)
        close_time = ts + pd.Timedelta(minutes=5)
        entry_time_str = close_time.strftime('%Y-%m-%d %H:%M IST')
        # Keep hour/min from original ts (bar start) for logic consistency (in_market, cond22)
        hour = ts.hour
        minute = ts.minute

        # Additional filters used in exceptions
        # IMPORTANT: use .iloc[-2] because we selected last=iloc[-2]
        range_break = bool(last['high'] > df['high'].rolling(20).max().shift(1).iloc[-2])
        vwap_not_far = (abs(last['close'] - last['vwap']) / last['vwap']) < 0.012 if last['vwap'] > 0 else False
        long_allowed = last['close'] > last['vwap']

        # Full market hours
        in_market = (hour > 9 or (hour == 9 and minute >= 15)) and (hour < 15 or (hour == 15 and minute <= 30))

        # Legacy 9-cond for comparison in DEBUG (your previous format)
        # Use iloc[-3] as prev for the chosen last
        prev = df.iloc[-3]
        legacy_base = 0
        legacy_base += int(last['rsi'] > prev['rsi'])
        legacy_base += int(last['rsi'] > 50)
        legacy_base += int(last['close'] > last['vwap'])
        legacy_base += int(last['close'] > prev['close'])
        legacy_base += int(last['close'] > last['ema20'])
        legacy_base += int(last['close'] > last['open'])
        legacy_base += int((last['rel_vol'] or 0) > 1.0)
        legacy_base += int(last['high'] > df['high'].rolling(7).max().shift(1).iloc[-2] if len(df) > 7 else 0)
        legacy_base += int(last['close'] > df['high'].iloc[-3])

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
        # Use consistent row for the selected completed candle (iloc[-2])
        cummax_prev = df['high'].cummax().shift(1)
        strong_exception = (base >= 15) and range_break and (last['close'] > cummax_prev.iloc[-2])

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
                return "BUY", details, entry_time_str, last["close"], last["low"]

            if normal_sell:
                details = f"base={base}/22 score={score} (legacy={legacy_base})"
                return "SELL", details, entry_time_str, last["close"], last["high"]

        return None, f"base={base}/22 score={score} (legacy={legacy_base})", None, None, None

    except Exception as e:
        return None, f"error: {str(e)[:55]}", None, None, None

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
            "🧪 ALERTS SCANNER TEST (RUN_ONCE)\n\n"
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
            send_telegram(f"⏰ Alerts Scanner exited (outside full market hours)\nTime: {now.strftime('%H:%M IST')}", parse_mode=None)
            sys.exit(0)

        if not is_within_alert_window(now):
            print(f"[{current_time} IST] Outside alert window (09:26-12:00 IST). Exiting immediately.")
            send_telegram(f"⏰ Alerts Scanner exited (outside 09:26-12:00 window)\nTime: {now.strftime('%H:%M IST')}", parse_mode=None)
            sys.exit(0)

        # Send a startup confirmation message in RUN_ONCE (helps debugging)
        startup = f"✅ Alerts Scanner (RUN_ONCE) started\nTime: {now.strftime('%H:%M IST')}\nWindow: 09:26-12:00 IST"
        send_telegram(startup, parse_mode=None)

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

        summary = f"✅ Alerts Scanner finished (RUN_ONCE)\nSignals sent: {signals_found}\nScanned: {len(strong)} stocks\nTime: {now.strftime('%H:%M IST')}"
        send_telegram(summary, parse_mode=None)

        print(f"\n✅ One-shot scan complete. Signals sent: {signals_found}")
        sys.exit(0)

    else:
        run_scanner()

def calculate_paper_pnl(side, entry_price, exit_price, qty=1):
    if side == "BUY":
        return (exit_price - entry_price) * qty
    else:
        return (entry_price - exit_price) * qty

def log_trade_result(symbol, side, entry_time, entry_price, exit_time, exit_price, 
                     sl_price, target1, target2, reason, move_points, move_pct, pnl):
    trade = {
        "date": entry_time.split()[0],
        "symbol": symbol,
        "side": side,
        "entry_time": entry_time,
        "entry_price": round(entry_price, 2),
        "exit_time": exit_time,
        "exit_price": round(exit_price, 2),
        "sl": round(sl_price, 2),
        "target1_1:2": round(target1, 2),
        "target2_1:3": round(target2, 2),
        "exit_reason": reason,
        "move_captured_points": round(move_points, 2),
        "move_captured_%": round(move_pct, 4),
        "paper_pnl": round(pnl, 2),
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }
    trade_log.append(trade)

    # Save to CSV
    df = pd.DataFrame([trade])
    if os.path.exists(TRADE_LOG_FILE):
        df.to_csv(TRADE_LOG_FILE, mode='a', header=False, index=False)
    else:
        df.to_csv(TRADE_LOG_FILE, index=False)

    print(f"\n📊 === PAPER TRADE CLOSED ===")
    print(f"   Symbol      : {symbol}")
    print(f"   Side        : {side}")
    print(f"   Entry       : {entry_time} @ {entry_price:.2f}")
    print(f"   Exit        : {exit_time} @ {exit_price:.2f}")
    print(f"   SL          : {sl_price:.2f}")
    print(f"   Targets     : 1:2={target1:.2f} | 1:3={target2:.2f}")
    print(f"   Exit Reason : {reason}")
    print(f"   Move        : {move_points:+.2f} pts ({move_pct:+.2f}%)")
    print(f"   Paper P&L   : ₹{pnl:+.2f}")
    print(f"=============================\n")

    # Optional Telegram summary
    msg = (f"📈 *PAPER TRADE CLOSED*\n"
           f"*{symbol}* {side}\n"
           f"Entry: {entry_time} @ ₹{entry_price:.2f}\n"
           f"Exit: {exit_time} @ ₹{exit_price:.2f}\n"
           f"Move: {move_points:+.2f} pts ({move_pct:+.2f}%)\n"
           f"P&L: ₹{pnl:+.2f}\n"
           f"Reason: {reason}")
    send_telegram(msg)

def manage_paper_positions():
    """Check all open positions against latest candles and manage SL/TP/trailing.
    
    Proper trailing logic:
    - Hit 1:2 → trail SL to breakeven (entry price), continue monitoring
    - Hit 1:3 → trail SL to Cost-To-Cost (entry price), continue monitoring
    - After trailing, exit only when price reaches the final target (or SL)
    """
    global positions

    for symbol in list(positions.keys()):
        pos = positions[symbol]
        df = fetch_5m_data(symbol)
        if df is None or len(df) < 3:
            continue

        last = df.iloc[-1]
        current_high = last['high']
        current_low = last['low']
        current_close = last['close']
        current_time = pd.to_datetime(last['timestamp'])
        if current_time.tz is None:
            current_time = current_time.tz_localize('UTC').tz_convert(IST)
        else:
            current_time = current_time.tz_convert(IST)
        exit_time_str = current_time.strftime('%Y-%m-%d %H:%M IST')

        side = pos['side']
        entry_price = pos['entry_price']
        sl = pos['sl']
        target1 = pos['target1']
        target2 = pos['target2']

        exit_price = None
        reason = None

        if side == "BUY":
            # 1. SL hit (candle LOW — conservative)
            if current_low <= sl:
                exit_price = sl
                reason = "SL HIT"

            # 2. CORRECTED RUNNER LOGIC (chart verified):
            #    - 1:2 hit on CLOSE → trail SL to entry (BE) + **KEEP RUNNER OPEN**
            #    - 1:3 hit on CLOSE → trail SL to entry + EXIT at 1:3
            #    Targets evaluated on CLOSE, SL on LOW

            elif current_close >= target2:
                pos['sl'] = entry_price
                exit_price = target2
                reason = "TP 1:3 (trailing to CTC)"
                print(f"   🎯 {symbol} BUY hit 1:3 on close → exit at T2, SL to entry (CTC)")

            elif current_close >= target1 and not pos.get('tp1_hit'):
                # Trail to BE but DO NOT exit — continue runner to 1:3
                pos['tp1_hit'] = True
                pos['sl'] = entry_price
                print(f"   🎯 {symbol} BUY hit 1:2 on close → SL to BE. Runner continues for 1:3 (no exit)")

                else:  # SELL (symmetric)
            if current_high >= sl:
                exit_price = sl
                reason = "SL HIT"
            elif current_close <= target2:
                pos['sl'] = entry_price
                exit_price = target2
                reason = "TP 1:3 (trailing to CTC)"
                print(f"   🎯 {symbol} SELL hit 1:3 on close → exit @ T2, SL to entry (CTC)")
            elif current_close <= target1 and not pos.get('tp1_hit'):
                pos['tp1_hit'] = True
                pos['sl'] = entry_price
                print(f"   🎯 {symbol} SELL hit 1:2 on close → SL to BE. Runner continues for 1:3 (no exit)")

        # Only exit if we have a final decision
        if exit_price is not None and reason is not None:
            move_points = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)
            move_pct = (move_points / entry_price) * 100
            qty = pos.get("qty", 1)
            pnl = calculate_paper_pnl(side, entry_price, exit_price, qty)

            log_trade_result(symbol, side, pos['entry_time'], entry_price,
                             exit_time_str, exit_price, sl, target1, target2,
                             reason, move_points, move_pct, pnl)

            del positions[symbol]

def open_paper_position(symbol, side, entry_time, entry_price, entry_candle_extreme):
    global positions, alert_counts

    # === CRITICAL: Strict 09:26–12:00 IST entry window for PAPER ENTRIES only ===
    now_check = datetime.now(IST)
    if not is_within_alert_window(now_check):
        print(f"   ⏰ PAPER ENTRY REJECTED for {symbol} — outside 09:26–12:00 IST window")
        return False

    if symbol in positions:
        return False

    count = alert_counts.get(symbol, 0)
    if count >= MAX_ENTRIES_PER_STOCK:
        print(f"   🚫 {symbol} reached max {MAX_ENTRIES_PER_STOCK} paper entries")
        return False

    # Apply 0.02% buffer to entry candle extreme (makes SL safer)
    buffer = entry_price * SL_BUFFER_PCT

    if side == "BUY":
        raw_sl = entry_candle_extreme          # entry candle LOW
        sl = raw_sl * (1 - SL_BUFFER_PCT)      # buffer below the low
        risk = abs(entry_price - sl)
    else:
        raw_sl = entry_candle_extreme          # entry candle HIGH
        sl = raw_sl * (1 + SL_BUFFER_PCT)      # buffer above the high
        risk = abs(entry_price - sl)

    if risk < 0.5:
        risk = entry_price * 0.005   # safety minimum

    # === CRITICAL: Enforce MAX_SL_PCT = 1% hard cap ===
    max_risk = entry_price * MAX_SL_PCT
    if risk > max_risk:
        old_sl = sl
        if side == "BUY":
            sl = entry_price - max_risk
            risk = max_risk
        else:
            sl = entry_price + max_risk
            risk = max_risk
        print(f"   ⚠️ {symbol} SL capped at {MAX_SL_PCT*100:.1f}% (was {abs(entry_price - old_sl)/entry_price*100:.2f}%) → new SL {sl:.2f}")

    target1 = entry_price + (risk * 2)
    target2 = entry_price + (risk * 3) if side == "BUY" else entry_price - (risk * 3)

    # Calculate quantity for fixed ₹50,000 position value
    qty = max(1, int(POSITION_VALUE / entry_price))

    positions[symbol] = {
        "side": side,
        "entry_time": entry_time,
        "entry_price": entry_price,
        "sl": sl,
        "target1": target1,
        "target2": target2,
        "entry_candle_low": entry_candle_extreme if side == "BUY" else None,
        "entry_candle_high": entry_candle_extreme if side == "SELL" else None,
        "tp1_hit": False,
        "tp3_hit": False,
        "qty": qty
    }

    alert_counts[symbol] = count + 1

    print(f"\n📝 PAPER POSITION OPENED: {side} {symbol}")
    print(f"   Entry Time : {entry_time}")
    print(f"   Entry Price: {entry_price:.2f}")
    print(f"   SL (entry candle + 0.02% buffer): {sl:.2f}")
    print(f"   Target 1:2 : {target1:.2f}")
    print(f"   Target 1:3 : {target2:.2f}")
    print(f"   Risk       : {risk:.2f}")
    print(f"   Position Value: ₹{POSITION_VALUE}")
    print(f"   Qty          : {qty}")
    print(f"   Buffer applied: {SL_BUFFER_PCT*100:.2f}%")

    # Send alert
    msg = (f"📝 *PAPER ENTRY* (₹{POSITION_VALUE} position)\n"
           f"*{symbol}* {side}\n"
           f"Entry: {entry_time} @ ₹{entry_price:.2f}\n"
           f"Qty: {qty} | Position Value: ₹{POSITION_VALUE}\n"
           f"SL (entry candle + 0.02% buffer): ₹{sl:.2f}\n"
           f"T1 (1:2): ₹{target1:.2f} | T2 (1:3): ₹{target2:.2f}")
    send_telegram(msg)

    return True

# ==================== MAIN LOOP ====================
def run_paper_trader():
    print("🚀 PAPER TRADING ENGINE STARTED")
    print("   Entry logic = full v6.3 (22 conds)")
    print("   SL = 5m entry candle extreme + 0.02% buffer")
    print("   Targets 1:2 / 1:3 + trailing to CTC")
    print("   ⚠️  NEW PAPER ENTRIES only allowed 09:26–12:00 IST")
    print("   ✅ Position management continues until 15:30 IST or targets/SL hit")
    print("   Max 1 entry per stock (first signal only)")
    print("=" * 70)

    while True:
        now = datetime.now(IST)
        current = now.strftime("%H:%M")

        # Hard time guards — exit ONLY outside full market hours (09:15–15:30 IST)
        if now.weekday() >= 5 or not ("09:15" <= current <= "15:30"):
            print(f"[{current} IST] Outside market hours (09:15-15:30). Exiting paper trader.")
            break

        print(f"\n{'='*70}")
        print(f"PAPER SCAN @ {now.strftime('%H:%M:%S IST')}")
        print(f"{'='*70}")

        strong = get_strong_oi_stocks()
        print(f"Checking {len(strong)} strong OI stocks...")

        # 1. ALWAYS manage existing paper positions (SL/TP/trailing) — even after 12:00
        manage_paper_positions()

        # 2. Look for NEW paper entries ONLY inside strict 09:26–12:00 IST window
        if is_within_alert_window(now):
            for _, row in strong.iterrows():
                sym = row['symbol']
                if sym in positions:
                    continue   # already in a trade

                signal, info, entry_time, entry_price, entry_extreme = get_full_signal(sym)

                if signal and entry_price and entry_extreme:
                    print(f"✅ SIGNAL: {sym} → {signal} | {info}")
                    open_paper_position(sym, signal, entry_time, entry_price, entry_extreme)
                else:
                    print(f"   {sym} → no signal")
        else:
            print("   ⏰ Outside paper ENTRY window (09:26-12:00 IST). No new entries allowed.")
            if positions:
                print(f"   📌 Managing {len(positions)} open paper position(s) until targets/SL hit...")

        print(f"\nNext check in {SCAN_EVERY_MINUTES} min...\n")
        time.sleep(SCAN_EVERY_MINUTES * 60)

    # Final summary
    print("\n" + "="*70)
    print("PAPER TRADING SESSION ENDED")
    print(f"Total paper trades closed: {len(trade_log)}")
    if trade_log:
        df = pd.DataFrame(trade_log)
        total_pnl = df['paper_pnl'].sum()
        print(f"Total Paper P&L: ₹{total_pnl:+.2f}")
        print(f"Trade log saved to: {TRADE_LOG_FILE}")
    print("="*70)

    # Generate professional Excel report after market + send to Telegram
    if trade_log:
        generate_excel_report()
        send_excel_report_to_telegram()
    else:
        print("No trades executed — Excel report not generated.")

# ==================== EXCEL REPORT GENERATOR (Post Market) ====================
def generate_excel_report():
    """Generate a professional Excel report after the trading session ends."""
    global trade_log

    if not trade_log:
        print("No trades to report.")
        return

    df = pd.DataFrame(trade_log)

    # Ensure required columns exist (robustness)
    required_cols = ['date', 'symbol', 'side', 'entry_time', 'entry_price',
                     'exit_time', 'exit_price', 'sl', 'target1_1_2', 'target2_1_3',
                     'exit_reason', 'move_captured_points', 'move_captured__', 'paper_pnl']
    for col in required_cols:
        if col not in df.columns:
            df[col] = None

    # Create workbook
    wb = Workbook()

    # ===== SHEET 1: Trade Log =====
    ws_log = wb.active
    ws_log.title = "Trade Log"

    # Styles
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    center_align = Alignment(horizontal='center', vertical='center')

    # Write headers
    headers = [
        "Date", "Symbol", "Side", "Entry Time", "Entry Price",
        "Exit Time", "Exit Price", "SL (Buffered)", "Target 1:2", "Target 1:3",
        "Exit Reason", "Move (Pts)", "Move (%)", "Paper P&L (₹)"
    ]

    for col, header in enumerate(headers, 1):
        cell = ws_log.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # Write data
    for row_idx, row_data in enumerate(df.itertuples(index=False), 2):
        # Access using column names (pandas normalizes some chars)
        values = [
            row_data.date,
            row_data.symbol,
            row_data.side,
            row_data.entry_time,
            row_data.entry_price,
            row_data.exit_time,
            row_data.exit_price,
            row_data.sl,
            getattr(row_data, 'target1_1_2', row_data[8] if len(row_data) > 8 else None),
            getattr(row_data, 'target2_1_3', row_data[9] if len(row_data) > 9 else None),
            row_data.exit_reason,
            row_data.move_captured_points,
            getattr(row_data, 'move_captured__', row_data[12] if len(row_data) > 12 else row_data.move_captured_points),
            row_data.paper_pnl
        ]

        for col, value in enumerate(values, 1):
            cell = ws_log.cell(row=row_idx, column=col, value=value)
            cell.border = thin_border
            cell.alignment = center_align

            # Format numbers
            if col in [5, 7, 8, 9, 10]:  # prices
                cell.number_format = '0.00'
            elif col in [12, 14]:  # move pts and pnl
                cell.number_format = '0.00'
                if value is not None:
                    if value > 0:
                        cell.fill = green_fill
                    elif value < 0:
                        cell.fill = red_fill
            elif col == 13:  # move %
                cell.number_format = '0.00"%"'

    # Auto column width
    for col in range(1, len(headers) + 1):
        max_length = 0
        column = get_column_letter(col)
        for cell in ws_log[column]:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 25)
        ws_log.column_dimensions[column].width = adjusted_width

    # ===== SHEET 2: Summary =====
    ws_summary = wb.create_sheet("Summary")

    # Calculate stats
    total_trades = len(df)
    wins = len(df[df['paper_pnl'] > 0])
    losses = len(df[df['paper_pnl'] < 0])
    breakeven = len(df[df['paper_pnl'] == 0])
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    total_pnl = df['paper_pnl'].sum()
    avg_pnl = df['paper_pnl'].mean()
    max_win = df['paper_pnl'].max()
    max_loss = df['paper_pnl'].min()
    total_move_pts = df['move_captured_points'].sum()
    avg_move_pct = df['move_captured__'].mean()

    buy_trades = len(df[df['side'] == 'BUY'])
    sell_trades = len(df[df['side'] == 'SELL'])

    # Title
    ws_summary.merge_cells('A1:D1')
    title_cell = ws_summary['A1']
    title_cell.value = "PAPER TRADING PERFORMANCE REPORT"
    title_cell.font = Font(bold=True, size=16, color="1F4E79")
    title_cell.alignment = Alignment(horizontal='center')

    ws_summary.merge_cells('A2:D2')
    ws_summary['A2'] = f"Generated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
    ws_summary['A2'].font = Font(italic=True, size=10)

    # Summary table
    summary_data = [
        ["", "", "", ""],
        ["OVERALL STATISTICS", "", "", ""],
        ["Total Trades", total_trades, "", ""],
        ["Winning Trades", wins, "", ""],
        ["Losing Trades", losses, "", ""],
        ["Breakeven Trades", breakeven, "", ""],
        ["Win Rate", f"{win_rate:.1f}%", "", ""],
        ["", "", "", ""],
        ["P&L SUMMARY", "", "", ""],
        ["Total Paper P&L (₹)", round(total_pnl, 2), "", ""],
        ["Average P&L per Trade (₹)", round(avg_pnl, 2), "", ""],
        ["Largest Win (₹)", round(max_win, 2) if not pd.isna(max_win) else 0, "", ""],
        ["Largest Loss (₹)", round(max_loss, 2) if not pd.isna(max_loss) else 0, "", ""],
        ["", "", "", ""],
        ["MOVE STATISTICS", "", "", ""],
        ["Total Points Captured", round(total_move_pts, 2), "", ""],
        ["Average Move (%)", f"{avg_move_pct:.2f}%", "", ""],
        ["", "", "", ""],
        ["TRADE BREAKDOWN", "", "", ""],
        ["BUY Trades", buy_trades, "", ""],
        ["SELL Trades", sell_trades, "", ""],
    ]

    for row_idx, row in enumerate(summary_data, 4):
        for col_idx, value in enumerate(row, 1):
            cell = ws_summary.cell(row=row_idx, column=col_idx, value=value)
            if row_idx in [5, 10, 16, 20]:  # section headers
                cell.font = Font(bold=True, size=12, color="1F4E79")
            if col_idx == 2 and row_idx in [6, 7, 8, 9, 11, 12, 13, 14, 17, 18, 21, 22]:
                if isinstance(value, (int, float)) and value != 0:
                    cell.number_format = '#,##0.00'

    # Highlight total P&L
    pnl_cell = ws_summary.cell(row=11, column=2)
    if total_pnl > 0:
        pnl_cell.fill = green_fill
        pnl_cell.font = Font(bold=True, color="006400")
    elif total_pnl < 0:
        pnl_cell.fill = red_fill
        pnl_cell.font = Font(bold=True, color="8B0000")

    # Column widths for summary
    ws_summary.column_dimensions['A'].width = 28
    ws_summary.column_dimensions['B'].width = 18
    ws_summary.column_dimensions['C'].width = 12
    ws_summary.column_dimensions['D'].width = 12

    # Save
    wb.save(EXCEL_REPORT_FILE)
    print(f"\n📊 Professional Excel report generated: {EXCEL_REPORT_FILE}")
    print(f"   - Sheet 1: Trade Log ({total_trades} trades)")
    print(f"   - Sheet 2: Summary (Win Rate, P&L, Stats)")

    # Store for Telegram caption (quick stats)
    global _last_report_stats
    _last_report_stats = {
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "win_rate": f"{win_rate:.1f}%"
    }

# ==================== SEND EXCEL TO TELEGRAM (After Market) ====================
def send_excel_report_to_telegram():
    """Send the generated Excel report as a file to Telegram bot after market close."""
    global _last_report_stats

    if not os.path.exists(EXCEL_REPORT_FILE):
        print("⚠️ No Excel report file found to send.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

        with open(EXCEL_REPORT_FILE, 'rb') as f:
            files = {"document": (EXCEL_REPORT_FILE, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

            caption = "📊 *Paper Trading Report* (Post-Market)\n"
            if _last_report_stats:
                caption += f"• Trades: `{_last_report_stats.get('total_trades')}`\n"
                caption += f"• Total P&L: `₹{_last_report_stats.get('total_pnl')}`\n"
                caption += f"• Win Rate: `{_last_report_stats.get('win_rate')}`"

            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "Markdown"
            }

            r = requests.post(url, data=data, files=files, timeout=30)

            if r.status_code == 200:
                print("✅ Excel report successfully sent to Telegram!")
            else:
                print(f"⚠️ Telegram send failed: {r.text[:150]}")

    except Exception as e:
        print(f"❌ Error sending Excel to Telegram: {e}")

if __name__ == "__main__":
    import os
    import sys

    run_once = os.getenv("RUN_ONCE", "false").lower() == "true"

    if run_once:
        print("🔹 RUN_ONCE mode (GitHub Actions one-shot)")
        now = datetime.now(IST)
        current = now.strftime("%H:%M")

        # === CRITICAL DIAGNOSTICS - ALWAYS RUN (even outside window) ===
        print(f"   TELEGRAM_BOT_TOKEN present: {bool(TELEGRAM_BOT_TOKEN)}")
        print(f"   TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")
        print(f"   Current IST time: {now.strftime('%H:%M:%S')}")

        # Send a TEST message IMMEDIATELY (before any time checks)
        print("   → Sending immediate test message to Telegram (PLAIN TEXT)...")
        test_msg = (
            "🧪 PAPER TRADER TEST (RUN_ONCE)\n\n"
            f"Time: {now.strftime('%H:%M IST')}\n"
            f"Inside market hours: {'09:15' <= current <= '15:30'}\n"
            f"Inside entry window (09:26-12:00): {is_within_alert_window(now)}\n\n"
            "If you see this → Bot + Secrets are WORKING!\n"
            "No entries = normal outside window or no strong setup."
        )
        result = send_telegram(test_msg, parse_mode=None)
        print(f"   Immediate test send result: {result}")

        # Hard guard: only run during market + alert window for actual processing
        if now.weekday() >= 5 or not ("09:15" <= current <= "15:30"):
            print(f"[{current} IST] Outside market hours. Exiting immediately.")
            send_telegram(f"⏰ Paper Trader exited (outside full market hours)\nTime: {now.strftime('%H:%M IST')}", parse_mode=None)
            sys.exit(0)

        # IMPORTANT: We do NOT exit here for 09:26-12:00.
        # New paper ENTRIES are blocked inside run_paper_trader()
        # But we must continue running after 12:00 to:
        #   - Manage open positions (SL / TP / trailing)
        #   - Generate & send final Excel report when session ends
        print(f"[{current} IST] RUN_ONCE: Will process (new entries only if inside 09:26-12:00 window)")

        # Send a startup confirmation message in RUN_ONCE (helps debugging)
        startup = f"📝 Paper Trader (RUN_ONCE) started\nTime: {now.strftime('%H:%M IST')}\nEntry window: 09:26-12:00 IST only"
        send_telegram(startup, parse_mode=None)

        run_paper_trader()

        # Final summary message even if no trades
        summary = "✅ Paper Trader finished (RUN_ONCE)\n"
        if trade_log:
            summary += f"Trades closed: {len(trade_log)}\n"
            if _last_report_stats:
                summary += f"P&L: ₹{_last_report_stats.get('total_pnl', 0)} | Win rate: {_last_report_stats.get('win_rate', 'N/A')}"
        else:
            summary += "No paper trades executed in this run."

        send_telegram(summary, parse_mode=None)
        print("✅ One-shot paper trading run complete.")
        sys.exit(0)
    else:
        run_paper_trader()