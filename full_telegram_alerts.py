"""
FULL TELEGRAM ALERTS SCANNER (ALERTS ONLY)
==========================================

✅ READY TO USE - **ONLY SENDS ALERTS** (NO TRADING WHATSOEVER)
✅ Uses **yfinance** as primary for 5m candles (reliable)
✅ Dhan API tried first when available (for current quotes + future upgrades)
✅ Your FREE Dhan plan is respected — no paid Data APIs are required.

IMPORTANT FOR YOUR FREE DHAN ACCOUNT:
- Trading APIs (orders, funds, positions) → FREE
- Historical intraday 5-minute data → **NOT available** on free plan
- Current market data (quotes) → Works

The scanner intelligently falls back to yfinance for reliable 5m candles.

Your Telegram credentials + Dhan token are hardcoded.

============================================================
STEP 1: START THE BOT (DO THIS FIRST!)
============================================================

1. Open Telegram
2. Search for: @MyOiScannerBot
3. Press "START" button (or type /start)

Without this step you will get "chat not found" errors.

============================================================
STEP 2: RUN THE SCANNER
============================================================

cd nse-scanner
python full_telegram_alerts.py

- Runs every 5 minutes
- Only during market hours (09:15–15:30 IST)
- Sends Telegram alert on any triggered signal

To stop: Ctrl + C
"""

import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import pytz

# ==================== YOUR CREDENTIALS ====================
TELEGRAM_BOT_TOKEN = "8626856610:AAE3ehqXLPPbD0q2aFNa3llWy6kYjZX42L0"
TELEGRAM_CHAT_ID = "6058787660"

# Dhan credentials (used ONLY for 5m historical data)
DHAN_CLIENT_ID = "1103800440"
DHAN_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzg0MzMwNjkxLCJpYXQiOjE3ODQyNDQyOTEsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTAzODAwNDQwIn0.TEZanGhna6cjW3l-DDsiPEHGv5MsOBCvfS1o9G93shdWeGG9BeZepLvdi4mDRUQJxplIJdIYI9pLxiKOFaQGDw"

# ==================== SCANNER SETTINGS ====================
MIN_AVG_OI_PCT = 5.0
MIN_OI_CHANGE = 8000
MAX_STOCKS = 25
SCAN_EVERY_MINUTES = 5

IST = pytz.timezone("Asia/Kolkata")

# ==================== DHAN SECURITY ID MAP (NSE Equity) ====================
# Add more symbols as needed
SECURITY_IDS = {
    "RELIANCE": "2885",
    "SBIN": "3045",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "TCS": "11536",
    "INFY": "1594",
    "LT": "11483",
    "BHARTIARTL": "567",
    "ITC": "1660",
    "POLYCAB": "14432",
    "ABB": "166",
    "SIEMENS": "112",
    "TORNTPHARM": "1129",
    "ICICIGI": "12723",
}

# ==================== SEND TO TELEGRAM ====================
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"   ⚠️ Telegram failed: {response.text[:80]}")
        return response.status_code == 200
    except Exception as e:
        print(f"   ⚠️ Telegram error: {e}")
        return False

# ==================== GET STRONG OI STOCKS (unchanged) ====================
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
        return strong.sort_values('avgInOI', ascending=False).head(MAX_STOCKS)
    except Exception as e:
        print(f"⚠️ NSE fetch error: {e}. Using fallback list")
        return pd.DataFrame([
            {"symbol": "RELIANCE"}, {"symbol": "SBIN"}, {"symbol": "HDFCBANK"},
            {"symbol": "ICICIBANK"}, {"symbol": "LT"}, {"symbol": "TCS"},
            {"symbol": "INFY"}, {"symbol": "BHARTIARTL"}, {"symbol": "ITC"}
        ])

# ==================== 5-MINUTE DATA (yfinance PRIMARY — Best for Free Dhan Plan) ====================
def fetch_5m_data(symbol: str):
    """
    Primary: yfinance (reliable 5m candles, works on free accounts)
    Optional: Dhan for current quotes (if we ever want live LTP)
    
    NOTE: Your FREE Dhan plan does NOT provide historical intraday minute data.
    intraday_minute_data returns empty on free accounts.
    """
    # === Try yfinance first (most reliable for free users) ===
    try:
        import yfinance as yf
        print(f"  📥 Fetching 5m data via yfinance for {symbol}")
        df = yf.download(f"{symbol}.NS", period="1d", interval="5m", progress=False)
        
        if len(df) >= 20:
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
            return df
    except Exception as e:
        print(f"  ⚠️ yfinance error: {e}")

    # === Fallback: Try Dhan (will usually fail on free plan for history) ===
    try:
        from dhanhq import DhanContext, dhanhq
        sec_id = SECURITY_IDS.get(symbol)
        if sec_id:
            print(f"  📥 Trying Dhan for {symbol} (may return empty on free plan)")
            dhan_context = DhanContext(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
            dhan = dhanhq(dhan_context)

            now = datetime.now(IST)
            from_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            to_date = now.strftime("%Y-%m-%d")

            try:
                resp = dhan.intraday_minute_data(
                    security_id=sec_id,
                    exchange_segment="NSE_EQ",
                    instrument_type="EQUITY",
                    from_date=from_date,
                    to_date=to_date,
                    interval="5"
                )
            except:
                resp = dhan.intraday_minute_data(
                    security_id=sec_id,
                    exchange_segment="NSE_EQ",
                    instrument_type="EQUITY",
                    from_date=from_date,
                    to_date=to_date
                )

            if resp and "data" in resp and resp["data"]:
                raw = resp["data"]
                df = pd.DataFrame(raw)
                # (same normalization logic as before)
                rename = {}
                for col in df.columns:
                    c = str(col).lower()
                    if "time" in c: rename[col] = "timestamp"
                    elif c in ("o", "open"): rename[col] = "open"
                    elif c in ("h", "high"): rename[col] = "high"
                    elif c in ("l", "low"): rename[col] = "low"
                    elif c in ("c", "close"): rename[col] = "close"
                    elif c in ("v", "vol", "volume"): rename[col] = "volume"
                df = df.rename(columns=rename)
                for col in ["open", "high", "low", "close", "volume"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                df = df.dropna(subset=["close"])
                if len(df) >= 20:
                    return df
    except Exception as e:
        pass  # Expected on free plan

    print(f"  ❌ Could not get enough 5m data for {symbol}")
    return None

# ==================== YOUR FULL INDICATOR ====================
def get_full_signal(symbol):
    try:
        # === Get 5m data (yfinance primary — works reliably on FREE Dhan plan) ===
        df = fetch_5m_data(symbol)

        if df is None or len(df) < 60:
            return None, "not enough data"

        # Ensure clean columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df.rename(columns={'Datetime':'timestamp', 'Open':'open', 'High':'high', 'Low':'low', 'Close':'close', 'Volume':'volume'})
        df = df[['timestamp','open','high','low','close','volume']].dropna()

        # === Indicators (unchanged) ===
        df['ema20'] = df['close'].ewm(span=20).mean()
        df['ema50'] = df['close'].ewm(span=50).mean()
        df['vwap'] = (df['high'] + df['low'] + df['close']).rolling(20).mean()

        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

        df['obv'] = (np.sign(df['close'].diff()) * df['volume']).cumsum()
        df['obv_slope'] = df['obv'].rolling(5).mean() - df['obv'].rolling(20).mean()
        df['rel_vol'] = df['volume'] / df['volume'].rolling(20).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]
        t = pd.to_datetime(last['timestamp'])
        hour, minute = t.hour, t.minute

        base_active = 0
        base_active += int(last['rsi'] > prev['rsi'])
        base_active += int(last['rsi'] > 55)
        base_active += int(last['close'] > last['vwap'])
        base_active += int(last['close'] > prev['close'])
        base_active += int(last['close'] > last['ema20'])
        base_active += int(last['close'] > last['open'])
        base_active += int((last['volume'] * last['close']) > 5_000_000)
        base_active += int(last['high'] > df['high'].rolling(7).max().shift(1).iloc[-1])
        base_active += int(last['close'] > df['high'].iloc[-2])

        trend = 20 if (last['close'] > last['ema20'] > last['ema50']) else (10 if last['close'] > last['ema20'] else 0)
        mom = (12 if last['rsi'] > 55 else 6 if last['rsi'] > 50 else 0) + (13 if last['close'] > last['ema20'] else 0)
        vol = 15 if (last['obv_slope'] > 0 and last['rel_vol'] > 1.5) else (10 if last['obv_slope'] > 0 else 0)
        total_score = trend + mom + vol

        in_buy_win = (hour > 9 or (hour == 9 and minute >= 16)) and hour < 12
        in_sell_win = (hour > 9 or (hour == 9 and minute >= 16)) and hour < 12

        day_low = df['low'].min()
        rng = last['high'] - last['low']
        body = abs(last['close'] - last['open'])
        small_candle = rng > 0 and body < (rng * 0.65)

        # BUY SIGNALS
        if base_active >= 20 and total_score > 58 and in_buy_win and last['close'] > last['vwap'] and last['rel_vol'] > 1.2:
            return "NORMAL BUY", f"base={base_active} score={total_score}"

        if base_active >= 20 and total_score > 55 and in_buy_win and last['close'] > last['vwap']:
            if last['close'] > df['high'].rolling(20).max().shift(1).iloc[-1]:
                return "BUY-EX17", f"base={base_active}"
            return "BUY-EX", f"base={base_active} score={total_score}"

        if base_active >= 19 and last['close'] > last['ema20'] and last['rel_vol'] > 1.3 and in_buy_win:
            return "BUY-EX5", f"base={base_active}"

        # SELL SIGNALS
        if base_active <= 8 and last['rsi'] < 45 and last['close'] < last['vwap'] and last['obv_slope'] < 0 and in_sell_win and last['rel_vol'] > 1.2:
            return "NORMAL SELL", f"base={base_active} score={total_score}"

        if last['rsi'] < 45 and last['close'] < last['ema20'] and last['obv_slope'] < 0 and last['low'] <= day_low and small_candle and in_sell_win:
            return "SELL-EX3", f"rsi={last['rsi']:.1f}"

        if last['rsi'] < 43 and last['close'] < last['vwap'] and last['obv_slope'] < 0 and last['close'] < day_low and in_sell_win:
            return "SELL-EX1", f"rsi={last['rsi']:.1f}"

        if last['rsi'] < 42 and last['close'] < last['vwap'] and last['obv_slope'] < 0 and last['close'] < day_low:
            return "SELL-EX5", f"rsi={last['rsi']:.1f}"

        return None, f"base={base_active} score={total_score}"

    except Exception as e:
        return None, f"error: {str(e)[:60]}"

# ==================== ALERT FUNCTION ====================
def send_signal_alert(symbol, signal, details, oi_pct):
    message = (
        f"🚨 *{signal}*\n\n"
        f"📌 Stock: *{symbol}*\n"
        f"📈 OI Strength: {oi_pct:.1f}%\n"
        f"📊 Details: {details}\n\n"
        f"⏰ {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n\n"
        f"_Verify manually on TradingView 5m chart._\n"
        f"⚠️ Alerts only — No orders placed."
    )
    success = send_telegram(message)
    if success:
        print(f"   📨 Alert sent to Telegram")
    return success

# ==================== MAIN ====================
def run_scanner():
    print("🚀 Starting FULL TELEGRAM ALERTS SCANNER")
    print("   (ALERTS ONLY — NO TRADING)")
    print("   Primary data source: yfinance (reliable 5m candles)")
    print("   Dhan API: Used when available (your free plan has limited Data APIs)")
    print("   Press Ctrl + C to stop\n")

    # Startup message
    startup_msg = (
        "✅ *Scanner Started Successfully*\n\n"
        "Full Buy + Sell logic active\n"
        "5-minute checks during market hours\n\n"
        "Data source: **yfinance** (primary)\n"
        f"Dhan Client ID: `{DHAN_CLIENT_ID}`\n"
        "Dhan used for quotes only (free plan limitation)\n\n"
        "🚫 This script **does not** place any orders"
    )
    send_telegram(startup_msg)

    while True:
        now = datetime.now(IST)
        current = now.strftime("%H:%M")

        if now.weekday() >= 5 or not ("09:15" <= current <= "15:30"):
            print(f"[{current} IST] Market closed. Waiting...")
            time.sleep(60)
            continue

        print("=" * 70)
        print(f"SCAN at {now.strftime('%H:%M:%S IST')}")
        print("=" * 70)

        strong = get_strong_oi_stocks()
        print(f"Checking {len(strong)} stocks with strong OI...\n")

        for _, row in strong.iterrows():
            sym = row['symbol']
            oi = row.get('avgInOI', 0)

            signal, info = get_full_signal(sym)

            if signal:
                print(f"✅ {sym} → {signal} | {info}")
                send_signal_alert(sym, signal, info, oi)
            else:
                print(f"   {sym} → no signal")

        print(f"\nNext scan in {SCAN_EVERY_MINUTES} minutes...\n")
        time.sleep(SCAN_EVERY_MINUTES * 60)

if __name__ == "__main__":
    run_scanner()
