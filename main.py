import json
import time
import math
import os
import asyncio
import traceback
from collections import Counter
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
        self.end_headers()
        self.wfile.write(b"SUPER ADVANCED QUANTUM AI ENGINE ACTIVE")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
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
        print(f"⚠️ API Fetch Error: {e}")
    return None

def calculate_ema(numbers, period=7):
    """Calculates Exponential Moving Average for Number Momentum"""
    if len(numbers) < period:
        return sum(numbers) / len(numbers) if numbers else 4.5
    
    multiplier = 2 / (period + 1)
    ema = sum(numbers[:period]) / period
    for num in numbers[period:]:
        ema = (num - ema) * multiplier + ema
    return ema

def analyze_super_advanced_engine(history_list):
    """
    DEEP LEARNING ENGINE (NOISE-FREE):
    1. Deep N-Gram Longest Suffix Matching (Sizes & Numbers)
    2. Time-Series Momentum (Number EMA)
    3. Conditional Number Frequency Extraction
    """
    outcomes_size = [1 if str(item.get("size", "")).lower() in ["big", "biggg"] else 0 for item in history_list]
    outcomes_num = [int(item.get("number", 0)) for item in history_list]
    
    total_len = len(outcomes_size)
    if total_len < 50: 
        return None

    # --- 1. Deep Pattern Suffix Matching (Window up to 15) ---
    max_search_depth = 15
    matched_size_counts = {1: 0.0, 0: 0.0}
    matched_numbers = []
    found_pattern_len = 0

    # Search for the longest exact historical sequence that matches the current sequence
    for p_len in range(max_search_depth, 1, -1):
        target_seq = outcomes_size[-p_len:]
        
        for i in range(total_len - p_len - 1): # -1 to ensure we have a 'next' outcome
            if outcomes_size[i : i + p_len] == target_seq:
                next_size = outcomes_size[i + p_len]
                next_num = outcomes_num[i + p_len]
                
                # Exponential weight: Recent historical matches matter more
                weight = math.exp((i / total_len) * 3.0)
                matched_size_counts[next_size] += weight
                matched_numbers.append(next_num)
                found_pattern_len = p_len
        
        # If we found at least one match at this depth, stop searching shallower depths
        if found_pattern_len > 0:
            break

    # Calculate Pattern Probability
    total_weight = matched_size_counts[1] + matched_size_counts[0]
    if total_weight > 0:
        pattern_prob_big = matched_size_counts[1] / total_weight
    else:
        # Baseline fallback if completely unprecedented
        pattern_prob_big = sum(outcomes_size[-15:]) / 15.0

    # --- 2. Number Momentum (EMA Calculation) ---
    # Treats the outcomes like a stock chart to find upward/downward momentum
    ema_value = calculate_ema(outcomes_num[-20:], period=7)
    momentum_prob_big = max(0.1, min(0.9, ema_value / 9.0)) # Normalize 0-9 to probability

    # --- 3. Final Weighted Probability ---
    # 75% weight to Deep Pattern Match, 25% to Market Momentum
    final_prob = (pattern_prob_big * 0.75) + (momentum_prob_big * 0.25)

    # --- 4. Deep Number Prediction ---
    if final_prob >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        real_confidence = min(99.2, final_prob * 100)
        
        # Try to pick numbers that actually followed this exact pattern historically
        valid_nums = [n for n in matched_numbers if n >= 5]
        # Fallback to recent hot big numbers
        if not valid_nums: 
            valid_nums = [n for n in outcomes_num[-50:] if n >= 5]
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        real_confidence = min(99.2, (1 - final_prob) * 100)
        
        valid_nums = [n for n in matched_numbers if n <= 4]
        if not valid_nums: 
            valid_nums = [n for n in outcomes_num[-50:] if n <= 4]

    # Select the most statistically frequent valid number
    top_num = Counter(valid_nums).most_common(1)[0][0] if valid_nums else (7 if pred_size == "BIGGG" else 2)
    
    last_issue = int(history_list[-1]["issueNumber"])
    pattern_desc = f"🧠 DEEP N-GRAM (L-{found_pattern_len}) + EMA" if found_pattern_len > 0 else "📊 MOMENTUM TREND"

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
        short_period = str(item["period"])[-3:]
        size_str = "BIGGG" if item["size"].upper() in ["BIG", "BIGGG"] else "SMALL"
        
        if item["status"] == "WIN_NUMBER":
            status_str = "☠️" 
        elif item["status"] == "WIN_SIZE":
            status_str = "✅"  
        else:
            status_str = "🔴"   
            
        logs_text += f"`{short_period}` *{size_str}* {status_str}\n"
    return logs_text

async def start_hyper_bot():
    print("🚀 DEEP LEARNING QUANTUM ENGINE RUNNING!")
    
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(send_telegram(session, "🚀 *DEEP LEARNING ENGINE ONLINE!* (Noise-Free AI)"))
        
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
                        
                        if is_size_win and is_num_win:
                            win_status = "WIN_NUMBER"
                        elif is_size_win:
                            win_status = "WIN_SIZE"
                        else:
                            win_status = "LOSS"
                        
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
                            current_level += 1
                            if current_level > 3: 
                                current_level = 1

                        pending_pred = None

                    if not pending_pred or pending_pred["last_issue"] != last_issue:
                        pred_data = analyze_super_advanced_engine(history)
                        if pred_data:
                            pending_pred = pred_data
                            
                            history_block = format_history_logs(history_records) if history_records else "Waiting for history logs...\n"

                            pred_msg = (
                                f"🔥 *DEEP AI PREDICTION* 🔥\n\n"
                                f"📌 *Period:* `{pred_data['next_issue']}`\n"
                                f"🎯 *Size:* *{pred_data['pred_size_emoji']}*\n"
                                f"🔢 *No:* `{pred_data['pred_num']}`\n"
                                f"📊 *Confidence:* `{pred_data['confidence']:.2f}%`\n\n"
                                f"🚩 *Level:* `LEVEL {current_level}`\n"
                                f"🔍 *AI Logic:* `{pred_data['pattern_desc']}`\n"
                                f"-----------------------------------\n"
                                f"📜 *RECENT HISTORY LOGS:*\n"
                                f"{history_block}"
                                f"-----------------------------------"
                            )
                            asyncio.create_task(send_telegram(session, pred_msg))
                            print(f"[{last_issue}] Period {pred_data['next_issue']} -> {pred_data['pred_size']} No:{pred_data['pred_num']} ({pred_data['confidence']:.2f}%) | Logic: {pred_data['pattern_desc']}")

            except Exception as e:
                print(f"❌ Main Loop Exception: {e}")
                traceback.print_exc()

            await asyncio.sleep(6)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(start_hyper_bot())

