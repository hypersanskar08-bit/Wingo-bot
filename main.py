import json
import time
import math
import os
import asyncio
import traceback
from collections import Counter, defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import aiohttp

# ==================== CONFIGURATION ====================
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"

BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655"
WIN_STICKER_ID = "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ"
# =======================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SUPER ADVANCED QUANTUM AI ENGINE ACTIVE")
    
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            await response.text()
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

async def send_win_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    payload = {"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as response:
            await response.text()
    except Exception:
        asyncio.create_task(send_telegram(session, "🎉🥳 **WINNER WINNER BIG WIN!** 🏆🔥"))

async def fetch_data(session):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == 0 and "data" in data and "list" in data["data"]:
                    return data["data"]["list"]
    except Exception as e:
        pass
    return None

def analyze_trend_shift(outcomes, window=10):
    """Detects if the market is trending (streaks) or volatile (zig-zag)."""
    if len(outcomes) < window: return "NEUTRAL"
    recent = outcomes[-window:]
    changes = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1])
    if changes >= window * 0.7: return "VOLATILE"
    if changes <= window * 0.3: return "TRENDING"
    return "NEUTRAL"

def calculate_transition_matrix(sequence, order=2):
    """Builds a probability matrix based on past N sequences."""
    matrix = defaultdict(list)
    for i in range(len(sequence) - order):
        state = tuple(sequence[i:i+order])
        next_val = sequence[i+order]
        matrix[state].append(next_val)
    return matrix

def super_advanced_pattern_learner(history_list):
    """
    QUANTUM V6 HYPER ENGINE:
    1. Trend Shift Detector (Volatility vs Streak)
    2. Deep Number Learner (Order-2 & Order-3 Transition Matrices)
    3. Weighted Recency Decay
    """
    outcomes_size = [1 if str(item.get("size", "")).lower() in ["big", "biggg"] else 0 for item in history_list]
    outcomes_num = [int(item.get("number", 0)) for item in history_list]
    
    total_len = len(outcomes_size)
    if total_len < 50: return None

    # --- 1. Market Condition Analysis ---
    market_state = analyze_trend_shift(outcomes_size, window=12)

    # --- 2. Deep Size Pattern Matching (L-15) with Recency Decay ---
    matched_size_weight = {1: 0.0, 0: 0.0}
    found_depth = 0
    matched_historical_nums = []

    for depth in range(15, 2, -1):
        target_seq = outcomes_size[-depth:]
        for i in range(total_len - depth - 1):
            if outcomes_size[i : i + depth] == target_seq:
                next_size = outcomes_size[i + depth]
                next_num = outcomes_num[i + depth]
                
                # Exponential decay: recent matches are heavily favored
                time_weight = math.exp((i / total_len) * 4.0)
                matched_size_weight[next_size] += time_weight
                matched_historical_nums.append((next_num, time_weight))
                found_depth = depth
        if found_depth > 0: break

    total_weight = matched_size_weight[1] + matched_size_weight[0]
    deep_prob_big = matched_size_weight[1] / total_weight if total_weight > 0 else 0.5

    # --- 3. Deep Number Transition Analysis (Order-2 & Order-3) ---
    num_matrix_3 = calculate_transition_matrix(outcomes_num, order=3)
    num_matrix_2 = calculate_transition_matrix(outcomes_num, order=2)
    
    current_state_3 = tuple(outcomes_num[-3:])
    current_state_2 = tuple(outcomes_num[-2:])
    
    predicted_nums_from_matrix = []
    if current_state_3 in num_matrix_3:
        predicted_nums_from_matrix = num_matrix_3[current_state_3]
    elif current_state_2 in num_matrix_2:
        predicted_nums_from_matrix = num_matrix_2[current_state_2]

    # --- 4. Logic Fusion & Trend Shift Adjustments ---
    final_prob_big = deep_prob_big
    
    # Adjust prediction based on market volatility
    if market_state == "VOLATILE":
        # In volatile markets, expect a flip from the very last outcome
        last_outcome = outcomes_size[-1]
        final_prob_big = (final_prob_big * 0.4) + ((0.8 if last_outcome == 0 else 0.2) * 0.6)
    elif market_state == "TRENDING":
        # In trending markets, expect continuation
        last_outcome = outcomes_size[-1]
        final_prob_big = (final_prob_big * 0.4) + ((0.8 if last_outcome == 1 else 0.2) * 0.6)

    # --- 5. Determine Final Outputs ---
    if final_prob_big >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        confidence = min(99.6, final_prob_big * 100)
        target_group = [5, 6, 7, 8, 9]
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        confidence = min(99.6, (1 - final_prob_big) * 100)
        target_group = [0, 1, 2, 3, 4]

    # Number Selection Logic: Blend Matrix Prediction with Historical Match
    valid_matrix_nums = [n for n in predicted_nums_from_matrix if n in target_group]
    valid_historical_nums = [n for n, w in matched_historical_nums if n in target_group]
    
    combined_nums = valid_matrix_nums * 2 + valid_historical_nums # Give matrix slightly more weight
    
    if combined_nums:
        pred_num = Counter(combined_nums).most_common(1)[0][0]
    else:
        # Fallback to general frequency in recent history
        recent_valid = [n for n in outcomes_num[-50:] if n in target_group]
        pred_num = Counter(recent_valid).most_common(1)[0][0] if recent_valid else (7 if pred_size == "BIGGG" else 2)

    last_issue = int(history_list[-1]["issueNumber"])
    
    # Generate Advanced Description
    desc_parts = []
    if market_state != "NEUTRAL": desc_parts.append(f"{market_state} MKT")
    if found_depth > 0: desc_parts.append(f"L-{found_depth} MATCH")
    if valid_matrix_nums: desc_parts.append("MATRIX TRN")
    
    pattern_desc = " + ".join(desc_parts) if desc_parts else "QUANTUM BASELINE"

    return {
        "last_issue": last_issue,
        "next_issue": last_issue + 1,
        "pred_size": pred_size,
        "pred_size_emoji": pred_size_emoji,
        "pred_num": pred_num,
        "confidence": confidence,
        "pattern_desc": pattern_desc
    }

def format_history_logs(history_records):
    logs_text = ""
    for item in history_records[-8:]:
        short_period = str(item["period"])[-3:]
        size_str = "BIGGG" if item["size"].upper() in ["BIG", "BIGGG"] else "SMALL"
        status_str = "☠️" if item["status"] == "WIN_NUMBER" else "✅" if item["status"] == "WIN_SIZE" else ""
        logs_text += f"`{short_period}` *{size_str}* {status_str}\n"
    return logs_text

async def start_hyper_bot():
    print("🚀 QUANTUM V6 ENGINE RUNNING!")
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(send_telegram(session, "🚀 *QUANTUM V6 (Trend Shift & Matrix) ENGINE ONLINE!*"))
        current_level = 1
        pending_pred = None
        history_records = []
        
        while True:
            try:
                raw_list = await fetch_data(session)
                if raw_list:
                    history = list(reversed(raw_list))
                    last_item = history[-1]
                    last_issue = int(last_item["issueNumber"])
                    actual_size = "BIGGG" if str(last_item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
                    actual_num = int(last_item.get("number", -1))

                    if pending_pred and pending_pred["next_issue"] == last_issue:
                        is_size_win = (actual_size == pending_pred["pred_size"])
                        is_num_win = (actual_num == pending_pred["pred_num"])
                        
                        win_status = "WIN_NUMBER" if (is_size_win and is_num_win) else "WIN_SIZE" if is_size_win else "LOSS"
                        
                        history_records.append({
                            "period": last_issue,
                            "size": actual_size,
                            "number": actual_num,
                            "status": win_status
                        })

                        if is_size_win:
                            asyncio.create_task(send_win_sticker(session))
                            current_level = 1
                        else:
                            current_level = 1 if current_level >= 4 else current_level + 1

                        pending_pred = None

                    if not pending_pred or pending_pred["last_issue"] != last_issue:
                        pred_data = super_advanced_pattern_learner(history)
                        if pred_data:
                            pending_pred = pred_data
                            history_block = format_history_logs(history_records) if history_records else "Waiting for logs...\n"

                            pred_msg = (
                                f"🔥 *QUANTUM V6 PREDICTION* 🔥\n\n"
                                f"📌 *Period:* `{pred_data['next_issue']}`\n"
                                f"🎯 *Size:* *{pred_data['pred_size_emoji']}*\n"
                                f"🔢 *No:* `{pred_data['pred_num']}`\n"
                                f"📊 *Confidence:* `{pred_data['confidence']:.2f}%`\n\n"
                                f"🚩 *Level:* `LEVEL {current_level}`\n"
                                f"🔍 *Logic:* `{pred_data['pattern_desc']}`\n"
                                f"-----------------------------------\n"
                                f"📜 *HISTORY:*\n"
                                f"{history_block}"
                                f"-----------------------------------"
                            )
                            asyncio.create_task(send_telegram(session, pred_msg))
            except Exception as e:
                traceback.print_exc()
            await asyncio.sleep(6)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    time.sleep(1)
    asyncio.run(start_hyper_bot())


