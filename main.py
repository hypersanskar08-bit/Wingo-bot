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
logger = logging.getLogger("QuantumV32_1")
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

STATE_FILE = "engine_state_v32_1.json"
PATTERN_SIG_LEN = 14
ERROR_SIG_LEN = 8  # 🔥 FIX 1: Error signature short kiya
PATTERN_LENGTHS = [4, 6, 8, 10, 12, 14]
ERROR_THRESHOLD = 2
SIMILAR_ERROR_RADIUS = 1
HOT_NUMBER_WINDOW = 20
ARITH_WINDOW = 40
BET_LEVELS = [1.0, 2.5, 6.0, 12.0]

# ==================== STATE ====================
def default_engine_state():
    return {
        "alpha": 1.0, "beta": 1.0,
        "hits": 0, "misses": 0, "total": 0,
        "recent_hits": 0, "recent_total": 0,
        "brier_sum": 0.0, "gradient_weight": 1.0,
        "regime_stats": {}
    }

# 🔥 Removed dead engines (jack, skip, vshape, cyclic, mirror)
ENGINES = ["pattern", "trend", "arith", "symmetry", "opposite",
           "number_seq", "repeated_num", "double_detect", "overlap_series",
           "connected_series", "auto_formula"]

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f: s = json.load(f)
            for eng in ENGINES:
                if eng not in s.get("engine_stats", {}):
                    s.setdefault("engine_stats", {})[eng] = default_engine_state()
            # Remove dead engines
            for dead in ["mirror", "cyclic", "vshape", "skip_series", "jack_corr"]:
                if dead in s.get("engine_stats", {}):
                    del s["engine_stats"][dead]
            s.setdefault("total_wins", 0); s.setdefault("total_losses", 0)
            s.setdefault("current_loss_streak", 0); s.setdefault("max_b2b_loss", 0)
            s.setdefault("current_level", 1)
            s.setdefault("pattern_memory", {}); s.setdefault("error_patterns", {})
            s.setdefault("calibration_offset", 0.0); s.setdefault("learned_overrides", 0)
            s.setdefault("number_memory", {}); s.setdefault("hot_numbers", {})
            s.setdefault("arith_rules", {}); s.setdefault("trans_memory", {})
            s.setdefault("series_history", {}); s.setdefault("connected_history", {})
            s.setdefault("auto_formulas", {})
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
        "arith_rules": {}, "trans_memory": {}, "series_history": {},
        "connected_history": {}, "auto_formulas": {}
    }

def save_state(state):
    try:
        for key, cap in [("prediction_memory", 500), ("pattern_memory", 5000),
                         ("error_patterns", 500), ("number_memory", 5000),
                         ("arith_rules", 3000), ("trans_memory", 5000),
                         ("series_history", 5000), ("connected_history", 3000),
                         ("auto_formulas", 2000)]:
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

def error_signature(outcomes, length=ERROR_SIG_LEN):
    """🔥 FIX 1: Short signature for error tracking (8 length)"""
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

# ==================== ENGINE 1: PATTERN ====================
def engine_pattern(outcomes):
    n = len(outcomes)
    if n < 10: return 0.5
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
        if total >= 2:
            prob = (big + 1) / (total + 2)
            w = (L ** 1.2) * math.log(total + 1)
            results.append(prob); weights.append(w)
    sig14 = pattern_signature(outcomes, 14)
    if sig14 and sig14 in STATE.get("pattern_memory", {}):
        pm = STATE["pattern_memory"][sig14]
        pb = pm.get("next_big", 0); ps = pm.get("next_small", 0)
        if pb + ps >= 2:
            prob_db = (pb + 1) / (pb + ps + 2)
            results.append(prob_db); weights.append(14 ** 1.2 * math.log(pb + ps + 1))
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

# ==================== ENGINE 3: ARITHMETIC ====================
def engine_arithmetic(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 10: return 0.5
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
                    nxt = recent_sizes[i+3]
                    w = math.exp((i / len(recent)) * 2.5)
                    if nxt == 1: big_votes += w
                    else: small_votes += w
    if prev is not None:
        target_sum = last + prev
        if target_sum <= 9:
            for i in range(len(recent) - 3):
                a, b = recent[i], recent[i+1]
                if a + b == target_sum:
                    if i + 2 < len(recent_sizes):
                        nxt = recent_sizes[i+2]
                        w = math.exp((i / len(recent)) * 2.0) * 0.6
                        if nxt == 1: big_votes += w
                        else: small_votes += w
    if prev2 is not None and prev2 == last:
        for i in range(len(recent) - 4):
            if recent[i] == recent[i+2] == prev2:
                if i + 3 < len(recent_sizes):
                    w = math.exp((i / len(recent)) * 2.5) * 0.8
                    if recent_sizes[i+3] == 1: big_votes += w
                    else: small_votes += w
    if last in (0, 9):
        for i in range(len(recent) - 1):
            if recent[i] == last:
                if i + 1 < len(recent_sizes):
                    w = math.exp((i / len(recent)) * 2.0)
                    if recent_sizes[i+1] == 1: big_votes += w
                    else: small_votes += w
    total = big_votes + small_votes
    if total < 0.5: return 0.5
    return (big_votes + 0.5) / (total + 1.0)

# ==================== ENGINE 4: SYMMETRY ====================
def engine_symmetry(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 15: return 0.5
    last3 = numbers[-3:]
    if len(last3) < 3: return 0.5
    if last3[0] == last3[2] and last3[0] != last3[1]:
        A, B = last3[0], last3[1]
        midpoint = (A + B) / 2
        mid_is_big = midpoint >= 5
        big_w = small_w = 0.0
        for i in range(len(numbers) - 4):
            if numbers[i] == numbers[i+2] == A and numbers[i+1] == B:
                recency = math.exp((i / len(numbers)) * 3.0)
                if abs(numbers[i+3] - midpoint) <= 1: recency *= 1.5
                if sizes[i+3] == 1: big_w += recency
                else: small_w += recency
        if big_w + small_w >= 0.5:
            return (big_w + 0.5) / (big_w + small_w + 1.0)
        return 0.60 if mid_is_big else 0.40
    if len(numbers) >= 4 and numbers[-1] == numbers[-2] and numbers[-3] != numbers[-1]:
        A = numbers[-1]
        big_w = small_w = 0.0
        for i in range(len(numbers) - 2):
            if numbers[i] == numbers[i+1] == A:
                if i + 2 < len(sizes):
                    recency = math.exp((i / len(numbers)) * 3.0)
                    if sizes[i+2] == 1: big_w += recency
                    else: small_w += recency
        total = big_w + small_w
        if total >= 1.0: return (big_w + 0.5) / (total + 1.0)
    return 0.5

# ==================== ENGINE 5: OPPOSITE ====================
def engine_opposite(outcomes):
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

# ==================== ENGINE 7: REPEATED NUMBER ====================
def engine_repeated_number(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 20: return 0.5
    recent_nums = numbers[-12:]
    freq = Counter(recent_nums)
    if not freq: return 0.5
    top_num, top_count = freq.most_common(1)[0]
    if top_count < 3: return 0.5
    current_prev_size = None
    for i in range(len(numbers) - 1, -1, -1):
        if numbers[i] == top_num:
            if i > 0: current_prev_size = sizes[i - 1]
            break
    if current_prev_size is None: return 0.5
    big_after = 0.0; small_after = 0.0; matches = 0
    for i in range(1, len(numbers)):
        if numbers[i] == top_num and sizes[i-1] == current_prev_size:
            if i + 1 < len(sizes):
                recency = math.exp((i / len(numbers)) * 3.5)
                if sizes[i + 1] == 1: big_after += recency
                else: small_after += recency
                matches += 1
    total = big_after + small_after
    if matches < 2 or total < 1.0:
        if top_num <= 4: return 0.40
        else: return 0.60
    return (big_after + 0.5) / (total + 1.0)

# ==================== ENGINE 8: DOUBLE DETECTOR ====================
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

# ==================== ENGINE 9: OVERLAPPING SERIES ====================
def detect_overlap_series(numbers, offset=0, window_size=4):
    if len(numbers) < window_size: return None
    window = numbers[offset:offset+window_size] if offset+window_size <= len(numbers) else numbers[-(window_size):]
    if len(window) < 4: return None
    a, b, c, d = window[0], window[1], window[2], window[3]
    sig_parts = []
    if c == (a + b) % 10: sig_parts.append("P@0")
    if c == abs(a - b): sig_parts.append("M@0")
    if d == (b + c) % 10: sig_parts.append("P@1")
    if d == abs(b - c): sig_parts.append("M@1")
    if d == (a + b) % 10: sig_parts.append("P@skip")
    if d == abs(a - b): sig_parts.append("M@skip")
    if not sig_parts: return None
    return "|".join(sorted(sig_parts))

def engine_overlapping_series(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 15: return 0.5
    current_sig = detect_overlap_series(numbers, offset=len(numbers)-4, window_size=4)
    sig_5 = None
    if len(numbers) >= 5:
        w5 = numbers[-5:]
        parts = []
        for j in range(3):
            a, b = w5[j], w5[j+1]
            if j + 2 < 5:
                actual = w5[j+2]
                if actual == (a + b) % 10: parts.append(f"P@{j}")
                elif actual == abs(a - b): parts.append(f"M@{j}")
        if parts: sig_5 = "|".join(sorted(parts))
    if not current_sig and not sig_5: return 0.5
    combined_sig = current_sig or ""
    if sig_5: combined_sig = combined_sig + "::" + sig_5 if combined_sig else sig_5
    series_hist = STATE.get("series_history", {})
    hist = series_hist.get(combined_sig)
    if hist and (hist.get("big", 0) + hist.get("small", 0)) >= 3:
        total = hist["big"] + hist["small"]
        return (hist["big"] + 0.5) / (total + 1.0)
    rule_type = set()
    for part in (current_sig or "").split("|"):
        if "P" in part: rule_type.add("PLUS")
        if "M" in part: rule_type.add("MINUS")
    for part in (sig_5 or "").split("|"):
        if "P" in part: rule_type.add("PLUS")
        if "M" in part: rule_type.add("MINUS")
    loose_sig = "&".join(sorted(rule_type))
    if loose_sig:
        hist_loose = series_hist.get("LOOSE::" + loose_sig)
        if hist_loose and (hist_loose.get("big", 0) + hist_loose.get("small", 0)) >= 5:
            total = hist_loose["big"] + hist_loose["small"]
            return (hist_loose["big"] + 0.5) / (total + 1.0)
    recent_nums = numbers[-10:]
    recent_sizes = sizes[-10:]
    big_after = 0.0; small_after = 0.0
    for i in range(2, len(recent_nums) - 1):
        a, b = recent_nums[i-2], recent_nums[i-1]
        c = recent_nums[i]
        if c == (a + b) % 10 or c == abs(a - b):
            if i + 1 < len(recent_sizes):
                w = math.exp((i / len(recent_nums)) * 2.0)
                if recent_sizes[i+1] == 1: big_after += w
                else: small_after += w
    total = big_after + small_after
    if total >= 0.5:
        return (big_after + 0.5) / (total + 1.0)
    return 0.5

# ==================== ENGINE 10: CONNECTED SERIES ====================
def analyze_connected_series(numbers):
    if len(numbers) < 5: return None, 0.0, ""
    recent = numbers[-7:]
    n = len(recent)
    skip_indices = set()
    first_occurrence = {}
    for i in range(n):
        num = recent[i]
        if num in first_occurrence:
            skip_indices.add(first_occurrence[num])
        else:
            first_occurrence[num] = i
    filtered = [(i, recent[i]) for i in range(n) if i not in skip_indices]
    meaningful = []
    for idx, (i, num) in enumerate(filtered):
        connected = False
        for jdx, (j, other) in enumerate(filtered):
            if i == j: continue
            if abs(num - other) <= 2:
                connected = True
                break
        if connected: meaningful.append((i, num))
    if len(meaningful) < 2: return None, 0.0, ""
    predictions = []
    for i in range(len(meaningful)):
        for j in range(i+1, len(meaningful)):
            pos_a, a = meaningful[i]
            pos_b, b = meaningful[j]
            gap = abs(a - b)
            pos_weight = 0.5 + (max(pos_a, pos_b) / n) * 0.5
            if gap == 1:
                if pos_b > pos_a:
                    direction = 1 if b > a else -1
                    pred = b + direction
                    if 0 <= pred <= 9:
                        predictions.append((pred, 1.0 * pos_weight, f"{a},{b}→{pred}"))
            elif gap == 2:
                pred = (a + b) // 2
                predictions.append((pred, 0.85 * pos_weight, f"mid({a},{b})={pred}"))
    if not predictions: return None, 0.0, ""
    number_votes = {}
    for pred, w, info in predictions:
        number_votes[pred] = number_votes.get(pred, 0) + w
    best_num = max(number_votes, key=number_votes.get)
    total_weight = sum(number_votes.values())
    conf_num = number_votes[best_num] / total_weight if total_weight > 0 else 0
    info_str = " | ".join([p[2] for p in predictions[:2]])
    return best_num, conf_num, info_str

def engine_connected_series(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    if len(numbers) < 7: return 0.5
    pred_num, conf, _ = analyze_connected_series(numbers)
    if pred_num is None: return 0.5
    # 🔥 FIX 3: Simplified signature
    recent = numbers[-5:]  # 5 numbers only
    sig_parts = []
    for i in range(len(recent)):
        for j in range(i+1, len(recent)):
            gap = abs(recent[i] - recent[j])
            if gap in (1, 2): sig_parts.append(f"{gap}")
    sig_key = "CS_" + "-".join(sorted(set(sig_parts))) if sig_parts else ""
    if pred_num >= 5:
        base = 0.5 + min(0.35, conf * 0.4)
    else:
        base = 0.5 - min(0.35, conf * 0.4)
    if sig_key:
        ch = STATE.get("connected_history", {}).get(sig_key)
        if ch and (ch.get("big", 0) + ch.get("small", 0)) >= 3:
            total = ch["big"] + ch["small"]
            hist_lean = ch["big"] / total
            base = base * 0.7 + hist_lean * 0.3
    return max(0.15, min(0.85, base))

# ==================== ENGINE 11: AUTO FORMULA ====================
def apply_formula(a, b, c, formula_id):
    try:
        if formula_id == "sum": return (a + b) % 10
        if formula_id == "diff": return abs(a - b)
        if formula_id == "mid": return (a + b) // 2
        if formula_id == "mul": return (a * b) % 10
        if formula_id == "sum3": return (a + b + c) % 10
        if formula_id == "diff3": return abs(a - b - c)
        if formula_id == "diffsum": return abs((a + b) - c)
        if formula_id == "diffplus1": return (abs(a - b) + 1) % 10
        if formula_id == "summinus1": return (a + b - 1) % 10
        if formula_id == "avg3": return (a + b + c) // 3
        if formula_id == "units": return ((a + b) % 10)
        if formula_id == "gap1": return (b + 1) % 10
        if formula_id == "gap2": return (b + 2) % 10
        if formula_id == "gapminus1": return (b - 1) % 10
        if formula_id == "gapminus2": return (b - 2) % 10
        if formula_id == "alt_sumdiff": return (a + b if a > b else abs(a - b)) % 10
    except Exception:
        return None
    return None

ALL_FORMULAS = ["sum", "diff", "mid", "mul", "sum3", "diff3", "diffsum",
                "diffplus1", "summinus1", "avg3", "units", "gap1", "gap2",
                "gapminus1", "gapminus2", "alt_sumdiff"]

def discover_formulas(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    if len(numbers) < 20: return
    for formula in ALL_FORMULAS:
        hits = 0; total = 0
        for i in range(len(numbers) - 3):
            a, b, c = numbers[i], numbers[i+1], numbers[i+2]
            pred = apply_formula(a, b, c, formula)
            if pred is None: continue
            if c == pred:
                total += 1; hits += 1
            else:
                total += 1
        if total >= 10:
            accuracy = hits / total
            af = STATE.setdefault("auto_formulas", {}).setdefault(formula, {"hits": 0, "total": 0, "accuracy": 0})
            af["hits"] = hits
            af["total"] = total
            af["accuracy"] = accuracy

def engine_auto_formula(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    if len(numbers) < 10: return 0.5
    af = STATE.get("auto_formulas", {})
    if not af: return 0.5
    good_formulas = [(f, d) for f, d in af.items() if d.get("accuracy", 0) >= 0.15]
    if not good_formulas: return 0.5
    a, b, c = numbers[-3], numbers[-2], numbers[-1]
    votes = {}
    for formula_id, data in good_formulas:
        pred = apply_formula(a, b, c, formula_id)
        if pred is None: continue
        acc = data.get("accuracy", 0)
        votes[pred] = votes.get(pred, 0) + acc
    if not votes: return 0.5
    big_w = sum(w for n, w in votes.items() if n >= 5)
    small_w = sum(w for n, w in votes.items() if n < 5)
    total = big_w + small_w
    if total < 0.5: return 0.5
    return (big_w + 0.5) / (total + 1.0)

# ==================== ADAPTIVE WEIGHTS ====================
REGIME_EXPERT_WEIGHTS = {
    "ALTERNATING":   {"pattern": 0.14, "trend": 0.08, "arith": 0.14, "symmetry": 0.12, "opposite": 0.08, "number_seq": 0.10, "repeated_num": 0.12, "double_detect": 0.10, "overlap_series": 0.16, "connected_series": 0.10, "auto_formula": 0.10},
    "BIG_HEAVY":     {"pattern": 0.14, "trend": 0.15, "arith": 0.13, "symmetry": 0.11, "opposite": 0.13, "number_seq": 0.09, "repeated_num": 0.09, "double_detect": 0.10, "overlap_series": 0.14, "connected_series": 0.10, "auto_formula": 0.12},
    "SMALL_HEAVY":   {"pattern": 0.14, "trend": 0.15, "arith": 0.13, "symmetry": 0.11, "opposite": 0.13, "number_seq": 0.09, "repeated_num": 0.09, "double_detect": 0.10, "overlap_series": 0.14, "connected_series": 0.10, "auto_formula": 0.12},
    "LONG_STREAK":   {"pattern": 0.10, "trend": 0.11, "arith": 0.10, "symmetry": 0.08, "opposite": 0.22, "number_seq": 0.08, "repeated_num": 0.09, "double_detect": 0.12, "overlap_series": 0.15, "connected_series": 0.10, "auto_formula": 0.10},
    "BALANCED":      {"pattern": 0.15, "trend": 0.10, "arith": 0.13, "symmetry": 0.11, "opposite": 0.10, "number_seq": 0.09, "repeated_num": 0.12, "double_detect": 0.09, "overlap_series": 0.15, "connected_series": 0.10, "auto_formula": 0.11},
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
    """🔥 FIX 2: Weight recovery for high-confidence engines"""
    actual = 1.0 if actual_big else 0.0
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"].setdefault(eng, default_engine_state())
        grad = (prob - actual) * (prob - 0.5) * 2.0
        old_w = stats.get("gradient_weight", 1.0)
        new_w = max(0.3, min(3.0, old_w - lr * grad))
        # 🔥 FIX: High-confidence recovery
        confidence = max(prob, 1 - prob)
        if confidence >= 0.70 and new_w < 0.10:
            new_w = 0.10  # Minimum floor for high-confidence engines
        stats["gradient_weight"] = new_w

# ==================== ANTI-ERROR (FIX 1: Short signature) ====================
def check_anti_error(outcomes, current_p_big):
    """🔥 FIX 1: Uses 8-length signature instead of 14"""
    sig = error_signature(outcomes, ERROR_SIG_LEN)
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

# ==================== MAIN PREDICTOR ====================
def predict_next(history):
    outcomes = [h["size"] for h in history]

    engine_probs = {
        "pattern": engine_pattern(outcomes),
        "trend": engine_trend(outcomes),
        "arith": engine_arithmetic(history),
        "symmetry": engine_symmetry(history),
        "opposite": engine_opposite(outcomes),
        "number_seq": engine_number_sequence(history),
        "repeated_num": engine_repeated_number(history),
        "double_detect": engine_double_detect(history),
        "overlap_series": engine_overlapping_series(history),
        "connected_series": engine_connected_series(history),
        "auto_formula": engine_auto_formula(history),
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

    base_conf = max(p_big, 1 - p_big)
    cal_pen = STATE.get("calibration_offset", 0.0) * 0.5
    conf = max(0.52, min(0.95, base_conf - cal_pen))
    pred_size = 1 if p_big >= 0.5 else 0

    numbers = [h["number"] for h in history if h["number"] >= 0]
    hot_top = None
    if len(numbers) >= HOT_NUMBER_WINDOW:
        top = Counter(numbers[-HOT_NUMBER_WINDOW:]).most_common(1)
        if top: hot_top = top[0]

    arith_note = ""
    if len(numbers) >= 2:
        a, b = numbers[-1], numbers[-2]
        arith_note = f"|{a}-{b}|={abs(a-b)} {a}+{b}={a+b}"

    cs_pred, cs_conf, cs_info = analyze_connected_series(numbers)
    cs_str = f"→{cs_pred} ({cs_info})" if cs_pred is not None else "N/A"

    af = STATE.get("auto_formulas", {})
    top_formulas = sorted(af.items(), key=lambda x: x[1].get("accuracy", 0), reverse=True)[:2]
    formulas_str = " ".join([f"{f[:6]}:{d.get('accuracy', 0):.2f}" for f, d in top_formulas]) if top_formulas else "N/A"

    return {
        "pred_size": pred_size, "confidence": conf,
        "engine_probs": engine_probs, "weights": weights,
        "regime": regime, "entropy": predictability,
        "signature": pattern_signature(outcomes),
        "num_sig": number_signature(history, 5),
        "hot_top": hot_top, "arith_note": arith_note,
        "cs_str": cs_str, "cs_pred": cs_pred, "cs_conf": cs_conf,
        "formulas_str": formulas_str,
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

    arith_candidates = set()
    if len(numbers) >= 2:
        a, b = numbers[-1], numbers[-2]
        for val in [abs(a-b), (a+b) % 10]:
            if val in candidates: arith_candidates.add(val)

    trans_candidates = set()
    if len(numbers) >= 2:
        trans_key = f"{numbers[-2]}->{numbers[-1]}"
        tm = STATE.get("trans_memory", {}).get(trans_key)
        if tm:
            for n_str, cnt in tm.items():
                try:
                    n = int(n_str)
                    if n in candidates and cnt >= 2: trans_candidates.add(n)
                except: pass

    cs_pred, cs_conf, _ = analyze_connected_series(numbers)
    cs_candidate = cs_pred if cs_pred is not None and cs_pred in candidates else None

    af_candidates = set()
    if len(numbers) >= 3:
        a, b, c = numbers[-3], numbers[-2], numbers[-1]
        for f_id, f_data in STATE.get("auto_formulas", {}).items():
            if f_data.get("accuracy", 0) >= 0.15:
                pred = apply_formula(a, b, c, f_id)
                if pred is not None and pred in candidates:
                    af_candidates.add(pred)

    s2n = {0: Counter(), 1: Counter()}
    for i in range(1, len(numbers)):
        if numbers[i] in candidates: s2n[sizes[i-1]][numbers[i]] += 1

    scores = {}
    for n in candidates:
        s1 = (freq_all.get(n, 0) + 1) / (len(numbers) + K)
        s2 = (freq_recent.get(n, 0) + 1) / (tr + K)
        s6 = (s2n[sizes[-1]].get(n, 0) + 1) / (sum(s2n[sizes[-1]].values()) + K)
        arith_bonus = 0.35 if n in arith_candidates else 0.0
        trans_bonus = 0.30 if n in trans_candidates else 0.0
        cs_bonus = 0.60 * cs_conf if n == cs_candidate else 0.0
        af_bonus = 0.30 if n in af_candidates else 0.0
        scores[n] = (s1*0.10 + s2*0.12 + mk[0][n]*0.13 + mk[1][n]*0.13 + mk[2][n]*0.13 + s6*0.39
                     + arith_bonus + trans_bonus + cs_bonus + af_bonus)
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
        STATE["current_level"] = min(STATE.get("current_level", 1) + 1, 4)

def update_pattern_memory(signature, actual_big):
    if not signature: return
    pm = STATE.setdefault("pattern_memory", {}).setdefault(signature, {"next_big": 0, "next_small": 0})
    if actual_big: pm["next_big"] += 1
    else: pm["next_small"] += 1

def update_error_pattern(outcomes, won):
    """🔥 FIX 1: Uses 8-length signature"""
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
    for i in range(len(numbers) - 2):
        key = f"{numbers[i]}->{numbers[i+1]}"
        tm = STATE.setdefault("trans_memory", {}).setdefault(key, {})
        nxt = str(numbers[i+2])
        tm[nxt] = tm.get(nxt, 0) + 1

def update_arith_rules(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 5: return
    for i in range(len(numbers) - 3):
        a, b, c = numbers[i], numbers[i+1], numbers[i+2]
        diff = abs(a - b)
        if diff == c:
            key = f"|{a}-{b}|={c}"
            rule = STATE.setdefault("arith_rules", {}).setdefault(key, {"next_big": 0, "next_small": 0, "count": 0})
            if sizes[i+3] == 1: rule["next_big"] += 1
            else: rule["next_small"] += 1
            rule["count"] += 1
        s = a + b
        if s == c:
            key = f"{a}+{b}={c}"
            rule = STATE.setdefault("arith_rules", {}).setdefault(key, {"next_big": 0, "next_small": 0, "count": 0})
            if sizes[i+3] == 1: rule["next_big"] += 1
            else: rule["next_small"] += 1
            rule["count"] += 1

def update_series_history(history):
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 6: return
    for end_idx in range(4, len(numbers)):
        window_start = end_idx - 4
        if window_start < 0: continue
        sig = detect_overlap_series(numbers[:end_idx], offset=window_start, window_size=4)
        if not sig: continue
        if end_idx >= len(sizes): continue
        next_size = sizes[end_idx]
        sh = STATE.setdefault("series_history", {}).setdefault(sig, {"big": 0, "small": 0})
        if next_size == 1: sh["big"] += 1
        else: sh["small"] += 1

def update_connected_history(history):
    """🔥 FIX 3: Simplified signature"""
    numbers = [h["number"] for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(numbers) < 8: return
    for end_idx in range(7, len(numbers)):
        sub_nums = numbers[:end_idx]
        pred_num, conf, _ = analyze_connected_series(sub_nums)
        if pred_num is None: continue
        recent = sub_nums[-5:]  # 🔥 5 numbers
        sig_parts = []
        for i in range(len(recent)):
            for j in range(i+1, len(recent)):
                gap = abs(recent[i] - recent[j])
                if gap in (1, 2): sig_parts.append(f"{gap}")
        if not sig_parts: continue
        sig_key = "CS_" + "-".join(sorted(set(sig_parts)))
        if end_idx >= len(sizes): continue
        actual_size = sizes[end_idx]
        ch = STATE.setdefault("connected_history", {}).setdefault(sig_key, {"big": 0, "small": 0})
        if actual_size == 1: ch["big"] += 1
        else: ch["small"] += 1

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
    arith_db = len(STATE.get("arith_rules", {}))
    trans_db = len(STATE.get("trans_memory", {}))
    series_db = len(STATE.get("series_history", {}))
    cs_db = len(STATE.get("connected_history", {}))
    formulas = len(STATE.get("auto_formulas", {}))
    learned = STATE.get("learned_overrides", 0)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *LIFETIME STATS*\n"
        f"✅ *Win:* `{wins}` | ❌ *Loss:* `{losses}`\n"
        f"📉 *Max B2B Loss:* `{max_b2b}` | 🔥 *Streak:* `{cur}`\n"
        f"🎯 *Win Rate:* `{wr:.1f}%`\n"
        f"💰 *Next Level:* `{level}` ({BET_LEVELS[min(level-1, 3)]}X)\n"
        f"🧠 *Pat:* `{patterns}` | 🔢 *Num:* `{num_db}` | 🔀 *Trans:* `{trans_db}`\n"
        f"🧬 *Ser:* `{series_db}` | 🔗 *CS:* `{cs_db}` | ⚡ *Form:* `{formulas}`\n"
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
            update_engine_stats(self.pending["engine_probs"], ab, regime)
            gradient_update(self.pending["engine_probs"], ab, STATE.get("gradient_lr", 0.01))
            brier = (self.pending["prob_big"] - (1 if ab else 0)) ** 2
            STATE["calibration_offset"] = STATE.get("calibration_offset", 0.0) * 0.95 + brier * 0.05
            actual_size_str = "BIGGG" if ab else "SMALL"
            win = (actual_size_str == self.pending["pred_size"])
            update_global_stats(win)
            update_pattern_memory(self.pending.get("pattern_sig"), ab)
            # 🔥 FIX: Use 8-length signature for error tracking
            update_error_pattern(outcomes, win)
            update_number_memory(history)
            update_arith_rules(history)
            update_series_history(history)
            update_connected_history(history)
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
                "pattern_sig": sig, "flipped": pred["anti_error_active"]
            }
            STATE["prediction_memory"][str(ni)] = {"size": self.pending["pred_size"], "number": pn}

            hb = format_synced_history_logs(history)
            ep = pred["engine_probs"]
            cons1 = (f"PAT:{ep['pattern']:.2f} TRD:{ep['trend']:.2f} "
                     f"ARH:{ep['arith']:.2f} SYM:{ep['symmetry']:.2f} "
                     f"OPP:{ep['opposite']:.2f} NSQ:{ep['number_seq']:.2f}")
            cons2 = (f"REP:{ep['repeated_num']:.2f} DBL:{ep['double_detect']:.2f} "
                     f"OVL:{ep['overlap_series']:.2f} CS:{ep['connected_series']:.2f} "
                     f"FORM:{ep['auto_formula']:.2f}")
            weights_str = " ".join([f"{k[:3].upper()}:{v:.2f}" for k, v in pred["weights"].items()])

            override_note = ""
            if pred["anti_error_active"]: override_note = f"\n🚨 *FLIP:* `{pred['anti_error_reason']}`"
            elif pred["anti_error_reason"]: override_note = f"\n⚠️ *Dampened:* `{pred['anti_error_reason']}`"

            hot_info = ""
            if pred["hot_top"]:
                hn, hc = pred["hot_top"]
                hot_info = f"\n🔥 *Hot:* `{hn}` ({hc}x)"

            num_sig_str = pred["num_sig"] or "N/A"

            level = STATE.get("current_level", 1)
            fund_advice = f"{BET_LEVELS[min(level-1, 3)]}X"
            stats_footer = build_stats_footer()

            msg = (
                f"🎯 *QUANTUM V32.1 (FIXED)* 🎯\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 *Period:* `{ni}`\n"
                f"🎲 *Number:* `{pn}`\n"
                f"🔥 *Target:* *{'BIGGG 🟢' if ps == 1 else 'SMALL 🔴'}*\n"
                f"📊 *Confidence:* `{conf*100:.1f}%`\n"
                f"📈 *Regime:* `{pred['regime']}` | *Entropy:* `{pred['entropy']:.2f}`\n"
                f"💰 *Fund:* `{fund_advice}` (Level {level})"
                f"{override_note}\n"
                f"🧩 *Size Sig:* `{sig or 'N/A'}`\n"
                f"🔗 *Connected:* `{pred['cs_str']}`\n"
                f"⚡ *Top Formulas:* `{pred['formulas_str']}`\n"
                f"🔢 *Num Sig:* `{num_sig_str}` | ⚡ `{pred['arith_note']}`"
                f"{hot_info}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *11-Engine Consensus:*\n"
                f"`{cons1}`\n`{cons2}`\n"
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
    logger.info("Warmup V32.1...")
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
    update_arith_rules(history)
    update_series_history(history)
    update_connected_history(history)
    discover_formulas(history)
    logger.info(f"Formulas discovered: {len(STATE.get('auto_formulas', {}))}")
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
                f"Num:{len(STATE.get('number_memory', {}))} "
                f"CS:{len(STATE.get('connected_history', {}))}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V32.1 FIXED ACTIVE", status=200)

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
      

