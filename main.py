import json
import time
import math
import os
import asyncio
from collections import defaultdict, Counter
import aiohttp
from aiohttp import web

# ==================== CONFIG ====================
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")

# 🔥 LEVELS — Level 4 back (12X) with safeguards
BET_LEVELS = [1.0, 2.5, 6.0, 12.0]
MAX_LEVEL = 4

# 🔥 SHORT SIG LENGTHS — Reduced for statistical viability
SHORT_LENGTHS = [5, 6, 7]           # Prediction lengths
SHORT_DISPLAY_LENGTHS = [5, 6, 7, 8] # Display lengths

# 🔥 PATTERN RULES (relaxed)
TOP_ACC_THRESHOLD = 0.67
TOP_MIN_SAMPLES = 25
STRONG_ACC_THRESHOLD = 0.59
FLIP_MAX_ACC = 0.45
FLIP_MIN_SAMPLES = 15
PATTERN_MIN_SAMPLES = 15
PATTERN_DISPLAY_MIN = 3

PATTERN_BLACKLIST_LOSSES = 3
MAX_LOSS_STREAK_HARD = 5
LEVEL4_COOLDOWN = 900  # 15 min after Level 4 loss

# ==================== STATE ====================
def _default_eng():
    return {"alpha": 1.0, "beta": 1.0, "hits": 0, "misses": 0, "total": 0,
            "recent_hits": 0, "recent_total": 0, "gradient_weight": 1.0}

ENGINES = ["pattern", "trend", "number_seq", "hot_number", "streak_break",
           "rhythm", "hot_cold", "gambler_instinct",
           "jack_pressure", "number_flow", "trend_fatigue"]

STATE_FILE = "engine_state_v28_5_8.json"
STATE = {
    "engine_stats": {e: _default_eng() for e in ENGINES},
    "number_memory": {}, "pattern_accuracy": {}, "pattern_recent": {},
    "pattern_blacklist": [], "pattern_losses": {},
    "total_wins": 0, "total_losses": 0,
    "current_level": 1, "current_loss_streak": 0, "max_b2b_loss": 0,
    "bot_recent_form": [], "prediction_memory": {},
    "last_processed_issue": 0, "calibration_offset": 0.0, "cooldown_until": 0,
    "level4_hits": 0, "level4_losses": 0,
}

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: s = json.load(f)
            for k in STATE:
                if k not in s: s[k] = STATE[k]
            for e in ENGINES:
                if e not in s.get("engine_stats", {}): s["engine_stats"][e] = _default_eng()
            STATE = s
        except Exception as ex: print(f"State load err: {ex}")

def save_state():
    try:
        for key, cap in [("prediction_memory", 500), ("number_memory", 5000),
                         ("pattern_accuracy", 8000), ("pattern_recent", 8000),
                         ("pattern_blacklist", 500), ("pattern_losses", 3000),
                         ("bot_recent_form", 20)]:
            if len(STATE.get(key, {})) > cap:
                if isinstance(STATE[key], list): STATE[key] = STATE[key][-cap:]
                else:
                    for k in sorted(STATE[key].keys())[:-cap]: del STATE[key][k]
        with open(STATE_FILE, "w") as f: json.dump(STATE, f)
    except Exception as e: print(f"Save err: {e}")

load_state()

# ==================== HELPERS ====================
def make_sig(arr, length):
    if len(arr) < length: return None
    return "".join("B" if x else "S" for x in arr[-length:])

def validate(raw_list):
    if not raw_list or not isinstance(raw_list, list): return None
    out, seen = [], set()
    for item in reversed(raw_list):
        try:
            iss = int(item.get("issueNumber", 0))
            if iss == 0 or iss in seen: continue
            sz_raw = str(item.get("size", "")).upper()
            if sz_raw not in ["BIG", "BIGGG", "SMALL"]: continue
            sz = 1 if sz_raw in ["BIG", "BIGGG"] else 0
            num_raw = item.get("number", None)
            num = -1 if (num_raw is None or num_raw == "") else int(float(num_raw))
            out.append({"issue": iss, "size": sz, "number": num})
            seen.add(iss)
        except: continue
    return out

# ==================== REGIME & ENTROPY ====================
def detect_regime(arr):
    if len(arr) < 20: return "BALANCED"
    r = arr[-20:]
    alt = sum(1 for i in range(len(r)-1) if r[i] != r[i+1])
    br = sum(r) / len(r)
    if alt >= 15: return "ALTERNATING"
    if br >= 0.70: return "BIG_HEAVY"
    if br <= 0.30: return "SMALL_HEAVY"
    if all(x == r[0] for x in r): return "LONG_STREAK"
    return "BALANCED"

def entropy(arr, w=40):
    r = arr[-w:]
    if len(r) < 15: return 1.0
    p1 = sum(r) / len(r)
    if p1 in (0, 1): return 0.0
    return max(0.0, 1.0 - (-(p1*math.log2(p1) + (1-p1)*math.log2(1-p1))))

# ==================== PATTERN ACCURACY (Time-Decay) ====================
def update_pattern_accuracy(arr_before, actual_next):
    for L in SHORT_DISPLAY_LENGTHS:
        if len(arr_before) < L: continue
        sig = make_sig(arr_before, L)
        if not sig: continue
        pa = STATE.setdefault("pattern_accuracy", {}).setdefault(sig, {"big": 0, "small": 0, "weighted_big": 0.0, "weighted_small": 0.0})
        if actual_next == 1:
            pa["big"] += 1
            pa["weighted_big"] = pa.get("weighted_big", 0.0) + 1.0
        else:
            pa["small"] += 1
            pa["weighted_small"] = pa.get("weighted_small", 0.0) + 1.0
        # Decay old weights
        pa["weighted_big"] = pa.get("weighted_big", 0.0) * 0.98
        pa["weighted_small"] = pa.get("weighted_small", 0.0) * 0.98
        pr = STATE.setdefault("pattern_recent", {}).setdefault(sig, [])
        pr.append(actual_next)
        if len(pr) > 20: STATE["pattern_recent"][sig] = pr[-20:]

def get_sig_probability(arr, L):
    """Combined: raw + time-decayed weight"""
    if len(arr) < L: return 0.5, 0
    sig = make_sig(arr, L)
    if not sig: return 0.5, 0
    pa = STATE.get("pattern_accuracy", {}).get(sig)
    if not pa: return 0.5, 0
    b, s = pa.get("big", 0), pa.get("small", 0)
    t = b + s
    if t == 0: return 0.5, 0
    # Raw probability with Laplace
    raw_p = (b + 1) / (t + 2)
    # Weighted probability (recent matters more)
    wb = pa.get("weighted_big", 0.0)
    ws = pa.get("weighted_small", 0.0)
    wtotal = wb + ws
    if wtotal > 0.5:
        weighted_p = (wb + 0.5) / (wtotal + 1.0)
        # Blend: 50% raw, 50% weighted
        p = raw_p * 0.5 + weighted_p * 0.5
    else:
        p = raw_p
    return p, t

def get_sig_recent_prob(arr, L, window=10):
    if len(arr) < L: return 0.5, 0
    sig = make_sig(arr, L)
    if not sig: return 0.5, 0
    pr = STATE.get("pattern_recent", {}).get(sig)
    if not pr: return 0.5, 0
    recent = pr[-window:]
    if len(recent) < 3: return 0.5, len(recent)
    b = sum(recent)
    return (b + 1) / (len(recent) + 2), len(recent)

# 🔥 CORE RULES
def apply_pattern_rules(p_raw, n):
    if n < PATTERN_MIN_SAMPLES:
        return p_raw, False, "FLAT"
    if p_raw >= TOP_ACC_THRESHOLD and n >= TOP_MIN_SAMPLES:
        return p_raw, False, "TOP"
    if p_raw >= STRONG_ACC_THRESHOLD:
        return p_raw, False, "STRONG"
    if p_raw < FLIP_MAX_ACC and n >= FLIP_MIN_SAMPLES:
        return 1.0 - p_raw, True, "FLIP"
    return p_raw, False, "NORMAL"

def get_best_pattern(arr, for_display=False):
    """for_display=True → show n>=3; else → n>=15"""
    best = None
    best_score = -1
    min_n = PATTERN_DISPLAY_MIN if for_display else PATTERN_MIN_SAMPLES
    lengths = SHORT_DISPLAY_LENGTHS if for_display else SHORT_LENGTHS

    for L in lengths:
        if len(arr) < L: continue
        sig = make_sig(arr, L)
        if not sig: continue
        if sig in STATE.get("pattern_blacklist", []): continue
        p_raw, n = get_sig_probability(arr, L)
        if n < min_n: continue

        p, was_flipped, tag = apply_pattern_rules(p_raw, n)

        deviation = abs(p - 0.5)
        # 🔥 Shorter patterns get more weight now (since 8-12 statistically dead)
        length_weight = 1.0 if L <= 6 else (0.9 if L == 7 else 0.7)
        score = (deviation * 3.5) + min(1.0, n / 15) * 0.5 + length_weight * 0.3
        if tag == "TOP": score += 1.5
        elif tag == "STRONG": score += 0.5
        elif tag == "FLIP": score += 0.4

        if score > best_score:
            rp_raw, rn = get_sig_recent_prob(arr, L, 10)
            if rn >= PATTERN_MIN_SAMPLES:
                rp, _, _ = apply_pattern_rules(rp_raw, rn)
            else:
                rp = rp_raw
            best_score = score
            best = (sig, L, p, n, rp, rn, was_flipped, tag, p_raw)
    return best

# ==================== ENGINES ====================
def eng_pattern(arr):
    n = len(arr)
    if n < 15: return 0.5
    best = get_best_pattern(arr, for_display=False)
    if not best: return 0.5
    sig, L, p_hist, samples, p_recent, rn, was_flipped, tag, p_raw = best

    if tag == "FLAT": return 0.5

    if rn >= 5:
        p_combined = p_hist * 0.6 + p_recent * 0.4
    else:
        p_combined = p_hist

    deviation = abs(p_combined - 0.5)
    if deviation < 0.03: return 0.5

    length_boost = 1.0 if L <= 6 else 0.8
    sample_boost = min(1.0, samples / 15)

    if tag == "TOP": boost = 1.6
    elif tag == "STRONG": boost = 1.25
    elif tag == "FLIP": boost = 1.3
    else: boost = 1.0

    amplified = 0.5 + (p_combined - 0.5) * (1.0 + length_boost * 0.5 + sample_boost * 0.5) * boost
    return max(0.05, min(0.95, amplified))

def eng_trend(arr):
    n = len(arr)
    if n < 20: return 0.5
    def ema(a, s):
        k = 2 / (s + 1); e = a[0]
        for x in a[1:]: e = k*x + (1-k)*e
        return e
    e3, e5 = ema(arr[-20:], 3), ema(arr[-20:], 5)
    e13, e21 = ema(arr[-20:], 13), ema(arr[-20:], 21)
    macd = (e3 - e13) + (e5 - e21)
    ms = math.tanh(macd * 4.0)
    vel = 0.0
    if n >= 10:
        l5 = sum(arr[-5:]) / 5; p5 = sum(arr[-10:-5]) / 5
        vel = (l5 - p5) * 2.0
    rec = arr[-6:]
    burst = 0.0
    if sum(rec[-3:]) == 3: burst = 0.7
    elif sum(rec[-3:]) == 0: burst = -0.7
    comb = ms*0.5 + vel*0.3 + burst*0.2
    return max(0.15, min(0.85, 0.5 + 0.5*math.tanh(comb*2.0)))

def eng_number_seq(history):
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    szs = [h["size"] for h in history if h["number"] >= 0]
    if len(nums) < 20: return 0.5
    ln = nums[-3:]
    if len(ln) < 3: return 0.5
    if ln[0] < ln[1] < ln[2]: pat = "ASC"
    elif ln[0] > ln[1] > ln[2]: pat = "DESC"
    elif ln[0] == ln[1] == ln[2]: pat = "SAME3"
    elif ln[1] == ln[2]: pat = "SAME2"
    elif abs(ln[0]-ln[1]) == 1 and abs(ln[1]-ln[2]) == 1: pat = "SEQ"
    else: pat = "OTHER"
    b = s = 0.0
    for i in range(len(nums) - 3):
        sq = nums[i:i+3]
        m = False
        if pat == "ASC" and sq[0] < sq[1] < sq[2]: m = True
        elif pat == "DESC" and sq[0] > sq[1] > sq[2]: m = True
        elif pat == "SAME3" and sq[0] == sq[1] == sq[2]: m = True
        elif pat == "SAME2" and sq[1] == sq[2]: m = True
        elif pat == "SEQ" and abs(sq[0]-sq[1]) == 1 and abs(sq[1]-sq[2]) == 1: m = True
        if m and i + 3 < len(szs):
            w = math.exp((i / len(nums)) * 3.0)
            if szs[i+3] == 1: b += w
            else: s += w
    t = b + s
    if t < 1.0: return 0.5
    return (b + 0.5) / (t + 1.0)

def eng_hot_number(history):
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    if len(nums) < 20: return 0.5
    rec = nums[-20:]
    freq = Counter(rec)
    top = freq.most_common(3)
    if not top: return 0.5
    bw = sum(c for n, c in top if n >= 5)
    sw = sum(c for n, c in top if n < 5)
    mn, mc = top[0]
    dom = mc / len(rec)
    base = bw / (bw + sw) if (bw + sw) > 0 else 0.5
    if dom >= 0.30:
        base = max(0.1, min(0.9, base + (0.15 if mn >= 5 else -0.15)))
    return base

def eng_streak_break(arr):
    if len(arr) < 6: return 0.5
    rec = arr[-10:]
    sl = 1; sv = rec[-1]
    for i in range(len(rec) - 2, -1, -1):
        if rec[i] == sv: sl += 1
        else: break
    if sl >= 6: return 0.15 if sv else 0.85
    elif sl == 5: return 0.22 if sv else 0.78
    elif sl == 4: return 0.32 if sv else 0.68
    elif sl == 3: return 0.42 if sv else 0.58
    return 0.5

def eng_rhythm(arr):
    n = len(arr)
    if n < 20: return 0.5
    rec = arr[-30:] if n >= 30 else arr
    m = len(rec); mn = sum(rec)/m
    bl = 0; bc = 0.0
    for lag in range(2, 9):
        if lag >= m - 2: continue
        num = sum((rec[i]-mn)*(rec[i+lag]-mn) for i in range(m-lag))
        d1 = sum((rec[i]-mn)**2 for i in range(m-lag))
        d2 = sum((rec[i+lag]-mn)**2 for i in range(m-lag))
        den = math.sqrt(d1*d2)
        if den > 0:
            c = num/den
            if abs(c) > abs(bc): bc = c; bl = lag
    if bl == 0 or abs(bc) < 0.20: return 0.5
    tgt = rec[-bl] if bc > 0 else 1 - rec[-bl]
    stg = min(0.35, abs(bc)*0.5)
    return 0.5 + stg if tgt == 1 else 0.5 - stg

def eng_hot_cold(arr):
    form = STATE.get("bot_recent_form", [])
    if len(form) < 3 or len(arr) < 3: return 0.5
    mo = sum(arr[-3:]) / 3.0
    hs = 0
    for r in reversed(form):
        if r == 1: hs += 1
        else: break
    cs = 0
    for r in reversed(form):
        if r == 0: cs += 1
        else: break
    if hs >= 3: p = 0.5 + (mo - 0.5) * 1.5
    elif cs >= 3: p = 0.5 - (mo - 0.5) * 1.0
    else: p = 0.5 + (mo - 0.5) * 0.5
    return max(0.15, min(0.85, p))

def eng_gambler(history):
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    szs = [h["size"] for h in history if h["number"] >= 0]
    if len(nums) < 20 or len(szs) < 20: return 0.5
    sigs = []; wts = []
    psj = 20
    for i in range(len(nums) - 1, -1, -1):
        if nums[i] in (0, 5): psj = len(nums) - 1 - i; break
    if psj >= 8:
        jp = min(1.0, (psj - 5) / 10.0)
        rj = [n for n in nums[-30:] if n in (0, 5)]
        if rj:
            lj = rj[-1]
            jb = 0.5 + (0.15 * jp if lj == 0 else -0.15 * jp)
            sigs.append(jb); wts.append(0.6)
    rs = szs[-15:]
    alt = sum(1 for i in range(len(rs)-1) if rs[i] != rs[i+1])
    cr = alt / (len(rs) - 1)
    if cr > 0.75:
        np = 1 - rs[-1]
        sigs.append(0.5 + (0.10 if np else -0.10)); wts.append(0.4)
    tw = STATE.get("total_wins", 0); tl = STATE.get("total_losses", 0)
    if tw + tl >= 10 and tw/(tw+tl) < 0.45:
        last = szs[-1]; ctr = 1 - last
        sigs.append(0.5 + (0.08 if ctr else -0.08)); wts.append(0.3)
    if not sigs: return 0.5
    tw_ = sum(wts)
    return max(0.20, min(0.80, sum(s*w for s, w in zip(sigs, wts)) / tw_))

# 🔥 NEW ENGINE 9: JACK PRESSURE
def eng_jack_pressure(history):
    """
    Wingo Mindset: Players track when 0 or 5 will appear.
    Analyze jack patterns and predict side based on timing.
    """
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    szs = [h["size"] for h in history if h["number"] >= 0]
    if len(nums) < 30: return 0.5

    # Recent jack distribution
    recent_30 = nums[-30:]
    jacks_0 = recent_30.count(0)
    jacks_5 = recent_30.count(5)
    total_jacks = jacks_0 + jacks_5

    # Expected: ~6 jacks in 30 rounds (20%)
    # If < 3, jack is "due"
    if total_jacks >= 4: return 0.5

    # Which side is next jack likely?
    last_jack = None
    for n in reversed(nums):
        if n in (0, 5): last_jack = n; break

    if last_jack == 0:
        # Last was SMALL jack → next likely BIG jack (5)
        return 0.58
    elif last_jack == 5:
        # Last was BIG jack → next likely SMALL jack (0)
        return 0.42
    return 0.5

# 🔥 NEW ENGINE 10: NUMBER FLOW
def eng_number_flow(history):
    """
    Wingo Mindset: Consecutive number patterns (7→8→9 or 5→4→3).
    Players bet on momentum continuation.
    """
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    if len(nums) < 15: return 0.5

    last3 = nums[-3:]
    if len(last3) < 3: return 0.5

    # Ascending momentum
    if last3[0] < last3[1] < last3[2]:
        # Continuation likely
        return 0.62
    # Descending momentum
    if last3[0] > last3[1] > last3[2]:
        return 0.38

    # Check 2-step flow
    if abs(nums[-1] - nums[-2]) == 1:
        if nums[-1] > nums[-2]:
            return 0.55
        else:
            return 0.45
    return 0.5

# 🔥 NEW ENGINE 11: TREND FATIGUE
def eng_trend_fatigue(arr):
    """
    Wingo Mindset: Long trends exhaust — mean reversion expected.
    When a side dominates for too long, reversal is due.
    """
    if len(arr) < 20: return 0.5
    recent = arr[-20:]
    big_count = sum(recent)
    big_rate = big_count / 20

    # If side is very dominant, fatigue expected
    if big_rate >= 0.75:
        # BIG exhausted → SMALL due
        return 0.30
    elif big_rate <= 0.25:
        # SMALL exhausted → BIG due
        return 0.70
    return 0.5

# ==================== WEIGHTS ====================
REGIME_W = {
    "ALTERNATING": {"pattern": 0.24, "trend": 0.06, "number_seq": 0.10, "hot_number": 0.08, "streak_break": 0.12, "rhythm": 0.10, "hot_cold": 0.08, "gambler_instinct": 0.06, "jack_pressure": 0.06, "number_flow": 0.06, "trend_fatigue": 0.04},
    "BIG_HEAVY":   {"pattern": 0.20, "trend": 0.12, "number_seq": 0.10, "hot_number": 0.09, "streak_break": 0.10, "rhythm": 0.09, "hot_cold": 0.07, "gambler_instinct": 0.06, "jack_pressure": 0.06, "number_flow": 0.06, "trend_fatigue": 0.05},
    "SMALL_HEAVY": {"pattern": 0.20, "trend": 0.12, "number_seq": 0.10, "hot_number": 0.09, "streak_break": 0.10, "rhythm": 0.09, "hot_cold": 0.07, "gambler_instinct": 0.06, "jack_pressure": 0.06, "number_flow": 0.06, "trend_fatigue": 0.05},
    "LONG_STREAK": {"pattern": 0.18, "trend": 0.10, "number_seq": 0.08, "hot_number": 0.08, "streak_break": 0.20, "rhythm": 0.09, "hot_cold": 0.08, "gambler_instinct": 0.05, "jack_pressure": 0.05, "number_flow": 0.04, "trend_fatigue": 0.05},
    "BALANCED":    {"pattern": 0.24, "trend": 0.10, "number_seq": 0.10, "hot_number": 0.09, "streak_break": 0.11, "rhythm": 0.09, "hot_cold": 0.08, "gambler_instinct": 0.06, "jack_pressure": 0.05, "number_flow": 0.05, "trend_fatigue": 0.03},
}

def get_weights(regime):
    base = REGIME_W.get(regime, REGIME_W["BALANCED"]).copy()
    b = {}
    for e, w in base.items():
        st = STATE["engine_stats"].get(e, _default_eng())
        a, bb = st.get("alpha", 1.0), st.get("beta", 1.0)
        pm = a / (a + bb)
        ra = st["recent_hits"]/st["recent_total"] if st.get("recent_total", 0) >= 5 else pm
        comb = pm*0.5 + ra*0.5
        gw = st.get("gradient_weight", 1.0)
        boost = max(0.3, min(2.5, comb/0.5)) * gw
        b[e] = w * boost
    t = sum(b.values()) or 1.0
    return {k: v/t for k, v in b.items()}

def combine(probs, wts):
    tw = sum(wts.values()) or 1.0
    return sum(probs[e]*wts[e] for e in probs) / tw

def grad_update(probs, ab, lr=0.01):
    for e, p in probs.items():
        st = STATE["engine_stats"].setdefault(e, _default_eng())
        g = (p - ab) * (p - 0.5) * 2.0
        ow = st.get("gradient_weight", 1.0)
        st["gradient_weight"] = max(0.3, min(3.0, ow - lr*g))

# ==================== PATTERN BLACKLIST ====================
def check_pattern_blacklist(arr):
    best = get_best_pattern(arr, for_display=False)
    if not best: return False
    return best[0] in STATE.get("pattern_blacklist", [])

def update_pattern_blacklist(sig, won):
    if not sig: return
    if won:
        STATE.setdefault("pattern_losses", {})[sig] = 0
    else:
        pl = STATE.setdefault("pattern_losses", {})
        pl[sig] = pl.get(sig, 0) + 1
        if pl[sig] >= PATTERN_BLACKLIST_LOSSES:
            bl = STATE.setdefault("pattern_blacklist", [])
            if sig not in bl:
                bl.append(sig)
                print(f"🚫 Pattern blacklisted: {sig} ({pl[sig]} losses)")

# ==================== SIGNAL LABEL ====================
def sig_label(c):
    d = c - 0.50
    if d < 0.02: return "🟥 NONE"
    if d < 0.05: return "🟧 WEAK"
    if d < 0.10: return "🟨 MODERATE"
    if d < 0.15: return "🟩 STRONG"
    return "🟢 V.STRONG"

def get_form_label():
    form = STATE.get("bot_recent_form", [])
    if len(form) < 3: return "N/A"
    hot_s = 0
    for r in reversed(form):
        if r == 1: hot_s += 1
        else: break
    cold_s = 0
    for r in reversed(form):
        if r == 0: cold_s += 1
        else: break
    r5 = form[-5:]
    fs = f"{sum(r5)}/{len(r5)}"
    if hot_s >= 3: return f"🔥 HOT ({fs})"
    if cold_s >= 3: return f"❄️ COLD ({fs})"
    return f"😐 NEUTRAL ({fs})"

# ==================== NUMBER PREDICTOR ====================
def predict_number(history, direction):
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    sizes = [h["size"] for h in history if h["number"] >= 0]
    if len(nums) < 40: return 8 if direction == 1 else 2
    cands = [5, 6, 7, 8, 9] if direction == 1 else [0, 1, 2, 3, 4]
    K = len(cands)
    fa = Counter(nums); fr = Counter(nums[-50:])
    tr = len(nums[-50:])
    mk = [{n: 0.0 for n in cands} for _ in range(3)]
    for o in range(1, 4):
        if len(nums) < o + 2: continue
        ls = tuple(nums[-o:]); c, t = Counter(), 0
        for i in range(len(nums) - o):
            if tuple(nums[i:i+o]) == ls:
                nx = nums[i+o]
                if nx in cands: c[nx] += 1; t += 1
        for n in cands: mk[o-1][n] = (c.get(n, 0) + 1) / (t + K)
    s2n = {0: Counter(), 1: Counter()}
    for i in range(1, len(nums)):
        if nums[i] in cands: s2n[sizes[i-1]][nums[i]] += 1
    sc = {}
    for n in cands:
        s1 = (fa.get(n, 0) + 1) / (len(nums) + K)
        s2 = (fr.get(n, 0) + 1) / (tr + K)
        s6 = (s2n[sizes[-1]].get(n, 0) + 1) / (sum(s2n[sizes[-1]].values()) + K)
        sc[n] = s1*0.10 + s2*0.12 + mk[0][n]*0.13 + mk[1][n]*0.13 + mk[2][n]*0.13 + s6*0.39
    return max(sc, key=sc.get)

# ==================== MAIN PREDICTOR ====================
def predict_next(history):
    arr = [h["size"] for h in history]
    pat_blacklisted = check_pattern_blacklist(arr)

    probs = {
        "pattern": eng_pattern(arr) if not pat_blacklisted else 0.5,
        "trend": eng_trend(arr),
        "number_seq": eng_number_seq(history),
        "hot_number": eng_hot_number(history),
        "streak_break": eng_streak_break(arr),
        "rhythm": eng_rhythm(arr),
        "hot_cold": eng_hot_cold(arr),
        "gambler_instinct": eng_gambler(history),
        "jack_pressure": eng_jack_pressure(history),
        "number_flow": eng_number_flow(history),
        "trend_fatigue": eng_trend_fatigue(arr),
    }
    regime = detect_regime(arr)
    wts = get_weights(regime)
    p = combine(probs, wts)
    pred = entropy(arr)
    p = (0.5 + (p-0.5)*(0.5 + pred*0.5)) if p > 0.5 else (0.5 - (0.5-p)*(0.5 + pred*0.5))

    direction = 1 if p >= 0.5 else 0
    agree_count = 0
    for e, prob in probs.items():
        if (prob > 0.52 and direction == 1) or (prob < 0.48 and direction == 0):
            agree_count += 1

    base = max(p, 1-p)
    conf = max(0.50, min(0.95, base))
    lab = sig_label(conf)
    ps = 1 if p >= 0.5 else 0

    nums = [h["number"] for h in history if h["number"] >= 0]
    hot = Counter(nums[-20:]).most_common(1) if len(nums) >= 20 else None

    disp_best = get_best_pattern(arr, for_display=True)
    best_sig = "N/A"; best_note = ""; best_label = ""
    if disp_best:
        sig, L, p_hist, samples, p_recent, rn, was_flipped, tag, p_raw = disp_best
        best_sig = f"{L}-{sig}"
        if tag == "FLAT":
            best_label = "📚 LEARNING"
            best_note = f"Acc:{p_raw*100:.0f}% (n={samples}/15 needed)"
        elif tag == "TOP":
            best_label = "🏆 TOP PRIORITY"
            best_note = f"H:{p_hist*100:.0f}%(n={samples}) R:{p_recent*100:.0f}%(n={rn})"
        elif tag == "STRONG":
            best_label = "⭐ STRONG"
            best_note = f"H:{p_hist*100:.0f}%(n={samples}) R:{p_recent*100:.0f}%(n={rn})"
        elif tag == "FLIP":
            best_label = "🔄 FLIPPED"
            best_note = f"Raw:{p_raw*100:.0f}% → Flipped:{p_hist*100:.0f}% (n={samples})"
        elif tag == "NORMAL":
            best_label = "○ NORMAL"
            best_note = f"H:{p_hist*100:.0f}%(n={samples}) R:{p_recent*100:.0f}%(n={rn})"
    elif pat_blacklisted:
        best_label = "🚫 BLACKLISTED"

    form_label = get_form_label()

    return {"pred_size": ps, "conf": conf, "lab": lab,
            "probs": probs, "wts": wts, "regime": regime, "entropy": pred,
            "best_sig": best_sig, "best_note": best_note, "best_label": best_label,
            "form": form_label, "hot": hot, "agree": agree_count,
            "pat_blacklisted": pat_blacklisted}

# ==================== STATS ====================
def upd_eng_stats(probs, ab, reg):
    for e, p in probs.items():
        st = STATE["engine_stats"].setdefault(e, _default_eng())
        pb = p >= 0.5; hit = pb == ab
        if hit: st["alpha"] += 1.0
        else: st["beta"] += 1.0
        st["total"] = st.get("total", 0) + 1
        st["recent_total"] = st.get("recent_total", 0) + 1
        if hit:
            st["hits"] = st.get("hits", 0) + 1
            st["recent_hits"] = st.get("recent_hits", 0) + 1
        else: st["misses"] = st.get("misses", 0) + 1
        if st["recent_total"] > 50:
            st["recent_hits"] = int(st["recent_hits"] * 0.8)
            st["recent_total"] = int(st["recent_total"] * 0.8)

def upd_global(win, current_level):
    if win:
        STATE["total_wins"] = STATE.get("total_wins", 0) + 1
        STATE["current_loss_streak"] = 0
        STATE["current_level"] = 1
    else:
        STATE["total_losses"] = STATE.get("total_losses", 0) + 1
        STATE["current_loss_streak"] = STATE.get("current_loss_streak", 0) + 1
        STATE["max_b2b_loss"] = max(STATE.get("max_b2b_loss", 0), STATE["current_loss_streak"])

        # 🔥 Level 4 safeguard: cooldown after Level 4 loss
        if current_level == 4:
            STATE["level4_losses"] = STATE.get("level4_losses", 0) + 1
            STATE["cooldown_until"] = time.time() + LEVEL4_COOLDOWN
            STATE["current_level"] = 1
            STATE["current_loss_streak"] = 0
            print("🛑 Level 4 loss → 15 min cooldown")
        else:
            STATE["current_level"] = min(STATE.get("current_level", 1) + 1, MAX_LEVEL)
            # Hard reset after 5 consecutive losses
            if STATE["current_loss_streak"] >= MAX_LOSS_STREAK_HARD:
                STATE["cooldown_until"] = time.time() + 300
                STATE["current_level"] = 1
                STATE["current_loss_streak"] = 0
                print("🛑 Hard reset after 5 losses")

    f = STATE.setdefault("bot_recent_form", [])
    f.append(1 if win else 0)
    STATE["bot_recent_form"] = f[-20:]

# ==================== FORMAT ====================
def fmt_history(history):
    out = ""
    for item in history[-8:]:
        i = item["issue"]; sp = str(i)[-3:]
        ss = "BIGGG" if item["size"] == 1 else "SMALL"
        num = item["number"]; nd = str(num) if num != -1 else "?"
        p = STATE.get("prediction_memory", {}).get(str(i))
        if p and p["size"] == ss and p.get("number") == num and num != -1: icon = "  ☠️☠️☠️"
        elif p and p["size"] == ss: icon = "  ✅✅✅"
        else: icon = ""
        out += f"`{sp}` *{ss}* ({nd}){icon}\n"
    return out

def fmt_footer():
    w = STATE.get("total_wins", 0); l = STATE.get("total_losses", 0)
    mb = STATE.get("max_b2b_loss", 0); cs = STATE.get("current_loss_streak", 0)
    lv = STATE.get("current_level", 1)
    t = w + l; wr = (w/t*100) if t > 0 else 0.0
    pacc = len(STATE.get("pattern_accuracy", {}))
    pbl = len(STATE.get("pattern_blacklist", []))
    l4h = STATE.get("level4_hits", 0)
    l4l = STATE.get("level4_losses", 0)
    lvl_s = f"{BET_LEVELS[min(lv-1, len(BET_LEVELS)-1)]}X"
    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *LIFETIME*\n"
            f"✅ *W:* `{w}` | ❌ *L:* `{l}`\n"
            f"📉 *Max B2B:* `{mb}` | 🔥 *Streak:* `{cs}`\n"
            f"🎯 *WR:* `{wr:.1f}%`\n"
            f"💰 *Level:* `{lv}` ({lvl_s})\n"
            f"🧬 *Sig-DB:* `{pacc}` | 🚫 *BL:* `{pbl}`\n"
            f"⚡ *L4 Stats:* `{l4h}W/{l4l}L`")

# ==================== TELEGRAM ====================
async def tg_send(session, msg):
    try:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200: print(f"TG err: {await r.text()}")
    except Exception as e: print(f"TG: {e}")

async def tg_sticker(session):
    try:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker",
                                json={"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID},
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            await r.text()
    except Exception as e: print(f"Sticker: {e}")

# ==================== API ====================
async def fetch_data(session):
    for att in range(1, 4):
        try:
            async with session.get(API_URL, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    if isinstance(d, dict):
                        if "data" in d and isinstance(d["data"], dict) and "list" in d["data"]:
                            return d["data"]["list"]
                        if "list" in d: return d["list"]
                    elif isinstance(d, list): return d
        except Exception as e: print(f"API att {att}: {e}")
        await asyncio.sleep(2 ** att)
    return None

# ==================== BOT LOOP ====================
class Bot:
    def __init__(self): self.pending = None

    async def run(self, session):
        print("🚀 QUANTUM V28.5.8 STARTED")
        while True:
            try: await self.step(session)
            except Exception as e:
                print(f"SM err: {e}")
                import traceback; traceback.print_exc()
            await asyncio.sleep(5)

    async def step(self, session):
        if time.time() < STATE.get("cooldown_until", 0):
            await asyncio.sleep(5)
            return

        raw = await fetch_data(session)
        if not raw: return
        history = validate(raw)
        if not history or len(history) < 30: return
        last = history[-1]; li = last["issue"]
        if li == STATE.get("last_processed_issue", 0): return
        arr = [h["size"] for h in history]
        regime = detect_regime(arr)

        if self.pending and self.pending["next_issue"] == li:
            ab = last["size"] == 1
            pred_big = self.pending["pred_size"] == "BIGGG"
            upd_eng_stats(self.pending["probs"], ab, regime)
            grad_update(self.pending["probs"], ab)
            actual_s = "BIGGG" if ab else "SMALL"
            win = actual_s == self.pending["pred_size"]

            current_lv = STATE.get("current_level", 1)
            if current_lv == 4 and win:
                STATE["level4_hits"] = STATE.get("level4_hits", 0) + 1
            upd_global(win, current_lv)

            if len(arr) >= 2:
                arr_before = arr[:-1]
                update_pattern_accuracy(arr_before, ab)

            if self.pending.get("best_sig") and self.pending["best_sig"] != "N/A":
                sig_str = self.pending["best_sig"].split("-", 1)[-1]
                update_pattern_blacklist(sig_str, win)

            if win: asyncio.create_task(tg_sticker(session))
            self.pending = None

        if not self.pending or self.pending["last_issue"] != li:
            pred = predict_next(history)
            ni = li + 1
            pn = predict_number(history, pred["pred_size"])
            self.pending = {
                "last_issue": li, "next_issue": ni,
                "pred_size": "BIGGG" if pred["pred_size"] == 1 else "SMALL",
                "pred_number": pn, "probs": pred["probs"],
                "best_sig": pred["best_sig"],
            }
            STATE.setdefault("prediction_memory", {})[str(ni)] = {"size": self.pending["pred_size"], "number": pn}

            hb = fmt_history(history)
            ep = pred["probs"]
            cons1 = f"PAT:{ep['pattern']:.2f} TRD:{ep['trend']:.2f} NSQ:{ep['number_seq']:.2f} HOT:{ep['hot_number']:.2f}"
            cons2 = f"BRK:{ep['streak_break']:.2f} RHY:{ep['rhythm']:.2f} HCD:{ep['hot_cold']:.2f} GMB:{ep['gambler_instinct']:.2f}"
            cons3 = f"JCK:{ep['jack_pressure']:.2f} FLW:{ep['number_flow']:.2f} FTG:{ep['trend_fatigue']:.2f}"
            wstr = " ".join(f"{k[:3].upper()}:{v:.2f}" for k, v in pred["wts"].items())

            hot = f"\n🔥 *Hot:* `{pred['hot'][0][0]}` ({pred['hot'][0][1]}x)" if pred["hot"] else ""

            pat_display = ""
            if pred["best_label"]:
                pat_display = f"\n🎯 *Best Sig:* `{pred['best_sig']}`\n"
                pat_display += f"💪 *Pattern:* `{pred['best_label']}`"
                if pred["best_note"]:
                    pat_display += f"\n   {pred['best_note']}"

            lv = STATE.get("current_level", 1)
            fund = f"{BET_LEVELS[min(lv-1, len(BET_LEVELS)-1)]}X"
            footer = fmt_footer()

            msg = (f"🎯 *QUANTUM V28.5.8 WINGO MIND* 🎯\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"📌 *Period:* `{ni}`\n"
                   f"🎲 *Number:* `{pn}`\n"
                   f"🔥 *Target:* *{'BIGGG 🟢' if pred['pred_size'] == 1 else 'SMALL 🔴'}*\n"
                   f"📊 *Conf:* `{pred['conf']*100:.1f}%` | {pred['lab']}\n"
                   f"🎰 *Form:* {pred['form']}\n"
                   f"🎯 *Agreement:* `{pred['agree']}/11`\n"
                   f"📈 *Regime:* `{pred['regime']}` | *Ent:* `{pred['entropy']:.2f}`\n"
                   f"💰 *Fund:* `{fund}` (Level {lv})"
                   f"{pat_display}"
                   f"{hot}\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"🧠 *11-Engine:*\n`{cons1}`\n`{cons2}`\n`{cons3}`\n"
                   f"⚖️ *Weights:* `{wstr}`\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"📜 *TREND (8):*\n{hb}"
                   f"{footer}")
            asyncio.create_task(tg_send(session, msg))

        STATE["last_processed_issue"] = li
        save_state()

# ==================== WARMUP ====================
async def warmup(session):
    print("Warmup V28.5.8...")
    raw = await fetch_data(session)
    if not raw: return
    history = validate(raw)
    if len(history) < 100: return
    arr = [h["size"] for h in history]
    for i in range(5, len(arr)):
        arr_before = arr[:i]
        actual = arr[i]
        update_pattern_accuracy(arr_before, actual)
    for i in range(50, len(history) - 1):
        part = history[:i]; a = [h["size"] for h in part]
        reg = detect_regime(a)
        try:
            pr = predict_next(part)
            ab = history[i]["size"] == 1
            upd_eng_stats(pr["probs"], ab, reg)
            grad_update(pr["probs"], ab)
        except: continue
    save_state()
    print(f"Warmup done. Sig-DB:{len(STATE.get('pattern_accuracy', {}))}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V28.5.8 WINGO MIND ACTIVE", status=200)

async def main():
    app = web.Application(); app.router.add_get("/", health)
    runner = web.AppRunner(app); await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web server on port {port}")
    async with aiohttp.ClientSession() as session:
        await warmup(session)
        await Bot().run(session)

if __name__ == "__main__":
    asyncio.run(main())
