import json
import time
import math
import os
import asyncio
import traceback
import logging
import cmath
from logging.handlers import RotatingFileHandler
from collections import Counter, deque, defaultdict
import aiohttp
from aiohttp import web
from datetime import datetime, timezone

# ==================== CONFIG ====================
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")
# ================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QuantumV26")
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
logger.addHandler(handler)

STATE_FILE = "engine_state_v26.json"

# ==================== PERSISTENT STATE ====================
def default_engine_state():
    return {
        "alpha": 1.0, "beta": 1.0,
        "hits": 0, "misses": 0, "total": 0,
        "recent_hits": 0, "recent_total": 0,
        "brier_sum": 0.0,
        "tp": 0, "fp": 0, "fn": 0, "tn": 0,
        "gradient_weight": 1.0,  # For online gradient descent
        "regime_stats": {}
    }

ENGINES = ["markov", "ngram", "runlen", "regime", "streak",
           "autocorr", "alternation", "repeat", "knn", "number_feat",
           "attention", "fft", "hmm", "lyapunov", "kalman"]  # 15 engines

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                s = json.load(f)
            for eng in ENGINES:
                if eng not in s.get("engine_stats", {}):
                    s.setdefault("engine_stats", {})[eng] = default_engine_state()
            return s
        except Exception as e:
            logger.error(f"State load error: {e}")
    return {
        "engine_stats": {eng: default_engine_state() for eng in ENGINES},
        "prediction_memory": {},
        "last_processed_issue": 0,
        "calibration_offset": 0.0,
        "hourly_profiles": {},
        "error_history": [],
        "gradient_lr": 0.01,       # Learning rate
        "hmm_transition": None,    # Learned transition matrix
        "hmm_emission": None,      # Learned emission
    }

def save_state(state):
    try:
        if len(state.get("prediction_memory", {})) > 500:
            for k in sorted(state["prediction_memory"].keys())[:-500]:
                del state["prediction_memory"][k]
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"State save error: {e}")

STATE = load_state()

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
        except Exception:
            continue
    return sanitized

# ==================== FEATURE EXTRACTION ====================
def detect_regime(outcomes):
    if len(outcomes) < 20: return "BALANCED"
    recent = outcomes[-20:]
    alternations = sum(1 for i in range(len(recent)-1) if recent[i] != recent[i+1])
    big_rate = sum(recent) / len(recent)
    if alternations >= 15: return "ALTERNATING"
    if big_rate >= 0.70: return "BIG_HEAVY"
    if big_rate <= 0.30: return "SMALL_HEAVY"
    if all(x == recent[0] for x in recent): return "LONG_STREAK"
    if 0.4 <= big_rate <= 0.6 and alternations >= 10: return "CHOPPY"
    return "BALANCED"

def calculate_entropy(outcomes, window=40):
    recent = outcomes[-window:]
    if len(recent) < 15: return 1.0
    p1 = sum(recent) / len(recent)
    if p1 == 0 or p1 == 1: return 0.0
    p0 = 1 - p1
    return max(0.0, 1.0 - (-(p1*math.log2(p1) + p0*math.log2(p0))))

def hour_bucket(issue_num):
    try:
        s = str(issue_num)
        if len(s) >= 12:
            return int(s[8:10])
    except Exception: pass
    return datetime.now(timezone.utc).hour

# ==================== 10 CORE ENGINES ====================
def engine_markov(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    results, weights = [], []
    for order in range(1, 6):
        if n < order + 5: continue
        last_seq = tuple(outcomes[-order:])
        big = small = 0
        for i in range(n - order):
            if tuple(outcomes[i:i+order]) == last_seq:
                if outcomes[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            results.append((big+1)/(total+2))
            weights.append(order * math.log(total+1))
    return sum(r*w for r,w in zip(results,weights))/sum(weights) if results else 0.5

def engine_ngram(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    s = "".join('B' if x else 'S' for x in outcomes)
    res, wts = [], []
    for L in range(2, 11):
        if n <= L+2: continue
        tail = s[-L:]
        bw = sw = 0.0
        m = 0
        for i in range(n-L):
            if s[i:i+L] == tail:
                m += 1
                r = math.exp((i/n)*6.0)
                if s[i+L] == 'B': bw += r
                else: sw += r
        if m >= 2:
            res.append((bw+0.5)/(bw+sw+1.0))
            wts.append(L*math.log(m+1))
    return sum(r*w for r,w in zip(res,wts))/sum(wts) if res else 0.5

def engine_runlen(outcomes):
    if len(outcomes) < 15: return 0.5
    runs = []
    cv, cl = outcomes[0], 1
    for x in outcomes[1:]:
        if x == cv: cl += 1
        else: runs.append((cv,cl)); cv,cl = x,1
    runs.append((cv,cl))
    if len(runs) < 3: return 0.5
    cv, cl = runs[-1]
    b = s = 0
    for i in range(len(runs)-1):
        if runs[i][0]==cv and abs(runs[i][1]-cl)<=1:
            if runs[i+1][0]==1: b += 1
            else: s += 1
    t = b+s
    if t >= 3: return (b+1)/(t+2)
    base = sum(outcomes)/len(outcomes)
    if cl>=5: return 0.15 if cv==1 else 0.85
    if cl>=4: return 0.25 if cv==1 else 0.75
    if cl>=3: return 0.35 if cv==1 else 0.65
    return base

def engine_regime(outcomes):
    if len(outcomes) < 30: return 0.5
    r = sum(outcomes[-15:])/15
    o = sum(outcomes[-30:-15])/15
    return r if abs(r-o) > 0.3 else (r*0.7 + o*0.3)

def engine_streak(outcomes):
    if len(outcomes) < 6: return 0.5
    r = outcomes[-6:]
    if sum(r)==6: return 0.20
    if sum(r)==0: return 0.80
    if r==[1,0,1,0,1,0]: return 0.75
    if r==[0,1,0,1,0,1]: return 0.25
    l3 = outcomes[-3:]
    if sum(l3)==3: return 0.35
    if sum(l3)==0: return 0.65
    return 0.5

def engine_autocorr(outcomes):
    n = len(outcomes)
    if n < 30: return 0.5
    mean = sum(outcomes)/n
    bc, bl = 0.0, 0
    for lag in range(2, min(16, n//3)):
        num = sum((outcomes[i]-mean)*(outcomes[i+lag]-mean) for i in range(n-lag))
        d1 = sum((outcomes[i]-mean)**2 for i in range(n-lag))
        d2 = sum((outcomes[i+lag]-mean)**2 for i in range(n-lag))
        den = math.sqrt(d1*d2)
        if den>0:
            c = num/den
            if abs(c)>abs(bc): bc, bl = c, lag
    if bl>0 and abs(bc)>0.15:
        v = outcomes[n-bl]
        return float(v) if bc>0 else float(1-v)
    return 0.5

def engine_alternation(outcomes):
    if len(outcomes) < 6: return 0.5
    r = outcomes[-6:]
    if r==[1,0,1,0,1,0] or r==[0,1,0,1,0,1]:
        return 0.70 if r[-1]==0 else 0.30
    return 0.5

def engine_repeat(outcomes):
    n = len(outcomes)
    if n < 8: return 0.5
    s = "".join('B' if x else 'S' for x in outcomes)
    for L in [3,4]:
        if n < 2*L: continue
        if s[-2*L:-L] == s[-L:]:
            if s[-L:] == 'B'*L: return 0.30
            if s[-L:] == 'S'*L: return 0.70
    return 0.5

def engine_knn(outcomes, k=15, state_len=8):
    n = len(outcomes)
    if n < state_len+20: return 0.5
    cs = tuple(outcomes[-state_len:])
    cands = []
    for i in range(n-state_len-1):
        hs = tuple(outcomes[i:i+state_len])
        d = sum(1 for a,b in zip(hs,cs) if a!=b)
        cands.append((d, outcomes[i+state_len], i))
    if not cands: return 0.5
    cands.sort(key=lambda x:(x[0], -x[2]))
    tk = cands[:k]
    bw = sw = 0.0
    for d,v,i in tk:
        w = math.exp(-d*0.5) * math.exp((i/n)*2.0)
        if v==1: bw += w
        else: sw += w
    t = bw+sw
    return (bw+0.5)/(t+1.0) if t>0 else 0.5

def engine_number_feat(history_list, _):
    if len(history_list) < 30: return 0.5
    nums = [h["number"] for h in history_list if h["number"]>=0]
    sizes = [h["size"] for h in history_list if h["number"]>=0]
    if len(nums) < 30: return 0.5
    last_nums = nums[-3:]
    b = s = 0
    for i in range(len(nums)-3):
        if tuple(nums[i:i+3]) == tuple(last_nums):
            if sizes[i+3]==1: b += 1
            else: s += 1
    t = b+s
    if t>=3: return (b+1)/(t+2)
    last_n = nums[-1]
    bw = sw = 0.0
    for i in range(len(nums)-1):
        if abs(nums[i]-last_n)<=1:
            r = math.exp((i/len(nums))*3.0)
            if sizes[i+1]==1: bw += r
            else: sw += r
    t = bw+sw
    return (bw+0.5)/(t+1.0) if t>0 else 0.5

# ==================== 🔥 ENGINE 11: ATTENTION MECHANISM ====================
def engine_attention(outcomes, num_heads=4):
    """
    Multi-head attention over historical windows.
    Query = last K outcomes, Key = historical windows, Value = next outcome.
    """
    n = len(outcomes)
    if n < 30: return 0.5
    query_len = 6
    query = outcomes[-query_len:]
    
    # Build (key, value) pairs
    keys, values, positions = [], [], []
    for i in range(n - query_len - 1):
        keys.append(outcomes[i:i+query_len])
        values.append(outcomes[i + query_len])
        positions.append(i)
    
    if not keys: return 0.5
    
    # Multi-head with different temperature (sharpness)
    head_results = []
    for temp in [1.0, 2.0, 4.0, 8.0]:
        scores = []
        for k_idx, key in enumerate(keys):
            # Similarity = negative Hamming
            sim = sum(1 for a,b in zip(key, query) if a==b)
            sim_norm = sim / query_len
            # Recency bonus
            rec = (positions[k_idx] / n)
            scores.append(math.exp(sim_norm * temp) * math.exp(rec * 1.0))
        
        total_w = sum(scores) or 1.0
        weighted_big = sum(scores[i] * values[i] for i in range(len(values)))
        p_big = weighted_big / total_w
        head_results.append(p_big)
    
    return sum(head_results) / len(head_results)

# ==================== 🔥 ENGINE 12: FFT PERIODICITY ====================
def engine_fft(outcomes):
    """
    Fast Fourier Transform based periodicity detection.
    Find dominant frequency and predict next value from phase.
    """
    n = len(outcomes)
    if n < 32: return 0.5
    # Use last power-of-2 window
    N = 1
    while N * 2 <= min(n, 128): N *= 2
    if N < 16: return 0.5
    
    # Center the signal (subtract mean)
    signal = outcomes[-N:]
    mean = sum(signal) / N
    centered = [x - mean for x in signal]
    
    # DFT (small N so O(N^2) is fine)
    freqs = []
    for k in range(N // 2):
        real = sum(centered[t] * math.cos(2*math.pi*k*t/N) for t in range(N))
        imag = -sum(centered[t] * math.sin(2*math.pi*k*t/N) for t in range(N))
        mag = math.sqrt(real*real + imag*imag)
        freqs.append((k, mag, real, imag))
    
    if not freqs: return 0.5
    # Dominant frequency (skip DC = k=0)
    freqs_nonzero = [f for f in freqs if f[0] > 0]
    if not freqs_nonzero: return 0.5
    dom = max(freqs_nonzero, key=lambda x: x[1])
    k_dom, mag_dom, real_dom, imag_dom = dom
    
    # Signal strength
    total_mag = sum(f[1] for f in freqs_nonzero) or 1.0
    strength = mag_dom / total_mag
    
    if strength < 0.15: return 0.5
    
    # Predict next via inverse (phase shift)
    t_next = N
    phase_val = real_dom * math.cos(2*math.pi*k_dom*t_next/N) - imag_dom * math.sin(2*math.pi*k_dom*t_next/N)
    # Normalize to probability
    p_big = 0.5 + 0.4 * math.tanh(phase_val / (mag_dom + 1e-6))
    return max(0.05, min(0.95, p_big * strength + 0.5 * (1 - strength)))

# ==================== 🔥 ENGINE 13: HIDDEN MARKOV MODEL ====================
def engine_hmm(outcomes):
    """
    3-state HMM: TRENDING_BIG, TRENDING_SMALL, CHOPPY
    Viterbi-style inference with learned transitions.
    """
    n = len(outcomes)
    if n < 30: return 0.5
    
    # Initialize/learn transition matrix
    if STATE.get("hmm_transition") is None or len(STATE.get("hmm_transition", [])) != 3:
        STATE["hmm_transition"] = [[0.3,0.3,0.4],[0.3,0.3,0.4],[0.4,0.3,0.3]]
    
    trans = STATE["hmm_transition"]
    # Emission: P(outcome=1 | state)
    emissions = [0.85, 0.15, 0.50]  # state 0: Big, state 1: Small, state 2: Choppy
    
    # Forward algorithm
    alpha = [1/3, 1/3, 1/3]
    for t in range(n):
        x = outcomes[t]
        em = [emissions[s] if x == 1 else (1 - emissions[s]) for s in range(3)]
        new_alpha = [
            em[s] * sum(alpha[prev] * trans[prev][s] for prev in range(3))
            for s in range(3)
        ]
        total = sum(new_alpha) or 1.0
        alpha = [a / total for a in new_alpha]
    
    # Predict next
    next_prob = [sum(alpha[prev] * trans[prev][s] for prev in range(3)) for s in range(3)]
    p_big = sum(next_prob[s] * emissions[s] for s in range(3))
    return p_big

# ==================== 🔥 ENGINE 14: LYAPUNOV EXPONENT (CHAOS DETECTION) ====================
def engine_lyapunov(outcomes):
    """
    Estimate largest Lyapunov exponent.
    If positive → chaotic → unpredictable → return 0.5 (neutral).
    If negative/zero → predictable → return trend continuation.
    """
    n = len(outcomes)
    if n < 30: return 0.5
    
    # Compute divergence of nearby trajectories
    m = 5  # embedding dimension
    # Build embedded vectors
    vectors = []
    for i in range(n - m):
        vectors.append(outcomes[i:i+m])
    if len(vectors) < 10: return 0.5
    
    # For each vector, find nearest neighbor
    divergences = []
    for i in range(len(vectors) - 1):
        # Find nearest neighbor (not itself, not too close in time)
        best_j, best_d = None, float('inf')
        for j in range(len(vectors)):
            if abs(i - j) < 5: continue
            d = sum(1 for a,b in zip(vectors[i], vectors[j]) if a!=b)
            if d < best_d and d > 0:
                best_d = d; best_j = j
        if best_j is not None:
            # Track divergence over next few steps
            for step in range(1, min(5, len(vectors) - i, len(vectors) - best_j)):
                d_future = sum(1 for a,b in zip(vectors[i+step], vectors[best_j+step]) if a!=b)
                if d_future > 0 and best_d > 0:
                    divergences.append(math.log(d_future / best_d) / step)
    
    if not divergences: return 0.5
    lyap = sum(divergences) / len(divergences)
    
    # If lyap > 0 → chaotic (bad for prediction)
    # If lyap <= 0 → stable → trust recent trend
    if lyap > 0.3:
        return 0.5  # Chaotic, no signal
    else:
        # Use recent trend
        recent = outcomes[-5:]
        trend = sum(recent) / len(recent)
        return 0.5 + (trend - 0.5) * (1 - max(0, lyap))

# ==================== 🔥 ENGINE 15: KALMAN FILTER ====================
def engine_kalman(outcomes):
    """
    1D Kalman filter to estimate hidden "true state" of market bias.
    Model: state = true_prob_big, observation = outcome (noisy).
    """
    n = len(outcomes)
    if n < 10: return 0.5
    
    # Process noise Q, observation noise R
    Q = 0.02
    R = 0.25
    
    x = 0.5  # Initial state estimate
    P = 1.0  # Initial uncertainty
    
    for obs in outcomes:
        # Predict
        P_pred = P + Q
        # Update
        K = P_pred / (P_pred + R)
        x = x + K * (obs - x)
        P = (1 - K) * P_pred
    
    return max(0.05, min(0.95, x))

# ==================== MIXTURE OF EXPERTS ====================
REGIME_EXPERT_WEIGHTS = {
    "ALTERNATING":   {"alternation":0.15,"attention":0.15,"knn":0.12,"ngram":0.10,"hmm":0.10,"fft":0.08,"streak":0.08,"markov":0.08,"kalman":0.06,"number_feat":0.02,"autocorr":0.03,"lyapunov":0.01,"runlen":0.01,"regime":0.005,"repeat":0.005},
    "BIG_HEAVY":     {"runlen":0.14,"regime":0.12,"attention":0.12,"hmm":0.11,"markov":0.10,"knn":0.10,"ngram":0.09,"kalman":0.08,"fft":0.06,"streak":0.05,"autocorr":0.02,"alternation":0.005,"repeat":0.003,"lyapunov":0.001,"number_feat":0.0},
    "SMALL_HEAVY":   {"runlen":0.14,"regime":0.12,"attention":0.12,"hmm":0.11,"markov":0.10,"knn":0.10,"ngram":0.09,"kalman":0.08,"fft":0.06,"streak":0.05,"autocorr":0.02,"alternation":0.005,"repeat":0.003,"lyapunov":0.001,"number_feat":0.0},
    "CHOPPY":        {"attention":0.15,"knn":0.13,"hmm":0.11,"fft":0.10,"autocorr":0.10,"alternation":0.10,"markov":0.08,"ngram":0.08,"kalman":0.07,"lyapunov":0.05,"streak":0.02,"runlen":0.005,"regime":0.003,"repeat":0.002,"number_feat":0.0},
    "LONG_STREAK":   {"runlen":0.18,"streak":0.14,"regime":0.12,"attention":0.11,"hmm":0.10,"knn":0.10,"markov":0.08,"ngram":0.07,"kalman":0.05,"autocorr":0.03,"fft":0.015,"alternation":0.003,"repeat":0.002,"lyapunov":0.0,"number_feat":0.0},
    "BALANCED":      {"markov":0.10,"ngram":0.10,"knn":0.10,"attention":0.10,"hmm":0.09,"fft":0.08,"kalman":0.08,"runlen":0.08,"regime":0.07,"autocorr":0.06,"streak":0.05,"alternation":0.05,"lyapunov":0.02,"repeat":0.01,"number_feat":0.01},
}

def get_moe_weights(regime):
    base = REGIME_EXPERT_WEIGHTS.get(regime, REGIME_EXPERT_WEIGHTS["BALANCED"]).copy()
    boosted = {}
    for eng, bw in base.items():
        stats = STATE["engine_stats"].get(eng, default_engine_state())
        a, b = stats.get("alpha",1.0), stats.get("beta",1.0)
        pm = a / (a + b)
        if stats.get("recent_total",0) >= 5:
            ra = stats["recent_hits"] / stats["recent_total"]
        else:
            ra = pm
        combined = pm * 0.5 + ra * 0.5
        grad_w = stats.get("gradient_weight", 1.0)
        boost = max(0.3, min(2.5, combined / 0.5)) * grad_w
        boosted[eng] = bw * boost
    total = sum(boosted.values()) or 1.0
    return {k: v/total for k,v in boosted.items()}

# ==================== BAYESIAN COMBINE ====================
def bayesian_posterior(engine_probs, weights):
    def to_logit(p):
        p = max(0.01, min(0.99, p))
        return math.log(p / (1 - p))
    logit_sum = sum(to_logit(prob) * weights[eng] for eng, prob in engine_probs.items())
    p_big = 1 / (1 + math.exp(-logit_sum))
    preds = list(engine_probs.values())
    mean_p = sum(preds) / len(preds)
    var = sum((p - mean_p)**2 for p in preds) / len(preds)
    return p_big, var

# ==================== ONLINE GRADIENT DESCENT ====================
def gradient_update(engine_probs, actual_big, lr=0.01):
    """
    Update each engine's gradient_weight based on prediction loss.
    Loss = (predicted_prob - actual)^2 (Brier)
    dL/dw ≈ (predicted - actual) * feature_contribution
    """
    actual = 1.0 if actual_big else 0.0
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"].setdefault(eng, default_engine_state())
        # Gradient of squared loss w.r.t. weight, simplified
        grad = (prob - actual) * (prob - 0.5) * 2.0
        # Update weight with momentum
        old_w = stats.get("gradient_weight", 1.0)
        new_w = old_w - lr * grad
        # Clip to prevent runaway
        new_w = max(0.3, min(3.0, new_w))
        stats["gradient_weight"] = new_w

# ==================== ANOMALY DETECTION ====================
def detect_anomaly(outcomes):
    if len(outcomes) < 60: return 0.0
    r = sum(outcomes[-20:]) / 20
    o = sum(outcomes[-60:-20]) / 40
    drift = abs(r - o)
    return min(1.0, drift / 0.35)

# ==================== TIME-OF-DAY ====================
def hourly_bias(outcomes, issue_num):
    hour = hour_bucket(issue_num)
    prof = STATE["hourly_profiles"].get(str(hour))
    if prof and prof.get("total",0) >= 20:
        return prof["big"] / prof["total"]
    if len(outcomes) >= 20:
        return sum(outcomes[-50:]) / min(50, len(outcomes))
    return 0.5

def update_hourly_profile(issue_num, actual_big):
    hour = str(hour_bucket(issue_num))
    p = STATE["hourly_profiles"].setdefault(hour, {"big":0, "total":0})
    p["total"] += 1
    if actual_big: p["big"] += 1

# ==================== MAIN META-ENGINE ====================
def meta_engine_predict(history, regime):
    outcomes = [h["size"] for h in history]
    
    engine_probs = {
        "markov": engine_markov(outcomes),
        "ngram": engine_ngram(outcomes),
        "runlen": engine_runlen(outcomes),
        "regime": engine_regime(outcomes),
        "streak": engine_streak(outcomes),
        "autocorr": engine_autocorr(outcomes),
        "alternation": engine_alternation(outcomes),
        "repeat": engine_repeat(outcomes),
        "knn": engine_knn(outcomes),
        "number_feat": engine_number_feat(history, None),
        "attention": engine_attention(outcomes),
        "fft": engine_fft(outcomes),
        "hmm": engine_hmm(outcomes),
        "lyapunov": engine_lyapunov(outcomes),
        "kalman": engine_kalman(outcomes),
    }
    
    weights = get_moe_weights(regime)
    p_big, var = bayesian_posterior(engine_probs, weights)
    
    # Dampening layers
    predict_ent = calculate_entropy(outcomes)
    if p_big > 0.5:
        p_big = 0.5 + (p_big-0.5) * (0.4 + predict_ent*0.6)
    else:
        p_big = 0.5 - (0.5-p_big) * (0.4 + predict_ent*0.6)
    
    if var > 0.06: p_big = 0.5 + (p_big-0.5)*0.65
    
    anomaly = detect_anomaly(outcomes)
    if anomaly > 0.5: p_big = 0.5 + (p_big-0.5)*(1 - anomaly*0.5)
    
    # Time-of-day small bias
    hb = hourly_bias(outcomes, history[-1]["issue"])
    p_big = p_big*0.97 + hb*0.03
    
    base_conf = max(p_big, 1-p_big)
    cal_pen = STATE.get("calibration_offset", 0.0) * 0.5
    conf = max(0.52, min(0.95, base_conf - cal_pen))
    
    pred_size = 1 if p_big >= 0.5 else 0
    return pred_size, conf, engine_probs, p_big, var, anomaly

# ==================== ADVANCED NUMBER PREDICTOR ====================
def advanced_number_predictor(history_list, predicted_size):
    numbers = [h["number"] for h in history_list]
    sizes = [h["size"] for h in history_list]
    if len(numbers) < 40:
        return 8 if predicted_size == 1 else 2
    candidates = [5,6,7,8,9] if predicted_size == 1 else [0,1,2,3,4]
    K = len(candidates)
    freq_all = Counter(numbers)
    freq_recent = Counter(numbers[-50:])
    tr = len(numbers[-50:])
    mk = [{n:0.0 for n in candidates} for _ in range(3)]
    for o in range(1,4):
        if len(numbers) < o+2: continue
        ls = tuple(numbers[-o:])
        c, t = Counter(), 0
        for i in range(len(numbers)-o):
            if tuple(numbers[i:i+o]) == ls:
                nx = numbers[i+o]
                if nx in candidates: c[nx]+=1; t+=1
        for n in candidates: mk[o-1][n] = (c.get(n,0)+1)/(t+K)
    s2n = {0:Counter(),1:Counter()}
    for i in range(1, len(numbers)):
        if numbers[i] in candidates:
            s2n[sizes[i-1]][numbers[i]] += 1
    scores = {}
    for n in candidates:
        s1 = (freq_all.get(n,0)+1)/(len(numbers)+K)
        s2 = (freq_recent.get(n,0)+1)/(tr+K)
        s6 = (s2n[sizes[-1]].get(n,0)+1)/(sum(s2n[sizes[-1]].values())+K)
        scores[n] = s1*0.10 + s2*0.12 + mk[0][n]*0.13 + mk[1][n]*0.13 + mk[2][n]*0.13 + s6*0.39
    return max(scores, key=scores.get)

# ==================== ENGINE STATS UPDATE ====================
def update_engine_stats(engine_probs, actual_big, regime):
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"].setdefault(eng, default_engine_state())
        pb = prob >= 0.5
        hit = (pb == actual_big)
        if hit: stats["alpha"] += 1.0
        else: stats["beta"] += 1.0
        stats["total"] += 1
        stats["recent_total"] += 1
        if hit:
            stats["hits"] += 1; stats["recent_hits"] += 1
        else: stats["misses"] += 1
        pa = 1.0 if actual_big else 0.0
        stats["brier_sum"] += (prob - pa)**2
        if pb and actual_big: stats["tp"] += 1
        elif not pb and actual_big: stats["fn"] += 1
        elif pb and not actual_big: stats["fp"] += 1
        else: stats["tn"] += 1
        rs = stats.setdefault("regime_stats", {}).setdefault(regime, {"hits":0,"total":0})
        rs["total"] += 1
        if hit: rs["hits"] += 1
        if stats["recent_total"] > 50:
            stats["recent_hits"] = int(stats["recent_hits"]*0.8)
            stats["recent_total"] = int(stats["recent_total"]*0.8)

# ==================== HISTORY FORMATTER ====================
def format_synced_history_logs(history):
    out = ""
    for item in history[-8:]:
        issue = item["issue"]
        sp = str(issue)[-3:]
        ss = "BIGGG" if item["size"]==1 else "SMALL"
        num = item["number"]
        nd = str(num) if num != -1 else "?"
        pred = STATE["prediction_memory"].get(str(issue))
        if pred and pred["size"]==ss and pred.get("number")==num and num!=-1:
            icon = "  ☠️☠️☠️"
        elif pred and pred["size"]==ss:
            icon = "  ✅✅✅"
        else:
            icon = ""
        out += f"`{sp}` *{ss}* ({nd}){icon}\n"
    return out

# ==================== TELEGRAM ====================
async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={"chat_id":CHAT_ID,"text":message,"parse_mode":"Markdown"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200: logger.error(f"TG err: {await r.text()}")
    except Exception as e: logger.error(f"TG: {e}")

async def send_win_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    try:
        async with session.post(url, json={"chat_id":CHAT_ID,"sticker":WIN_STICKER_ID},
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            await r.text()
    except Exception as e: logger.error(f"Sticker: {e}")

# ==================== API ====================
async def fetch_data(session):
    for att in range(1,4):
        try:
            async with session.get(API_URL, headers={'User-Agent':'Mozilla/5.0'},
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d.get("code")==0 and "data" in d and "list" in d["data"]:
                        return d["data"]["list"]
        except Exception as e:
            logger.warning(f"API att {att}: {e}")
        await asyncio.sleep(2**att)
    return None

# ==================== STATE MACHINE ====================
class BotStateMachine:
    def __init__(self):
        self.state = "WAITING"
        self.pending = None
    
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
        last = history[-1]
        li = last["issue"]
        if li == STATE["last_processed_issue"]: return
        
        outcomes = [h["size"] for h in history]
        regime = detect_regime(outcomes)
        
        # EVALUATE
        if self.pending and self.pending["next_issue"] == li:
            ab = last["size"] == 1
            update_engine_stats(self.pending["engine_probs"], ab, regime)
            update_hourly_profile(li, ab)
            gradient_update(self.pending["engine_probs"], ab, STATE.get("gradient_lr", 0.01))
            brier = (self.pending["prob_big"] - (1 if ab else 0))**2
            STATE["calibration_offset"] = STATE.get("calibration_offset", 0.0)*0.95 + brier*0.05
            STATE.setdefault("error_history", []).append(brier)
            if last["size"] == self.pending["pred_size"]:
                await send_win_sticker(session)
            self.pending = None
        
        # PREDICT
        if not self.pending or self.pending["last_issue"] != li:
            ps, conf, eng_probs, p_big, var, anomaly = meta_engine_predict(history, regime)
            pn = advanced_number_predictor(history, ps)
            ni = li + 1
            self.pending = {
                "last_issue": li, "next_issue": ni,
                "pred_size": "BIGGG" if ps==1 else "SMALL",
                "pred_number": pn, "prob_big": p_big, "engine_probs": eng_probs
            }
            STATE["prediction_memory"][str(ni)] = {
                "size": self.pending["pred_size"], "number": pn
            }
            
            hb = format_synced_history_logs(history)
            cons = " ".join([f"{k[:3].upper()}:{v:.2f}" for k,v in eng_probs.items()])
            
            # Top 3 by posterior mean
            rank = sorted(ENGINES, key=lambda e: STATE["engine_stats"][e]["alpha"]/(STATE["engine_stats"][e]["alpha"]+STATE["engine_stats"][e]["beta"]), reverse=True)
            top3 = " ".join([e[:5].upper() for e in rank[:3]])
            
            msg = (
                f"🎯 *QUANTUM V26 SINGULARITY* 🎯\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 *Period:* `{ni}`\n"
                f"🎲 *Number:* `{pn}`\n"
                f"🔥 *Target:* *{'BIGGG 🟢' if ps==1 else 'SMALL 🔴'}*\n"
                f"📊 *Confidence:* `{conf*100:.1f}%`\n"
                f"📈 *Regime:* `{regime}` | *Entropy:* `{calculate_entropy(outcomes):.2f}`\n"
                f"⚖️ *Var:* `{var:.3f}` | *Anomaly:* `{anomaly:.2f}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *15-Engine Consensus:*\n`{cons}`\n"
                f"🏆 *Top 3:* `{top3}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📜 *TREND (8)*:\n{hb}"
            )
            asyncio.create_task(send_telegram(session, msg))
        
        STATE["last_processed_issue"] = li
        save_state(STATE)

# ==================== WARMUP ====================
async def warmup(session):
    logger.info("Warmup starting...")
    raw = await fetch_data(session)
    if not raw: return
    history = validate_and_sanitize(raw)
    if len(history) < 100: return
    for i in range(50, len(history)-1):
        partial = history[:i]
        outcomes = [h["size"] for h in partial]
        regime = detect_regime(outcomes)
        try:
            ps, _, ep, pb, _, _ = meta_engine_predict(partial, regime)
            ab = history[i]["size"] == 1
            update_engine_stats(ep, ab, regime)
            gradient_update(ep, ab, STATE.get("gradient_lr", 0.01))
        except Exception: continue
    save_state(STATE)
    logger.info("Warmup complete.")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V26 SINGULARITY ACTIVE", status=200)

async def main():
    app = web.Application()
    app.router.add_get('/', health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    logger.info(f"Live on {port}")
    async with aiohttp.ClientSession() as session:
        await warmup(session)
        await BotStateMachine().run(session)

if __name__ == "__main__":
    asyncio.run(main())


