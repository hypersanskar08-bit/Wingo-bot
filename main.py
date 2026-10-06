
import json
import time
import math
import os
import asyncio
import traceback
from collections import Counter
import aiohttp
from aiohttp import web

# ==================== CONFIGURATION ====================
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"
BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655"
WIN_STICKER_ID = "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ"
# =======================================================

async def handle_health_check(request):
    return web.Response(text="QUANTUM V20 ENSEMBLE AI ACTIVE", status=200)

async def start_async_health_server():
    try:
        app = web.Application()
        app.router.add_get('/', handle_health_check)
        app.router.add_head('/', handle_health_check)
        port = int(os.environ.get("PORT", 10000))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"🌐 Ultra-Fast Health Server Running on Port {port}")
    except Exception as e:
        print(f"⚠️ Web Server Warning: {e}")

async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            await response.text()
    except Exception as e:
        print(f"Telegram send error: {e}")

async def send_win_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    payload = {"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as response:
            await response.text()
    except Exception as e:
        print(f"Sticker error: {e}")

async def fetch_data(session):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with session.get(API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == 0 and "data" in data and "list" in data["data"]:
                    return data["data"]["list"]
    except Exception as e:
        print(f"Fetch error: {e}")
    return None

# ==================== ADVANCED AI ENGINES ====================

def calculate_shannon_entropy(sequence, window=24):
    if len(sequence) < window: return 1.0
    recent = sequence[-window:]
    p_big = sum(recent) / len(recent)
    p_small = 1.0 - p_big
    if p_big == 0 or p_small == 0: return 0.0
    return - (p_big * math.log2(p_big) + p_small * math.log2(p_small))

def calculate_rsi(outcomes, period=14):
    if len(outcomes) < period: return 50.0
    recent = outcomes[-period:]
    gains = sum(1 for x in recent if x == 1)
    losses = sum(1 for x in recent if x == 0)
    if losses == 0: return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def markov_2nd_order(outcomes):
    if len(outcomes) < 10: return 0.5
    last_two = tuple(outcomes[-2:])
    matches, big_next = 0, 0
    for i in range(len(outcomes) - 2):
        if tuple(outcomes[i:i+2]) == last_two:
            matches += 1
            if outcomes[i+2] == 1: big_next += 1
    return (big_next / matches) if matches > 0 else 0.5

def dynamic_deep_pattern_miner(outcomes):
    total_len = len(outcomes)
    if total_len < 30: return 0.5
    curr_str = "".join(['B' if x == 1 else 'S' for x in outcomes])
    for L in range(10, 2, -1):
        if total_len <= L: continue
        tail = curr_str[-L:]
        big_w, small_w = 0.0, 0.0
        for i in range(total_len - L):
            if curr_str[i : i + L] == tail:
                recency = math.exp((i / total_len) * 6.0)
                if curr_str[i + L] == 'B': big_w += recency
                else: small_w += recency
        if big_w + small_w > 0:
            return big_w / (big_w + small_w)
    return 0.5

def v20_ensemble_engine(history_list, current_level):
    outcomes = [1 if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else 0 for item in history_list]
    if len(outcomes) < 30: return None

    entropy = calculate_shannon_entropy(outcomes)
    rsi = calculate_rsi(outcomes)
    miner_prob = dynamic_deep_pattern_miner(outcomes)
    markov_prob = markov_2nd_order(outcomes)
    
    rsi_adjustment = 0.0
    if rsi >= 75: rsi_adjustment = -0.15
    elif rsi <= 25: rsi_adjustment = 0.15

    final_prob_big = (miner_prob * 0.50) + (markov_prob * 0.35) + rsi_adjustment + 0.075
    final_prob_big = max(0.01, min(0.99, final_prob_big))

    if entropy > 0.98 and (0.42 <= final_prob_big <= 0.58):
        return {
            "last_issue": int(history_list[-1]["issueNumber"]),
            "next_issue": int(history_list[-1]["issueNumber"]) + 1,
            "pred_size": "SKIP",
            "pred_size_emoji": "⚠️ SKIP (MARKET RANDOM)",
            "confidence": 0.0,
            "bet_advice": "0X (DO NOT BET)",
            "metrics": f"RSI: {rsi:.1f} | ENT: {entropy:.2f}"
        }

    if final_prob_big >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        confidence_real = final_prob_big
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        confidence_real = 1.0 - final_prob_big

    display_confidence = min(99.9, confidence_real * 100)

    kelly_fraction = (2 * confidence_real) - 1.0
    if kelly_fraction > 0.60: bet_advice = "3.0X 🔥 MAX STRIKE"
    elif kelly_fraction > 0.40: bet_advice = "2.0X 💰 HIGH RISK"
    elif kelly_fraction > 0.20: bet_advice = "1.0X 💵 NORMAL"
    else: bet_advice = "0.5X 🪙 LOW AMOUNT"

    return {
        "last_issue": int(history_list[-1]["issueNumber"]),
        "next_issue": int(history_list[-1]["issueNumber"]) + 1,
        "pred_size": pred_size,
        "pred_size_emoji": pred_size_emoji,
        "confidence": display_confidence,
        "bet_advice": bet_advice,
        "metrics": f"RSI: {rsi:.1f} | ENT: {entropy:.2f} | KLY: {kelly_fraction:.2f}"
    }

def format_synced_history_logs(server_history):
    logs_text = ""
    for item in server_history[-8:]:
        short_period = str(item["issueNumber"])[-3:]
        size_str = "BIGGG" if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
        logs_text += f"`{short_period}` *{size_str}*\n"
    return logs_text

async def start_hyper_bot():
    print("🚀 QUANTUM V20 ENSEMBLE AI INITIALIZING...")
    await start_async_health_server()

    async with aiohttp.ClientSession() as session:
        current_level = 1
        pending_pred = None
        total_wins, total_losses = 0, 0
        
        print("✅ BOT LOOP ACTIVE & MONITORING MARKET...")
        while True:
            try:
                raw_list = await fetch_data(session)
                if raw_list:
                    history = list(reversed(raw_list))
                    last_item = history[-1]
                    last_issue = int(last_item["issueNumber"])
                    actual_size = "BIGGG" if str(last_item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"

                    if pending_pred and pending_pred["next_issue"] == last_issue:
                        if pending_pred["pred_size"] != "SKIP":
                            is_win = (actual_size == pending_pred["pred_size"])
                            if is_win:
                                total_wins += 1
                                current_level = 1
                                asyncio.create_task(send_win_sticker(session))
                            else:
                                total_losses += 1
                                current_level += 1
                                if current_level > 5: current_level = 1
                        pending_pred = None

                    if not pending_pred or pending_pred["last_issue"] != last_issue:
                        pred_data = v20_ensemble_engine(history, current_level)
                        if pred_data:
                            pending_pred = pred_data
                            history_block = format_synced_history_logs(history)

                            pred_msg = (
                                f"🏛️ *V20 INSTITUTIONAL QUANT* 🏛️\n\n"
                                f"📌 *Period:* `{pred_data['next_issue']}`\n"
                                f"🎯 *Prediction:* *{pred_data['pred_size_emoji']}*\n"
                                f"📊 *Win Prob:* `{pred_data['confidence']:.2f}%`\n"
                                f"⚖️ *Kelly Sizing:* `{pred_data['bet_advice']}`\n\n"
                                f"🚩 *Level:* `LEVEL {current_level}`\n"
                                f"⚙️ *Quant Data:* `{pred_data['metrics']}`\n"
                                f"-----------------------------------\n"
                                f"📜 *MARKET TREND (8)*:\n"
                                f"{history_block}"
                            )
                            asyncio.create_task(send_telegram(session, pred_msg))
            except Exception as e:
                print(f"Error in loop: {e}")
            await asyncio.sleep(6)

if __name__ == "__main__":
    asyncio.run(start_hyper_bot())

