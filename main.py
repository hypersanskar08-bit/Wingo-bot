import json
import time
import math
import os
import asyncio
import traceback
from collections import Counter, defaultdict
import aiohttp
from aiohttp import web

# ==================== CONFIGURATION ====================
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"
BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655"
WIN_STICKER_ID = "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ"
# =======================================================

# ⚡ ULTRA-FAST ASYNC HEALTH SERVER FOR UPTIMEROBOT & RENDER
async def handle_health_check(request):
    return web.Response(text="QUANTUM V18 DYNAMIC DEEP PATTERN MINER ACTIVE", status=200)

async def start_async_health_server():
    app = web.Application()
    # Handles both GET and HEAD requests perfectly
    app.router.add_get('/', handle_health_check)
    app.router.add_head('/', handle_health_check)
    
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Ultra-Fast Health Server Running on Port {port}")

async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            await response.text()
    except Exception:
        pass

async def send_win_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    payload = {"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as response:
            await response.text()
    except Exception:
        pass

async def fetch_data(session):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == 0 and "data" in data and "list" in data["data"]:
                    return data["data"]["list"]
    except Exception:
        pass
    return None

# ==================== DYNAMIC DEEP PATTERN LEARNER ====================

def calculate_shannon_entropy(sequence, window=20):
    if len(sequence) < window: return 1.0
    recent = sequence[-window:]
    p_big = sum(recent) / len(recent)
    p_small = 1.0 - p_big
    if p_big == 0 or p_small == 0: return 0.0
    return - (p_big * math.log2(p_big) + p_small * math.log2(p_small))

def dynamic_deep_pattern_miner(outcomes_size):
    total_len = len(outcomes_size)
    if total_len < 30: return 0.5, "INSUFFICIENT-DATA"
    
    curr_str = "".join(['B' if x == 1 else 'S' for x in outcomes_size])
    
    for L in range(8, 1, -1):
        if total_len <= L: continue
        tail = curr_str[-L:]
        
        big_weight = 0.0
        small_weight = 0.0
        total_matches = 0
        
        for i in range(total_len - L):
            sub = curr_str[i : i + L]
            if sub == tail:
                next_char = curr_str[i + L]
                recency = math.exp((i / total_len) * 6.0)
                if next_char == 'B':
                    big_weight += recency
                else:
                    small_weight += recency
                total_matches += 1
                
        if total_matches > 0:
            total_w = big_weight + small_weight
            prob_big = big_weight / total_w if total_w > 0 else 0.5
            return prob_big, f"DEEP-MINER (L{L} Tail: '{tail}' | Matches: {total_matches})"
            
    return 0.5, "NO-HISTORICAL-MATCH"

def exact_number_ngram_engine(outcomes_num):
    total_len = len(outcomes_num)
    if total_len < 30: return None, "NO DATA"
    
    for depth in [3, 2]:
        target_seq = tuple(outcomes_num[-depth:])
        num_weights = Counter()
        total_matches = 0.0
        
        for i in range(total_len - depth - 1):
            if tuple(outcomes_num[i : i + depth]) == target_seq:
                next_num = outcomes_num[i + depth]
                weight = math.exp((i / total_len) * 5.0)
                num_weights[next_num] += weight
                total_matches += weight
                
        if total_matches > 0:
            best_num = num_weights.most_common(1)[0][0]
            return best_num, f"NUM-NGRAM L{depth}"
            
    return None, "NO MATCH"

def strict_parity_hot_number_sync(pred_size, outcomes_num, raw_predicted_num):
    valid_range = [5, 6, 7, 8, 9] if pred_size == "BIGGG" else [0, 1, 2, 3, 4]
    
    if raw_predicted_num is not None and raw_predicted_num in valid_range:
        return raw_predicted_num
        
    recent_nums = outcomes_num[-50:]
    valid_hot_nums = [n for n in recent_nums if n in valid_range]
    
    if valid_hot_nums:
        weights = Counter()
        total = len(valid_hot_nums)
        for i, n in enumerate(valid_hot_nums):
            weights[n] += math.exp((i / total) * 4.0)
        return weights.most_common(1)[0][0]
        
    return 7 if pred_size == "BIGGG" else 2

# ==================== MASTER V18 MATRIX ENGINE ====================

def v18_master_engine(history_list, current_level):
    outcomes_size = [1 if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else 0 for item in history_list]
    outcomes_num = [int(item.get("number", 0)) for item in history_list]
    
    if len(outcomes_size) < 30: return None

    entropy = calculate_shannon_entropy(outcomes_size, window=20)
    prob_big, miner_tag = dynamic_deep_pattern_miner(outcomes_size)
    raw_num, ngram_tag = exact_number_ngram_engine(outcomes_num)

    if miner_tag != "NO-HISTORICAL-MATCH":
        combined_prob_big = prob_big
        logic_desc = f"{miner_tag} + {ngram_tag}"
    else:
        recent_20 = outcomes_size[-20:]
        combined_prob_big = sum(recent_20) / len(recent_20)
        logic_desc = f"RATIO-FLOW + {ngram_tag}"

    if combined_prob_big >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        confidence = min(99.9, combined_prob_big * 100)
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        confidence = min(99.9, (1.0 - combined_prob_big) * 100)

    final_pred_num = strict_parity_hot_number_sync(pred_size, outcomes_num, raw_num)
    last_issue = int(history_list[-1]["issueNumber"])

    return {
        "last_issue": last_issue,
        "next_issue": last_issue + 1,
        "pred_size": pred_size,
        "pred_size_emoji": pred_size_emoji,
        "pred_num": final_pred_num,
        "confidence": confidence,
        "entropy": entropy,
        "pattern_desc": logic_desc
    }

def format_synced_history_logs(server_history, bot_records):
    logs_text = ""
    bot_dict = {item["period"]: item["status"] for item in bot_records}
    
    for item in server_history[-10:]:
        period = int(item["issueNumber"])
        short_period = str(period)[-3:]
        size_str = "BIGGG" if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
        
        status = bot_dict.get(period)
        if status == "WIN_NUMBER": status_str = "☠️"
        elif status == "WIN_SIZE": status_str = "✅"
        elif status == "LOSS": status_str = "🔴"
        else: status_str = "➖"
            
        logs_text += f"`{short_period}` *{size_str}* {status_str}\n"
    return logs_text

async def start_hyper_bot():
    print("🚀 QUANTUM V18 DYNAMIC DEEP PATTERN MINER ACTIVE!")
    
    # 1. Health Server Ko Main Async Loop Me Start Karein
    await start_async_health_server()

    async with aiohttp.ClientSession() as session:
        current_level = 1
        pending_pred = None
        history_records = []
        
        total_wins = 0
        total_losses = 0
        max_b2b_loss = 0
        current_b2b_loss = 0
        
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
                        
                        if len(history_records) > 50: history_records.pop(0)

                        if is_size_win:
                            total_wins += 1
                            current_b2b_loss = 0
                            asyncio.create_task(send_win_sticker(session))
                            current_level = 1 
                        else:
                            total_losses += 1
                            current_b2b_loss += 1
                            if current_b2b_loss > max_b2b_loss:
                                max_b2b_loss = current_b2b_loss
                            current_level += 1
                            if current_level > 5: current_level = 1

                        pending_pred = None

                    if not pending_pred or pending_pred["last_issue"] != last_issue:
                        pred_data = v18_master_engine(history, current_level)
                        if pred_data:
                            pending_pred = pred_data
                            history_block = format_synced_history_logs(history, history_records)

                            pred_msg = (
                                f"👑 *QUANTUM V18 DEEP PATTERN MINER* 👑\n\n"
                                f"📌 *Period:* `{pred_data['next_issue']}`\n"
                                f"🎯 *Size:* *{pred_data['pred_size_emoji']}*\n"
                                f"🔢 *No:* `{pred_data['pred_num']}`\n"
                                f"📊 *Confidence:* `{pred_data['confidence']:.2f}%`\n"
                                f"🌀 *Entropy:* `{pred_data['entropy']:.2f}`\n\n"
                                f"🚩 *Level:* `LEVEL {current_level}`\n"
                                f"🔍 *Logic:* `{pred_data['pattern_desc']}`\n"
                                f"-----------------------------------\n"
                                f"📜 *RECENT HISTORY LOGS (10):*\n"
                                f"{history_block}"
                                f"-----------------------------------\n"
                                f"📈 *SESSION STATS:*\n"
                                f"✅ *Total Wins:* `{total_wins}`\n"
                                f"🔴 *Total Losses:* `{total_losses}`\n"
                                f"☠️ *Max B2B Loss:* `{max_b2b_loss}`"
                            )
                            asyncio.create_task(send_telegram(session, pred_msg))
            except Exception:
                pass
            await asyncio.sleep(6)

if __name__ == "__main__":
    asyncio.run(start_hyper_bot())




