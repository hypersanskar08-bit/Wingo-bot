import json
import time
import math
import os
import asyncio
import traceback
import logging
from logging.handlers import RotatingFileHandler
from collections import Counter, deque
import aiohttp
from aiohttp import web

# ==================== CONFIGURATION & SECURITY (Point 30) ====================
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")
# =======================================================

# ==================== LOGGING SYSTEM (Point 31) ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QuantumV24")
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

# ==================== PERSISTENT STATE (Point 32, 33) ====================
STATE_FILE = "engine_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"State load error: {e}")
    return {
        "engine_stats": {}, "prediction_memory": {}, 
        "last_processed_issue": 0, "global_calibration_offset": 0.0
    }

def save_state(state):
    try:
        # Memory Limit (Point 33): Trim prediction memory to last 500
        if len(state.get("prediction_memory", {})) > 500:
            keys = sorted(state["prediction_memory"].keys())
            for k in keys[:-500]:
                del state["prediction_memory"][k]
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"State save error: {e}")

STATE = load_state()

# ==================== ENGINE DEFINITIONS (Point 4, 5, 6, 7, 8) ====================
ENGINES = ["markov", "ngram", "runlen", "regime", "streak", "autocorr", "alternation", "repeat"]

def init_engine_stats():
    for eng in ENGINES:
        if eng not in STATE["engine_stats"]:
            STATE["engine_stats"][eng] = {
                "total": 0, "correct": 0, "incorrect": 0, "accuracy": 0.5,
                "recent_total": 0, "recent_correct": 0, "recent_accuracy": 0.5,
                "brier_sum": 0.0, "brier_score": 0.25,
                "streak": 0, "best_regime": "", "worst_regime": "",
                "tp": 0, "fp": 0, "fn": 0, "tn": 0
            }

init_engine_stats()

# ==================== DATA QUALITY LAYER (Point 24) ====================
def validate_and_sanitize(raw_list):
    if not raw_list or not isinstance(raw_list, list): return None
    sanitized = []
    seen_issues = set()
    for item in reversed(raw_list): # Ensure chronological
        try:
            issue = int(item.get("issueNumber", 0))
            if issue == 0 or issue in seen_issues: continue
            size_raw = str(item.get("size", "")).upper()
            if size_raw not in ["BIG", "BIGGG", "SMALL"]: continue
            size = "BIGGG" if size_raw in ["BIG", "BIGGG"] else "SMALL"
            
            num_raw = item.get("number", None)
            num = -1 if (num_raw is None or num_raw == "") else int(float(num_raw))
            
            sanitized.append({"issue": issue, "size": size, "number": num})
            seen_issues.add(issue)
        except Exception:
            continue
    return sanitized

# ==================== FEATURE EXTRACTION (Point 2, 3, 9, 10) ====================
def detect_regime(outcomes):
    if len(outcomes) < 20: return "BALANCED"
    recent = outcomes[-20:]
    alternations = sum(1 for i in range(len(recent)-1) if recent[i] != recent[i+1])
    big_rate = sum(recent) / len(recent)
    
    if alternations > 14: return "ALTERNATING"
    if big_rate >= 0.7: return "BIG_HEAVY"
    if big_rate <= 0.3: return "SMALL_HEAVY"
    if len(set(recent)) == 1: return "LONG_STREAK"
    return "BALANCED"

def calculate_entropy(outcomes, window=40):
    recent = outcomes[-window:]
    if len(recent) < 15: return 1.0
    p1 = sum(recent) / len(recent)
    if p1 == 0 or p1 == 1: return 0.0
    p0 = 1 - p1
    entropy = -(p1 * math.log2(p1) + p0 * math.log2(p0))
    return max(0.0, 1.0 - entropy) # 0 = random, 1 = predictable

def multi_window_analysis(outcomes):
    windows = [10, 20, 40, 80, 150]
    signals = []
    for w in windows:
        if len(outcomes) >= w:
            signals.append(sum(outcomes[-w:]) / w)
    if not signals: return 0.5
    return sum(signals) / len(signals)

def periodicity_autocorr(outcomes):
    n = len(outcomes)
    if n < 30: return 0.5
    mean = sum(outcomes) / n
    best_corr, best_lag = 0.0, 0
    for lag in range(2, min(16, n // 3)):
        num = sum((outcomes[i] - mean) * (outcomes[i + lag] - mean) for i in range(n - lag))
        d1 = sum((outcomes[i] - mean) ** 2 for i in range(n - lag))
        d2 = sum((outcomes[i + lag] - mean) ** 2 for i in range(n - lag))
        den = math.sqrt(d1 * d2)
        if den > 0:
            corr = num / den
            if abs(corr) > abs(best_corr):
                best_corr, best_lag = corr, lag
    if best_lag > 0 and abs(best_corr) > 0.15:
        val = outcomes[n - best_lag]
        return float(val) if best_corr > 0 else float(1 - val)
    return 0.5

# ==================== CORE ENGINES (Point 4, 5, 6, 7, 8) ====================
def engine_markov(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    results, weights = [], []
    for order in range(1, 6):
        if n < order + 5: continue
        last_seq = tuple(outcomes[-order:])
        big, small = 0, 0
        for i in range(n - order):
            if tuple(outcomes[i:i+order]) == last_seq:
                if outcomes[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            prob = (big + 1) / (total + 2) # Laplace smoothing
            weight = order * math.log(total + 1)
            results.append(prob); weights.append(weight)
    return sum(r*w for r,w in zip(results, weights)) / sum(weights) if results else 0.5

def engine_ngram(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    curr_str = "".join('B' if x == 1 else 'S' for x in outcomes)
    results, weights = [], []
    for L in range(2, 11):
        if n <= L + 2: continue
        tail = curr_str[-L:]
        big_w, small_w, matches = 0.0, 0.0, 0
        for i in range(n - L):
            if curr_str[i:i+L] == tail:
                matches += 1
                recency = math.exp((i / n) * 6.0)
                if curr_str[i+L] == 'B': big_w += recency
                else: small_w += recency
        if matches >= 2:
            prob = (big_w + 0.5) / (big_w + small_w + 1.0)
            weight = L * math.log(matches + 1)
            results.append(prob); weights.append(weight)
    return sum(r*w for r,w in zip(results, weights)) / sum(weights) if results else 0.5

def engine_runlen(outcomes):
    if len(outcomes) < 15: return 0.5
    runs = []
    cur_val, cur_len = outcomes[0], 1
    for x in outcomes[1:]:
        if x == cur_val: cur_len += 1
        else: runs.append((cur_val, cur_len)); cur_val, cur_len = x, 1
    runs.append((cur_val, cur_len))
    if len(runs) < 3: return 0.5
    
    cur_val, cur_len = runs[-1]
    big, small = 0, 0
    for i in range(len(runs) - 1):
        if runs[i][0] == cur_val and abs(runs[i][1] - cur_len) <= 1:
            if runs[i+1][0] == 1: big += 1
            else: small += 1
    total = big + small
    if total >= 3: return (big + 1) / (total + 2)
    
    base = sum(outcomes) / len(outcomes)
    if cur_len >= 5: return 0.15 if cur_val == 1 else 0.85
    elif cur_len >= 4: return 0.25 if cur_val == 1 else 0.75
    elif cur_len >= 3: return 0.35 if cur_val == 1 else 0.65
    return base

def engine_regime(outcomes):
    if len(outcomes) < 30: return 0.5
    recent = sum(outcomes[-15:]) / 15
    older = sum(outcomes[-30:-15]) / 15
    if abs(recent - older) > 0.3: return recent
    return (recent * 0.7) + (older * 0.3)

def engine_streak(outcomes):
    if len(outcomes) < 6: return 0.5
    recent = outcomes[-6:]
    if sum(recent) == 6: return 0.20
    if sum(recent) == 0: return 0.80
    if recent == [1,0,1,0,1,0]: return 0.75
    if recent == [0,1,0,1,0,1]: return 0.25
    last3 = outcomes[-3:]
    if sum(last3) == 3: return 0.35
    if sum(last3) == 0: return 0.65
    return 0.5

def engine_autocorr(outcomes):
    return periodicity_autocorr(outcomes)

def engine_alternation(outcomes):
    n = len(outcomes)
    if n < 6: return 0.5
    recent = outcomes[-6:]
    if recent == [1,0,1,0,1,0] or recent == [0,1,0,1,0,1]:
        return 0.70 if recent[-1] == 0 else 0.30
    return 0.5

def engine_repeat(outcomes):
    n = len(outcomes)
    if n < 8: return 0.5
    curr_str = "".join('B' if x == 1 else 'S' for x in outcomes)
    for L in [3, 4]:
        if n < 2 * L: continue
        last_block = curr_str[-L:]
        if curr_str[-2*L:-L] == last_block:
            if last_block == 'B' * L: return 0.30 # Expect reversal after repeated B block
            if last_block == 'S' * L: return 0.70
    return 0.5

# ==================== ADAPTIVE META-LAYER (Point 11, 12, 13, 14, 15) ====================
def get_adaptive_weights(regime):
    base = {"markov":0.15, "ngram":0.15, "runlen":0.15, "regime":0.15, "streak":0.10, "autocorr":0.10, "alternation":0.10, "repeat":0.10}
    boosted = {}
    for eng, bw in base.items():
        stats = STATE["engine_stats"][eng]
        # Minimum sample size protection (Point 12)
        if stats["total"] < 10: acc = 0.5
        else: acc = (stats["correct"] + 1) / (stats["total"] + 2)
        
        # Recent vs Historical (Point 13)
        rec_acc = stats["recent_accuracy"] if stats["recent_total"] >= 5 else acc
        combined_acc = (rec_acc * 0.6) + (acc * 0.4)
        
        # Engine Degradation (Point 29) & Boost
        boost = max(0.3, min(2.5, combined_acc / 0.5))
        boosted[eng] = bw * boost
    total = sum(boosted.values()) or 1.0
    return {k: v/total for k,v in boosted.items()}

def update_engine_stats(engine_probs, actual_big, regime):
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"][eng]
        predicted_big = prob >= 0.5
        hit = (predicted_big == actual_big)
        
        stats["total"] += 1
        stats["recent_total"] += 1
        if hit: stats["correct"] += 1; stats["recent_correct"] += 1
        else: stats["incorrect"] += 1
        
        stats["accuracy"] = stats["correct"] / stats["total"]
        stats["recent_accuracy"] = stats["recent_correct"] / stats["recent_total"]
        
        # Brier Score (Point 21)
        p_actual = 1.0 if actual_big else 0.0
        stats["brier_sum"] += (prob - p_actual) ** 2
        stats["brier_score"] = stats["brier_sum"] / stats["total"]
        
        # Confusion Matrix (Point 22)
        if predicted_big and actual_big: stats["tp"] += 1
        elif not predicted_big and actual_big: stats["fn"] += 1
        elif predicted_big and not actual_big: stats["fp"] += 1
        else: stats["tn"] += 1
        
        # Streak tracking
        if hit: stats["streak"] = max(1, stats["streak"] + 1) if stats["streak"] > 0 else 1
        else: stats["streak"] = min(-1, stats["streak"] - 1) if stats["streak"] < 0 else -1
        
        # Regime compatibility
        if hit:
            if not stats["best_regime"] or stats["best_regime"] == regime: stats["best_regime"] = regime
        else:
            if not stats["worst_regime"] or stats["worst_regime"] == regime: stats["worst_regime"] = regime
            
        # Reset recent window (Point 33)
        if stats["recent_total"] > 50:
            stats["recent_correct"] = int(stats["recent_correct"] * 0.8)
            stats["recent_total"] = int(stats["recent_total"] * 0.8)

def meta_engine_predict(outcomes, regime):
    engine_probs = {
        "markov": engine_markov(outcomes), "ngram": engine_ngram(outcomes),
        "runlen": engine_runlen(outcomes), "regime": engine_regime(outcomes),
        "streak": engine_streak(outcomes), "autocorr": engine_autocorr(outcomes),
        "alternation": engine_alternation(outcomes), "repeat": engine_repeat(outcomes)
    }
    
    weights = get_adaptive_weights(regime)
    
    # Logit Ensemble
    def to_logit(p):
        p = max(0.01, min(0.99, p))
        return math.log(p / (1 - p))
    
    logit_sum = sum(to_logit(prob) * weights[eng] for eng, prob in engine_probs.items())
    final_prob_big = 1 / (1 + math.exp(-logit_sum))
    
    # Entropy Damping (Point 10)
    predictability = calculate_entropy(outcomes)
    if final_prob_big > 0.5:
        final_prob_big = 0.5 + (final_prob_big - 0.5) * (0.5 + predictability * 0.5)
    else:
        final_prob_big = 0.5 - (0.5 - final_prob_big) * (0.5 + predictability * 0.5)
        
    # Disagreement Detector (Point 14)
    preds = list(engine_probs.values())
    variance = sum((p - 0.5) ** 2 for p in preds) / len(preds)
    if variance > 0.06:
        final_prob_big = 0.5 + (final_prob_big - 0.5) * 0.7 # Dampen confidence
        
    # Confidence Calibration (Point 15, 23)
    # Replace arbitrary 55+conf*40 with calibrated confidence
    base_conf = max(final_prob_big, 1 - final_prob_big)
    # Apply global calibration offset based on historical Brier scores
    calibrated_conf = base_conf - (STATE.get("global_calibration_offset", 0.0))
    calibrated_conf = max(0.52, min(0.98, calibrated_conf)) # Bound between 52% and 98%
    
    pred_size = "BIGGG" if final_prob_big >= 0.5 else "SMALL"
    return pred_size, calibrated_conf, engine_probs

# ==================== ADVANCED NUMBER ENGINE (Point 16, 17) ====================
def advanced_number_predictor(history_list, predicted_size):
    numbers = []
    sizes = []
    for item in history_list:
        try:
            numbers.append(int(item.get("number", -1)))
            sizes.append(1 if item["size"] == "BIGGG" else 0)
        except: pass

    if len(numbers) < 40: return 8 if predicted_size == "BIGGG" else 2
    
    candidates = [5, 6, 7, 8, 9] if predicted_size == "BIGGG" else [0, 1, 2, 3, 4]
    K = len(candidates)
    
    # 1. Frequency & Recent Frequency (Point 16)
    freq_all = Counter(numbers)
    recent_nums = numbers[-50:]
    freq_recent = Counter(recent_nums)
    total_recent = len(recent_nums)
    
    # 2. Transitions (1st, 2nd, 3rd order)
    mk = [{n: 0.0 for n in candidates} for _ in range(3)]
    for order in range(1, 4):
        if len(numbers) < order + 2: continue
        last_seq = tuple(numbers[-order:])
        counts = Counter()
        total = 0
        for i in range(len(numbers) - order):
            if tuple(numbers[i:i+order]) == last_seq:
                nxt = numbers[i+order]
                if nxt in candidates: counts[nxt] += 1; total += 1
        for n in candidates: mk[order-1][n] = (counts.get(n, 0) + 1) / (total + K)
        
    # 3. Pair & Triple Patterns (Point 16)
    pair_counts = Counter()
    for i in range(len(numbers) - 2):
        if numbers[i] in candidates and numbers[i+1] in candidates:
            pair_counts[(numbers[i], numbers[i+1], numbers[i+2])] += 1
            
    # 4. Number + Size Relationship (Point 17)
    # Conditional distribution: Previous Size -> Number, Previous Number -> Next Size
    size_to_num = {0: Counter(), 1: Counter()}
    num_to_size = {n: Counter() for n in range(10)}
    for i in range(1, len(numbers)):
        prev_size = sizes[i-1]
        curr_num = numbers[i]
        if curr_num in candidates: size_to_num[prev_size][curr_num] += 1
        if numbers[i-1] in num_to_size: num_to_size[numbers[i-1]][sizes[i]] += 1

    # Score candidates
    scores = {}
    for n in candidates:
        s_freq_all = (freq_all.get(n, 0) + 1) / (len(numbers) + K)
        s_freq_recent = (freq_recent.get(n, 0) + 1) / (total_recent + K)
        s_mk1, s_mk2, s_mk3 = mk[0][n], mk[1][n], mk[2][n]
        
        # Conditional score
        last_size = sizes[-1]
        last_num = numbers[-1]
        s_size_cond = (size_to_num[last_size].get(n, 0) + 1) / (sum(size_to_num[last_size].values()) + K)
        s_num_cond = (num_to_size.get(last_num, {}).get(1 if predicted_size == "BIGGG" else 0, 0) + 1) / (sum(num_to_size.get(last_num, {}).values()) + 2)
        
        scores[n] = (s_freq_all * 0.10) + (s_freq_recent * 0.15) + (s_mk1 * 0.15) + (s_mk2 * 0.15) + (s_mk3 * 0.15) + (s_size_cond * 0.15) + (s_num_cond * 0.15)
        
    return max(scores, key=scores.get)

# ==================== HISTORY FORMATTER (Emoji Fix) ====================
def format_synced_history_logs(server_history):
    logs_text = ""
    for item in server_history[-8:]:
        issue = item["issue"]
        short_period = str(issue)[-3:]
        size_str = item["size"]
        num = item["number"]
        num_display = str(num) if num != -1 else "?"
        
        pred = STATE["prediction_memory"].get(str(issue))
        
        if pred and pred["size"] == size_str and pred.get("number") == num and num != -1:
            icon = "  ☠️☠️☠️"       # Perfect: size + number both match
        elif pred and pred["size"] == size_str:
            icon = "  ✅✅✅"       # Only size match
        else:
            icon = ""              # Loss
            
        logs_text += f"`{short_period}` *{size_str}* ({num_display}){icon}\n"
    return logs_text

# ==================== TELEGRAM REPORTING (Point 27) ====================
async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200: logger.error(f"Telegram send error: {await response.text()}")
    except Exception as e: logger.error(f"Telegram connection error: {e}")

async def send_win_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    payload = {"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as response:
            await response.text()
    except Exception as e: logger.error(f"Sticker error: {e}")

# ==================== API RELIABILITY (Point 25) ====================
async def fetch_data(session):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for attempt in range(1, 4):
        try:
            async with session.get(API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("code") == 0 and "data" in data and "list" in data["data"]:
                        return data["data"]["list"]
                logger.warning(f"API attempt {attempt} failed with status {response.status}")
        except Exception as e:
            logger.warning(f"API attempt {attempt} error: {e}")
        await asyncio.sleep(2 ** attempt) # Exponential backoff
    return None

# ==================== PREDICTION STATE MACHINE (Point 26) ====================
class BotStateMachine:
    def __init__(self):
        self.state = "WAITING_FOR_DATA"
        self.pending_pred = None
        
    async def run(self, session):
        while True:
            try:
                await self.step(session)
            except Exception as e:
                logger.error(f"State Machine Error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(5)
            
    async def step(self, session):
        raw_list = await fetch_data(session)
        if not raw_list: return
        
        history = validate_and_sanitize(raw_list)
        if not history or len(history) < 25: return
        
        last_item = history[-1]
        last_issue = last_item["issue"]
        
        # Duplicate Protection (Point 34)
        if last_issue == STATE["last_processed_issue"]: return
        
        outcomes = [1 if item["size"] == "BIGGG" else 0 for item in history]
        regime = detect_regime(outcomes)
        
        # --- RESULT EVALUATED ---
        if self.pending_pred and self.pending_pred["next_issue"] == last_issue:
            actual_size = last_item["size"]
            actual_num = last_item["number"]
            actual_big = (actual_size == "BIGGG")
            
            # Update Engine Stats
            update_engine_stats(self.pending_pred["engine_probs"], actual_big, regime)
            
            # Update global calibration offset (Point 15)
            brier = (self.pending_pred["prob_big"] - (1 if actual_big else 0)) ** 2
            STATE["global_calibration_offset"] = (STATE["global_calibration_offset"] * 0.95) + (brier * 0.05)
            
            if actual_size == self.pending_pred["pred_size"]:
                await send_win_sticker(session)
                
            self.pending_pred = None
            self.state = "WAITING_FOR_NEXT_RESULT"
            
        # --- ANALYZING & REPORT_CREATED ---
        if not self.pending_pred or self.pending_pred["last_issue"] != last_issue:
            self.state = "ANALYZING"
            pred_size, confidence, engine_probs = meta_engine_predict(outcomes, regime)
            pred_number = advanced_number_predictor(history, pred_size)
            next_issue = last_issue + 1
            
            self.pending_pred = {
                "last_issue": last_issue, "next_issue": next_issue,
                "pred_size": pred_size, "pred_number": pred_number,
                "prob_big": 1.0 if pred_size == "BIGGG" else 0.0,
                "engine_probs": engine_probs
            }
            
            # Save to Prediction Memory
            STATE["prediction_memory"][str(next_issue)] = {
                "size": pred_size, "number": pred_number
            }
            
            # --- TELEGRAM REPORTING UPGRADE (Point 27) ---
            history_block = format_synced_history_logs(history)
            engine_consensus = " ".join([f"{k[:3].upper()}:{v:.2f}" for k,v in engine_probs.items()])
            
            # Automatic Engine Self-Audit (Point 28)
            best_eng = max(ENGINES, key=lambda e: STATE["engine_stats"][e]["recent_accuracy"])
            worst_eng = min(ENGINES, key=lambda e: STATE["engine_stats"][e]["recent_accuracy"])
            audit_str = f"Best: {best_eng.upper()} ({STATE['engine_stats'][best_eng]['recent_accuracy']*100:.0f}%) | Worst: {worst_eng.upper()} ({STATE['engine_stats'][worst_eng]['recent_accuracy']*100:.0f}%)"
            
            pred_msg = (
                f"🎯 *QUANTUM V24 META-ENGINE* 🎯\n"
                f"`===================================`\n"
                f"📌 *Period:* `{next_issue}`\n"
                f"🎲 *Number:* `{pred_number}`\n"
                f"🔥 *Target:* *{'BIGGG 🟢' if pred_size == 'BIGGG' else 'SMALL 🔴'}*\n"
                f"📊 *Calibrated Prob:* `{confidence*100:.1f}%`\n"
                f"📈 *Regime:* `{regime}` | *Entropy:* `{calculate_entropy(outcomes):.2f}`\n"
                f"`===================================`\n"
                f"🧠 *Engine Consensus:*\n`{engine_consensus}`\n"
                f"📋 *Self-Audit:* `{audit_str}`\n"
                f"`===================================`\n"
                f"📜 *MARKET TREND (8)*:\n"
                f"{history_block}"
            )
            asyncio.create_task(send_telegram(session, pred_msg))
            self.state = "REPORT_CREATED"
            
        STATE["last_processed_issue"] = last_issue
        save_state(STATE)
        self.state = "WAITING_FOR_NEXT_RESULT"

# ==================== WALK-FORWARD VALIDATION (Point 1, 20) ====================
async def walk_forward_warmup(session):
    """Runs a historical simulation on startup to warm up engine weights"""
    logger.info("Starting Walk-Forward Validation...")
    raw_list = await fetch_data(session)
    if not raw_list: return
    history = validate_and_sanitize(raw_list)
    if len(history) < 100: return
    
    # Simulate on last 100 periods
    for i in range(50, len(history) - 1):
        outcomes = [1 if item["size"] == "BIGGG" else 0 for item in history[:i]]
        regime = detect_regime(outcomes)
        pred_size, _, engine_probs = meta_engine_predict(outcomes, regime)
        
        actual_big = (history[i]["size"] == "BIGGG")
        update_engine_stats(engine_probs, actual_big, regime)
    logger.info("Walk-Forward Validation Complete. Engine weights warmed up.")

# ==================== MAIN ====================
async def handle_health_check(request):
    return web.Response(text="QUANTUM V24 META-ENGINE ACTIVE", status=200)

async def main():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"✅ Web Server Live on Port {port}")

    async with aiohttp.ClientSession() as session:
        await walk_forward_warmup(session)
        bot = BotStateMachine()
        await bot.run(session)

if __name__ == "__main__":
    asyncio.run(main())


