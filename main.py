import json
import time
import math
import os
import asyncio
import traceback
import logging
from logging.handlers import RotatingFileHandler
from collections import Counter
import aiohttp
from aiohttp import web

# ==================== CONFIG ====================
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")
# ================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QuantumV28_4")
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

STATE_FILE = "engine_state_v28_4.json"
PATTERN_SIG_LEN = 14
PATTERN_LENGTHS = [4, 6, 8, 10, 12, 14]
ERROR_THRESHOLD = 2
SIMILAR_ERROR_RADIUS = 1
HOT_NUMBER_WINDOW = 20

BET_LEVELS = [1.0, 2.5, 6.0]
MAX_LEVEL = 3

MIN_PATTERN_SAMPLES = 3
MAX_BOOST = 4.0
FLIP_BELOW_ACCURACY = 0.58
FLIP_MIN_SAMPLES = 5

# ==================== STATE ====================
def default_engine_state():
    return {
        "alpha": 1.0, "beta": 1.0,
        "hits": 0, "misses": 0, "total": 0,
        "recent_hits": 0, "recent_total": 0,
        "brier_sum": 0.0, "gradient_weight": 1.0,
        "regime_stats": {}
    }

# 🔥 8 Engines — added rhythm, hot_cold, gambler_instinct
ENGINES = ["pattern", "trend", "number_seq", "hot_number", "streak_break",
           "rhythm", "hot_cold", "gambler_instinct"]

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f: s = json.load(f)
            for eng in ENGINES:
                if eng not in s.get("engine_stats", {}):
                    s.setdefault("engine_stats", {})[eng] = default_engine_state()
            s.setdefault("total_wins", 0); s.setdefault("total_losses", 0)
            s.setdefault("current_loss_streak", 0); s.setdefault("max_b2b_loss", 0)
            s.setdefault("current_level", 1)
            s.setdefault("pattern_memory", {}); s.setdefault("error_patterns", {})
            s.setdefault("calibration_offset", 0.0); s.setdefault("learned_overrides", 0)
            s.setdefault("number_memory", {}); s.setdefault("hot_numbers", {})
            s.setdefault("pattern_strength", {})
            s.setdefault("pattern_flips", 0)
            s.setdefault("pattern_flips_won", 0)
            # 🔥 Gambler mind state
            s.setdefault("bot_recent_form", [])   # Last 10 results (1=win, 0=loss)
            s.setdefault("jack_history", [])       # Last N jack positions
            return s
        except Exception as e:
            logger.error(f"State load error: {e}")
    return {
        "engine_stats": {eng: default_engine_state() for eng in ENGINES},
        "prediction_memory": {}, "last_processed_issue": 0,
        "calibration_offset": 0.0, "total_wins": 0, "total_losses": 0,
        "current_loss_streak": 0, "max_b2b_loss": 0, "current_level": 1,
        "pattern_memory": {}, "error_patterns": {}, "gradient_lr": 0.01,
        "learned_overrides": 0, "number_memory": {}, "hot_numbers": {},
        "pattern_strength": {}, "pattern_flips": 0, "pattern_flips_won": 0,
        "bot_recent_form": [], "jack_history": []
    }

def save_state(state):
    try:
        for key, cap in [("prediction_memory", 500), ("pattern_memory", 5000),
                         ("error_patterns", 500), ("number_memory", 5000),
                         ("pattern_strength", 5000), ("bot_recent_form", 20),
                         ("jack_history", 50)]:
            if len(state.get(key, {})) > cap:
                if isinstance(state[key], list):
                    state[key] = state[key][-cap:]
                else:
                    for k in sorted(state[key].keys())[:-cap]:
                        del state[key][k]
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"State save error: {e}")

STATE = load_state()

# ==================== SIGNATURES ====================
def pattern_signature(outcomes, length=PATTERN_SIG_LEN):
    if len(outcomes) < length: return None
    return "".join('B' if x else 'S' for x in outcomes[-length:])

def short_pattern_sig(outcomes, length=6):
    if len(outcomes) < length: return None
    return "".join('B' if x else 'S' for x in outcomes[-length:])

def number_signature(history, length=5):
    nums = [h["number"] for h in history if h["number"] >= 0]
    if len(nums) < length: return None
    return "-".join(str(n) for n in nums[-length:])

def hamming_distance(s1, s2):
    if not s1 or not s2 or len(s1) != len(s2): return 999
    return sum(1 for a, b in zip(s1, s2) if a != b)

def find_similar_error_signatures(current_sig, radius=SIMILAR_ERROR_RADIUS):
    if not current_sig: return []
    matches = []
    for sig, data in STATE.get("error_patterns", {}).items():
        if data.get("fail_count", 0) >= ERROR_THRESHOLD:
            d = hamming_distance(sig, current_sig)
            if 0 < d <= radius: matches.append((sig, data, d))
    return matches

# ==================== DATA VALIDATION ====================
def validate_and_sanitize(raw_list):
    if not raw_list or not isinstance(raw_list, list): return None
    sanitized, seen = [], set()
    for item in reversed(raw_list):
        try:
            issue = int(item.get("issueNumber", 0))
            if issue == 0 or issue in seen: continue
            size_raw = str(item.get("size", "")).upper()
            if size_raw not in ["BIG", "BIGGG", "SMALL"]: continue
            size = 1 if size_raw in ["BIG", "BIGGG"] else 0
            num_raw = item.get("number", None)
            num = -1 if (num_raw is None or num_raw == "") else int(float(num_raw))
            sanitized.append({"issue": issue, "size": size, "number": num})
            seen.add(issue)
        except Exception: continue
    return sanitized

# ==================== REGIME & ENTROPY ====================
def detect_regime(outcomes):
    if len(outcomes) < 20: return "BALANCED"
    recent = outcomes[-20:]
    alternations = sum(1 for i in range(len(recent)-1) if recent[i] != recent[i+1])
    big_rate = sum(recent) / len(recent)
    if alternations >= 15: return "ALTERNATING"
    if big_rate >= 0.70: return "BIG_HEAVY"
    if big_rate <= 0.30: return "SMALL_HEAVY"
    if all(x == recent[0] for x in recent): return "LONG_STREAK"
    return "BALANCED"

def calculate_entropy(outcomes, window=40):
    recent = outcomes[-window:]
    if len(recent) < 15: return 1.0
    p1 = sum(recent) / len(recent)
    if p1 == 0 or p1 == 1: return 0.0
    return max(0.0, 1.0 - (-(p1*math.log2(p1) + (1-p1)*math.log2(1-p1))))

# ==================== PATTERN STRENGTH ====================
def compute_pattern_strength(sig):
    stats = STATE.get("pattern_strength", {}).get(sig)
    if not stats: return None, 0.0, 0
    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    total = hits + misses
    if total < MIN_PATTERN_SAMPLES: return None, 0.0, total
    acc = (hits + 1) / (total + 2)
    freq_factor = min(1.0, total / 15.0)
    acc_factor = max(0.0, (acc - 0.50) * 3.0)
    strength = freq_factor * acc_factor
    return acc, min(1.0, strength), total

def get_pattern_priority(outcomes):
    sig = short_pattern_sig(outcomes, 6)
    if not sig: return None, 0.0, 0
    return compute_pattern_strength(sig)

def check_pattern_flip(outcomes, p_big):
    sig = short_pattern_sig(outcomes, 6)
    if not sig: return p_big, False, ""
    stats = STATE.get("pattern_strength", {}).get(sig)
    if not stats: return p_big, False, ""
    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    total = hits + misses
    if total < FLIP_MIN_SAMPLES: return p_big, False, ""
    acc = (hits + 1) / (total + 2)
    if acc < FLIP_BELOW_ACCURACY:
        reason = f"ACC {acc*100:.0f}%<{FLIP_BELOW_ACCURACY*100:.0f}% (n={total})"
        return 1.0 - p_big, True, reason
    return p_big, False, ""

# ==================== ENGINE 1: PATTERN ====================
def engine_pattern(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    sig_full = "".join('B' if x else 'S' for x in outcomes)
    results, weights = [], []
    for L in PATTERN_LENGTHS:
        if n < L + 3: continue
        tail = sig_full[-L:]
        big = small = 0
        for i in range(n - L):
            if sig_full[i:i+L] == tail:
                if outcomes[i+L] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 3:
            prob = (big + 1) / (total + 2)
            deviation = abs(prob - 0.5)
            if deviation < 0.10: continue
            freq_boost = min(2.0, total / 3.0)
            w = (L ** 1.5) * math.log(total + 1) * freq_boost
            results.append(prob); weights.append(w)
    sig14 = pattern_signature(outcomes, 14)
    if sig14 and sig14 in STATE.get("pattern_memory", {}):
        pm = STATE["pattern_memory"][sig14]
        pb = pm.get("next_big", 0); ps = pm.get("next_small", 0)
        if pb + ps >= 3:
            prob_db = (pb + 1) / (pb + ps + 2)
            deviation = abs(prob_db - 0.5)
            if deviation >= 0.10:
                w = 14 ** 1.5 * math.log(pb + ps + 1)
                results.append(prob_db); weights.append(w)
    p_acc, p_strength, p_samples = get_pattern_priority(outcomes)
    if p_acc is not None and p_strength > 0.05:
        boost = 1.0 + p_strength * (MAX_BOOST - 1.0)
        amplified_prob = 0.5 + (p_acc - 0.5) * (1.0 + p_strength)
        amplified_prob = max(0.05, min(0.95, amplified_prob))
        results.append(amplified_prob)
        weights.append(6 ** 1.7 * boost * math.log(p_samples + 1))
    if not results: return 0.5
    return sum(r*w for r,w in zip(results, weights)) / sum(weights)

# ==================== ENGINE 2: TREND ====================
def engine_trend(outcomes):
    n = len(outcomes)
    if n < 20: return 0.5
    def ema(arr, span):
        a = 2 / (span + 1); e = arr[0]
        for x in arr[1:]: e = a * x + (1 - a) * e
        return e
    ema3 = ema(outcomes[-20:], 3); ema5 = ema(outcomes[-20:], 5)
    ema13 = ema(outcomes[-20:], 13); ema21 = ema(outcomes[-20:], 21)
    macd = (ema3 - ema13) + (ema5 - ema21)
    macd_signal = math.tanh(macd * 4.0)
    velocity = 0.0
    if n >= 10:
        last5 = sum(outcomes[-5:]) / 5; prev5 = sum(outcomes[-10:-5]) / 5
        velocity = (last5 - prev5) * 2.0
    recent = outcomes[-6:]
    burst = 0.0
    if sum(recent[-3:]) == 3: burst = 0.7
    elif sum(recent[-3:]) == 0: burst = -0.7
    combined = macd_signal * 0.5 + velocity * 0.3 + burst * 0.2
    return max(0.15, min(0.85, 0.5 + 0.5 * math.tanh(combined * 2.0)))

# ==================== ENGINE 3: NUMBER SEQUENCE ====================
def engine_number_sequence(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 20: return 0.5
    last_nums = numbers[-3:]
    if len(last_nums) < 3: return 0.5
    if last_nums[0] < last_nums[1] < last_nums[2]: pattern = "ASC"
    elif last_nums[0] > last_nums[1] > last_nums[2]: pattern = "DESC"
    elif last_nums[0] == last_nums[1] == last_nums[2]: pattern = "SAME3"
    elif last_nums[1] == last_nums[2]: pattern = "SAME2"
    elif abs(last_nums[0] - last_nums[1]) == 1 and abs(last_nums[1] - last_nums[2]) == 1: pattern = "SEQ"
    else: pattern = "OTHER"
    big_count = small_count = 0.0
    for i in range(len(numbers) - 3):
        seq = numbers[i:i+3]
        match = False
        if pattern == "ASC" and seq[0] < seq[1] < seq[2]: match = True
        elif pattern == "DESC" and seq[0] > seq[1] > seq[2]: match = True
        elif pattern == "SAME3" and seq[0] == seq[1] == seq[2]: match = True
        elif pattern == "SAME2" and seq[1] == seq[2]: match = True
        elif pattern == "SEQ" and abs(seq[0]-seq[1]) == 1 and abs(seq[1]-seq[2]) == 1: match = True
        if match and i + 3 < len(sizes):
            recency_w = math.exp((i / len(numbers)) * 3.0)
            if sizes[i+3] == 1: big_count += recency_w
            else: small_count += recency_w
    total = big_count + small_count
    if total < 1.0: return 0.5
    return (big_count + 0.5) / (total + 1.0)

# ==================== ENGINE 4: HOT NUMBER ====================
def engine_hot_number(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    if len(numbers) < HOT_NUMBER_WINDOW: return 0.5
    recent_nums = numbers[-HOT_NUMBER_WINDOW:]
    freq = Counter(recent_nums)
    top = freq.most_common(3)
    if not top: return 0.5
    big_weight = 0.0; small_weight = 0.0
    for num, cnt in top:
        if num >= 5: big_weight += cnt
        else: small_weight += cnt
    most_freq_num, most_freq_cnt = top[0]
    dominance = most_freq_cnt / len(recent_nums)
    base = big_weight / (big_weight + small_weight) if (big_weight + small_weight) > 0 else 0.5
    if dominance >= 0.30:
        side_boost = 0.15 if most_freq_num >= 5 else -0.15
        base = max(0.1, min(0.9, base + side_boost))
    return base

# ==================== ENGINE 5: STREAK BREAK ====================
def engine_streak_break(outcomes):
    if len(outcomes) < 6: return 0.5
    recent = outcomes[-10:]
    streak_len = 1
    streak_val = recent[-1]
    for i in range(len(recent) - 2, -1, -1):
        if recent[i] == streak_val: streak_len += 1
        else: break
    if streak_len >= 6: return 0.15 if streak_val == 1 else 0.85
    elif streak_len == 5: return 0.22 if streak_val == 1 else 0.78
    elif streak_len == 4: return 0.32 if streak_val == 1 else 0.68
    elif streak_len == 3: return 0.42 if streak_val == 1 else 0.58
    return 0.5

# ==================== 🥁 ENGINE 6: RHYTHM ====================
def engine_rhythm(outcomes):
    """
    Gambler's "feel" for the game's rhythm.
    Detects periodic waves/cycles in the outcome sequence.
    Uses autocorrelation at small lags to find the "beat".
    """
    n = len(outcomes)
    if n < 20: return 0.5
    
    recent = outcomes[-30:] if n >= 30 else outcomes
    m = len(recent)
    mean = sum(recent) / m
    
    # Find best lag (2-8) with strongest correlation
    best_lag = 0
    best_corr = 0.0
    for lag in range(2, 9):
        if lag >= m - 2: continue
        num = sum((recent[i] - mean) * (recent[i+lag] - mean) for i in range(m - lag))
        d1 = sum((recent[i] - mean) ** 2 for i in range(m - lag))
        d2 = sum((recent[i+lag] - mean) ** 2 for i in range(m - lag))
        den = math.sqrt(d1 * d2)
        if den > 0:
            corr = num / den
            if abs(corr) > abs(best_corr):
                best_corr = corr
                best_lag = lag
    
    if best_lag == 0 or abs(best_corr) < 0.20:
        return 0.5
    
    # Predict based on rhythm: what was at position (n - best_lag)?
    if best_corr > 0:
        # Positive correlation: next follows the pattern
        target = recent[-best_lag]
    else:
        # Negative: next is opposite
        target = 1 - recent[-best_lag]
    
    # Strength of signal based on correlation
    strength = min(0.35, abs(best_corr) * 0.5)
    if target == 1:
        return 0.5 + strength
    else:
        return 0.5 - strength

# ==================== 🔥 ENGINE 7: HOT/COLD HAND ====================
def engine_hot_cold(outcomes):
    """
    Tracks bot's OWN recent prediction form.
    - Hot hand: last 4+ predictions won → follow the bot's current engine consensus with boost
    - Cold hand: last 4+ predictions lost → dampen confidence
    Returns probability based on recent form and current trend.
    """
    form = STATE.get("bot_recent_form", [])
    if len(form) < 3:
        return 0.5
    
    # Last N results (0=loss, 1=win)
    recent_form = form[-6:]
    wins = sum(recent_form)
    total = len(recent_form)
    win_rate = wins / total
    
    # Hot hand detection
    hot_streak = 0
    for r in reversed(form):
        if r == 1: hot_streak += 1
        else: break
    
    cold_streak = 0
    for r in reversed(form):
        if r == 0: cold_streak += 1
        else: break
    
    # Base prediction: follow recent outcome momentum
    # When bot is on hot streak, current trend is working. Boost that direction.
    if len(outcomes) < 3: return 0.5
    recent_outcomes = outcomes[-3:]
    momentum = sum(recent_outcomes) / 3.0
    
    # Adjust based on bot's form
    if hot_streak >= 3:
        # Bot is reading market well — boost current momentum
        p_big = 0.5 + (momentum - 0.5) * 1.5
    elif cold_streak >= 3:
        # Bot is misreading — reverse the momentum signal
        p_big = 0.5 - (momentum - 0.5) * 1.0
    else:
        p_big = 0.5 + (momentum - 0.5) * 0.5
    
    return max(0.15, min(0.85, p_big))

# ==================== 🎰 ENGINE 8: GAMBLER INSTINCT ====================
def engine_gambler_instinct(history):
    """
    Combines multiple "gambler sense" signals:
    - Jack timing (how many periods since 0 or 5 appeared)
    - Number spread (are numbers spreading or clustering?)
    - Recent choppiness
    - Session pressure (wins vs losses)
    """
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 20 or len(sizes) < 20: return 0.5
    
    signals = []
    weights = []
    
    # Signal 1: Jack timing
    # Find last jack (0 or 5) position
    periods_since_jack = 0
    for i in range(len(numbers) - 1, -1, -1):
        if numbers[i] in (0, 5):
            periods_since_jack = len(numbers) - 1 - i
            break
    else:
        periods_since_jack = 20
    
    # Jacks occur ~20% of time (2 numbers out of 10). Mean gap = 5 periods.
    # If > 8 periods since last jack, higher chance of next being jack
    if periods_since_jack >= 8:
        jack_pressure = min(1.0, (periods_since_jack - 5) / 10.0)
        # Jacks: 0 (SMALL), 5 (BIG). Look at last two jacks to see which is "due"
        recent_jacks = [n for n in numbers[-30:] if n in (0, 5)]
        if recent_jacks:
            last_jack = recent_jacks[-1]
            # If last jack was 0, expect 5 next (BIG)
            # If last jack was 5, expect 0 next (SMALL)
            jack_bias = 0.5 + (0.15 * jack_pressure if last_jack == 0 else -0.15 * jack_pressure)
            signals.append(jack_bias)
            weights.append(0.6)
    
    # Signal 2: Number spread
    # If recent numbers are clustered (few unique), next is likely different
    recent_nums = numbers[-12:]
    unique_ratio = len(set(recent_nums)) / len(recent_nums)
    if unique_ratio < 0.5:
        # Very clustered — expect spread, mixed signal
        pass
    elif unique_ratio > 0.85:
        # Well spread — normal
        pass
    
    # Signal 3: Choppiness (alternating pattern)
    recent_sizes = sizes[-15:]
    alternations = sum(1 for i in range(len(recent_sizes)-1) if recent_sizes[i] != recent_sizes[i+1])
    chop_ratio = alternations / (len(recent_sizes) - 1)
    if chop_ratio > 0.75:
        # Very choppy — follow last (continuation of chop)
        next_pred = 1 - recent_sizes[-1]
        signals.append(0.5 + (0.10 if next_pred == 1 else -0.10))
        weights.append(0.4)
    
    # Signal 4: Session pressure (if we're losing badly, be more conservative)
    total_wins = STATE.get("total_wins", 0)
    total_losses = STATE.get("total_losses", 0)
    if total_wins + total_losses >= 10:
        session_wr = total_wins / (total_wins + total_losses)
        if session_wr < 0.45:
            # Losing session — trust the last outcome less
            last = sizes[-1]
            counter = 1 - last
            signals.append(0.5 + (0.08 if counter == 1 else -0.08))
            weights.append(0.3)
    
    if not signals: return 0.5
    
    total_w = sum(weights)
    p_big = sum(s * w for s, w in zip(signals, weights)) / total_w
    return max(0.20, min(0.80, p_big))

# ==================== ADAPTIVE WEIGHTS ====================
REGIME_EXPERT_WEIGHTS = {
    "ALTERNATING":   {"pattern": 0.22, "trend": 0.09, "number_seq": 0.14, "hot_number": 0.10, "streak_break": 0.17, "rhythm": 0.13, "hot_cold": 0.08, "gambler_instinct": 0.07},
    "BIG_HEAVY":     {"pattern": 0.19, "trend": 0.17, "number_seq": 0.13, "hot_number": 0.11, "streak_break": 0.15, "rhythm": 0.10, "hot_cold": 0.08, "gambler_instinct": 0.07},
    "SMALL_HEAVY":   {"pattern": 0.19, "trend": 0.17, "number_seq": 0.13, "hot_number": 0.11, "streak_break": 0.15, "rhythm": 0.10, "hot_cold": 0.08, "gambler_instinct": 0.07},
    "LONG_STREAK":   {"pattern": 0.16, "trend": 0.13, "number_seq": 0.10, "hot_number": 0.09, "streak_break": 0.26, "rhythm": 0.10, "hot_cold": 0.09, "gambler_instinct": 0.07},
    "BALANCED":      {"pattern": 0.22, "trend": 0.12, "number_seq": 0.13, "hot_number": 0.11, "streak_break": 0.15, "rhythm": 0.11, "hot_cold": 0.09, "gambler_instinct": 0.07},
}

def get_adaptive_weights(regime):
    base = REGIME_EXPERT_WEIGHTS.get(regime, REGIME_EXPERT_WEIGHTS["BALANCED"]).copy()
    boosted = {}
    for eng, bw in base.items():
        stats = STATE["engine_stats"].get(eng, default_engine_state())
        a, b = stats.get("alpha", 1.0), stats.get("beta", 1.0)
        pm = a / (a + b)
        ra = stats["recent_hits"] / stats["recent_total"] if stats.get("recent_total", 0) >= 5 else pm
        combined = pm * 0.5 + ra * 0.5
        grad_w = stats.get("gradient_weight", 1.0)
        boost = max(0.3, min(2.5, combined / 0.5)) * grad_w
        boosted[eng] = bw * boost
    total = sum(boosted.values()) or 1.0
    return {k: v/total for k, v in boosted.items()}

def combine_engines(engine_probs, weights):
    total_w = sum(weights.values()) or 1.0
    return sum(engine_probs[e] * weights[e] for e in engine_probs) / total_w

def gradient_update(engine_probs, actual_big, lr=0.01):
    actual = 1.0 if actual_big else 0.0
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"].setdefault(eng, default_engine_state())
        grad = (prob - actual) * (prob - 0.5) * 2.0
        old_w = stats.get("gradient_weight", 1.0)
        stats["gradient_weight"] = max(0.3, min(3.0, old_w - lr * grad))

# ==================== ANTI-ERROR ====================
def check_anti_error(outcomes, current_p_big):
    sig = pattern_signature(outcomes)
    if not sig: return current_p_big, False, ""
    err = STATE.get("error_patterns", {}).get(sig)
    if err and err.get("fail_count", 0) >= ERROR_THRESHOLD:
        fail_ratio = err.get("fail_count", 0) / max(1, err.get("total", 1))
        if fail_ratio >= 0.6:
            return 1.0 - current_p_big, True, f"EXACT ({err['fail_count']}x)"
    similar = find_similar_error_signatures(sig, radius=SIMILAR_ERROR_RADIUS)
    if similar:
        ratio = sum(s[1].get("fail_count", 0) for s in similar) / max(1, sum(s[1].get("total", 1) for s in similar))
        if ratio >= 0.65:
            return current_p_big * 0.7 + 0.5 * 0.3, False, f"SIMILAR ({len(similar)})"
    return current_p_big, False, ""

# ==================== SIGNAL LABEL ====================
def get_signal_label(confidence):
    deviation = confidence - 0.50
    if deviation < 0.02: return "🟥 NONE"
    elif deviation < 0.05: return "🟧 WEAK"
    elif deviation < 0.10: return "🟨 MODERATE"
    elif deviation < 0.15: return "🟩 STRONG"
    else: return "🟢 V.STRONG"

# ==================== MAIN PREDICTOR ====================
def predict_next(history):
    outcomes = [h["size"] for h in history]

    engine_probs = {
        "pattern": engine_pattern(outcomes),
        "trend": engine_trend(outcomes),
        "number_seq": engine_number_sequence(history),
        "hot_number": engine_hot_number(history),
        "streak_break": engine_streak_break(outcomes),
        "rhythm": engine_rhythm(outcomes),
        "hot_cold": engine_hot_cold(outcomes),
        "gambler_instinct": engine_gambler_instinct(history),
    }

    regime = detect_regime(outcomes)
    weights = get_adaptive_weights(regime)
    p_big = combine_engines(engine_probs, weights)

    predictability = calculate_entropy(outcomes)
    if p_big > 0.5:
        p_big = 0.5 + (p_big - 0.5) * (0.5 + predictability * 0.5)
    else:
        p_big = 0.5 - (0.5 - p_big) * (0.5 + predictability * 0.5)

    p_big, override_active, override_reason = check_anti_error(outcomes, p_big)
    p_big, pat_flip_active, pat_flip_reason = check_pattern_flip(outcomes, p_big)

    base_conf = max(p_big, 1 - p_big)
    cal_pen = STATE.get("calibration_offset", 0.0) * 0.5
    conf = max(0.50, min(0.95, base_conf - cal_pen))
    signal_label = get_signal_label(conf)

    pred_size = 1 if p_big >= 0.5 else 0

    numbers = [h["number"] for h in history if h["number"] >= 0]
    hot_top = None
    if len(numbers) >= HOT_NUMBER_WINDOW:
        top = Counter(numbers[-HOT_NUMBER_WINDOW:]).most_common(1)
        if top: hot_top = top[0]

    p_acc, p_strength, p_samples = get_pattern_priority(outcomes)
    strength_note = ""
    strength_label = ""
    if p_acc is not None:
        strength_note = f"Acc:{p_acc*100:.0f}% (n={p_samples})"
        if p_strength >= 0.6: strength_label = "🔥 SUPER"
        elif p_strength >= 0.3: strength_label = "⭐ STRONG"
        elif p_strength >= 0.1: strength_label = "○ WEAK"
        else: strength_label = "💤 FLAT"

    # 🔥 Bot's current form
    form = STATE.get("bot_recent_form", [])
    if form:
        recent5 = form[-5:]
        wins5 = sum(recent5)
        form_str = f"{wins5}/{len(recent5)}"
        # Streak
        hot = sum(1 for r in reversed(form) if r == 1)
        cold = sum(1 for r in reversed(form) if r == 0)
        if hot >= 3: form_label = f"🔥 HOT ({form_str})"
        elif cold >= 3: form_label = f"❄️ COLD ({form_str})"
        else: form_label = f"😐 NEUTRAL ({form_str})"
    else:
        form_label = "N/A"

    return {
        "pred_size": pred_size, "confidence": conf,
        "signal_label": signal_label,
        "engine_probs": engine_probs, "weights": weights,
        "regime": regime, "entropy": predictability,
        "signature": pattern_signature(outcomes),
        "short_sig": short_pattern_sig(outcomes, 6) or "N/A",
        "num_sig": number_signature(history, 5),
        "hot_top": hot_top,
        "strength_note": strength_note,
        "strength_label": strength_label,
        "pattern_strength": p_strength,
        "form_label": form_label,
        "anti_error_active": override_active,
        "anti_error_reason": override_reason,
        "pat_flip_active": pat_flip_active,
        "pat_flip_reason": pat_flip_reason
    }

# ==================== NUMBER PREDICTOR ====================
def advanced_number_predictor(history_list, predicted_size):
    numbers = [h["number"] for h in history_list]
    sizes = [h["size"] for h in history_list]
    if len(numbers) < 40: return 8 if predicted_size == 1 else 2
    candidates = [5, 6, 7, 8, 9] if predicted_size == 1 else [0, 1, 2, 3, 4]
    K = len(candidates)
    freq_all = Counter(numbers); freq_recent = Counter(numbers[-50:])
    tr = len(numbers[-50:])
    mk = [{n: 0.0 for n in candidates} for _ in range(3)]
    for o in range(1, 4):
        if len(numbers) < o + 2: continue
        ls = tuple(numbers[-o:]); c, t = Counter(), 0
        for i in range(len(numbers) - o):
            if tuple(numbers[i:i+o]) == ls:
                nx = numbers[i+o]
                if nx in candidates: c[nx] += 1; t += 1
        for n in candidates: mk[o-1][n] = (c.get(n, 0) + 1) / (t + K)
    s2n = {0: Counter(), 1: Counter()}
    for i in range(1, len(numbers)):
        if numbers[i] in candidates: s2n[sizes[i-1]][numbers[i]] += 1
    scores = {}
    for n in candidates:
        s1 = (freq_all.get(n, 0) + 1) / (len(numbers) + K)
        s2 = (freq_recent.get(n, 0) + 1) / (tr + K)
        s6 = (s2n[sizes[-1]].get(n, 0) + 1) / (sum(s2n[sizes[-1]].values()) + K)
        scores[n] = s1*0.10 + s2*0.12 + mk[0][n]*0.13 + mk[1][n]*0.13 + mk[2][n]*0.13 + s6*0.39
    return max(scores, key=scores.get)

# ==================== STATS ====================
def update_engine_stats(engine_probs, actual_big, regime):
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"].setdefault(eng, default_engine_state())
        pb = prob >= 0.5; hit = (pb == actual_big)
        if hit: stats["alpha"] += 1.0
        else: stats["beta"] += 1.0
        stats["total"] += 1; stats["recent_total"] += 1
        if hit: stats["hits"] += 1; stats["recent_hits"] += 1
        else: stats["misses"] += 1
        pa = 1.0 if actual_big else 0.0
        stats["brier_sum"] += (prob - pa) ** 2
        rs = stats.setdefault("regime_stats", {}).setdefault(regime, {"hits": 0, "total": 0})
        rs["total"] += 1
        if hit: rs["hits"] += 1
        if stats["recent_total"] > 50:
            stats["recent_hits"] = int(stats["recent_hits"] * 0.8)
            stats["recent_total"] = int(stats["recent_total"] * 0.8)

def update_global_stats(win):
    if win:
        STATE["total_wins"] = STATE.get("total_wins", 0) + 1
        STATE["current_loss_streak"] = 0; STATE["current_level"] = 1
    else:
        STATE["total_losses"] = STATE.get("total_losses", 0) + 1
        STATE["current_loss_streak"] = STATE.get("current_loss_streak", 0) + 1
        if STATE["current_loss_streak"] > STATE.get("max_b2b_loss", 0):
            STATE["max_b2b_loss"] = STATE["current_loss_streak"]
        STATE["current_level"] = min(STATE.get("current_level", 1) + 1, MAX_LEVEL)
    # 🔥 Track bot's form
    form = STATE.setdefault("bot_recent_form", [])
    form.append(1 if win else 0)
    if len(form) > 20:
        STATE["bot_recent_form"] = form[-20:]

def update_pattern_memory(signature, actual_big):
    if not signature: return
    pm = STATE.setdefault("pattern_memory", {}).setdefault(signature, {"next_big": 0, "next_small": 0})
    if actual_big: pm["next_big"] += 1
    else: pm["next_small"] += 1

def update_error_pattern(signature, won):
    if not signature: return
    if won:
        ep = STATE.setdefault("error_patterns", {}).get(signature)
        if ep:
            ep["fail_count"] = max(0, ep["fail_count"] - 1)
            ep["total"] = ep.get("total", 1) + 1
            if ep["fail_count"] == 0: del STATE["error_patterns"][signature]
    else:
        ep = STATE.setdefault("error_patterns", {}).setdefault(signature, {"fail_count": 0, "total": 0})
        ep["fail_count"] += 1; ep["total"] = ep.get("total", 0) + 1

def update_pattern_strength(sig, actual_big, pred_big):
    if not sig: return
    stats = STATE.setdefault("pattern_strength", {}).setdefault(sig, {"hits": 0, "misses": 0})
    if actual_big == pred_big:
        stats["hits"] += 1
    else:
        stats["misses"] += 1

def update_number_memory(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 4: return
    for i in range(len(numbers) - 3):
        seq = f"{numbers[i]}-{numbers[i+1]}-{numbers[i+2]}"
        nm = STATE.setdefault("number_memory", {}).setdefault(seq, {"next_big": 0, "next_small": 0})
        if sizes[i+3] == 1: nm["next_big"] += 1
        else: nm["next_small"] += 1

# ==================== FORMATTERS ====================
def format_synced_history_logs(history):
    out = ""
    for item in history[-8:]:
        issue = item["issue"]; sp = str(issue)[-3:]
        ss = "BIGGG" if item["size"] == 1 else "SMALL"
        num = item["number"]; nd = str(num) if num != -1 else "?"
        pred = STATE["prediction_memory"].get(str(issue))
        if pred and pred["size"] == ss and pred.get("number") == num and num != -1: icon = "  ☠️☠️☠️"
        elif pred and pred["size"] == ss: icon = "  ✅✅✅"
        else: icon = ""
        out += f"`{sp}` *{ss}* ({nd}){icon}\n"
    return out

def build_stats_footer():
    wins = STATE.get("total_wins", 0); losses = STATE.get("total_losses", 0)
    max_b2b = STATE.get("max_b2b_loss", 0); cur = STATE.get("current_loss_streak", 0)
    level = STATE.get("current_level", 1)
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0.0
    patterns = len(STATE.get("pattern_memory", {}))
    errors = len(STATE.get("error_patterns", {}))
    num_db = len(STATE.get("number_memory", {}))
    strong_pats = len(STATE.get("pattern_strength", {}))
    learned = STATE.get("learned_overrides", 0)
    pflips = STATE.get("pattern_flips", 0)
    pflips_won = STATE.get("pattern_flips_won", 0)
    pflip_acc = (pflips_won / pflips * 100) if pflips > 0 else 0.0
    level_str = f"{BET_LEVELS[min(level-1, len(BET_LEVELS)-1)]}X"
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *LIFETIME STATS*\n"
        f"✅ *Win:* `{wins}` | ❌ *Loss:* `{losses}`\n"
        f"📉 *Max B2B:* `{max_b2b}` | 🔥 *Streak:* `{cur}`\n"
        f"🎯 *Win Rate:* `{wr:.1f}%`\n"
        f"💰 *Level:* `{level}` ({level_str})\n"
        f"🧠 *Pat:* `{patterns}` | 💪 *Strong:* `{strong_pats}`\n"
        f"🔢 *Num:* `{num_db}` | ⚠️ *Err:* `{errors}`\n"
        f"🎓 *AE-Flips:* `{learned}` | 🔄 *Pat-Flips:* `{pflips}` (won {pflips_won}, {pflip_acc:.0f}%)"
    )

# ==================== TELEGRAM ====================
async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200: logger.error(f"TG err: {await r.text()}")
    except Exception as e: logger.error(f"TG: {e}")

async def send_win_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    try:
        async with session.post(url, json={"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID},
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            await r.text()
    except Exception as e: logger.error(f"Sticker: {e}")

# ==================== API ====================
async def fetch_data(session):
    for att in range(1, 4):
        try:
            async with session.get(API_URL, headers={'User-Agent': 'Mozilla/5.0'},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d.get("code") == 0 and "data" in d and "list" in d["data"]:
                        return d["data"]["list"]
        except Exception as e: logger.warning(f"API att {att}: {e}")
        await asyncio.sleep(2 ** att)
    return None

# ==================== STATE MACHINE ====================
class BotStateMachine:
    def __init__(self): self.pending = None

    async def run(self, session):
        while True:
            try: await self.step(session)
            except Exception as e: logger.error(f"SM: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(5)

    async def step(self, session):
        raw = await fetch_data(session)
        if not raw: return
        history = validate_and_sanitize(raw)
        if not history or len(history) < 30: return
        last = history[-1]; li = last["issue"]
        if li == STATE["last_processed_issue"]: return
        outcomes = [h["size"] for h in history]
        regime = detect_regime(outcomes)

        # ---- EVALUATE ----
        if self.pending and self.pending["next_issue"] == li:
            ab = last["size"] == 1
            pred_big = (self.pending["pred_size"] == "BIGGG")
            update_engine_stats(self.pending["engine_probs"], ab, regime)
            gradient_update(self.pending["engine_probs"], ab, STATE.get("gradient_lr", 0.01))
            brier = (self.pending["prob_big"] - (1 if ab else 0)) ** 2
            STATE["calibration_offset"] = STATE.get("calibration_offset", 0.0) * 0.95 + brier * 0.05
            actual_size_str = "BIGGG" if ab else "SMALL"
            win = (actual_size_str == self.pending["pred_size"])
            update_global_stats(win)
            sig = self.pending.get("pattern_sig")
            update_pattern_memory(sig, ab)
            update_error_pattern(sig, win)
            original_pred_big = self.pending.get("original_pred_big", pred_big)
            update_pattern_strength(self.pending.get("short_sig"), ab, original_pred_big)
            if self.pending.get("pat_flipped"):
                STATE["pattern_flips"] = STATE.get("pattern_flips", 0) + 1
                if win:
                    STATE["pattern_flips_won"] = STATE.get("pattern_flips_won", 0) + 1
            update_number_memory(history)
            if self.pending.get("flipped"):
                STATE["learned_overrides"] = STATE.get("learned_overrides", 0) + 1
            if win: asyncio.create_task(send_win_sticker(session))
            self.pending = None

        # ---- PREDICT ----
        if not self.pending or self.pending["last_issue"] != li:
            outcomes_local = [h["size"] for h in history]
            ep_orig = {
                "pattern": engine_pattern(outcomes_local),
                "trend": engine_trend(outcomes_local),
                "number_seq": engine_number_sequence(history),
                "hot_number": engine_hot_number(history),
                "streak_break": engine_streak_break(outcomes_local),
                "rhythm": engine_rhythm(outcomes_local),
                "hot_cold": engine_hot_cold(outcomes_local),
                "gambler_instinct": engine_gambler_instinct(history),
            }
            regime_local = detect_regime(outcomes_local)
            w_local = get_adaptive_weights(regime_local)
            original_p_big = combine_engines(ep_orig, w_local)
            original_pred_big = (original_p_big >= 0.5)

            pred = predict_next(history)
            ps = pred["pred_size"]; conf = pred["confidence"]; sig = pred["signature"]

            ni = li + 1
            pn = advanced_number_predictor(history, ps)
            self.pending = {
                "last_issue": li, "next_issue": ni,
                "pred_size": "BIGGG" if ps == 1 else "SMALL",
                "pred_number": pn,
                "prob_big": conf if ps == 1 else 1 - conf,
                "engine_probs": pred["engine_probs"],
                "pattern_sig": sig,
                "short_sig": pred["short_sig"],
                "original_pred_big": original_pred_big,
                "flipped": pred["anti_error_active"],
                "pat_flipped": pred["pat_flip_active"]
            }
            STATE["prediction_memory"][str(ni)] = {"size": self.pending["pred_size"], "number": pn}

            hb = format_synced_history_logs(history)
            ep = pred["engine_probs"]
            cons1 = (f"PAT:{ep['pattern']:.2f} TRD:{ep['trend']:.2f} "
                     f"NSQ:{ep['number_seq']:.2f} HOT:{ep['hot_number']:.2f} "
                     f"BRK:{ep['streak_break']:.2f}")
            cons2 = (f"RHY:{ep['rhythm']:.2f} HCD:{ep['hot_cold']:.2f} "
                     f"GMB:{ep['gambler_instinct']:.2f}")
            weights_str = " ".join([f"{k[:3].upper()}:{v:.2f}" for k, v in pred["weights"].items()])

            override_note = ""
            if pred["anti_error_active"]: override_note = f"\n🚨 *AE-FLIP:* `{pred['anti_error_reason']}`"
            elif pred["anti_error_reason"]: override_note = f"\n⚠️ *Dampened:* `{pred['anti_error_reason']}`"

            pat_flip_note = ""
            if pred["pat_flip_active"]:
                pat_flip_note = f"\n🔄 *PAT-FLIP:* `{pred['pat_flip_reason']}`"

            hot_info = ""
            if pred["hot_top"]:
                hn, hc = pred["hot_top"]
                hot_info = f"\n🔥 *Hot:* `{hn}` ({hc}x)"

            strength_info = ""
            if pred["strength_note"]:
                strength_info = f"\n💪 *Pattern:* `{pred['strength_label']}` {pred['strength_note']}"

            num_sig_str = pred["num_sig"] or "N/A"
            level = STATE.get("current_level", 1)
            fund_advice = f"{BET_LEVELS[min(level-1, len(BET_LEVELS)-1)]}X"
            stats_footer = build_stats_footer()

            msg = (
                f"🎯 *QUANTUM V28.4 GAMBLER MIND* 🎯\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 *Period:* `{ni}`\n"
                f"🎲 *Number:* `{pn}`\n"
                f"🔥 *Target:* *{'BIGGG 🟢' if ps == 1 else 'SMALL 🔴'}*\n"
                f"📊 *Confidence:* `{conf*100:.1f}%` | {pred['signal_label']}\n"
                f"🎰 *Bot Form:* {pred['form_label']}\n"
                f"📈 *Regime:* `{pred['regime']}` | *Entropy:* `{pred['entropy']:.2f}`\n"
                f"💰 *Fund:* `{fund_advice}` (Level {level})"
                f"{override_note}"
                f"{pat_flip_note}\n"
                f"🧩 *Size Sig:* `{sig or 'N/A'}`\n"
                f"🎯 *Short Sig:* `{pred['short_sig']}`"
                f"{strength_info}\n"
                f"🔢 *Num Sig:* `{num_sig_str}`"
                f"{hot_info}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *8-Engine Consensus:*\n`{cons1}`\n`{cons2}`\n"
                f"⚖️ *Weights:* `{weights_str}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📜 *TREND (8)*:\n{hb}"
                f"{stats_footer}"
            )
            asyncio.create_task(send_telegram(session, msg))

        STATE["last_processed_issue"] = li
        save_state(STATE)

# ==================== WARMUP ====================
async def warmup(session):
    logger.info("Warmup V28.4...")
    raw = await fetch_data(session)
    if not raw: return
    history = validate_and_sanitize(raw)
    if len(history) < 100: return
    for i in range(PATTERN_SIG_LEN, len(history) - 1):
        partial = history[:i]
        outcomes = [h["size"] for h in partial]
        sig = pattern_signature(outcomes, 14)
        if sig:
            nb = history[i]["size"] == 1
            pm = STATE.setdefault("pattern_memory", {}).setdefault(sig, {"next_big": 0, "next_small": 0})
            if nb: pm["next_big"] += 1
            else: pm["next_small"] += 1
    for i in range(6, len(history) - 1):
        partial = history[:i]
        outcomes = [h["size"] for h in partial]
        short_sig = short_pattern_sig(outcomes, 6)
        if not short_sig: continue
        outcomes_local = [h["size"] for h in partial]
        ep = {
            "pattern": engine_pattern(outcomes_local),
            "trend": engine_trend(outcomes_local),
            "number_seq": engine_number_sequence(partial),
            "hot_number": engine_hot_number(partial),
            "streak_break": engine_streak_break(outcomes_local),
            "rhythm": engine_rhythm(outcomes_local),
            "hot_cold": engine_hot_cold(outcomes_local),
            "gambler_instinct": engine_gambler_instinct(partial),
        }
        regime = detect_regime(outcomes_local)
        w = get_adaptive_weights(regime)
        orig_p = combine_engines(ep, w)
        orig_pred_big = (orig_p >= 0.5)
        ab = history[i]["size"] == 1
        update_pattern_strength(short_sig, ab, orig_pred_big)
    update_number_memory(history)
    for i in range(50, len(history) - 1):
        partial = history[:i]
        outcomes = [h["size"] for h in partial]
        regime = detect_regime(outcomes)
        try:
            pred = predict_next(partial)
            ab = history[i]["size"] == 1
            update_engine_stats(pred["engine_probs"], ab, regime)
            gradient_update(pred["engine_probs"], ab, STATE.get("gradient_lr", 0.01))
        except Exception: continue
    save_state(STATE)
    logger.info(f"Warmup done. Pat:{len(STATE.get('pattern_memory', {}))} "
                f"Strong:{len(STATE.get('pattern_strength', {}))} "
                f"Engines:{len(STATE.get('engine_stats', {}))}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V28.4 GAMBLER MIND ACTIVE", status=200)

async def main():
    app = web.Application(); app.router.add_get('/', health)
    runner = web.AppRunner(app); await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    logger.info(f"Live on {port}")
    async with aiohttp.ClientSession() as session:
        await warmup(session)
        await BotStateMachine().run(session)

if __name__ == "__main__":
    asyncio.run(main())

