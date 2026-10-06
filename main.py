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

PREDICTION_MEMORY = {}

# Adaptive engine performance tracker
ENGINE_STATS = {
    "markov":   {"hits": 0, "total": 0},
    "ngram":    {"hits": 0, "total": 0},
    "runlen":   {"hits": 0, "total": 0},
    "regime":   {"hits": 0, "total": 0},
    "streak":   {"hits": 0, "total": 0},
    "autocorr": {"hits": 0, "total": 0},
}

async def handle_health_check(request):
    return web.Response(text="QUANTUM V23 HYPERION ACTIVE", status=200)

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

# ==================== ENGINE 1: BAYESIAN MULTI-ORDER MARKOV (1-5) ====================
def markov_bayesian(outcomes):
    n = len(outcomes)
    if n < 12: return 0.5
    results, weights = [], []
    max_order = min(5, n // 5)
    for order in range(1, max_order + 1):
        last_seq = tuple(outcomes[-order:])
        big_count, small_count = 0, 0
        for i in range(n - order):
            if tuple(outcomes[i:i+order]) == last_seq:
                if outcomes[i + order] == 1: big_count += 1
                else: small_count += 1
        total = big_count + small_count
        if total >= 2:
            # Laplace (add-1) smoothing
            prob = (big_count + 1) / (total + 2)
            weight = order * math.log(total + 1)
            results.append(prob)
            weights.append(weight)
    if not results: return 0.5
    return sum(r * w for r, w in zip(results, weights)) / sum(weights)

# ==================== ENGINE 2: ADAPTIVE N-GRAM BAYESIAN (3-10) ====================
def ngram_bayesian(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    curr_str = "".join('B' if x == 1 else 'S' for x in outcomes)
    results, weights = [], []
    for L in range(3, 11):
        if n <= L + 2: continue
        tail = curr_str[-L:]
        big_w, small_w = 0.0, 0.0
        matches = 0
        for i in range(n - L):
            if curr_str[i:i+L] == tail:
                matches += 1
                recency = math.exp((i / n) * 6.0)
                if curr_str[i+L] == 'B': big_w += recency
                else: small_w += recency
        if matches >= 2:
            total_w = big_w + small_w
            prob = (big_w + 0.5) / (total_w + 1.0) if total_w > 0 else 0.5
            weight = L * math.log(matches + 1)
            results.append(prob)
            weights.append(weight)
    if not results: return 0.5
    return sum(r * w for r, w in zip(results, weights)) / sum(weights)

# ==================== ENGINE 3: RUN-LENGTH DISTRIBUTION ANALYSIS ====================
def run_length_engine(outcomes):
    if len(outcomes) < 15: return 0.5
    # Encode runs
    runs = []
    cur_val = outcomes[0]
    cur_len = 1
    for x in outcomes[1:]:
        if x == cur_val:
            cur_len += 1
        else:
            runs.append((cur_val, cur_len))
            cur_val = x
            cur_len = 1
    runs.append((cur_val, cur_len))
    if len(runs) < 3: return 0.5

    cur_run_val, cur_run_len = runs[-1]

    # Historical continuations of similar runs
    big_next, small_next = 0, 0
    for i in range(len(runs) - 1):
        val, rlen = runs[i]
        if val == cur_run_val and abs(rlen - cur_run_len) <= 1:
            if runs[i + 1][0] == 1: big_next += 1
            else: small_next += 1

    total = big_next + small_next
    if total >= 3:
        return (big_next + 1) / (total + 2)

    # Fallback: mean reversion based on run length
    base = sum(outcomes) / len(outcomes)
    if cur_run_len >= 6:
        return 0.12 if cur_run_val == 1 else 0.88
    elif cur_run_len >= 5:
        return 0.18 if cur_run_val == 1 else 0.82
    elif cur_run_len >= 4:
        return 0.28 if cur_run_val == 1 else 0.72
    elif cur_run_len >= 3:
        return 0.38 if cur_run_val == 1 else 0.62
    return base

# ==================== ENGINE 4: REGIME CHANGE DETECTOR ====================
def regime_engine(outcomes):
    n = len(outcomes)
    if n < 30: return 0.5, 0.0
    recent = outcomes[-15:]
    older = outcomes[-30:-15]
    recent_rate = sum(recent) / len(recent)
    older_rate = sum(older) / len(older)
    change_magnitude = abs(recent_rate - older_rate)

    if change_magnitude > 0.30:
        # Strong regime shift — follow new direction with confidence
        return recent_rate, change_magnitude

    # Blended approach
    blended = (recent_rate * 0.7) + (older_rate * 0.3)
    return blended, change_magnitude

# ==================== ENGINE 5: STREAK MOMENTUM ====================
def streak_engine(outcomes):
    if len(outcomes) < 6: return 0.5
    recent = outcomes[-7:]
    # Perfect zigzag
    if recent == [1,0,1,0,1,0,1]: return 0.15
    if recent == [0,1,0,1,0,1,0]: return 0.85
    # Full streak
    if sum(recent) == len(recent): return 0.20
    if sum(recent) == 0: return 0.80
    last5 = outcomes[-5:]
    if sum(last5) == 5: return 0.25
    if sum(last5) == 0: return 0.75
    last3 = outcomes[-3:]
    if sum(last3) == 3: return 0.35
    if sum(last3) == 0: return 0.65
    return 0.5

# ==================== ENGINE 6: AUTOCORRELATION / PERIODICITY ====================
def autocorr_engine(outcomes):
    n = len(outcomes)
    if n < 25: return 0.5, 0.0
    mean = sum(outcomes) / n
    var_sum = sum((x - mean) ** 2 for x in outcomes)
    if var_sum == 0: return 0.5, 0.0
    best_corr, best_lag = 0.0, 0
    for lag in range(2, min(16, n // 3)):
        num = sum((outcomes[i] - mean) * (outcomes[i + lag] - mean) for i in range(n - lag))
        d1 = sum((outcomes[i] - mean) ** 2 for i in range(n - lag))
        d2 = sum((outcomes[i + lag] - mean) ** 2 for i in range(n - lag))
        den = math.sqrt(d1 * d2)
        if den > 0:
            corr = num / den
            if abs(corr) > abs(best_corr):
                best_corr = corr
                best_lag = lag
    if best_lag > 0 and abs(best_corr) > 0.12:
        lagged_val = outcomes[n - best_lag]
        if best_corr > 0:
            return float(lagged_val), abs(best_corr)
        else:
            return float(1 - lagged_val), abs(best_corr)
    return 0.5, 0.0

# ==================== ENTROPY / PREDICTABILITY ====================
def entropy_predictability(outcomes, window=40):
    recent = outcomes[-window:]
    if len(recent) < 15: return 1.0
    p1 = sum(recent) / len(recent)
    if p1 == 0 or p1 == 1: return 0.0
    p0 = 1 - p1
    entropy = -(p1 * math.log2(p1) + p0 * math.log2(p0))
    return max(0.0, 1.0 - entropy)  # 0 = random, 1 = fully predictable

# ==================== ADAPTIVE WEIGHT COMPUTATION ====================
def get_adaptive_weights():
    base = {"markov": 0.20, "ngram": 0.22, "runlen": 0.15,
            "regime": 0.13, "streak": 0.10, "autocorr": 0.20}
    boosted = {}
    for name, bw in base.items():
        stats = ENGINE_STATS[name]
        if stats["total"] < 5:
            acc = 0.5
        else:
            acc = (stats["hits"] + 1) / (stats["total"] + 2)  # Laplace-smoothed accuracy
        boost = max(0.3, min(2.2, acc / 0.5))
        boosted[name] = bw * boost
    total = sum(boosted.values()) or 1.0
    return {k: v / total for k, v in boosted.items()}

# ==================== ADVANCED NUMBER PREDICTOR (3rd order Markov) ====================
def advanced_number_predictor(history_list, predicted_size):
    numbers = []
    for item in history_list:
        try:
            v = item.get("number", None)
            if v is None or v == "": numbers.append(-1)
            else: numbers.append(int(float(v)))
        except: numbers.append(-1)

    if len(numbers) < 40:
        return 8 if predicted_size == "BIGGG" else 2

    candidates = [5, 6, 7, 8, 9] if predicted_size == "BIGGG" else [0, 1, 2, 3, 4]
    K = len(candidates)

    # Markov orders 1, 2, 3
    mk = [{n: 0.0 for n in candidates} for _ in range(3)]
    for order in range(1, 4):
        if len(numbers) < order + 2: continue
        last_seq = tuple(numbers[-order:])
        counts = Counter()
        total = 0
        for i in range(len(numbers) - order):
            if tuple(numbers[i:i+order]) == last_seq:
                nxt = numbers[i + order]
                if nxt in candidates:
                    counts[nxt] += 1
                    total += 1
        for n in candidates:
            mk[order-1][n] = (counts.get(n, 0) + 1) / (total + K)  # Laplace

    # Recency weighted frequency
    recent = numbers[-80:]
    total_recent = len(recent)
    freq_w = {n: 0.0 for n in candidates}
    for idx, n in enumerate(recent):
        if n in candidates:
            freq_w[n] += math.exp((idx / total_recent) * 3.5)
    total_fw = sum(freq_w.values()) or 1.0
    freq_norm = {n: freq_w[n] / total_fw for n in candidates}

    # Global frequency
    scope = numbers[-250:]
    freq_all = Counter(scope)
    total_all = len(scope)
    global_norm = {n: (freq_all.get(n, 0) + 1) / (total_all + K) for n in candidates}

    weights = [0.15, 0.25, 0.20, 0.22, 0.18]
    scores = {}
    for n in candidates:
        scores[n] = (mk[0][n] * weights[0] + mk[1][n] * weights[1] +
                     mk[2][n] * weights[2] + freq_norm[n] * weights[3] +
                     global_norm[n] * weights[4])
    return max(scores, key=scores.get)

# ==================== MAIN STRIKE ENGINE (ENSEMBLE) ====================
def v23_strike_engine(history_list, current_level):
    outcomes = [1 if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else 0 for item in history_list]
    if len(outcomes) < 25: return None

    # Get all engine predictions
    p_markov = markov_bayesian(outcomes)
    p_ngram = ngram_bayesian(outcomes)
    p_runlen = run_length_engine(outcomes)
    p_regime, regime_strength = regime_engine(outcomes)
    p_streak = streak_engine(outcomes)
    p_autocorr, autocorr_strength = autocorr_engine(outcomes)

    # Adaptive weights (self-learning)
    weights = get_adaptive_weights()

    # Logit-space Bayesian ensemble
    def to_logit(p):
        p = max(0.02, min(0.98, p))
        return math.log(p / (1 - p))

    logit_sum = (
        to_logit(p_markov) * weights["markov"] +
        to_logit(p_ngram) * weights["ngram"] +
        to_logit(p_runlen) * weights["runlen"] +
        to_logit(p_regime) * weights["regime"] +
        to_logit(p_streak) * weights["streak"] +
        to_logit(p_autocorr) * weights["autocorr"]
    )
    final_prob_big = 1 / (1 + math.exp(-logit_sum))

    # Entropy-based confidence damping
    predictability = entropy_predictability(outcomes)
    if final_prob_big > 0.5:
        final_prob_big = 0.5 + (final_prob_big - 0.5) * (0.5 + predictability * 0.5)
    else:
        final_prob_big = 0.5 - (0.5 - final_prob_big) * (0.5 + predictability * 0.5)

    # Engine disagreement → confidence penalty
    engine_preds = [p_markov, p_ngram, p_runlen, p_regime, p_streak, p_autocorr]
    variance = sum((p - 0.5) ** 2 for p in engine_preds) / len(engine_preds)
    if variance > 0.06:
        # High disagreement → dampen
        final_prob_big = 0.5 + (final_prob_big - 0.5) * 0.7

    if final_prob_big >= 0.50:
        pred_size = "BIGGG"
        pred_size_emoji = "BIGGG 🟢"
        confidence_real = final_prob_big
    else:
        pred_size = "SMALL"
        pred_size_emoji = "SMALL 🔴"
        confidence_real = 1.0 - final_prob_big

    display_confidence = 55.0 + (confidence_real * 40.0)
    pred_number = advanced_number_predictor(history_list, pred_size)

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
        # Store engine probs for adaptive learning
        "engine_preds": {
            "markov": p_markov, "ngram": p_ngram, "runlen": p_runlen,
            "regime": p_regime, "streak": p_streak, "autocorr": p_autocorr
        },
        "metrics": (f"MKV:{p_markov:.2f} NGR:{p_ngram:.2f} RLL:{p_runlen:.2f} "
                    f"RGM:{p_regime:.2f} STR:{p_streak:.2f} ACR:{p_autocorr:.2f} "
                    f"ENT:{predictability:.2f}")
    }

# ==================== HISTORY FORMATTER ====================
def format_synced_history_logs(server_history):
    logs_text = ""
    for item in server_history[-8:]:
        issue = int(item["issueNumber"])
        short_period = str(issue)[-3:]
        size_str = "BIGGG" if str(item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
        try:
            raw = item.get("number", None)
            num = -1 if (raw is None or raw == "") else int(float(raw))
        except Exception:
            num = -1

        is_jack = num in (0, 5)
        pred = PREDICTION_MEMORY.get(issue)
        num_display = str(num) if num != -1 else "?"

        if pred and pred["size"] == size_str:
            icon = "  ✅✅✅"
        elif is_jack:
            icon = "  ☠️☠️☠️"
        else:
            icon = ""

        logs_text += f"`{short_period}` *{size_str}* ({num_display}){icon}\n"
    return logs_text

# ==================== ADAPTIVE LEARNING UPDATE ====================
def update_engine_stats(engine_preds, actual_big):
    """Update each engine's rolling accuracy"""
    for name, prob in engine_preds.items():
        predicted_big = prob >= 0.5
        hit = (predicted_big == actual_big)
        ENGINE_STATS[name]["total"] += 1
        if hit: ENGINE_STATS[name]["hits"] += 1
        # Rolling window reset (keep last 100)
        if ENGINE_STATS[name]["total"] > 100:
            ENGINE_STATS[name]["hits"] = int(ENGINE_STATS[name]["hits"] * 0.85)
            ENGINE_STATS[name]["total"] = int(ENGINE_STATS[name]["total"] * 0.85)

# ==================== MAIN BOT LOOP ====================
async def bot_loop(session):
    current_level = 1
    pending_pred = None
    print("🚀 QUANTUM V23 HYPERION BOT LOOP STARTED...")

    while True:
        try:
            raw_list = await fetch_data(session)
            if raw_list:
                history = list(reversed(raw_list))
                last_item = history[-1]
                last_issue = int(last_item["issueNumber"])
                actual_size = "BIGGG" if str(last_item.get("size", "")).upper() in ["BIG", "BIGGG"] else "SMALL"
                try:
                    raw = last_item.get("number", None)
                    actual_number = -1 if (raw is None or raw == "") else int(float(raw))
                except Exception:
                    actual_number = -1

                # --- Resolve previous prediction ---
                if pending_pred and pending_pred["next_issue"] == last_issue:
                    actual_big = (actual_size == "BIGGG")
                    # Adaptive learning update
                    if "engine_preds" in pending_pred:
                        update_engine_stats(pending_pred["engine_preds"], actual_big)

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

                # --- New prediction ---
                if not pending_pred or pending_pred["last_issue"] != last_issue:
                    pred_data = v23_strike_engine(history, current_level)
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

                        # Show engine weights in metrics (transparency)
                        weights = get_adaptive_weights()
                        weight_str = " ".join([f"{k[:3].upper()}:{v:.2f}" for k, v in weights.items()])

                        pred_msg = (
                            f"🎯 *V23 HYPERION STRIKE* 🎯\n\n"
                            f"📌 *Period:* `{pred_data['next_issue']}`\n"
                            f"🎲 *Number:* `{pred_data['pred_number']}`\n"
                            f"🔥 *Target:* *{pred_data['pred_size_emoji']}*\n"
                            f"📊 *Win Prob:* `{pred_data['confidence']:.1f}%`\n"
                            f"💰 *Fund Advice:* `{pred_data['bet_advice']}`\n\n"
                            f"🚩 *Current Status:* `LEVEL {current_level}`\n"
                            f"⚙️ *Engines:* `{pred_data['metrics']}`\n"
                            f"🧬 *Adaptive W:* `{weight_str}`\n"
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


