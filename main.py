import json
import time
import math
import os
import asyncio
import traceback
import logging
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
logger = logging.getLogger("QuantumV26_3")
handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
handler.addHandler(handler) if False else logger.addHandler(handler)

STATE_FILE = "engine_state_v26_3.json"
PATTERN_SIG_LEN = 8  # Pattern signature length

# ==================== PERSISTENT STATE ====================
def default_engine_state():
    return {
        "alpha": 1.0, "beta": 1.0,
        "hits": 0, "misses": 0, "total": 0,
        "recent_hits": 0, "recent_total": 0,
        "brier_sum": 0.0, "tp": 0, "fp": 0, "fn": 0, "tn": 0,
        "gradient_weight": 1.0, "regime_stats": {}
    }

ENGINES = ["markov", "ngram", "runlen", "regime", "streak",
           "autocorr", "alternation", "repeat", "knn", "number_feat",
           "attention", "fft", "hmm", "lyapunov", "kalman", "trend_shift",
           "pattern_memory"]  # 🔥 Engine 17

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                s = json.load(f)
            for eng in ENGINES:
                if eng not in s.get("engine_stats", {}):
                    s.setdefault("engine_stats", {})[eng] = default_engine_state()
            s.setdefault("total_wins", 0)
            s.setdefault("total_losses", 0)
            s.setdefault("current_loss_streak", 0)
            s.setdefault("max_b2b_loss", 0)
            s.setdefault("pattern_memory", {})   # 🔥 Pattern DB
            s.setdefault("error_patterns", {})   # 🔥 Error DB
            return s
        except Exception as e:
            logger.error(f"State load error: {e}")
    return {
        "engine_stats": {eng: default_engine_state() for eng in ENGINES},
        "prediction_memory": {}, "last_processed_issue": 0,
        "calibration_offset": 0.0, "hourly_profiles": {},
        "error_history": [], "gradient_lr": 0.01,
        "hmm_transition": None, "hmm_emission": None,
        "total_wins": 0, "total_losses": 0,
        "current_loss_streak": 0, "max_b2b_loss": 0,
        "pattern_memory": {}, "error_patterns": {}
    }

def save_state(state):
    try:
        if len(state.get("prediction_memory", {})) > 500:
            for k in sorted(state["prediction_memory"].keys())[:-500]:
                del state["prediction_memory"][k]
        # 🔥 Limit pattern memory
        if len(state.get("pattern_memory", {})) > 2000:
            keys = list(state["pattern_memory"].keys())
            for k in keys[:-2000]: del state["pattern_memory"][k]
        if len(state.get("error_patterns", {})) > 500:
            keys = list(state["error_patterns"].keys())
            for k in keys[:-500]: del state["error_patterns"][k]
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"State save error: {e}")

STATE = load_state()

# ==================== PATTERN SIGNATURE ====================
def pattern_signature(outcomes, length=PATTERN_SIG_LEN):
    if len(outcomes) < length: return None
    return "".join('B' if x else 'S' for x in outcomes[-length:])

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
    return max(0.0, 1.0 - (-(p1*math.log2(p1) + (1-p1)*math.log2(1-p1))))

def hour_bucket(issue_num):
    try:
        s = str(issue_num)
        if len(s) >= 12: return int(s[8:10])
    except Exception: pass
    return datetime.now(timezone.utc).hour

# ==================== 17 ENGINES ====================
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
            results.append((big+1)/(total+2)); weights.append(order * math.log(total+1))
    return sum(r*w for r,w in zip(results,weights))/sum(weights) if results else 0.5

def engine_ngram(outcomes):
    n = len(outcomes)
    if n < 15: return 0.5
    s = "".join('B' if x else 'S' for x in outcomes)
    res, wts = [], []
    for L in range(2, 11):
        if n <= L+2: continue
        tail = s[-L:]; bw = sw = 0.0; m = 0
        for i in range(n-L):
            if s[i:i+L] == tail:
                m += 1
                r = math.exp((i/n)*6.0)
                if s[i+L] == 'B': bw += r
                else: sw += r
        if m >= 2:
            res.append((bw+0.5)/(bw+sw+1.0)); wts.append(L*math.log(m+1))
    return sum(r*w for r,w in zip(res,wts))/sum(wts) if res else 0.5

def engine_runlen(outcomes):
    if len(outcomes) < 15: return 0.5
    runs = []; cv, cl = outcomes[0], 1
    for x in outcomes[1:]:
        if x == cv: cl += 1
        else: runs.append((cv,cl)); cv,cl = x,1
    runs.append((cv,cl))
    if len(runs) < 3: return 0.5
    cv, cl = runs[-1]; b = s = 0
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
    r = sum(outcomes[-15:])/15; o = sum(outcomes[-30:-15])/15
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
    mean = sum(outcomes)/n; bc, bl = 0.0, 0
    for lag in range(2, min(16, n//3)):
        num = sum((outcomes[i]-mean)*(outcomes[i+lag]-mean) for i in range(n-lag))
        d1 = sum((outcomes[i]-mean)**2 for i in range(n-lag))
        d2 = sum((outcomes[i+lag]-mean)**2 for i in range(n-lag))
        den = math.sqrt(d1*d2)
        if den>0:
            c = num/den
            if abs(c)>abs(bc): bc, bl = c, lag
    if bl>0 and abs(bc)>0.15:
        v = outcomes[n-bl]; return float(v) if bc>0 else float(1-v)
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
    cs = tuple(outcomes[-state_len:]); cands = []
    for i in range(n-state_len-1):
        hs = tuple(outcomes[i:i+state_len])
        d = sum(1 for a,b in zip(hs,cs) if a!=b)
        cands.append((d, outcomes[i+state_len], i))
    if not cands: return 0.5
    cands.sort(key=lambda x:(x[0], -x[2])); tk = cands[:k]
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
    last_nums = nums[-3:]; b = s = 0
    for i in range(len(nums)-3):
        if tuple(nums[i:i+3]) == tuple(last_nums):
            if sizes[i+3]==1: b += 1
            else: s += 1
    t = b+s
    if t>=3: return (b+1)/(t+2)
    last_n = nums[-1]; bw = sw = 0.0
    for i in range(len(nums)-1):
        if abs(nums[i]-last_n)<=1:
            r = math.exp((i/len(nums))*3.0)
            if sizes[i+1]==1: bw += r
            else: sw += r
    t = bw+sw
    return (bw+0.5)/(t+1.0) if t>0 else 0.5

def engine_attention(outcomes, num_heads=4):
    n = len(outcomes)
    if n < 30: return 0.5
    query_len = 6; query = outcomes[-query_len:]
    keys, values, positions = [], [], []
    for i in range(n - query_len - 1):
        keys.append(outcomes[i:i+query_len])
        values.append(outcomes[i + query_len]); positions.append(i)
    if not keys: return 0.5
    head_results = []
    for temp in [1.0, 2.0, 4.0, 8.0]:
        scores = []
        for k_idx, key in enumerate(keys):
            sim = sum(1 for a,b in zip(key, query) if a==b) / query_len
            rec = positions[k_idx] / n
            scores.append(math.exp(sim * temp) * math.exp(rec * 1.0))
        total_w = sum(scores) or 1.0
        head_results.append(sum(scores[i] * values[i] for i in range(len(values))) / total_w)
    return sum(head_results) / len(head_results)

def engine_fft(outcomes):
    n = len(outcomes)
    if n < 32: return 0.5
    N = 1
    while N * 2 <= min(n, 128): N *= 2
    if N < 16: return 0.5
    signal = outcomes[-N:]; mean = sum(signal) / N
    centered = [x - mean for x in signal]; freqs = []
    for k in range(N // 2):
        real = sum(centered[t] * math.cos(2*math.pi*k*t/N) for t in range(N))
        imag = -sum(centered[t] * math.sin(2*math.pi*k*t/N) for t in range(N))
        freqs.append((k, math.sqrt(real*real + imag*imag), real, imag))
    freqs_nonzero = [f for f in freqs if f[0] > 0]
    if not freqs_nonzero: return 0.5
    dom = max(freqs_nonzero, key=lambda x: x[1])
    k_dom, mag_dom, real_dom, imag_dom = dom
    total_mag = sum(f[1] for f in freqs_nonzero) or 1.0
    strength = mag_dom / total_mag
    if strength < 0.15: return 0.5
    phase_val = real_dom * math.cos(2*math.pi*k_dom*N/N) - imag_dom * math.sin(2*math.pi*k_dom*N/N)
    p_big = 0.5 + 0.4 * math.tanh(phase_val / (mag_dom + 1e-6))
    return max(0.05, min(0.95, p_big * strength + 0.5 * (1 - strength)))

def engine_hmm(outcomes):
    n = len(outcomes)
    if n < 30: return 0.5
    if STATE.get("hmm_transition") is None:
        STATE["hmm_transition"] = [[0.3,0.3,0.4],[0.3,0.3,0.4],[0.4,0.3,0.3]]
    trans = STATE["hmm_transition"]; emissions = [0.85, 0.15, 0.50]
    alpha = [1/3, 1/3, 1/3]
    for t in range(n):
        x = outcomes[t]
        em = [emissions[s] if x == 1 else (1 - emissions[s]) for s in range(3)]
        new_alpha = [em[s] * sum(alpha[p] * trans[p][s] for p in range(3)) for s in range(3)]
        total = sum(new_alpha) or 1.0
        alpha = [a / total for a in new_alpha]
    next_prob = [sum(alpha[p] * trans[p][s] for p in range(3)) for s in range(3)]
    return sum(next_prob[s] * emissions[s] for s in range(3))

def engine_lyapunov(outcomes):
    n = len(outcomes)
    if n < 30: return 0.5
    m = 5; vectors = [outcomes[i:i+m] for i in range(n - m)]
    if len(vectors) < 10: return 0.5
    divergences = []
    for i in range(len(vectors) - 1):
        best_j, best_d = None, float('inf')
        for j in range(len(vectors)):
            if abs(i - j) < 5: continue
            d = sum(1 for a,b in zip(vectors[i], vectors[j]) if a!=b)
            if d < best_d and d > 0: best_d = d; best_j = j
        if best_j is not None:
            for step in range(1, min(5, len(vectors) - i, len(vectors) - best_j)):
                d_future = sum(1 for a,b in zip(vectors[i+step], vectors[best_j+step]) if a!=b)
                if d_future > 0 and best_d > 0:
                    divergences.append(math.log(d_future / best_d) / step)
    if not divergences: return 0.5
    lyap = sum(divergences) / len(divergences)
    if lyap > 0.3: return 0.5
    recent = outcomes[-5:]; trend = sum(recent) / len(recent)
    return 0.5 + (trend - 0.5) * (1 - max(0, lyap))

def engine_kalman(outcomes):
    if len(outcomes) < 10: return 0.5
    Q, R = 0.02, 0.25; x, P = 0.5, 1.0
    for obs in outcomes:
        P_pred = P + Q; K = P_pred / (P_pred + R)
        x = x + K * (obs - x); P = (1 - K) * P_pred
    return max(0.05, min(0.95, x))

def engine_trend_shift(outcomes):
    n = len(outcomes)
    if n < 20: return 0.5
    af = 2/(3+1); as_ = 2/(15+1)
    ef = outcomes[0]; es = outcomes[0]
    for x in outcomes[1:]:
        ef = af * x + (1 - af) * ef
        es = as_ * x + (1 - as_) * es
    return max(0.10, min(0.90, 0.5 + 0.5 * math.tanh((ef - es) * 5.0)))

# 🔥 ENGINE 17: PATTERN MEMORY
def engine_pattern_memory(outcomes):
    """
    Uses stored pattern database:
    For each pattern signature, we stored what happened after it historically.
    """
    sig = pattern_signature(outcomes)
    if not sig: return 0.5
    pm = STATE.get("pattern_memory", {}).get(sig)
    if not pm: return 0.5
    big = pm.get("next_big", 0)
    small = pm.get("next_small", 0)
    total = big + small
    if total < 3: return 0.5
    return (big + 1) / (total + 2)

# ==================== MIXTURE OF EXPERTS ====================
REGIME_EXPERT_WEIGHTS = {
    "ALTERNATING":   {"alternation":0.12,"attention":0.11,"knn":0.10,"trend_shift":0.09,"ngram":0.08,"hmm":0.08,"pattern_memory":0.08,"fft":0.06,"streak":0.06,"markov":0.06,"kalman":0.06,"autocorr":0.03,"number_feat":0.02,"lyapunov":0.02,"runlen":0.005,"regime":0.004,"repeat":0.001},
    "BIG_HEAVY":     {"runlen":0.11,"regime":0.10,"attention":0.10,"hmm":0.09,"pattern_memory":0.09,"markov":0.08,"knn":0.08,"ngram":0.07,"kalman":0.07,"trend_shift":0.07,"fft":0.05,"streak":0.04,"autocorr":0.02,"alternation":0.003,"repeat":0.002,"lyapunov":0.001,"number_feat":0.0},
    "SMALL_HEAVY":   {"runlen":0.11,"regime":0.10,"attention":0.10,"hmm":0.09,"pattern_memory":0.09,"markov":0.08,"knn":0.08,"ngram":0.07,"kalman":0.07,"trend_shift":0.07,"fft":0.05,"streak":0.04,"autocorr":0.02,"alternation":0.003,"repeat":0.002,"lyapunov":0.001,"number_feat":0.0},
    "CHOPPY":        {"attention":0.13,"knn":0.11,"hmm":0.09,"pattern_memory":0.09,"fft":0.08,"autocorr":0.08,"alternation":0.08,"trend_shift":0.08,"markov":0.06,"ngram":0.06,"kalman":0.05,"lyapunov":0.05,"streak":0.02,"runlen":0.005,"regime":0.003,"repeat":0.002,"number_feat":0.0},
    "LONG_STREAK":   {"runlen":0.15,"streak":0.12,"pattern_memory":0.11,"regime":0.10,"attention":0.09,"hmm":0.08,"knn":0.08,"markov":0.07,"ngram":0.06,"kalman":0.04,"trend_shift":0.04,"autocorr":0.03,"fft":0.02,"alternation":0.003,"repeat":0.002,"lyapunov":0.0,"number_feat":0.0},
    "BALANCED":      {"trend_shift":0.11,"pattern_memory":0.11,"markov":0.08,"ngram":0.08,"knn":0.08,"attention":0.08,"hmm":0.07,"fft":0.06,"kalman":0.06,"runlen":0.06,"regime":0.05,"autocorr":0.05,"streak":0.04,"alternation":0.04,"lyapunov":0.02,"repeat":0.01,"number_feat":0.01},
}

def get_moe_weights(regime):
    base = REGIME_EXPERT_WEIGHTS.get(regime, REGIME_EXPERT_WEIGHTS["BALANCED"]).copy()
    boosted = {}
    for eng, bw in base.items():
        stats = STATE["engine_stats"].get(eng, default_engine_state())
        a, b = stats.get("alpha",1.0), stats.get("beta",1.0)
        pm = a / (a + b)
        ra = stats["recent_hits"] / stats["recent_total"] if stats.get("recent_total",0) >= 5 else pm
        combined = pm * 0.5 + ra * 0.5
        grad_w = stats.get("gradient_weight", 1.0)
        boost = max(0.3, min(2.5, combined / 0.5)) * grad_w
        boosted[eng] = bw * boost
    total = sum(boosted.values()) or 1.0
    return {k: v/total for k,v in boosted.items()}

def bayesian_posterior(engine_probs, weights):
    def to_logit(p):
        p = max(0.01, min(0.99, p))
        return math.log(p / (1 - p))
    logit_sum = sum(to_logit(prob) * weights[eng] for eng, prob in engine_probs.items())
    p_big = 1 / (1 + math.exp(-logit_sum))
    preds = list(engine_probs.values()); mean_p = sum(preds) / len(preds)
    var = sum((p - mean_p)**2 for p in preds) / len(preds)
    return p_big, var

def gradient_update(engine_probs, actual_big, lr=0.01):
    actual = 1.0 if actual_big else 0.0
    for eng, prob in engine_probs.items():
        stats = STATE["engine_stats"].setdefault(eng, default_engine_state())
        grad = (prob - actual) * (prob - 0.5) * 2.0
        old_w = stats.get("gradient_weight", 1.0)
        new_w = max(0.3, min(3.0, old_w - lr * grad))
        stats["gradient_weight"] = new_w

def detect_anomaly(outcomes):
    if len(outcomes) < 60: return 0.0
    r = sum(outcomes[-20:]) / 20; o = sum(outcomes[-60:-20]) / 40
    return min(1.0, abs(r - o) / 0.35)

def hourly_bias(outcomes, issue_num):
    hour = hour_bucket(issue_num)
    prof = STATE["hourly_profiles"].get(str(hour))
    if prof and prof.get("total",0) >= 20: return prof["big"] / prof["total"]
    if len(outcomes) >= 20: return sum(outcomes[-50:]) / min(50, len(outcomes))
    return 0.5

def update_hourly_profile(issue_num, actual_big):
    hour = str(hour_bucket(issue_num))
    p = STATE["hourly_profiles"].setdefault(hour, {"big":0, "total":0})
    p["total"] += 1
    if actual_big: p["big"] += 1

# ==================== META-ENGINE ====================
def meta_engine_predict(history, regime):
    outcomes = [h["size"] for h in history]
    
    engine_probs = {
        "markov": engine_markov(outcomes), "ngram": engine_ngram(outcomes),
        "runlen": engine_runlen(outcomes), "regime": engine_regime(outcomes),
        "streak": engine_streak(outcomes), "autocorr": engine_autocorr(outcomes),
        "alternation": engine_alternation(outcomes), "repeat": engine_repeat(outcomes),
        "knn": engine_knn(outcomes), "number_feat": engine_number_feat(history, None),
        "attention": engine_attention(outcomes), "fft": engine_fft(outcomes),
        "hmm": engine_hmm(outcomes), "lyapunov": engine_lyapunov(outcomes),
        "kalman": engine_kalman(outcomes), "trend_shift": engine_trend_shift(outcomes),
        "pattern_memory": engine_pattern_memory(outcomes)
    }
    
    weights = get_moe_weights(regime)
    p_big, var = bayesian_posterior(engine_probs, weights)
    
    # Trend shift override
    last_3 = outcomes[-3:] if len(outcomes) >= 3 else outcomes
    if len(last_3) == 3 and sum(last_3) in (0, 3):
        sp = engine_probs["trend_shift"]
        if sum(last_3) == 3 and sp > 0.6: p_big = p_big * 0.4 + sp * 0.6
        elif sum(last_3) == 0 and sp < 0.4: p_big = p_big * 0.4 + sp * 0.6
    
    predict_ent = calculate_entropy(outcomes)
    if p_big > 0.5: p_big = 0.5 + (p_big-0.5) * (0.4 + predict_ent*0.6)
    else: p_big = 0.5 - (0.5-p_big) * (0.4 + predict_ent*0.6)
    
    if var > 0.06: p_big = 0.5 + (p_big-0.5)*0.65
    
    anomaly = detect_anomaly(outcomes)
    if anomaly > 0.5: p_big = 0.5 + (p_big-0.5)*(1 - anomaly*0.5)
    
    hb = hourly_bias(outcomes, history[-1]["issue"])
    p_big = p_big*0.97 + hb*0.03
    
    # 🔥 ANTI-ERROR OVERRIDE
    sig = pattern_signature(outcomes)
    if sig:
        err = STATE.get("error_patterns", {}).get(sig)
        if err and err.get("fail_count", 0) >= 3:
            # This pattern historically failed us — flip the prediction slightly
            err_ratio = err.get("fail_count", 0) / max(1, err.get("total", 1))
            if err_ratio >= 0.7:
                p_big = 1.0 - p_big  # Flip
                logger.info(f"⚠️ Anti-Error Override active on pattern {sig} (fail ratio {err_ratio:.2f})")
    
    base_conf = max(p_big, 1-p_big)
    cal_pen = STATE.get("calibration_offset", 0.0) * 0.5
    conf = max(0.52, min(0.95, base_conf - cal_pen))
    
    pred_size = 1 if p_big >= 0.5 else 0
    return pred_size, conf, engine_probs, p_big, var, anomaly

# ==================== NUMBER PREDICTOR ====================
def advanced_number_predictor(history_list, predicted_size):
    numbers = [h["number"] for h in history_list]
    sizes = [h["size"] for h in history_list]
    if len(numbers) < 40: return 8 if predicted_size == 1 else 2
    candidates = [5,6,7,8,9] if predicted_size == 1 else [0,1,2,3,4]
    K = len(candidates)
    freq_all = Counter(numbers); freq_recent = Counter(numbers[-50:])
    tr = len(numbers[-50:])
    mk = [{n:0.0 for n in candidates} for _ in range(3)]
    for o in range(1,4):
        if len(numbers) < o+2: continue
        ls = tuple(numbers[-o:]); c, t = Counter(), 0
        for i in range(len(numbers)-o):
            if tuple(numbers[i:i+o]) == ls:
                nx = numbers[i+o]
                if nx in candidates: c[nx]+=1; t+=1
        for n in candidates: mk[o-1][n] = (c.get(n,0)+1)/(t+K)
    s2n = {0:Counter(),1:Counter()}
    for i in range(1, len(numbers)):
        if numbers[i] in candidates: s2n[sizes[i-1]][numbers[i]] += 1
    scores = {}
    for n in candidates:
        s1 = (freq_all.get(n,0)+1)/(len(numbers)+K)
        s2 = (freq_recent.get(n,0)+1)/(tr+K)
        s6 = (s2n[sizes[-1]].get(n,0)+1)/(sum(s2n[sizes[-1]].values())+K)
        scores[n] = s1*0.10 + s2*0.12 + mk[0][n]*0.13 + mk[1][n]*0.13 + mk[2][n]*0.13 + s6*0.39
    return max(scores, key=scores.get)

# ==================== STATS UPDATE ====================
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

def update_global_stats(win):
    if win:
        STATE["total_wins"] = STATE.get("total_wins", 0) + 1
        STATE["current_loss_streak"] = 0
    else:
        STATE["total_losses"] = STATE.get("total_losses", 0) + 1
        STATE["current_loss_streak"] = STATE.get("current_loss_streak", 0) + 1
        if STATE["current_loss_streak"] > STATE.get("max_b2b_loss", 0):
            STATE["max_b2b_loss"] = STATE["current_loss_streak"]

# 🔥 PATTERN MEMORY UPDATE
def update_pattern_memory(signature, actual_big, our_pred_was_big, won):
    if not signature: return
    pm = STATE.setdefault("pattern_memory", {}).setdefault(
        signature, {"next_big": 0, "next_small": 0, "our_wins": 0, "our_losses": 0}
    )
    if actual_big: pm["next_big"] += 1
    else: pm["next_small"] += 1
    if won: pm["our_wins"] += 1
    else: pm["our_losses"] += 1

# 🔥 ERROR PATTERN TRACKING
def update_error_pattern(signature, won):
    if not signature: return
    if won:
        # Success — reduce fail count gradually
        ep = STATE.setdefault("error_patterns", {}).get(signature)
        if ep:
            ep["fail_count"] = max(0, ep["fail_count"] - 1)
            ep["total"] = ep.get("total", 1) + 1
            if ep["fail_count"] == 0:
                del STATE["error_patterns"][signature]
    else:
        ep = STATE.setdefault("error_patterns", {}).setdefault(
            signature, {"fail_count": 0, "total": 0}
        )
        ep["fail_count"] += 1
        ep["total"] = ep.get("total", 0) + 1

# ==================== HISTORY FORMATTER ====================
def format_synced_history_logs(history):
    out = ""
    for item in history[-8:]:
        issue = item["issue"]; sp = str(issue)[-3:]
        ss = "BIGGG" if item["size"]==1 else "SMALL"
        num = item["number"]; nd = str(num) if num != -1 else "?"
        pred = STATE["prediction_memory"].get(str(issue))
        if pred and pred["size"]==ss and pred.get("number")==num and num!=-1:
            icon = "  ☠️☠️☠️"
        elif pred and pred["size"]==ss:
            icon = "  ✅✅✅"
        else:
            icon = ""
        out += f"`{sp}` *{ss}* ({nd}){icon}\n"
    return out

def build_stats_footer():
    wins = STATE.get("total_wins", 0); losses = STATE.get("total_losses", 0)
    max_b2b = STATE.get("max_b2b_loss", 0); cur = STATE.get("current_loss_streak", 0)
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0.0
    pattern_count = len(STATE.get("pattern_memory", {}))
    error_count = len(STATE.get("error_patterns", {}))
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *LIFETIME STATS*\n"
        f"✅ *Win:* `{wins}` | ❌ *Loss:* `{losses}`\n"
        f"📉 *Max B2B Loss:* `{max_b2b}` | 🔥 *Streak:* `{cur}`\n"
        f"🎯 *Win Rate:* `{wr:.1f}%`\n"
        f"🧠 *Pattern DB:* `{pattern_count}` | ⚠️ *Error Patterns:* `{error_count}`"
    )

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
                               timeout=aiohttp.ClientTimeout(total=8)) as r: await r.text()
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
        except Exception as e: logger.warning(f"API att {att}: {e}")
        await asyncio.sleep(2**att)
    return None

# ==================== STATE MACHINE ====================
class BotStateMachine:
    def __init__(self):
        self.state = "WAITING"; self.pending = None
    
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
            update_hourly_profile(li, ab)
            gradient_update(self.pending["engine_probs"], ab, STATE.get("gradient_lr", 0.01))
            brier = (self.pending["prob_big"] - (1 if ab else 0))**2
            STATE["calibration_offset"] = STATE.get("calibration_offset", 0.0)*0.95 + brier*0.05
            STATE.setdefault("error_history", []).append(brier)
            
            # 🔥 FIXED: String vs String comparison
            actual_size_str = "BIGGG" if ab else "SMALL"
            win = (actual_size_str == self.pending["pred_size"])
            update_global_stats(win)
            
            # 🔥 Update Pattern Memory & Error Patterns
            sig = self.pending.get("pattern_sig")
            our_was_big = (self.pending["pred_size"] == "BIGGG")
            update_pattern_memory(sig, ab, our_was_big, win)
            update_error_pattern(sig, win)
            
            if win: await send_win_sticker(session)
            self.pending = None
        
        # ---- PREDICT ----
        if not self.pending or self.pending["last_issue"] != li:
            ps, conf, eng_probs, p_big, var, anomaly = meta_engine_predict(history, regime)
            pn = advanced_number_predictor(history, ps)
            ni = li + 1
            current_sig = pattern_signature(outcomes)
            
            self.pending = {
                "last_issue": li, "next_issue": ni,
                "pred_size": "BIGGG" if ps==1 else "SMALL",
                "pred_number": pn, "prob_big": p_big,
                "engine_probs": eng_probs,
                "pattern_sig": current_sig  # 🔥 Save signature for later evaluation
            }
            STATE["prediction_memory"][str(ni)] = {
                "size": self.pending["pred_size"], "number": pn
            }
            
            hb = format_synced_history_logs(history)
            cons = " ".join([f"{k[:3].upper()}:{v:.2f}" for k,v in eng_probs.items()])
            rank = sorted(ENGINES, key=lambda e: STATE["engine_stats"][e]["alpha"]/(STATE["engine_stats"][e]["alpha"]+STATE["engine_stats"][e]["beta"]), reverse=True)
            top3 = " ".join([e[:5].upper() for e in rank[:3]])
            
            # Check if anti-error override was active
            sig_now = current_sig or ""
            ep = STATE.get("error_patterns", {}).get(sig_now)
            override_note = ""
            if ep and ep.get("fail_count", 0) >= 3:
                override_note = f"\n⚠️ *Anti-Error Active:* `fail={ep['fail_count']}/{ep.get('total',0)}`"
            
            stats_footer = build_stats_footer()
            
            msg = (
                f"🎯 *QUANTUM V26.3 PATTERN-AWARE* 🎯\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 *Period:* `{ni}`\n"
                f"🎲 *Number:* `{pn}`\n"
                f"🔥 *Target:* *{'BIGGG 🟢' if ps==1 else 'SMALL 🔴'}*\n"
                f"📊 *Confidence:* `{conf*100:.1f}%`\n"
                f"📈 *Regime:* `{regime}` | *Entropy:* `{calculate_entropy(outcomes):.2f}`\n"
                f"⚖️ *Var:* `{var:.3f}` | *Anomaly:* `{anomaly:.2f}`\n"
                f"🧩 *Pattern:* `{sig_now or 'N/A'}`"
                f"{override_note}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *17-Engine Consensus:*\n`{cons}`\n"
                f"🏆 *Top 3:* `{top3}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📜 *TREND (8)*:\n{hb}"
                f"{stats_footer}"
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
    
    # 🔥 Also seed pattern memory from historical walk
    for i in range(PATTERN_SIG_LEN, len(history) - 1):
        partial = history[:i]
        outcomes = [h["size"] for h in partial]
        sig = pattern_signature(outcomes)
        if sig:
            next_big = history[i]["size"] == 1
            pm = STATE.setdefault("pattern_memory", {}).setdefault(
                sig, {"next_big": 0, "next_small": 0, "our_wins": 0, "our_losses": 0}
            )
            if next_big: pm["next_big"] += 1
            else: pm["next_small"] += 1
    
    # Engine warmup
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
    logger.info(f"Warmup complete. Patterns seeded: {len(STATE.get('pattern_memory', {}))}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V26.3 PATTERN-AWARE ACTIVE", status=200)

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


