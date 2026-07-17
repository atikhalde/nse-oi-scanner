"""
FORCE TELEGRAM TEST
====================
Run this to FORCE a message from GitHub Actions even outside market hours.

Usage in GitHub:
- Temporarily change the workflow to run this file instead of the main scanner.
- Or run it locally.

This will prove whether secrets + bot connection actually work.
"""

import os
import requests
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

# These will come from GitHub Secrets if set, otherwise hardcoded fallback
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8626856610:AAE3ehqXLPPbD0q2aFNa3llWy6kYjZX42L0")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6058787660")

def force_send():
    now = datetime.now(IST)
    
    message = (
        "🚨 *FORCE TELEGRAM TEST* 🚨\n\n"
        f"Time (IST): {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Token present: {bool(TELEGRAM_BOT_TOKEN)}\n"
        f"Chat ID: {TELEGRAM_CHAT_ID}\n\n"
        "✅ If you received this message, your Telegram bot and GitHub secrets are **working correctly**.\n\n"
        "Next: Check the full logs of the Combined workflow for more details.\n"
        "If no signals appear → it's because we are outside the 09:26-12:00 IST window."
    )
    
    print("=== SENDING FORCE TELEGRAM MESSAGE ===")
    print(f"Bot Token (first 15 chars): {TELEGRAM_BOT_TOKEN[:15]}...")
    print(f"Chat ID: {TELEGRAM_CHAT_ID}")
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        r = requests.post(url, json=payload, timeout=15)
        print(f"HTTP Status: {r.status_code}")
        print(f"Response: {r.text[:300]}")
        
        if r.status_code == 200:
            print("\n✅✅✅ SUCCESS! Message was accepted by Telegram.")
            print("Check your Telegram chat RIGHT NOW.")
        else:
            print("\n❌ FAILED to send. See response above.")
    except Exception as e:
        print(f"❌ EXCEPTION: {e}")

if __name__ == "__main__":
    force_send()