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
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593183186417.asia-southeast1.run.app/api/wingo-history-1m-500")
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")
# ================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QuantumV34")
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

STATE_FILE = "engine_state_v34.json"
PATTERN_SIG_LEN = 14
PATTERN_LENGTHS = [4, 6, 8, 10, 12, 14]
ERROR_SIG_LEN = 8
ERROR_THRESHOLD = 2
HOT_NUMBER_WINDOW = 20
HOT_NUMBER_MIN_DOMINANCE = 0.35
ARITH_WINDOW = 40
BET_LEVELS = [1.0, 2.5, 6.0, 12.0]

# 🔥 PATTERN PRIORITY thresholds
PATTERN_MIN_SAMPLES = 5
PATTERN_GOOD_ACC = 0.60   # >=60% = high priority
PATTERN_GREAT_ACC = 0.70  # >=70% = super priority

# ==================== STATE ====================
def default_engine_state():
    return {
        "alpha": 1.0, "beta": 1.0,
        "hits": 0, "misses": 0, "total": 0,
        "recent_hits": 0, "recent_total": 0,
        "gradient_weight": 1.0,
    }

ENGINES = ["pattern", "trend", "trend_shift", "trend_follow", "slope",
           "number_seq", "hot_number", "streak_break", "arith", "double_detect"]

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
            s.setdefault("pattern_stats", {})  # 🔥 NEW: Per-pattern accuracy
            s.setdefault("calibration_offset", 0.0); s.setdefault("learned_overrides", 0)
            s.setdefault("number_memory", {})
            s.setdefault("confidence_buckets", {})  # 🔥 NEW: Calibration
            return s
        except Exception as e:
            logger.error(f"State load error: {e}")
    return {
        "engine_stats": {eng: default_engine_state() for eng in ENGINES},
        "prediction_memory": {}, "last_processed_issue": 0,
        "calibration_offset": 0.0, "total_wins": 0, "total_losses": 0,
        "current_loss_streak": 0, "max_b2b_loss": 0, "current_level": 1,
        "pattern_memory": {}, "error_patterns": {}, "gradient_lr": 0.01,
        "learned_overrides": 0, "number_memory": {},
        "pattern_stats": {}, "confidence_buckets": {}
    }

def save_state(state):
    try:
        for key, cap in [("prediction_memory", 500), ("pattern_memory", 5000),
                         ("error_patterns", 500), ("number_memory", 5000),
                         ("pattern_stats", 5000)]:
            if len(state.get(key, {})) > cap:
                for k in sorted(state[key].keys())[:-cap]:
                    del state[key][k]
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"State save error: {e}")

STATE = load_state()

# ==================== HELPERS ====================
def pattern_signature(outcomes, length=PATTERN_SIG_LEN):
    if len(outcomes) < length: return None
    return "".join('B' if x else 'S' for x in outcomes[-length:])

def short_pattern_sig(outcomes, length=8):
    if len(outcomes) < length: return None
    return "".join('B' if x else 'S' for x in outcomes[-length:])

def error_signature(outcomes, length=ERROR_SIG_LEN):
    if len(outcomes) < length: return None
    return "".join('B' if x else 'S' for x in outcomes[-length:])

def number_signature(history, length=5):
    nums = [h["number"] for h in history if h["number"] >= 0]
    if len(nums) < length: return None
    return "-".join(str(n) for n in nums[-length:])

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

# ==================== 🔥 PATTERN ACCURACY TRACKER ====================
def get_pattern_priority(outcomes):
    """
    Check current pattern signature's historical accuracy.
    Returns (probability, confidence_weight, sample_size).
    If pattern has high accuracy + frequency, it gets priority.
    """
    sig = short_pattern_sig(outcomes, 8)
    if not sig: return None, 0.0, 0
    stats = STATE.get("pattern_stats", {}).get(sig)
    if not stats: return None, 0.0, 0
    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    total = hits + misses
    if total < PATTERN_MIN_SAMPLES: return None, 0.0, total
    acc = hits / total
    # Laplace-smoothed for stability
    smoothed_acc = (hits + 1) / (total + 2)
    # Weight: how much to trust this pattern (frequency + accuracy)
    # Higher frequency + higher accuracy = more trust
    freq_factor = min(1.0, total / 20.0)  # caps at 20 samples
    acc_factor = max(0.0, (smoothed_acc - 0.5) * 2)  # 0.5 → 0, 1.0 → 1
    weight = freq_factor * acc_factor
    return smoothed_acc, weight, total

# ==================== ENGINE 1: PATTERN (Priority Boosted) ====================
def engine_pattern(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    sig_full = "".join('B' if x else 'S' for x in outcomes)
    results, weights = [], []

    # Multi-length pattern matching
    for L in PATTERN_LENGTHS:
        if n < L + 3: continue
        tail = sig_full[-L:]
        big = small = 0
        for i in range(n - L):
            if sig_full[i:i+L] == tail:
                if outcomes[i+L] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            prob = (big + 1) / (total + 2)
            w = (L ** 1.5) * math.log(total + 1)
            results.append(prob); weights.append(w)

    # Pattern DB lookup (14-length)
    sig14 = pattern_signature(outcomes, 14)
    if sig14 and sig14 in STATE.get("pattern_memory", {}):
        pm = STATE["pattern_memory"][sig14]
        pb = pm.get("next_big", 0); ps = pm.get("next_small", 0)
        if pb + ps >= 2:
            prob_db = (pb + 1) / (pb + ps + 2)
            results.append(prob_db); weights.append(14 ** 1.5 * math.log(pb + ps + 1))

    # 🔥 PATTERN PRIORITY BOOST: 8-length signature accuracy
    p_acc, p_weight, p_samples = get_pattern_priority(outcomes)
    if p_acc is not None and p_weight > 0.1:
        # Boost: multiply by weight (up to ~3x priority)
        priority_boost = 1.0 + p_weight * 3.0
        results.append(p_acc)
        weights.append(8 ** 1.7 * priority_boost * math.log(p_samples + 1))

    if not results: return 0.5
    return sum(r*w for r,w in zip(results, weights)) / sum(weights)

# ==================== ENGINE 2: TREND (Classic EMA) ====================
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

# ==================== 🔥 ENGINE 3: TREND SHIFT DETECTOR ====================
def engine_trend_shift(outcomes):
    """
    Detects when trend has shifted recently.
    Uses fast vs slow EMA crossover over short window.
    If crossover happened in last 2-3 periods, strong signal.
    """
    n = len(outcomes)
    if n < 15: return 0.5
    def ema(arr, span):
        a = 2 / (span + 1); e = arr[0]
        for x in arr[1:]: e = a * x + (1 - a) * e
        return e
    # Fast and slow EMA over recent
    fast = ema(outcomes[-10:], 3)
    slow = ema(outcomes[-15:], 8)
    # Check if crossed recently
    fast_prev = ema(outcomes[-11:-1], 3)
    slow_prev = ema(outcomes[-16:-1], 8)
    crossed_up = (fast_prev <= slow_prev) and (fast > slow)
    crossed_down = (fast_prev >= slow_prev) and (fast < slow)
    diff = fast - slow
    if crossed_up:
        return 0.75  # Strong BIG signal after upshift
    if crossed_down:
        return 0.25  # Strong SMALL signal after downshift
    # No crossover: medium strength
    return max(0.20, min(0.80, 0.5 + math.tanh(diff * 5.0) * 0.30))

# ==================== 🔥 ENGINE 4: TREND FOLLOW-UP ====================
def engine_trend_follow(outcomes):
    """
    If trend has been consistent for N periods, expect continuation (short-term).
    Uses run-length of same direction.
    """
    n = len(outcomes)
    if n < 8: return 0.5
    recent = outcomes[-8:]
    # Count consecutive same at the end
    streak = 1
    val = recent[-1]
    for i in range(len(recent) - 2, -1, -1):
        if recent[i] == val: streak += 1
        else: break
    # Also check 3-period momentum direction
    if n >= 6:
        s1 = sum(outcomes[-3:]) / 3
        s2 = sum(outcomes[-6:-3]) / 3
        momentum = s1 - s2
    else:
        momentum = 0
    # Follow-up bias: 2-3 run continues, 4+ starts to fade
    if streak == 2:
        base = 0.62 if val == 1 else 0.38
    elif streak == 3:
        base = 0.58 if val == 1 else 0.42
    elif streak >= 4:
        base = 0.45 if val == 1 else 0.55
    else:
        base = 0.50
    # Add momentum bias
    base += momentum * 0.10
    return max(0.15, min(0.85, base))

# ==================== 🔥 ENGINE 5: SLOPE ANALYZER ====================
def engine_slope(outcomes):
    """
    Linear regression slope of last 12 outcomes.
    Positive slope → BIG trend, Negative slope → SMALL trend.
    """
    n = len(outcomes)
    if n < 12: return 0.5
    window = outcomes[-12:]
    m = len(window)
    xs = list(range(m))
    mean_x = sum(xs) / m
    mean_y = sum(window) / m
    num = sum((xs[i] - mean_x) * (window[i] - mean_y) for i in range(m))
    den = sum((xs[i] - mean_x) ** 2 for i in range(m))
    if den == 0: return 0.5
    slope = num / den
    # Slope range: -0.5 to +0.5 typically
    # Convert to probability
    p_big = 0.5 + math.tanh(slope * 8.0) * 0.35
    return max(0.15, min(0.85, p_big))

# ==================== ENGINE 6: NUMBER SEQUENCE ====================
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
    elif abs(last_nums[0]-last_nums[1]) == 1 and abs(last_nums[1]-last_nums[2]) == 1: pattern = "SEQ"
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
    if total < 1.5: return 0.5
    return (big_count + 0.5) / (total + 1.0)

# ==================== ENGINE 7: HOT NUMBER ====================
def engine_hot_number(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    if len(numbers) < HOT_NUMBER_WINDOW: return 0.5
    recent = numbers[-HOT_NUMBER_WINDOW:]
    freq = Counter(recent)
    top = freq.most_common(3)
    if not top: return 0.5
    most_freq_num, most_freq_cnt = top[0]
    dominance = most_freq_cnt / len(recent)
    if dominance < HOT_NUMBER_MIN_DOMINANCE: return 0.5
    big_weight = sum(cnt for num, cnt in top if num >= 5)
    small_weight = sum(cnt for num, cnt in top if num < 5)
    total = big_weight + small_weight
    if total < 3: return 0.5
    base = big_weight / total
    side_boost = 0.15 if most_freq_num >= 5 else -0.15
    return max(0.15, min(0.85, base + side_boost))

# ==================== ENGINE 8: STREAK BREAK ====================
def engine_streak_break(outcomes):
    if len(outcomes) < 6: return 0.5
    recent = outcomes[-10:]
    streak_len = 1; streak_val = recent[-1]
    for i in range(len(recent) - 2, -1, -1):
        if recent[i] == streak_val: streak_len += 1
        else: break
    if streak_len >= 6: return 0.12 if streak_val == 1 else 0.88
    elif streak_len == 5: return 0.20 if streak_val == 1 else 0.80
    elif streak_len == 4: return 0.30 if streak_val == 1 else 0.70
    elif streak_len == 3: return 0.42 if streak_val == 1 else 0.58
    return 0.5

# ==================== ENGINE 9: ARITHMETIC ====================
def engine_arithmetic(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 15: return 0.5
    recent = numbers[-ARITH_WINDOW:]
    recent_sizes = sizes[-ARITH_WINDOW:]
    if len(recent) < 5: return 0.5
    last = recent[-1]; prev = recent[-2] if len(recent) >= 2 else None
    prev2 = recent[-3] if len(recent) >= 3 else None
    big_votes = 0.0; small_votes = 0.0
    if prev is not None:
        target = abs(last - prev)
        for i in range(len(recent) - 3):
            a, b, c = recent[i], recent[i+1], recent[i+2]
            if abs(a - b) == target:
                if i + 3 < len(recent_sizes):
                    w = math.exp((i / len(recent)) * 2.5)
                    if recent_sizes[i+3] == 1: big_votes += w
                    else: small_votes += w
    if prev is not None:
        target_sum = last + prev
        if target_sum <= 9:
            for i in range(len(recent) - 3):
                a, b = recent[i], recent[i+1]
                if a + b == target_sum:
                    if i + 2 < len(recent_sizes):
                        w = math.exp((i / len(recent)) * 2.0) * 0.6
                        if recent_sizes[i+2] == 1: big_votes += w
                        else: small_votes += w
    if prev2 is not None and prev2 == last:
        for i in range(len(recent) - 4):
            if recent[i] == recent[i+2] == prev2:
                if i + 3 < len(recent_sizes):
                    w = math.exp((i / len(recent)) * 2.5) * 0.8
                    if recent_sizes[i+3] == 1: big_votes += w
                    else: small_votes += w
    total = big_votes + small_votes
    if total < 1.0: return 0.5
    return (big_votes + 0.5) / (total + 1.0)

# ==================== ENGINE 10: DOUBLE DETECT ====================
def engine_double_detect(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(sizes) < 3: return 0.5
    last = sizes[-1]; prev = sizes[-2]; prev2 = sizes[-3] if len(sizes) >= 3 else None
    if last != prev: return 0.5
    exact_double = (numbers[-1] == numbers[-2])
    is_triple = (prev2 is not None and prev2 == prev == last)
    big_after = 0.0; small_after = 0.0; matches = 0
    for i in range(1, len(sizes) - 1):
        if sizes[i] == sizes[i+1] == last:
            if exact_double and numbers[i] != numbers[i+1]: continue
            if is_triple:
                if i < 1 or sizes[i-1] != last: continue
            if i + 2 < len(sizes):
                recency = math.exp((i / len(sizes)) * 3.5)
                if exact_double and numbers[i] == numbers[i+1] == numbers[-1]:
                    recency *= 1.5
                if sizes[i+2] == 1: big_after += recency
                else: small_after += recency
                matches += 1
    total = big_after + small_after
    if matches < 3 or total < 1.0:
        if is_triple: return 0.25 if last == 1 else 0.75
        return 0.38 if last == 1 else 0.62
    return (big_after + 0.5) / (total + 1.0)

# ==================== ADAPTIVE WEIGHTS ====================
REGIME_EXPERT_WEIGHTS = {
    "ALTERNATING":   {"pattern": 0.18, "trend": 0.08, "trend_shift": 0.12, "trend_follow": 0.08, "slope": 0.08, "number_seq": 0.10, "hot_number": 0.08, "streak_break": 0.15, "arith": 0.08, "double_detect": 0.05},
    "BIG_HEAVY":     {"pattern": 0.18, "trend": 0.14, "trend_shift": 0.10, "trend_follow": 0.12, "slope": 0.12, "number_seq": 0.08, "hot_number": 0.08, "streak_break": 0.08, "arith": 0.06, "double_detect": 0.04},
    "SMALL_HEAVY":   {"pattern": 0.18, "trend": 0.14, "trend_shift": 0.10, "trend_follow": 0.12, "slope": 0.12, "number_seq": 0.08, "hot_number": 0.08, "streak_break": 0.08, "arith": 0.06, "double_detect": 0.04},
    "LONG_STREAK":   {"pattern": 0.14, "trend": 0.10, "trend_shift": 0.10, "trend_follow": 0.15, "slope": 0.10, "number_seq": 0.06, "hot_number": 0.06, "streak_break": 0.22, "arith": 0.04, "double_detect": 0.03},
    "BALANCED":      {"pattern": 0.20, "trend": 0.10, "trend_shift": 0.12, "trend_follow": 0.10, "slope": 0.10, "number_seq": 0.08, "hot_number": 0.08, "streak_break": 0.10, "arith": 0.08, "double_detect": 0.04},
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
        new_w = max(0.3, min(3.0, old_w - lr * grad))
        confidence = max(prob, 1 - prob)
        if confidence >= 0.70 and new_w < 0.15:
            new_w = 0.15
        stats["gradient_weight"] = new_w

# ==================== ANTI-ERROR ====================
def check_anti_error(outcomes, current_p_big):
    sig = error_signature(outcomes, ERROR_SIG_LEN)
    if not sig: return current_p_big, False, ""
    err = STATE.get("error_patterns", {}).get(sig)
    if err and err.get("fail_count", 0) >= ERROR_THRESHOLD:
        fail_ratio = err.get("fail_count", 0) / max(1, err.get("total", 1))
        if fail_ratio >= 0.6:
            return 1.0 - current_p_big, True, f"FLIP ({err['fail_count']}x)"
    return current_p_big, False, ""

# ==================== 🔥 CONFIDENCE CALIBRATION ====================
def calibrate_confidence(raw_conf, pred_size):
    """
    Look at historical confidence buckets and their actual win rate.
    Shrink small-sample buckets toward 0.5.
    Return actual calibrated confidence.
    """
    # Bucket raw confidence into 5% bins
    bucket_key = str(int(raw_conf * 20) / 20)  # 0.55, 0.60, 0.65, ...
    buckets = STATE.get("confidence_buckets", {})
    bucket = buckets.get(bucket_key)
    if not bucket:
        return raw_conf  # No history yet
    wins = bucket.get("wins", 0)
    total = bucket.get("total", 0)
    if total < 10:
        # Small sample: shrink toward raw
        return raw_conf * 0.7 + 0.5 * 0.3
    actual_acc = wins / total
    # Blend 60% actual + 40% raw
    calibrated = raw_conf * 0.4 + actual_acc * 0.6
    # Bound
    return max(0.52, min(0.95, calibrated))

def update_confidence_bucket(raw_conf, win):
    bucket_key = str(int(raw_conf * 20) / 20)
    buckets = STATE.setdefault("confidence_buckets", {})
    b = buckets.setdefault(bucket_key, {"wins": 0, "total": 0})
    b["total"] += 1
    if win: b["wins"] += 1

# ==================== PATTERN STATS UPDATE ====================
def update_pattern_stats(outcomes, actual_big, pred_big):
    """
    Track per-pattern (8-length) accuracy.
    Called after outcome is known.
    """
    # Signature BEFORE the outcome (from the prediction time)
    # We stored it in pending_pred
    pass  # Handled in step() with stored signature

def record_pattern_outcome(sig, actual_big, pred_big):
    if not sig: return
    stats = STATE.setdefault("pattern_stats", {}).setdefault(sig, {"hits": 0, "misses": 0})
    if actual_big == pred_big:
        stats["hits"] += 1
    else:
        stats["misses"] += 1

# ==================== MAIN PREDICTOR ====================
def predict_next(history):
    outcomes = [h["size"] for h in history]

    engine_probs = {
        "pattern": engine_pattern(outcomes),
        "trend": engine_trend(outcomes),
        "trend_shift": engine_trend_shift(outcomes),
        "trend_follow": engine_trend_follow(outcomes),
        "slope": engine_slope(outcomes),
        "number_seq": engine_number_sequence(history),
        "hot_number": engine_hot_number(history),
        "streak_break": engine_streak_break(outcomes),
        "arith": engine_arithmetic(history),
        "double_detect": engine_double_detect(history),
    }

    regime = detect_regime(outcomes)
    weights = get_adaptive_weights(regime)
    p_big = combine_engines(engine_probs, weights)

    # Entropy damping
    predictability = calculate_entropy(outcomes)
    if p_big > 0.5:
        p_big = 0.5 + (p_big - 0.5) * (0.5 + predictability * 0.5)
    else:
        p_big = 0.5 - (0.5 - p_big) * (0.5 + predictability * 0.5)

    # Anti-error
    p_big, override_active, override_reason = check_anti_error(outcomes, p_big)

    # Raw confidence
    base_conf = max(p_big, 1 - p_big)
    cal_pen = STATE.get("calibration_offset", 0.0) * 0.5
    raw_conf = max(0.52, min(0.95, base_conf - cal_pen))

    # 🔥 Apply historical calibration
    final_conf = calibrate_confidence(raw_conf, p_big)

    pred_size = 1 if p_big >= 0.5 else 0

    numbers = [h["number"] for h in history if h["number"] >= 0]
    hot_top = None
    if len(numbers) >= HOT_NUMBER_WINDOW:
        top = Counter(numbers[-HOT_NUMBER_WINDOW:]).most_common(1)
        if top: hot_top = top[0]

    # Pattern priority info
    p_acc, p_weight, p_samples = get_pattern_priority(outcomes)
    pattern_note = ""
    if p_acc is not None:
        pattern_note = f"Acc:{p_acc:.2f}(n={p_samples})"

    # Current short sig
    short_sig = short_pattern_sig(outcomes, 8) or "N/A"

    return {
        "pred_size": pred_size, "confidence": final_conf, "raw_conf": raw_conf,
        "engine_probs": engine_probs, "weights": weights,
        "regime": regime, "entropy": predictability,
        "signature": pattern_signature(outcomes),
        "short_sig": short_sig, "pattern_note": pattern_note,
        "num_sig": number_signature(history, 5),
        "hot_top": hot_top,
        "anti_error_active": override_active,
        "anti_error_reason": override_reason
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
        STATE["current_level"] = min(STATE.get("current_level", 1) + 1, 4)

def update_pattern_memory(signature, actual_big):
    if not signature: return
    pm = STATE.setdefault("pattern_memory", {}).setdefault(signature, {"next_big": 0, "next_small": 0})
    if actual_big: pm["next_big"] += 1
    else: pm["next_small"] += 1

def update_error_pattern(outcomes, won):
    sig = error_signature(outcomes, ERROR_SIG_LEN)
    if not sig: return
    if won:
        ep = STATE.setdefault("error_patterns", {}).get(sig)
        if ep:
            ep["fail_count"] = max(0, ep["fail_count"] - 1)
            ep["total"] = ep.get("total", 1) + 1
            if ep["fail_count"] == 0: del STATE["error_patterns"][sig]
    else:
        ep = STATE.setdefault("error_patterns", {}).setdefault(sig, {"fail_count": 0, "total": 0})
        ep["fail_count"] += 1; ep["total"] = ep.get("total", 0) + 1

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
    p_stats = len(STATE.get("pattern_stats", {}))
    learned = STATE.get("learned_overrides", 0)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *LIFETIME STATS*\n"
        f"✅ *Win:* `{wins}` | ❌ *Loss:* `{losses}`\n"
        f"📉 *Max B2B:* `{max_b2b}` | 🔥 *Streak:* `{cur}`\n"
        f"🎯 *Win Rate:* `{wr:.1f}%`\n"
        f"💰 *Level:* `{level}` ({BET_LEVELS[min(level-1, 3)]}X)\n"
        f"🧠 *Pat:* `{patterns}` | 🔢 *Num:* `{num_db}` | 🎯 *PStats:* `{p_stats}`\n"
        f"⚠️ *Err:* `{errors}` | 🎓 *Flips:* `{learned}`"
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

            # 🔥 Update pattern stats with stored signature
            record_pattern_outcome(self.pending.get("short_sig"), ab, pred_big)
            # 🔥 Update confidence bucket
            update_confidence_bucket(self.pending.get("raw_conf", 0.5), win)

            update_pattern_memory(self.pending.get("pattern_sig"), ab)
            update_error_pattern(outcomes, win)
            update_number_memory(history)
            if self.pending.get("flipped"):
                STATE["learned_overrides"] = STATE.get("learned_overrides", 0) + 1
            if win: asyncio.create_task(send_win_sticker(session))
            self.pending = None

        # ---- PREDICT ----
        if not self.pending or self.pending["last_issue"] != li:
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
                "raw_conf": pred["raw_conf"],
                "flipped": pred["anti_error_active"]
            }
            STATE["prediction_memory"][str(ni)] = {"size": self.pending["pred_size"], "number": pn}

            hb = format_synced_history_logs(history)
            ep = pred["engine_probs"]
            cons1 = (f"PAT:{ep['pattern']:.2f} TRD:{ep['trend']:.2f} "
                     f"TSH:{ep['trend_shift']:.2f} TFL:{ep['trend_follow']:.2f} "
                     f"SLP:{ep['slope']:.2f}")
            cons2 = (f"NSQ:{ep['number_seq']:.2f} HOT:{ep['hot_number']:.2f} "
                     f"BRK:{ep['streak_break']:.2f} ARH:{ep['arith']:.2f} "
                     f"DBL:{ep['double_detect']:.2f}")
            weights_str = " ".join([f"{k[:3].upper()}:{v:.2f}" for k, v in pred["weights"].items()])

            override_note = ""
            if pred["anti_error_active"]: override_note = f"\n🚨 *{pred['anti_error_reason']}*"

            hot_info = ""
            if pred["hot_top"]:
                hn, hc = pred["hot_top"]
                hot_info = f"\n🔥 *Hot:* `{hn}` ({hc}x)"

            pattern_info = ""
            if pred["pattern_note"]:
                pattern_info = f"\n🎯 *Pattern Acc:* `{pred['pattern_note']}`"

            num_sig_str = pred["num_sig"] or "N/A"
            level = STATE.get("current_level", 1)
            fund_advice = f"{BET_LEVELS[min(level-1, 3)]}X"
            stats_footer = build_stats_footer()

            msg = (
                f"🎯 *QUANTUM V34 PATTERN-PRIORITY* 🎯\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 *Period:* `{ni}`\n"
                f"🎲 *Number:* `{pn}`\n"
                f"🔥 *Target:* *{'BIGGG 🟢' if ps == 1 else 'SMALL 🔴'}*\n"
                f"📊 *Confidence:* `{conf*100:.1f}%` (raw `{pred['raw_conf']*100:.1f}%`)\n"
                f"📈 *Regime:* `{pred['regime']}` | *Entropy:* `{pred['entropy']:.2f}`\n"
                f"💰 *Fund:* `{fund_advice}` (Level {level})"
                f"{override_note}\n"
                f"🧩 *Size Sig:* `{sig or 'N/A'}`\n"
                f"🎯 *Short Sig:* `{pred['short_sig']}`"
                f"{pattern_info}\n"
                f"🔢 *Num Sig:* `{num_sig_str}`"
                f"{hot_info}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *10-Engine Consensus:*\n`{cons1}`\n`{cons2}`\n"
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
    logger.info("Warmup V34...")
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
    update_number_memory(history)
    for i in range(50, len(history) - 1):
        partial = history[:i]
        outcomes = [h["size"] for h in partial]
        regime = detect_regime(outcomes)
        try:
            pred = predict_next(partial)
            ab = history[i]["size"] == 1
            pred_big = (pred["pred_size"] == 1)
            update_engine_stats(pred["engine_probs"], ab, regime)
            gradient_update(pred["engine_probs"], ab, STATE.get("gradient_lr", 0.01))
            # 🔥 Warm up pattern stats
            record_pattern_outcome(pred["short_sig"], ab, pred_big)
            # Warm up confidence buckets
            win = (ab == pred_big)
            update_confidence_bucket(pred["raw_conf"], win)
        except Exception: continue
    save_state(STATE)
    logger.info(f"Warmup done. Pat:{len(STATE.get('pattern_memory', {}))} "
                f"PStats:{len(STATE.get('pattern_stats', {}))} "
                f"Buckets:{len(STATE.get('confidence_buckets', {}))}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V34 PATTERN-PRIORITY ACTIVE", status=200)

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
      

