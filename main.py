)import urllib.request
import urllib.parse
import json
import time
import math
from collections import Counter
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import traceback

# ==================== CONFIGURATION ====================
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"

BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655" 
WIN_STICKER_ID = "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ" 
MEMORY_FILE = "bot_learning_memory.json"
# =======================================================

# Self-Learning Global Model Weights
MODEL_WEIGHTS = {
    "markov": 0.35,
    "pattern": 0.45,
    "trend": 0.20
}

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SELF-LEARNING AI QUANTUM ENGINE ACTIVE")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as res: pass
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

def send_win_sticker():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    payload = json.dumps({"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=8) as res: pass
    except Exception:
        send_telegram("🎉🥳 **WINNER WINNER BIG WIN!** 🏆🔥")

def fetch_data():
    req = urllib.request.Request(API_URL, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get("code") == 0 and "data" in data and "list" in data["data"]:
                return data["data"]["list"]
    except Exception as e:
        print(f"⚠️️ API Fetch Error: {e}")
        return None

# ================= SELF-LEARNING LOGIC =================
def load_learning_memory():
    """Load learning weights and accuracy stats from memory"""
    global MODEL_WEIGHTS
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, 'r') as f:
                data = json.load(f)
                MODEL_WEIGHTS = data.get("weights", MODEL_WEIGHTS)
                print(f"🧠 Loaded Self-Learning Weights: {MODEL_WEIGHTS}")
        except Exception as e:
            print(f"⚠️ Learning Memory Load Error: {e}")

def update_learning_memory(used_model, is_win):
    """Auto-adjust model weights based on Win/Loss result (Reinforcement Learning)"""
    global MODEL_WEIGHTS
    try:
        learning_rate = 0.03  # Weight adjustment step
        if is_win:
            MODEL_WEIGHTS[used_model] = min(0.70, MODEL_WEIGHTS[used_model] + learning_rate)
        else:
            MODEL_WEIGHTS[used_model] = max(0.10, MODEL_WEIGHTS[used_model] - learning_rate)

        # Normalize Weights to sum up to 1.0
        total_w = sum(MODEL_WEIGHTS.values())
        for k in MODEL_WEIGHTS:
            MODEL_WEIGHTS[k] = round(MODEL_WEIGHTS[k] / total_w, 3)

        with open(MEMORY_FILE, 'w') as f:
            json.dump({"weights": MODEL_WEIGHTS, "last_updated": time.time()}, f)
        print(f"🎓 Model Learned & Adjusted Weights -> {MODEL_WEIGHTS}")
    except Exception as e:
        print(f"⚠️ Learning Memory Save Error: {e}")

# ================= PREDICTION ENGINE =================
def markov_chain_prob(outcomes):
    if len(outcomes) < 10: return 0.5
    last_val = outcomes[-1]
    trans_from_last = [outcomes[i+1] for i in range(len(outcomes)-1) if outcomes[i] == last_val]
    return sum(trans_from_last) / len(trans_from_last) if trans_from_last else 0.5

def analyze_self_learning_engine(history_list):
    outcomes_size = [1 if str(item.get("size", "")).lower() in ["big", "biggg"] else 0 for item in history_list]
    outcomes_num = [int(item.get("number", 0)) for item in history_list]
    
    total_len = len(outcomes_size)
    if total_len < 20: return None

    # 1. Trend Analytics
    last_5 = outcomes_size[-5:]
    is_dragon = (sum(last_5) == 5 or sum(last_5) == 0)
    is_zigzag = all(last_5[i] != last_5[i+1] for i in range(len(last_5)-1))

    # 2. Markov Chain Calculation
    markov_p = markov_chain_prob(outcomes_size)

    # 3. Deep Pattern Matcher with Exponential Decay
    matched_len = 0
    weighted_big_score = 0.0
    weighted_total_score = 0.0
    matched_nums = []

    for p_len in range(12, 3, -1):
        target_pattern = outcomes_size[-p_len:]
        for i in range(total_len - p_len):
            if outcomes_size[i : i + p_len] == target_pattern:
                next_size = outcomes_size[i + p_len]
                next_num = outcomes_num[i + p_len]
                weight = math.exp((i / total_len) * 2.0)
                weighted_total_score += weight
                if next_size == 1: weighted_big_score += weight
                matched_nums.append(next_num)
                matched_len = p_len
        if weighted_total_score > 0: break

    pattern_p = (weighted_big_score / weighted_total_score) if weighted_total_score > 0 else (sum(outcomes_size[-10:]) / 10.0)
    trend_p = 0.78 if (is_dragon and last_5[-1] == 1) or (is_zigzag and last_5[-1] == 0) else 0.22

    # Weighted Ensemble Calculation (Dynamic Learning Weights applied)
    final_prob = (
        (pattern_p * MODEL_WEIGHTS["pattern"]) + 
        (markov_p * MODEL_WEIGHTS["markov"]) + 
        (trend_p * MODEL_WEIGHTS["trend"])
    )

    # Pattern Type Description & Winning Strategy Identification
    if is_dragon:
        pattern_desc = "🔥 AI DRAGON REINFORCEMENT"
        active_model = "trend"
    elif is_zigzag:
        pattern_desc = "⚡ ZIG-ZAG DYNAMIC PATTERN"
        active_model = "trend"
    elif matched_len > 0:
        pattern_desc = f"🧠 AI DEEP SCAN ({matched_len} ROUNDS)"
        active_model = "pattern"
    else:
        pattern_desc = "📊 MARKOV MATRIX PROBABILITY"
        active_model = "markov"

    # Prediction Decision
    if final_prob >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        real_confidence = min(98.8, final_prob * 100)
        valid_nums = [n for n in matched_nums if n >= 5] if matched_nums else [5, 6, 7, 8, 9]
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        real_confidence = min(98.8, (1 - final_prob) * 100)
        valid_nums = [n for n in matched_nums if n <= 4] if matched_nums else [0, 1, 2, 3, 4]

    top_num = Counter(valid_nums).most_common(1)[0][0] if valid_nums else (7 if pred_size == "BIGGG" else 2)
    last_issue = int(history_list[-1]["issueNumber"])

    return {
        "last_issue": last_issue,
        "next_issue": last_issue + 1,
        "pred_size": pred_size,
        "pred_size_emoji": pred_size_emoji,
        "pred_num": top_num,
        "confidence": real_confidence,
        "pattern_desc": pattern_desc,
        "active_model": active_model
    }

def format_history_logs(history_records):
    logs_text = ""
    recent_8 = history_records[-8:]
    for item in recent_8:
        short_period = str(item["period"])[-3:]
        size_str = "BIGGG" if item["size"].upper() in ["BIG", "BIGGG"] else "SMALL"
        status_str = "✅✅✅" if item["status"] == "WIN" else "🔴"
        logs_text += f"`{short_period}` *{size_str}* {status_str}\n"
    return logs_text

def start_hyper_bot():
    print("🚀 HYPER AI SELF-LEARNING ENGINE RUNNING!")
    send_telegram("🧠 *HYPER SELF-LEARNING AI ENGINE ONLINE!*")
    
    load_learning_memory()
    current_level = 1
    pending_pred = None
    history_records = []
    
    while True:
        try:
            raw_list = fetch_data()
            if raw_list:
                history = list(reversed(raw_list))
                last_item = history[-1]
                last_issue = int(last_item["issueNumber"])
                actual_size = "BIGGG" if str(last_item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"

                # 1. Result Evaluator & Self-Learning Trigger
                if pending_pred and pending_pred["next_issue"] == last_issue:
                    is_win = (actual_size == pending_pred["pred_size"])
                    
                    # Update AI Memory Weights based on result
                    update_learning_memory(pending_pred["active_model"], is_win)

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

                # 2. Prediction Dispatcher
                if not pending_pred or pending_pred["last_issue"] != last_issue:
                    pred_data = analyze_self_learning_engine(history)
                    if pred_data:
                        pending_pred = pred_data
                        
                        history_block = format_history_logs(history_records) if history_records else "Waiting for history logs...\n"

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
                        print(f"[{last_issue}] Period {pred_data['next_issue']} -> {pred_data['pred_size']} ({pred_data['confidence']:.2f}%) | AI Weight Tuning Active")

        except Exception as e:
            print(f"❌ Main Loop Exception: {e}")
            traceback.print_exc()

        time.sleep(6)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    start_hyper_bot()

