import urllib.request
import urllib.parse
import json
import time
import math
from collections import Counter
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os

# ==================== CONFIGURATION ====================
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"

BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655"
WIN_STICKER_ID = "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ" 
# =======================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HYPER QUANTUM ENGINE V4 ACTIVE")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as res: pass
    except Exception as e: print(f"❌ Telegram Error: {e}")

def send_win_sticker():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    payload = json.dumps({"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=8) as res: pass
    except Exception: send_telegram("🎉🥳 **WINNER WINNER BIG WIN!** 🏆🔥")

def fetch_data():
    req = urllib.request.Request(API_URL, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get("code") == 0 and "data" in data and "list" in data["data"]:
                return data["data"]["list"]
    except Exception: return None

def analyze_quantum_v4_engine(history_list):
    outcomes_size = [1 if item["size"].lower() == "big" else 0 for item in history_list]
    outcomes_num = [int(item["number"]) for item in history_list]
    
    total_len = len(outcomes_size)
    if total_len < 30: return None

    last_5 = outcomes_size[-5:]
    is_dragon = (sum(last_5) == 5 or sum(last_5) == 0)
    is_zigzag = all(last_5[i] != last_5[i+1] for i in range(len(last_5)-1))

    markov_score_big, markov_total = 0.0, 0.0
    last_state = outcomes_size[-2:]
    for i in range(total_len - 2):
        if outcomes_size[i:i+2] == last_state:
            weight = math.exp((i / total_len) * 1.5)
            markov_total += weight
            if outcomes_size[i+2] == 1:
                markov_score_big += weight

    markov_prob = (markov_score_big / markov_total) if markov_total > 0 else 0.50

    matched_len = 0
    pattern_score_big, pattern_total = 0.0, 0.0
    matched_nums = []

    for p_len in range(10, 2, -1):
        target_pattern = outcomes_size[-p_len:]
        for i in range(total_len - p_len):
            if outcomes_size[i : i + p_len] == target_pattern:
                next_size = outcomes_size[i + p_len]
                next_num = outcomes_num[i + p_len]
                
                weight = math.exp((i / total_len) * 2.0)
                pattern_total += weight
                if next_size == 1:
                    pattern_score_big += weight
                
                matched_nums.append(next_num)
                matched_len = p_len
        
        if pattern_total > 0:
            break

    pattern_prob = (pattern_score_big / pattern_total) if pattern_total > 0 else 0.50
    final_prob_big = (0.60 * pattern_prob) + (0.40 * markov_prob)

    if is_dragon:
        pattern_desc = "QUANTUM DRAGON TREND"
        final_prob_big = 0.72 if last_5[-1] == 1 else 0.28
    elif is_zigzag:
        pattern_desc = "ZIG-ZAG CYCLE (1-BY-1)"
        final_prob_big = 0.28 if last_5[-1] == 1 else 0.72
    else:
        pattern_desc = f"DEPTH MATCH ({matched_len} ROUNDS)" if matched_len > 0 else "MARKOV MATRIX ENGINE"

    if final_prob_big >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        real_confidence = final_prob_big * 100
        valid_nums = [n for n in matched_nums if n >= 5] if matched_nums else [5, 6, 7, 8, 9]
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        real_confidence = (1.0 - final_prob_big) * 100
        valid_nums = [n for n in matched_nums if n <= 4] if matched_nums else [0, 1, 2, 3, 4]

    top_num = Counter(valid_nums).most_common(1)[0][0] if valid_nums else (7 if "BIG" in pred_size else 2)
    last_issue = int(history_list[-1]["issueNumber"])

    return {
        "last_issue": last_issue,
        "next_issue": last_issue + 1,
        "pred_size": pred_size,
        "pred_size_emoji": pred_size_emoji,
        "pred_num": top_num,
        "confidence": real_confidence,
        "pattern_desc": pattern_desc
    }

def format_history_logs(history_records):
    logs_text = ""
    recent_8 = history_records[-8:]
    for item in recent_8:
        short_period = str(item["period"])[-2:]
        size_str = "BIGGG" if item["size"].upper() in ["BIG", "BIGGG"] else "SMALL"
        status_str = "✅✅✅" if item["status"] == "WIN" else ""
        logs_text += f"`{short_period}` *{size_str}* {status_str}\n"
    return logs_text

def start_hyper_bot():
    print("🚀 HYPER ADVANCED QUANTUM V4 ENGINE RUNNING!")
    current_level = 1
    pending_pred = None
    history_records = []
    
    while True:
        raw_list = fetch_data()
        if raw_list:
            history = list(reversed(raw_list))
            last_item = history[-1]
            last_issue = int(last_item["issueNumber"])
            actual_size = "BIGGG" if last_item["size"].upper() == "BIG" else "SMALL"

            if pending_pred and pending_pred["next_issue"] == last_issue:
                is_win = (actual_size == pending_pred["pred_size"])
                
                history_records.append({
                    "period": last_issue,
                    "size": actual_size,
                    "status": "WIN" if is_win else "LOSS"
                })

                if is_win:
                    send_win_sticker()
                    current_level = 1
                else:
                    current_level += 1
                    if current_level > 3: current_level = 1

                pending_pred = None

            if not pending_pred or pending_pred["last_issue"] != last_issue:
                pred_data = analyze_quantum_v4_engine(history)
                if pred_data:
                    pending_pred = pred_data
                    
                    history_block = format_history_logs(history_records) if history_records else "Waiting for history logs..."

                    pred_msg = (
                        f"🔥 *HYPER ADVANCED PREDICTION* 🔥\n\n"
                        f"📌 *Period:* `{pred_data['next_issue']}`\n"
                        f"🎯 *Size:* *{pred_data['pred_size_emoji']}*\n"
                        f"🔢 *No:* `{pred_data['pred_num']}`\n"
                        f"📊 *Confidence:* `{pred_data['confidence']:.2f}%`\n\n"
                        f"🚩 *Level:* `LEVEL {current_level}`\n"
                        f"🔍 *Pattern type:* `{pred_data['pattern_desc']}`\n"
                        f"-----------------------------------\n"
                        f"📜 *RECENT HISTORY LOGS:*\n"
                        f"{history_block}"
                        f"-----------------------------------"
                    )
                    send_telegram(pred_msg)
                    print(f"[{last_issue}] Sent Period {pred_data['next_issue']} -> {pred_data['pred_size']} ({pred_data['confidence']:.2f}%)")

        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    main()
    
    
  
