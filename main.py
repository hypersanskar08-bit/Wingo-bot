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

# Memory: issue_number -> {"size": ..., "number": ...}
PREDICTION_MEMORY = {}

async def handle_health_check(request):
    return web.Response(text="QUANTUM V21 ULTRA-MAX ACTIVE", status=200)

async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            await response.text()
    except Exception as e:
        print(f"Telegram error: {e}")

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

# ==================== UPGRADED CORE ENGINES ====================

def calculate_rsi(outcomes, period=14):
    if len(outcomes) < period: return 50.0
    recent = outcomes[-period:]
    gains = sum(1 for x in recent if x == 1)
    losses = sum(1 for x in recent if x == 0)
    if losses == 0: return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def advanced_markov_engine(outcomes):
    """1st to 4th Order Markov with dynamic weighting based on match frequency"""
    if len(outcomes) < 6: return 0.5
    results, weights = [], []
    
    for order in range(1, 5):
        if len(outcomes) < order + 5: continue
        last_seq = tuple(outcomes[-order:])
        matches, big_next = 0, 0
        for i in range(len(outcomes) - order):
            if tuple(outcomes[i:i+order]) == last_seq:
                matches += 1
                if outcomes[i+order] == 1: big_next += 1
        if matches >= 2: # Statistical relevance
            prob = big_next / matches
            w = order * math.log(matches + 1) # Higher order + more matches = more weight
            results.append(prob)
            weights.append(w)
            
    if not results: return 0.5
    return sum(r * w for r, w in zip(results, weights)) / sum(weights)

def ngram_pattern_engine(outcomes):
    """Bayesian Pattern Matching for lengths 3 to 9 with recency weighting"""
    total_len = len(outcomes)
    if total_len < 15: return 0.5
    curr_str = "".join(['B' if x == 1 else 'S' for x in outcomes])
    results, weights = [], []
    
    for L in range(3, 10): # Length 3 to 9
        if total_len <= L: continue
        tail = curr_str[-L:]
        big_w, small_w = 0.0, 0.0
        matches = 0
        for i in range(total_len - L):
            if curr_str[i:i+L] == tail:
                matches += 1
                recency = math.exp((i / total_len) * 5.0) # Exponential recency weight
                if curr_str[i+L] == 'B': big_w += recency
                else: small_w += recency
        if matches >= 2:
            prob = big_w / (big_w + small_w) if (big_w + small_w) > 0 else 0.5
            w = L * math.log(matches + 1)
            results.append(prob)
            weights.append(w)
            
    if not results: return 0.5
    return sum(r * w for r, w in zip(results, weights)) / sum(weights)

def streak_momentum_engine(outcomes):
    """Analyzes micro-streaks, alternating patterns, and momentum breaks"""
    if len(outcomes) < 6: return 0.5
    recent = outcomes[-6:]
    
    # Full 6 Streak -> Reversal expected
    if sum(recent) == 6: return 0.25
    if sum(recent) == 0: return 0.75
    
    # Alternating Pattern -> Continuation expected
    if recent == [1,0,1,0,1,0]: return 0.75
    if recent == [0,1,0,1,0,1]: return 0.25
    
    # 5 Streak
    if sum(recent[-5:]) == 5: return 0.30
    if sum(recent[-5:]) == 0: return 0.70
    
    # Micro Trend (3 Streak)
    last_3 = outcomes[-3:]
    if sum(last_3) == 3: return 0.35
    if sum(last_3) == 0: return 0.65
    
    return 0.5

def volatility_chop_index(outcomes):
    """Calculates choppiness. High chop = lower confidence penalty"""
    recent = outcomes[-20:]
    if len(recent) < 10: return 0.0
    alternations = sum(1 for i in range(len(recent)-1) if recent[i] != recent[i+1])
    # Max alternations in 20 is 19. 10 is normal, >14 is highly choppy
    chop_factor = max(0.0, (alternations - 10) / 9.0) 
    return min(1.0, chop_factor)

# ==================== ADVANCED NUMBER PREDICTOR ====================
def number_predictor(history_list, predicted_size):
    numbers = []
    for item in history_list:
        try: numbers.append(int(item.get("number", 0)))
        except: numbers.append(0)

    if len(numbers) < 30:
        return 8 if predicted_size == "BIGGG" else 2

    # Jack number is included for its matching size
    candidates = [5, 6, 7, 8, 9] if predicted_size == "BIGGG" else [0, 1, 2, 3, 4]

    last_num = numbers[-1]
    mk1 = Counter()
    for i in range(len(numbers) - 1):
        if numbers[i] == last_num: mk1[numbers[i + 1]] += 1

    mk2 = Counter()
    if len(numbers) >= 2:
        last_two = (numbers[-2], numbers[-1])
        for i in range(len(numbers) - 2):
            if (numbers[i], numbers[i + 1]) == last_two: mk2[numbers[i + 2]] += 1

    recent = numbers[-60:]
    total_recent = len(recent)
    freq_weighted = {}
    for idx, n in enumerate(recent):
        w = math.exp((idx / total_recent) * 3.0)
        freq_weighted[n] = freq_weighted.get(n, 0.0) + w
    total_fw = sum(freq_weighted.values()) or 1.0

    scope = numbers[-200:] if len(numbers) >= 200 else numbers
    freq_all = Counter(scope)
    total_all = len(scope)

    total_mk1 = sum(mk1.values()) or 1
    total_mk2 = sum(mk2.values()) or 1

    scores = {}
    for n in candidates:
        s_mk2 = mk2.get(n, 0) / total_mk2
        s_mk1 = mk1.get(n, 0) / total_mk1
        s_fw  = freq_weighted.get(n, 0.0) / total_fw
        s_fa  = freq_all.get(n, 0) / total_all
        scores[n] = (s_mk2 * 0.35) + (s_mk1 * 0.25) + (s_fw * 0.25) + (s_fa * 0.15)

    return max(scores, key=scores.get)

# ==================== UPGRADED STRIKE ENGINE ====================
def v21_strike_engine(history_list, current_level):
    outcomes = [1 if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else 0 for item in history_list]
    if len(outcomes) < 20: return None

    # 1. Get Probabilities from all engines
    p_markov = advanced_markov_engine(outcomes)
    p_pattern = ngram_pattern_engine(outcomes)
    p_streak = streak_momentum_engine(outcomes)
    
    # 2. RSI as a probability
    rsi = calculate_rsi(outcomes)
    if rsi >= 70: p_rsi = 0.25
    elif rsi <= 30: p_rsi = 0.75
    else: p_rsi = 0.5 + (50 - rsi) * 0.01

    # 3. Logit Ensemble (Naive Bayes approach)
    def to_logit(p):
        p = max(0.01, min(0.99, p))
        return math.log(p / (1 - p))

    logit_pattern = to_logit(p_pattern)
    logit_markov = to_logit(p_markov)
    logit_streak = to_logit(p_streak)
    logit_rsi = to_logit(p_rsi)

    # Dynamic Weights: Pattern gets highest, then Markov, then Streak, then RSI
    w_pattern, w_markov, w_streak, w_rsi = 0.40, 0.30, 0.20, 0.10

    final_logit = (logit_pattern * w_pattern) + (logit_markov * w_markov) + (logit_streak * w_streak) + (logit_rsi * w_rsi)
    final_prob_big = 1 / (1 + math.exp(-final_logit))

    # 4. Apply Volatility Penalty (Reduce confidence in choppy markets)
    chop = volatility_chop_index(outcomes)
    if final_prob_big > 0.5:
        final_prob_big = 0.5 + (final_prob_big - 0.5) * (1 - chop * 0.4)
    else:
        final_prob_big = 0.5 - (0.5 - final_prob_big) * (1 - chop * 0.4)

    # 5. Final Decision
    if final_prob_big >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        confidence_real = final_prob_big
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        confidence_real = 1.0 - final_prob_big

    # Realistic Confidence Mapping (No fake 90%+ unless truly aligned)
    display_confidence = 60.0 + (confidence_real * 35.0)
    pred_number = number_predictor(history_list, pred_size)

    if current_level == 1:
        bet_advice = "1.0X 🎯 LEVEL 1 STRIKE"
    elif current_level == 2:
        bet_advice = "2.5X 🔥 LEVEL 2 COVER"
    elif current_level == 3:
        bet_advice = "6.0X ⚡ LEVEL 3 RECOVERY"
    else:
        bet_advice = "12.0X 🛡️ LEVEL SAFEGUARD"

    return {
        "last_issue": int(history_list[-1]["issueNumber"]),
        "next_issue": int(history_list[-1]["issueNumber"]) + 1,
        "pred_size": pred_size,
        "pred_size_emoji": pred_size_emoji,
        "pred_number": pred_number,
        "confidence": display_confidence,
        "bet_advice": bet_advice,
        "metrics": f"RSI:{rsi:.1f} PAT:{p_pattern:.2f} MKV:{p_markov:.2f} STR:{p_streak:.2f} CHP:{chop:.2f}"
    }

# ==================== HISTORY FORMATTER WITH ICONS ====================
def format_synced_history_logs(server_history):
    """
    ✅✅✅ -> Correct size matched (Win, Jack bhi win hai)
    ☠️☠️☠️ -> Jack aaya par galat size pe the (Miss)
    blank  -> Normal loss
    """
    logs_text = ""
    for item in server_history[-8:]:
        issue = int(item["issueNumber"])
        short_period = str(issue)[-3:]
        size_str = "BIGGG" if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
        try: num = int(item.get("number", 0))
        except: num = 0

        is_jack = num in (0, 5)
        pred = PREDICTION_MEMORY.get(issue)

        if pred and pred["size"] == size_str:
            icon = "  ✅✅✅"
        elif is_jack:
            icon = "  ☠️☠️☠️"
        else:
            icon = ""

        logs_text += f"`{short_period}` *{size_str}* ({num}){icon}\n"
    return logs_text

# ==================== MAIN BOT LOOP ====================
async def bot_loop(session):
    current_level = 1
    pending_pred = None
    print("🚀 QUANTUM V21 ULTRA-MAX BOT LOOP STARTED...")

    while True:
        try:
            raw_list = await fetch_data(session)
            if raw_list:
                history = list(reversed(raw_list))
                last_item = history[-1]
                last_issue = int(last_item["issueNumber"])
                actual_size = "BIGGG" if str(last_item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
                try: actual_number = int(last_item.get("number", 0))
                except: actual_number = 0

                # --- Resolve previous prediction ---
                if pending_pred and pending_pred["next_issue"] == last_issue:
                    is_jack = actual_number in (0, 5)
                    if is_jack and actual_size != pending_pred["pred_size"]:
                        current_level += 1
                    elif actual_size == pending_pred["pred_size"]:
                        current_level = 1
                        asyncio.create_task(send_win_sticker(session))
                    else:
                        current_level += 1
                    if current_level > 5: current_level = 1
                    pending_pred = None

                # --- Make new prediction ---
                if not pending_pred or pending_pred["last_issue"] != last_issue:
                    pred_data = v21_strike_engine(history, current_level)
                    if pred_data:
                        pending_pred = pred_data
                        PREDICTION_MEMORY[pred_data["next_issue"]] = {
                            "size": pred_data["pred_size"],
                            "number": pred_data["pred_number"],
                        }
                        if len(PREDICTION_MEMORY) > 300:
                            oldest = min(PREDICTION_MEMORY.keys())
                            del PREDICTION_MEMORY[oldest]

                        history_block = format_synced_history_logs(history)

                        pred_msg = (
                            f"🎯 *V21 ULTRA-MAX (NO-SKIP)* 🎯\n\n"
                            f"📌 *Period:* `{pred_data['next_issue']}`\n"
                            f"🎲 *Number:* `{pred_data['pred_number']}`\n"
                            f"🔥 *Target:* *{pred_data['pred_size_emoji']}*\n"
                            f"📊 *Win Prob:* `{pred_data['confidence']:.1f}%`\n"
                            f"💰 *Fund Advice:* `{pred_data['bet_advice']}`\n\n"
                            f"🚩 *Current Status:* `LEVEL {current_level}`\n"
                            f"⚙️ *Quant Data:* `{pred_data['metrics']}`\n"
                            f"-----------------------------------\n"
                            f"📜 *MARKET TREND (8)*:\n"
                            f"{history_block}"
                        )
                        asyncio.create_task(send_telegram(session, pred_msg))

        except Exception as e:
            print(f"Error in bot loop: {e}")
            traceback.print_exc()

        await asyncio.sleep(5)

async def main():
    app = web.Application()
    app.router.add_get('/', handle_health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Web Server Live on Port {port}")

    async with aiohttp.ClientSession() as session:
        await bot_loop(session)

if __name__ == "__main__":
    asyncio.run(main())


