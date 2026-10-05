import urllib.request
import urllib.parse
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import traceback

# ==================== CONFIGURATION ====================
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"

BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk" # Apni BotFather wali Bot Token yahan confirm karein
CHAT_ID = "1264164655"                             # Aapki Personal Telegram User ID
# =======================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HYPER QUANTUM ENGINE v4 LIVE")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'Markdown'
        }).encode('utf-8')
        
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.read()
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

def fetch_api_data():
    try:
        req = urllib.request.Request(
            API_URL, 
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
    except Exception as e:
        print(f"⚠️ API Fetch Error: {e}")
        return None

def analyze_quantum_v4_engine(history_list):
    """
    Super Advanced Prediction Engine:
    1. Pattern Recognition (Streak vs Alternate)
    2. Last 10 Frequency Weightage
    3. Quantum Reversal Calculation
    """
    if not history_list or len(history_list) < 5:
        return "BIG", "STANDARD", "85%"

    sizes = [item.get("size", "big").upper() for item in history_list[:10]]
    
    # 1. Streak Detection (Dragon Pattern)
    if sizes[0] == sizes[1] == sizes[2]:
        prediction = sizes[0]
        pattern_type = "DRAGON STREAK"
        confidence = "95%"
    # 2. Alternating Pattern (B, S, B, S)
    elif sizes[0] != sizes[1] and sizes[1] != sizes[2]:
        prediction = "SMALL" if sizes[0] == "BIG" else "BIG"
        pattern_type = "ZIG-ZAG ALTERNATE"
        confidence = "92%"
    # 3. Frequency & Reversal Weightage
    else:
        big_count = sizes.count("BIG")
        small_count = sizes.count("SMALL")
        
        if big_count > small_count:
            prediction = "SMALL"  # Mean Reversal Logic
            pattern_type = "QUANTUM REVERSAL"
            confidence = "88%"
        else:
            prediction = "BIG"
            pattern_type = "QUANTUM REVERSAL"
            confidence = "88%"

    return prediction, pattern_type, confidence

def main():
    print("🚀 Bot starting main loop...")
    send_telegram("🚀 *HYPER QUANTUM ENGINE v4 ONLINE!*")
    
    last_period = None
    current_level = 1

    while True:
        try:
            data = fetch_api_data()
            
            if data and data.get("code") == 0:
                issue_list = data.get("data", {}).get("list", [])
                
                if issue_list:
                    latest_item = issue_list[0]
                    latest_period = str(latest_item.get("issueNumber", ""))
                    
                    # Next Period Calculate Karo
                    next_period = str(int(latest_period) + 1) if latest_period.isdigit() else "NEXT"
                    
                    if latest_period and latest_period != last_period:
                        last_period = latest_period
                        
                        # Quantum AI Prediction Generate Karo
                        pred_choice, pattern, confidence = analyze_quantum_v4_engine(issue_list)
                        
                        # History Logs Text Construct Karo
                        history_text = ""
                        for item in issue_list[:5]:
                            p_num = str(item.get("issueNumber", ""))[-3:]
                            p_sz = str(item.get("size", "")).upper()
                            p_num_val = item.get("number", "")
                            history_text += f"• `{p_num}` ➔ *{p_sz}* ({p_num_val})\n"

                        pred_msg = (
                            f"🔥 *HYPER ADVANCED PREDICTION*\n"
                            f"📌 *Period:* `{next_period}`\n"
                            f"🎯 *Size:* *{pred_choice}*\n"
                            f"📊 *Confidence:* `{confidence}`\n"
                            f"🚩 *Level:* `LEVEL {current_level}`\n"
                            f"🔍 *Pattern Type:* `{pattern}`\n"
                            f"------------------------------------\n"
                            f"📜 *RECENT HISTORY LOGS:*\n"
                            f"{history_text}"
                            f"------------------------------------"
                        )
                        send_telegram(pred_msg)
                        print(f"✅ [{next_period}] Sent Prediction Successfully!")
            else:
                print("⚠️ Retrying API fetch...")

        except Exception as e:
            print(f"❌ Loop Exception: {e}")
            traceback.print_exc()
        
        time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    main()


  
