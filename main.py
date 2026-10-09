import json, time, math, os, asyncio
from collections import defaultdict, Counter
import aiohttp
from aiohttp import web

# ==================== CONFIG ====================
API_URL   = os.environ.get("API_URL",   "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID   = os.environ.get("CHAT_ID",   "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")

BET_LEVELS         = [1.0, 2.5, 6.0, 12.0]
MAX_LEVEL          = 4
SHORT_LENGTHS      = [5, 6, 7]
SHORT_DISPLAY_LENS = [5, 6, 7, 8]
PATTERN_MIN        = 12
FLIP_MIN           = 12
BLACKLIST_LOSSES   = 3
MAX_LOSS_STREAK    = 5
LEVEL4_COOLDOWN    = 900

OBS_ETA            = 0.02
OBS_L2             = 0.0005
OBS_MIN_W          = 1e-4
OBS_MAX_W          = 0.20
MS_SCALES          = [0.999, 0.99, 0.95]
DIV_STRENGTH       = 0.20
CONTRARIAN_MIN_N   = 15
CONTRARIAN_THRESH  = 0.38

# ==================== MATH UTILS ====================
def logit(p):
    p = max(1e-9, min(1-1e-9, p))
    return math.log(p/(1-p))

def sigmoid(x):
    return 1/(1+math.exp(-max(-15, min(15, x))))

def _acf(arr, lag):
    n = len(arr)
    if n < lag+8: return 0.0
    m = sum(arr)/n
    v = sum((x-m)**2 for x in arr)/n
    if v < 1e-9: return 0.0
    return sum((arr[i]-m)*(arr[i-lag]-m) for i in range(lag,n))/((n-lag)*v)

def _hurst(arr):
    if len(arr) < 20: return 0.5
    n = len(arr); m = sum(arr)/n
    Z = []; s = 0
    for v in arr:
        s += v-m; Z.append(s)
    R = max(Z)-min(Z)
    S = math.sqrt(sum((x-m)**2 for x in arr)/n)
    return math.log(R/S+1e-9)/math.log(n) if S > 1e-9 else 0.5

def _cond_entropy(arr, order=1):
    if len(arr) < order+8: return 1.0
    cnt = defaultdict(lambda: {0:0, 1:0})
    for i in range(order, len(arr)):
        k = tuple(arr[i-order:i]); cnt[k][arr[i]] += 1
    H = 0.0; tot = 0
    for k, d in cnt.items():
        n = d[0]+d[1]; tot += n
        for c in d.values():
            if c > 0: p = c/n; H -= n*(p*math.log2(p))
    return H/max(tot, 1)

def _bic_order(arr, max_o=4):
    n = len(arr)
    if n < 20: return 1
    best, bo = float('inf'), 1
    for o in range(1, max_o+1):
        if n < o+8: break
        cnt = defaultdict(lambda: {0:0, 1:0})
        for i in range(o, n): cnt[tuple(arr[i-o:i])][arr[i]] += 1
        ll = 0.0
        for k, d in cnt.items():
            t = d[0]+d[1]
            for c in d.values():
                if c > 0: ll += c*math.log(c/t)
        bic = -2*ll+(2**o)*math.log(n)
        if bic < best: best, bo = bic, o
    return bo

def _dft(arr):
    n = len(arr)
    if n < 32: return None
    m = sum(arr)/n
    best_amp, best_k, best_re, best_im = 0.0, 0, 0.0, 0.0
    for k in range(2, n//2):
        re = sum((arr[t]-m)*math.cos(2*math.pi*k*t/n) for t in range(n))
        im = sum((arr[t]-m)*math.sin(2*math.pi*k*t/n) for t in range(n))
        amp = math.sqrt(re*re+im*im)/n
        if amp > best_amp:
            best_amp, best_k, best_re, best_im = amp, k, re, im
    if best_amp < 0.07: return None
    phase = math.atan2(best_im, best_re)
    next_pred = m+best_amp*math.cos(2*math.pi*best_k*n/n+phase)
    return {'amp': best_amp, 'prob': max(0.05, min(0.95, next_pred+0.5-m))}

# ==================== OBS ====================
class OnlineBayesianStacking:
    def __init__(self, engines, eta=OBS_ETA, l2=OBS_L2):
        self.engines = list(engines)
        n = len(self.engines)
        self.w = {e: 1.0/n for e in self.engines}
        self.eta = eta
        self.l2 = l2
        self.updates = 0

    def combine(self, probs):
        lo = 0.0; wsum = 0.0
        for e in self.engines:
            if e not in probs: continue
            p = max(1e-6, min(1-1e-6, probs[e]))
            lo += self.w[e] * logit(p)
            wsum += self.w[e]
        if wsum < 1e-9: return 0.5
        return sigmoid(lo / wsum)

    def update(self, probs, actual):
        if not probs: return
        p_c = self.combine(probs)
        err = p_c - actual
        for e in self.engines:
            if e not in probs: continue
            p = max(1e-6, min(1-1e-6, probs[e]))
            grad = err * logit(p)
            self.w[e] -= self.eta * (grad + self.l2 * self.w[e])
        self._project()
        self.updates += 1

    def _project(self):
        for e in self.engines:
            if self.w[e] < OBS_MIN_W: self.w[e] = OBS_MIN_W
            if self.w[e] > OBS_MAX_W: self.w[e] = OBS_MAX_W
        s = sum(self.w.values()) or 1.0
        for e in self.engines:
            self.w[e] /= s

    def weights(self): return dict(self.w)
    def to_dict(self): return {"w": self.w, "updates": self.updates}

    def load_dict(self, d):
        if not d: return
        w = d.get("w", {})
        for e in self.engines:
            if e in w:
                try: self.w[e] = float(w[e])
                except: pass
        self.updates = int(d.get("updates", 0))
        self._project()

# ==================== MULTI-SCALE TRACKER ====================
class MultiScaleTracker:
    def __init__(self, engines):
        self.engines = list(engines)
        self.score = {e: {s: 0.0 for s in MS_SCALES} for e in engines}
        self.total = {e: {s: 0.0 for s in MS_SCALES} for e in engines}

    def update(self, probs, actual):
        for e in self.engines:
            if e not in probs: continue
            p = max(1e-6, min(1-1e-6, probs[e]))
            ll = math.log(p) if actual == 1 else math.log(1.0-p)
            for s in MS_SCALES:
                self.score[e][s] = s * self.score[e][s] + ll
                self.total[e][s] = s * self.total[e][s] + 1.0

    def avg_logscore(self, e, scale):
        t = self.total[e].get(scale, 0.0)
        if t < 1.5: return -0.693
        return self.score[e][scale] / t

    def multi_scale_score(self, e):
        vals = [self.avg_logscore(e, s) for s in MS_SCALES]
        slow, med, fast = vals[0], vals[1], vals[2]
        shift = abs(fast - slow)
        w_fast = 0.25 + min(0.40, shift * 1.5)
        w_slow = max(0.10, 0.45 - shift * 0.8)
        w_med  = max(0.10, 1.0 - w_fast - w_slow)
        total_w = w_fast + w_slow + w_med
        return (fast*w_fast + slow*w_slow + med*w_med) / total_w

    def boost_weights(self, base_weights):
        out = {}
        for e in self.engines:
            sc = self.multi_scale_score(e)
            raw = math.exp(1.5 * (sc + 0.693))
            boost = max(0.50, min(2.0, raw))
            out[e] = base_weights.get(e, 0.0) * boost
        s = sum(out.values()) or 1.0
        return {k: v/s for k, v in out.items()}

    def to_dict(self):
        return {
            "score": {e: {str(s): self.score[e][s] for s in MS_SCALES} for e in self.engines},
            "total": {e: {str(s): self.total[e][s] for s in MS_SCALES} for e in self.engines},
        }

    def load_dict(self, d):
        if not d: return
        try:
            for e in self.engines:
                sd = d.get("score", {}).get(e, {})
                td = d.get("total", {}).get(e, {})
                for s in MS_SCALES:
                    self.score[e][s] = float(sd.get(str(s), 0.0))
                    self.total[e][s] = float(td.get(str(s), 0.0))
        except Exception as ex:
            print(f"MS load: {ex}")

# ==================== SOFT REGIME ====================
def soft_regime_probs(arr):
    if len(arr) < 20:
        return {"TRENDING": 1/6, "ALTERNATING": 1/6, "BIG_HEAVY": 1/6,
                "SMALL_HEAVY": 1/6, "LONG_STREAK": 1/6, "BALANCED": 1/6}
    w = arr[-min(40, len(arr)):]
    h = _hurst(w)
    alt = sum(1 for i in range(1,len(w)) if w[i] != w[i-1])/(len(w)-1)
    br = sum(w)/len(w)
    mn = min(w); mx = max(w)
    flat = 1.0 if mn == mx else 0.0

    s = {}
    s["TRENDING"]    = max(0.0, (h - 0.50) * 4.0) + max(0.0, (0.48 - alt) * 3.0)
    s["ALTERNATING"] = max(0.0, (0.52 - h) * 4.0) + max(0.0, (alt - 0.55) * 3.0)
    s["BIG_HEAVY"]   = max(0.0, (br - 0.56) * 6.0)
    s["SMALL_HEAVY"] = max(0.0, (0.44 - br) * 6.0)
    s["LONG_STREAK"] = (max(0.0, (0.70 - alt) * 4.0) + flat * 2.0) if (br > 0.78 or br < 0.22) else flat * 1.5
    s["BALANCED"]    = 0.35
    tot = sum(s.values()) or 1.0
    return {k: v/tot for k, v in s.items()}

def blend_regime_weights(regime_probs):
    keys = list(regime_probs.keys())
    out = {}
    all_engines = set()
    for k in keys: all_engines.update(REGIME_W.get(k, {}).keys())
    for e in all_engines:
        v = 0.0
        for k in keys:
            v += regime_probs[k] * REGIME_W.get(k, {}).get(e, 0.0)
        out[e] = v
    s = sum(out.values()) or 1.0
    return {k: v/s for k, v in out.items()}

def diversity_boost(probs, base_weights, strength=DIV_STRENGTH):
    if not probs: return base_weights
    vals = list(probs.values())
    consensus = sum(vals) / len(vals)
    out = {}
    for e, p in probs.items():
        div = abs(p - consensus)
        bonus = 1.0 + strength * div
        out[e] = base_weights.get(e, 0.0) * bonus
    s = sum(out.values()) or 1.0
    return {k: v/s for k, v in out.items()}

# ==================== STATE ====================
ENGINES = ["bayes_pattern","markov_bic","kalman_trend","acf_engine","spectral",
           "number_seq","hot_number","streak_break","hot_cold",
           "gambler_instinct","jack_pressure","number_flow","trend_fatigue","bayes_freq"]

def _def_eng():
    return {"alpha":1.0,"beta":1.0,"hits":0,"misses":0,"total":0,
            "recent_hits":0,"recent_total":0,"gradient_weight":1.0}

STATE_FILE = "quantum_v31_state.json"
STATE = {
    "engine_stats":     {e: _def_eng() for e in ENGINES},
    "number_memory":    {}, "pattern_accuracy":  {}, "pattern_recent": {},
    "pattern_blacklist":[], "pattern_losses":     {},
    "total_wins":0, "total_losses":0,
    "current_level":1,  "current_loss_streak":0,  "max_b2b_loss":0,
    "bot_recent_form":  [], "prediction_memory":  {},
    "last_processed_issue":0, "cooldown_until":0,
    "level4_hits":0, "level4_losses":0,
    "obs_state": {}, "ms_state": {},
    "recent_pred_dir": [], "recent_actual_dir": [],
}

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: s = json.load(f)
            for k in STATE:
                if k not in s: s[k] = STATE[k]
            for e in ENGINES:
                if e not in s.get("engine_stats",{}): s["engine_stats"][e] = _def_eng()
            STATE = s
        except Exception as ex: print(f"State load: {ex}")

def save_state():
    try:
        for key, cap in [("prediction_memory",500),("number_memory",5000),
                         ("pattern_accuracy",8000),("pattern_recent",8000),
                         ("pattern_blacklist",500),("pattern_losses",3000),
                         ("bot_recent_form",20),("recent_pred_dir",40),
                         ("recent_actual_dir",40)]:
            if len(STATE.get(key,{})) > cap:
                if isinstance(STATE[key],list): STATE[key] = STATE[key][-cap:]
                else:
                    for k in sorted(STATE[key].keys())[:-cap]: del STATE[key][k]
        try: STATE["obs_state"] = OBS.to_dict()
        except: pass
        try: STATE["ms_state"] = MS.to_dict()
        except: pass
        with open(STATE_FILE,"w") as f: json.dump(STATE, f)
    except Exception as e: print(f"Save: {e}")

load_state()

OBS = OnlineBayesianStacking(ENGINES, eta=OBS_ETA, l2=OBS_L2)
MS  = MultiScaleTracker(ENGINES)
OBS.load_dict(STATE.get("obs_state", {}))
MS.load_dict(STATE.get("ms_state", {}))

# ==================== HELPERS ====================
def make_sig(arr, L):
    if len(arr) < L: return None
    return "".join("B" if x else "S" for x in arr[-L:])

def validate(raw):
    if not raw or not isinstance(raw, list): return None
    out, seen = [], set()
    for item in reversed(raw):
        try:
            iss = int(item.get("issueNumber",0))
            if iss == 0 or iss in seen: continue
            sz_raw = str(item.get("size","")).upper()
            if sz_raw not in ["BIG","BIGGG","SMALL"]: continue
            sz = 1 if sz_raw in ["BIG","BIGGG"] else 0
            num_raw = item.get("number",None)
            num = -1 if (num_raw is None or num_raw=="") else int(float(num_raw))
            out.append({"issue":iss,"size":sz,"number":num})
            seen.add(iss)
        except: continue
    return out

def detect_regime(arr):
    if len(arr) < 20: return "BALANCED"
    w = arr[-min(40, len(arr)):]
    h = _hurst(w)
    alt = sum(1 for i in range(1,len(w)) if w[i] != w[i-1])/(len(w)-1)
    br  = sum(w)/len(w)
    if   h > 0.58 and alt < 0.44: return "TRENDING"
    elif h < 0.42 and alt > 0.62: return "ALTERNATING"
    elif br >= 0.68:               return "BIG_HEAVY"
    elif br <= 0.32:               return "SMALL_HEAVY"
    elif all(x == w[0] for x in w): return "LONG_STREAK"
    else:                           return "BALANCED"

def entropy(arr, w=40):
    r = arr[-w:]
    if len(r) < 15: return 1.0
    p = sum(r)/len(r)
    if p in (0,1): return 0.0
    return max(0.0, 1.0 - (-(p*math.log2(p)+(1-p)*math.log2(1-p))))

def contrarian_flip(pred_dir):
    preds = STATE.get("recent_pred_dir", [])
    actuals = STATE.get("recent_actual_dir", [])
    n = min(len(preds), len(actuals))
    if n < CONTRARIAN_MIN_N:
        return pred_dir, False
    window = 20
    p_recent = preds[-window:] if len(preds) >= window else preds
    a_recent = actuals[-window:] if len(actuals) >= window else actuals
    m = min(len(p_recent), len(a_recent))
    if m < CONTRARIAN_MIN_N:
        return pred_dir, False
    correct = sum(1 for i in range(m) if p_recent[i] == a_recent[i])
    acc = correct / m
    if acc < CONTRARIAN_THRESH:
        print(f"⚠️ CONTRARIAN MODE: recent acc={acc:.2f}, flipping")
        return 1 - pred_dir, True
    return pred_dir, False

# ==================== PATTERN MEMORY ====================
def _get_bpm(sig):
    pa = STATE.get("pattern_accuracy",{}).get(sig)
    if not pa: return 1.0, 1.0
    wb = pa.get("weighted_big", float(pa.get("big",0)))
    ws = pa.get("weighted_small", float(pa.get("small",0)))
    return wb+1.0, ws+1.0

def update_pattern_accuracy(arr_before, actual):
    for L in SHORT_DISPLAY_LENS:
        if len(arr_before) < L: continue
        sig = make_sig(arr_before, L)
        if not sig: continue
        pa = STATE.setdefault("pattern_accuracy",{}).setdefault(sig,
             {"big":0,"small":0,"weighted_big":0.0,"weighted_small":0.0})
        if actual == 1:
            pa["big"] += 1
            pa["weighted_big"] = pa.get("weighted_big",0.0) + 1.0
        else:
            pa["small"] += 1
            pa["weighted_small"] = pa.get("weighted_small",0.0) + 1.0
        pa["weighted_big"]   = pa.get("weighted_big",0.0) * 0.97
        pa["weighted_small"] = pa.get("weighted_small",0.0) * 0.97
        pr = STATE.setdefault("pattern_recent",{}).setdefault(sig,[])
        pr.append(actual)
        if len(pr) > 20: STATE["pattern_recent"][sig] = pr[-20:]

def _bayesian_prob(sig):
    alpha, beta = _get_bpm(sig)
    mean = alpha/(alpha+beta)
    var  = alpha*beta/((alpha+beta)**2*(alpha+beta+1))
    return mean, var, alpha+beta-2

def _apply_rules(p_raw, n):
    if n < PATTERN_MIN:           return p_raw, False, "FLAT"
    if p_raw >= 0.67 and n >= 25: return p_raw, False, "TOP"
    if p_raw >= 0.59:             return p_raw, False, "STRONG"
    if p_raw < 0.45 and n >= FLIP_MIN: return 1.0-p_raw, True, "FLIP"
    return p_raw, False, "NORMAL"

def _best_pattern(arr, for_display=False):
    best, best_score = None, -1
    min_n = 3 if for_display else PATTERN_MIN
    for L in (SHORT_DISPLAY_LENS if for_display else SHORT_LENGTHS):
        if len(arr) < L: continue
        sig = make_sig(arr, L)
        if not sig: continue
        if sig in STATE.get("pattern_blacklist",[]): continue
        p_bay, p_var, n = _bayesian_prob(sig)
        if n < min_n: continue
        p, flipped, tag = _apply_rules(p_bay, n)
        std = math.sqrt(max(p_var, 1e-9))
        dev = abs(p-0.5)
        len_w = 1.0 if L <= 6 else (0.9 if L == 7 else 0.75)
        score = (dev/max(std,0.1))*0.6 + min(1.0,n/15)*0.3 + len_w*0.1
        if tag == "TOP":    score += 1.2
        elif tag == "STRONG":score += 0.4
        elif tag == "FLIP": score += 0.2
        if score > best_score:
            pr_raw = STATE.get("pattern_recent",{}).get(sig,[])
            rn = len(pr_raw[-10:])
            p_rec = (sum(pr_raw[-10:])+1)/(rn+2) if rn >= 3 else p_bay
            best_score = score
            best = (sig, L, p, n, p_rec, rn, flipped, tag, p_bay, std)
    return best

# ==================== ENGINES ====================
def eng_bayes_pattern(arr):
    if len(arr) < 15: return 0.5
    bp = _best_pattern(arr, for_display=False)
    if not bp: return 0.5
    sig, L, p_hist, n, p_rec, rn, flipped, tag, p_bay, std = bp
    if tag == "FLAT": return 0.5
    p = p_hist*0.6 + p_rec*0.4 if rn >= 5 else p_hist
    dev = abs(p-0.5)
    if dev < 0.03: return 0.5
    ci_factor = min(1.5, 1.0/(max(std,0.05)*4))
    boost = {"TOP":1.4,"STRONG":1.15,"FLIP":1.15}.get(tag,1.0)
    amp = 0.5 + (p-0.5)*ci_factor*boost
    return max(0.05, min(0.95, amp))

def eng_markov_bic(arr):
    if len(arr) < 15: return 0.5
    n = len(arr)
    best_o = _bic_order(arr)
    results = []
    for o in range(1, min(best_o+3, 6)):
        if n < o+8: continue
        tr = defaultdict(lambda: {0:0.0, 1:0.0})
        for i in range(o, n):
            state = tuple(arr[i-o:i])
            age   = n-1-i
            w     = math.exp(-age*0.022)
            tr[state][arr[i]] += w
        cur = tuple(arr[-o:])
        if cur not in tr: continue
        c = tr[cur]; tot = c[0]+c[1]
        if tot < 0.5: continue
        prob = c[1]/tot; bias = abs(prob-0.5)
        if bias < 0.04: continue
        ev  = min(math.log(tot+1)/2.5, 1.0)
        ow  = math.exp(-abs(o-best_o)*0.25)*(1+min(tot,10)*0.04)
        conf = min(bias*5.2+ev*0.28+(0.10 if o==best_o else 0), 0.92)
        results.append((prob, conf, ow))
    if not results: return 0.5
    tw   = sum(ow for _,_,ow in results)
    prob = sum(p*ow for p,_,ow in results)/tw
    if abs(prob-0.5) < 0.04: return 0.5
    return max(0.05, min(0.95, prob))

def eng_kalman_trend(arr):
    if len(arr) < 15: return 0.5
    x, P = 0.5, 0.25
    Q, R = 0.004, 0.25
    for v in arr[-min(60,len(arr)):]:
        P += Q
        K  = P/(P+R)
        x += K*(v-x)
        P  = (1-K)*P
    kalman_bias = x-0.5
    def ema(a, span):
        k = 2/(span+1); e = a[0]
        for v in a[1:]: e = k*v+(1-k)*e
        return e
    w = arr[-min(30,len(arr)):]
    e3  = ema(w,  3); e8  = ema(w,  8)
    e13 = ema(w, 13); e21 = ema(w, 21)
    macd  = (e3-e13)+(e8-e21)
    macd_sig = math.tanh(macd*4.0)*0.5
    if len(arr) >= 10:
        l5 = sum(arr[-5:])/5; p5 = sum(arr[-10:-5])/5
        vel = (l5-p5)*1.5
    else: vel = 0.0
    rec = arr[-5:]; burst = 0.0
    if sum(rec) == 5: burst = 0.5
    elif sum(rec) == 0: burst = -0.5
    combined = kalman_bias*0.40 + macd_sig*0.30 + vel*0.20 + burst*0.10
    return max(0.10, min(0.90, 0.5+0.5*math.tanh(combined*2.0)))

def eng_acf(arr):
    if len(arr) < 25: return 0.5
    w = arr[-min(100,len(arr)):]
    sigs = []
    for lag in range(1, 7):
        ac = _acf(w, lag)
        if abs(ac) < 0.09: continue
        last_k = w[-lag]
        prob = (0.5+abs(ac)*0.45 if last_k else 0.5-abs(ac)*0.45) if ac > 0 \
               else (0.5-abs(ac)*0.45 if last_k else 0.5+abs(ac)*0.45)
        lag_w = math.exp(-lag*0.28)
        conf  = min(abs(ac)*3.2*lag_w, 0.85)
        if abs(prob-0.5) > 0.04:
            sigs.append((prob, conf, abs(ac)*lag_w))
    if not sigs: return 0.5
    tw   = sum(ow for _,_,ow in sigs)
    prob = sum(p*ow for p,_,ow in sigs)/tw
    return max(0.05, min(0.95, prob)) if abs(prob-0.5) > 0.04 else 0.5

def eng_spectral(arr):
    if len(arr) < 32: return 0.5
    w = arr[-min(128,len(arr)):]
    res = _dft(w)
    prob = 0.5
    if res: prob = res['prob']
    for period in [8, 16, 32]:
        if len(w) < period*2: continue
        recent = w[-period*2:]
        older  = recent[:period]
        newer  = recent[period:]
        matches = sum(1 for a,b in zip(older,newer) if a==b)/period
        if matches > 0.72:
            prob = (prob + float(newer[0]))/2
    if abs(prob-0.5) < 0.05: return 0.5
    return max(0.05, min(0.95, prob))

def eng_number_seq(history):
    nums = [int(h["number"]) for h in history if h["number"] >= 0]
    szs  = [h["size"]        for h in history if h["number"] >= 0]
    if len(nums) < 20: return 0.5
    ln = nums[-3:]
    if len(ln) < 3: return 0.5
    if ln[0]<ln[1]<ln[2]: pat="ASC"
    elif ln[0]>ln[1]>ln[2]: pat="DESC"
    elif ln[0]==ln[1]==ln[2]: pat="SAME3"
    elif ln[1]==ln[2]: pat="SAME2"
    elif abs(ln[0]-ln[1])==1 and abs(ln[1]-ln[2])==1: pat="SEQ"
    else: pat="OTHER"
    b=s=0.0
    for i in range(len(nums)-3):
        sq=nums[i:i+3]; m=False
        if pat=="ASC" and sq[0]<sq[1]<sq[2]: m=True
        elif pat=="DESC" and sq[0]>sq[1]>sq[2]: m=True
        elif pat=="SAME3" and sq[0]==sq[1]==sq[2]: m=True
        elif pat=="SAME2" and sq[1]==sq[2]: m=True
        elif pat=="SEQ" and abs(sq[0]-sq[1])==1 and abs(sq[1]-sq[2])==1: m=True
        if m and i+3<len(szs):
            w=math.exp((i/len(nums))*3.0)
            if szs[i+3]==1: b+=w
            else: s+=w
    t=b+s
    return (b+0.5)/(t+1.0) if t >= 1.0 else 0.5

def eng_hot_number(history):
    nums=[int(h["number"]) for h in history if h["number"]>=0]
    if len(nums)<20: return 0.5
    freq=Counter(nums[-20:]); top=freq.most_common(3)
    if not top: return 0.5
    bw=sum(c for n,c in top if n>=5); sw=sum(c for n,c in top if n<5)
    mn,mc=top[0]; dom=mc/len(nums[-20:])
    base=bw/(bw+sw) if (bw+sw)>0 else 0.5
    if dom>=0.30: base=max(0.1,min(0.9,base+(0.15 if mn>=5 else -0.15)))
    return base

def eng_streak_break(arr):
    if len(arr) < 6: return 0.5
    last_v = arr[-1]; cs = 0
    for v in reversed(arr):
        if v == last_v: cs += 1
        else: break
    if cs < 3: return 0.5
    fn = len(arr); cont = rev = 0
    for i in range(1, fn-cs):
        if all(arr[i+j]==arr[i] for j in range(cs)):
            if i+cs < fn:
                if arr[i+cs]==arr[i]: cont+=1
                else: rev+=1
    tot = cont+rev
    if tot >= 4:
        cr = cont/tot; bias = abs(cr-0.5)
        if bias > 0.10:
            prob = (0.5+bias*0.7) if (last_v and cr>0.5) or (not last_v and cr<0.5) \
                   else (0.5-bias*0.7)
            return max(0.10, min(0.90, prob))
    if cs>=6: return 0.15 if last_v else 0.85
    elif cs==5: return 0.22 if last_v else 0.78
    elif cs==4: return 0.32 if last_v else 0.68
    elif cs==3: return 0.42 if last_v else 0.58
    return 0.5

def eng_hot_cold(arr):
    form = STATE.get("bot_recent_form",[])
    if len(form)<3 or len(arr)<3: return 0.5
    mo = sum(arr[-3:])/3.0
    hs=0
    for r in reversed(form):
        if r==1: hs+=1
        else: break
    cs=0
    for r in reversed(form):
        if r==0: cs+=1
        else: break
    if hs>=3:   p=0.5+(mo-0.5)*1.5
    elif cs>=3: p=0.5-(mo-0.5)*1.0
    else:       p=0.5+(mo-0.5)*0.5
    return max(0.15, min(0.85, p))

def eng_gambler(history):
    nums=[int(h["number"]) for h in history if h["number"]>=0]
    szs =[h["size"]        for h in history if h["number"]>=0]
    if len(nums)<20 or len(szs)<20: return 0.5
    sigs=[]; wts=[]
    psj=20
    for i in range(len(nums)-1,-1,-1):
        if nums[i] in (0,5): psj=len(nums)-1-i; break
    if psj>=8:
        jp=min(1.0,(psj-5)/10.0)
        rj=[n for n in nums[-30:] if n in (0,5)]
        if rj:
            lj=rj[-1]
            jb=0.5+(0.15*jp if lj==0 else -0.15*jp)
            sigs.append(jb); wts.append(0.6)
    rs=szs[-15:]
    alt=sum(1 for i in range(len(rs)-1) if rs[i]!=rs[i+1])
    cr=alt/(len(rs)-1) if len(rs)>1 else 0
    if cr>0.75:
        np_=1-rs[-1]; sigs.append(0.5+(0.10 if np_ else -0.10)); wts.append(0.4)
    tw_=STATE.get("total_wins",0); tl_=STATE.get("total_losses",0)
    if tw_+tl_>=10 and tw_/(tw_+tl_)<0.45:
        last=szs[-1]; ctr=1-last
        sigs.append(0.5+(0.08 if ctr else -0.08)); wts.append(0.3)
    if not sigs: return 0.5
    tw=sum(wts)
    return max(0.20, min(0.80, sum(s*w for s,w in zip(sigs,wts))/tw))

def eng_jack_pressure(history):
    nums=[int(h["number"]) for h in history if h["number"]>=0]
    if len(nums)<30: return 0.5
    j0=nums[-30:].count(0); j5=nums[-30:].count(5)
    if (j0+j5)>=4: return 0.5
    for n in reversed(nums):
        if n in (0,5):
            return 0.58 if n==0 else 0.42
    return 0.5

def eng_number_flow(history):
    nums=[int(h["number"]) for h in history if h["number"]>=0]
    if len(nums)<15: return 0.5
    l3=nums[-3:]
    if len(l3)<3: return 0.5
    if l3[0]<l3[1]<l3[2]: return 0.62
    if l3[0]>l3[1]>l3[2]: return 0.38
    if abs(nums[-1]-nums[-2])==1:
        return 0.55 if nums[-1]>nums[-2] else 0.45
    return 0.5

def eng_trend_fatigue(arr):
    if len(arr)<20: return 0.5
    w=arr[-min(40,len(arr)):]
    h=_hurst(w)
    br=sum(w)/len(w)
    if h>0.60 and br>=0.72: return 0.28
    if h>0.60 and br<=0.28: return 0.72
    if br>=0.72: return 0.32
    if br<=0.28: return 0.68
    return 0.5

def eng_bayes_freq(arr):
    if len(arr)<10: return 0.5
    n=len(arr); sigs=[]
    for ws,wt in [(10,0.40),(20,0.28),(40,0.18),(80,0.10),(120,0.04)]:
        if n<ws: continue
        w=arr[-ws:]; k=sum(w)
        a,b=k+1.0,(ws-k)+1.0
        pm=a/(a+b)
        ps=math.sqrt(a*b/((a+b)**2*(a+b+1)))
        bias=abs(pm-0.5)
        if bias<0.07: continue
        conf=min(bias*3.5*(1-min(ps*5,0.9)),0.80)*wt*5
        if conf<0.05: continue
        sigs.append((pm,conf,wt))
    if not sigs: return 0.5
    tw=sum(wt for _,_,wt in sigs)
    prob=sum(p*wt for p,_,wt in sigs)/tw
    return max(0.05,min(0.95,prob)) if abs(prob-0.5)>0.05 else 0.5

# ==================== REGIME WEIGHTS ====================
REGIME_W = {
    "TRENDING":    {"bayes_pattern":0.18,"markov_bic":0.12,"kalman_trend":0.14,"acf_engine":0.12,"spectral":0.06,
                    "number_seq":0.06,"hot_number":0.06,"streak_break":0.06,"hot_cold":0.05,
                    "gambler_instinct":0.04,"jack_pressure":0.04,"number_flow":0.04,"trend_fatigue":0.02,"bayes_freq":0.01},
    "ALTERNATING": {"bayes_pattern":0.20,"markov_bic":0.10,"kalman_trend":0.06,"acf_engine":0.15,"spectral":0.06,
                    "number_seq":0.08,"hot_number":0.06,"streak_break":0.10,"hot_cold":0.05,
                    "gambler_instinct":0.04,"jack_pressure":0.04,"number_flow":0.03,"trend_fatigue":0.02,"bayes_freq":0.01},
    "BIG_HEAVY":   {"bayes_pattern":0.18,"markov_bic":0.12,"kalman_trend":0.10,"acf_engine":0.10,"spectral":0.05,
                    "number_seq":0.07,"hot_number":0.08,"streak_break":0.08,"hot_cold":0.05,
                    "gambler_instinct":0.05,"jack_pressure":0.04,"number_flow":0.04,"trend_fatigue":0.03,"bayes_freq":0.01},
    "SMALL_HEAVY": {"bayes_pattern":0.18,"markov_bic":0.12,"kalman_trend":0.10,"acf_engine":0.10,"spectral":0.05,
                    "number_seq":0.07,"hot_number":0.08,"streak_break":0.08,"hot_cold":0.05,
                    "gambler_instinct":0.05,"jack_pressure":0.04,"number_flow":0.04,"trend_fatigue":0.03,"bayes_freq":0.01},
    "LONG_STREAK": {"bayes_pattern":0.16,"markov_bic":0.10,"kalman_trend":0.08,"acf_engine":0.10,"spectral":0.06,
                    "number_seq":0.06,"hot_number":0.06,"streak_break":0.18,"hot_cold":0.06,
                    "gambler_instinct":0.04,"jack_pressure":0.04,"number_flow":0.03,"trend_fatigue":0.02,"bayes_freq":0.01},
    "BALANCED":    {"bayes_pattern":0.20,"markov_bic":0.12,"kalman_trend":0.10,"acf_engine":0.10,"spectral":0.05,
                    "number_seq":0.08,"hot_number":0.07,"streak_break":0.08,"hot_cold":0.05,
                    "gambler_instinct":0.05,"jack_pressure":0.04,"number_flow":0.04,"trend_fatigue":0.01,"bayes_freq":0.01},
}

def get_weights_hybrid(regime_probs):
    base = blend_regime_weights(regime_probs)
    boosted = {}
    for e, w in base.items():
        st = STATE["engine_stats"].get(e, _def_eng())
        a = st.get("alpha",1.0); b = st.get("beta",1.0)
        pm = a/(a+b)
        ra = st["recent_hits"]/st["recent_total"] if st.get("recent_total",0)>=5 else pm
        comb = pm*0.5 + ra*0.5
        boost = max(0.5, min(2.0, comb/0.5))
        boosted[e] = w * boost
    s = sum(boosted.values()) or 1.0
    boosted = {k: v/s for k,v in boosted.items()}
    return MS.boost_weights(boosted)

def _log_odds_fuse(probs, wts, entropy_gate=1.0):
    lo_sum=0.0; wt_sum=0.0
    for e,p in probs.items():
        if p==0.5: continue
        w = wts.get(e,0)*entropy_gate
        lo_sum += logit(p)*w; wt_sum += w
    return sigmoid(lo_sum/wt_sum) if wt_sum > 1e-9 else 0.5

def grad_update(probs, ab, lr=0.005):
    for e, p in probs.items():
        st = STATE["engine_stats"].setdefault(e, _def_eng())
        g  = (p-ab)*(p-0.5)*2.0
        ow = st.get("gradient_weight",1.0)
        st["gradient_weight"] = max(0.3, min(2.0, ow-lr*g))

# ==================== BLACKLIST ====================
def check_blacklist(arr):
    bp = _best_pattern(arr, for_display=False)
    if not bp: return False
    return bp[0] in STATE.get("pattern_blacklist",[])

def upd_blacklist(sig, won):
    if not sig or sig == "N/A": return
    if won: STATE.setdefault("pattern_losses",{})[sig] = 0
    else:
        pl = STATE.setdefault("pattern_losses",{})
        pl[sig] = pl.get(sig,0)+1
        if pl[sig] >= BLACKLIST_LOSSES:
            bl = STATE.setdefault("pattern_blacklist",[])
            if sig not in bl:
                bl.append(sig); print(f"🚫 BL: {sig}")

# ==================== SIGNAL LABELS ====================
def sig_label(c):
    d=c-0.5
    if d<0.02: return "🟥 NONE"
    if d<0.05: return "🟧 WEAK"
    if d<0.10: return "🟨 MODERATE"
    if d<0.15: return "🟩 STRONG"
    return "🟢 V.STRONG"

def form_label():
    form=STATE.get("bot_recent_form",[])
    if len(form)<3: return "N/A"
    hs=cs=0
    for r in reversed(form):
        if r==1: hs+=1
        else: break
    for r in reversed(form):
        if r==0: cs+=1
        else: break
    r5=form[-5:]; fs=f"{sum(r5)}/{len(r5)}"
    if hs>=3: return f"🔥 HOT ({fs})"
    if cs>=3: return f"❄️ COLD ({fs})"
    return f"😐 NEUTRAL ({fs})"

# ==================== NUMBER PREDICTOR ====================
def predict_number(history, direction):
    nums=[int(h["number"]) for h in history if h["number"]>=0]
    szs =[h["size"]        for h in history if h["number"]>=0]
    if len(nums)<40: return 8 if direction==1 else 2
    cands=[5,6,7,8,9] if direction==1 else [0,1,2,3,4]
    K=len(cands)
    fa=Counter(nums); fr=Counter(nums[-50:])
    tr=len(nums[-50:])

    mk=[{n:0.0 for n in cands} for _ in range(3)]
    for o in range(1,4):
        if len(nums)<o+2: continue
        ls=tuple(nums[-o:]); c,t=Counter(),0
        for i in range(len(nums)-o):
            if tuple(nums[i:i+o])==ls:
                nx=nums[i+o]
                if nx in cands: c[nx]+=1; t+=1
        for n in cands: mk[o-1][n]=(c.get(n,0)+1)/(t+K)

    s2n={0:Counter(),1:Counter()}
    for i in range(1,len(nums)):
        if nums[i] in cands: s2n[szs[i-1]][nums[i]]+=1

    last_seen={}
    for i,n in enumerate(nums):
        if n not in last_seen: last_seen[n]=i
    max_gap=max((last_seen.get(n,len(nums)) for n in cands), default=len(nums))

    ema={i:0.1 for i in range(10)}
    for v in reversed(nums[-80:]):
        for k in range(10): ema[k]=ema[k]*0.88+(0.12 if v==k else 0)
    mx_ema=max(ema[n] for n in cands) or 0.1

    sc={}
    for n in cands:
        s1=(fa.get(n,0)+1)/(len(nums)+K)
        s2=(fr.get(n,0)+1)/(tr+K)
        s6=(s2n[szs[-1]].get(n,0)+1)/(sum(s2n[szs[-1]].values())+K)
        gap_s=(last_seen.get(n,len(nums))/max_gap)*0.25
        cold_s=(1-ema[n]/mx_ema)*0.20
        sc[n]=s1*0.05+s2*0.08+mk[0][n]*0.13+mk[1][n]*0.12+mk[2][n]*0.11+s6*0.26+gap_s+cold_s
    return max(sc, key=sc.get)

# ==================== MAIN PREDICTOR ====================
def predict_next(history):
    arr     = [h["size"] for h in history]
    bl      = check_blacklist(arr)
    regime  = detect_regime(arr)
    regime_p = soft_regime_probs(arr)
    ent     = entropy(arr)
    h_val   = _hurst(arr[-min(40,len(arr)):])
    ce      = _cond_entropy(arr[-40:], order=1)

    probs = {
        "bayes_pattern":   eng_bayes_pattern(arr) if not bl else 0.5,
        "markov_bic":      eng_markov_bic(arr),
        "kalman_trend":    eng_kalman_trend(arr),
        "acf_engine":      eng_acf(arr),
        "spectral":        eng_spectral(arr),
        "number_seq":      eng_number_seq(history),
        "hot_number":      eng_hot_number(history),
        "streak_break":    eng_streak_break(arr),
        "hot_cold":        eng_hot_cold(arr),
        "gambler_instinct":eng_gambler(history),
        "jack_pressure":   eng_jack_pressure(history),
        "number_flow":     eng_number_flow(history),
        "trend_fatigue":   eng_trend_fatigue(arr),
        "bayes_freq":      eng_bayes_freq(arr),
    }

    wts_hybrid = get_weights_hybrid(regime_p)
    wts_div = diversity_boost(probs, wts_hybrid, strength=DIV_STRENGTH)

    obs_w = OBS.weights()
    blend_alpha = min(0.50, OBS.updates / 300.0)
    wts = {}
    for e in ENGINES:
        wts[e] = (1.0 - blend_alpha) * wts_div.get(e, 0.0) + blend_alpha * obs_w.get(e, 0.0)
    s = sum(wts.values()) or 1.0
    wts = {k: v/s for k,v in wts.items()}

    ent_gate = max(0.45, 1.0 - ce*0.45)
    p = _log_odds_fuse(probs, wts, ent_gate)

    direction = 1 if p >= 0.5 else 0

    direction, was_flipped = contrarian_flip(direction)
    if was_flipped:
        p = 1.0 - p

    agree = sum(1 for e,prob in probs.items()
                if (prob>0.52 and p>=0.5) or (prob<0.48 and p<0.5))

    conf  = max(0.50, min(0.92, max(p,1-p)))
    lab   = sig_label(conf)
    ps    = direction

    nums  = [h["number"] for h in history if h["number"]>=0]
    hot   = Counter(nums[-20:]).most_common(1) if len(nums)>=20 else None

    disp = _best_pattern(arr, for_display=True)
    best_sig="N/A"; best_note=""; best_label=""
    if disp:
        sig,L,p_hist,n,p_rec,rn,flipped,tag,p_bay,std=disp
        best_sig=f"{L}-{sig}"
        if tag=="FLAT":   best_label="📚 LEARNING";  best_note=f"Acc:{p_bay*100:.0f}% (n={n}/12)"
        elif tag=="TOP":  best_label="🏆 TOP";        best_note=f"Bay:{p_hist*100:.0f}%(n={n}) R:{p_rec*100:.0f}%(n={rn})"
        elif tag=="STRONG":best_label="⭐ STRONG";    best_note=f"Bay:{p_hist*100:.0f}%(n={n}) R:{p_rec*100:.0f}%(n={rn})"
        elif tag=="FLIP": best_label="🔄 FLIPPED";   best_note=f"Raw:{p_bay*100:.0f}%→Flip:{p_hist*100:.0f}%(n={n})"
        else:             best_label="○ NORMAL";      best_note=f"Bay:{p_hist*100:.0f}%(n={n}) R:{p_rec*100:.0f}%(n={rn})"
    elif bl: best_label="🚫 BLACKLISTED"

    return {"pred_size":ps,"conf":conf,"lab":lab,"probs":probs,"wts":wts,
            "regime":regime,"regime_p":regime_p,"entropy":ent,"hurst":h_val,"cond_ent":ce,
            "best_sig":best_sig,"best_note":best_note,"best_label":best_label,
            "form":form_label(),"hot":hot,"agree":agree,"pat_bl":bl,
            "obs_alpha":blend_alpha,"contrarian":was_flipped}

# ==================== STATS UPDATE ====================
def upd_eng_stats(probs, ab, reg):
    for e,p in probs.items():
        st=STATE["engine_stats"].setdefault(e,_def_eng())
        hit=(p>=0.5)==ab
        if hit: st["alpha"]+=1.0
        else:   st["beta"]+=1.0
        st["total"]=st.get("total",0)+1
        st["recent_total"]=st.get("recent_total",0)+1
        if hit: st["hits"]=st.get("hits",0)+1; st["recent_hits"]=st.get("recent_hits",0)+1
        else:   st["misses"]=st.get("misses",0)+1
        if st["recent_total"]>50:
            st["recent_hits"]=int(st["recent_hits"]*0.80)
            st["recent_total"]=int(st["recent_total"]*0.80)

def upd_global(win, lv):
    if win:
        STATE["total_wins"]=STATE.get("total_wins",0)+1
        STATE["current_loss_streak"]=0; STATE["current_level"]=1
    else:
        STATE["total_losses"]=STATE.get("total_losses",0)+1
        STATE["current_loss_streak"]=STATE.get("current_loss_streak",0)+1
        STATE["max_b2b_loss"]=max(STATE.get("max_b2b_loss",0),STATE["current_loss_streak"])
        if lv==4:
            STATE["level4_losses"]=STATE.get("level4_losses",0)+1
            STATE["cooldown_until"]=time.time()+LEVEL4_COOLDOWN
            STATE["current_level"]=1; STATE["current_loss_streak"]=0
        else:
            STATE["current_level"]=min(STATE.get("current_level",1)+1, MAX_LEVEL)
            if STATE["current_loss_streak"]>=MAX_LOSS_STREAK:
                STATE["cooldown_until"]=time.time()+300
                STATE["current_level"]=1; STATE["current_loss_streak"]=0
    f=STATE.setdefault("bot_recent_form",[])
    f.append(1 if win else 0); STATE["bot_recent_form"]=f[-20:]

# ==================== FORMAT ====================
def fmt_history(history):
    out=""
    for item in history[-8:]:
        sp=str(item["issue"])[-3:]
        ss="BIGGG" if item["size"]==1 else "SMALL"
        nd=str(item["number"]) if item["number"]!=-1 else "?"
        p=STATE.get("prediction_memory",{}).get(str(item["issue"]))
        if p and p["size"]==ss and p.get("number")==item["number"] and item["number"]!=-1:
            icon="  ☠️☠️☠️"
        elif p and p["size"]==ss:
            icon="  ✅✅✅"
        else:
            icon=""
        out+=f"`{sp}` *{ss}* ({nd}){icon}\n"
    return out

def fmt_footer():
    w=STATE.get("total_wins",0); l=STATE.get("total_losses",0)
    mb=STATE.get("max_b2b_loss",0); cs=STATE.get("current_loss_streak",0)
    lv=STATE.get("current_level",1); t=w+l
    wr=(w/t*100) if t>0 else 0.0
    pacc=len(STATE.get("pattern_accuracy",{}))
    pbl=len(STATE.get("pattern_blacklist",[]))
    l4h=STATE.get("level4_hits",0); l4l=STATE.get("level4_losses",0)
    lvl_s=f"{BET_LEVELS[min(lv-1,len(BET_LEVELS)-1)]}X"
    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *LIFETIME*\n"
            f"✅ *W:* `{w}` | ❌ *L:* `{l}`\n"
            f"📉 *Max B2B:* `{mb}` | 🔥 *Streak:* `{cs}`\n"
            f"🎯 *WR:* `{wr:.1f}%`\n"
            f"💰 *Level:* `{lv}` ({lvl_s})\n"
            f"🧬 *Sig-DB:* `{pacc}` | 🚫 *BL:* `{pbl}`\n"
            f"⚡ *L4:* `{l4h}W/{l4l}L`")

# ==================== TELEGRAM ====================
async def tg_send(session, msg):
    try:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                print(f"❌ TG ERR: {await r.text()}")
            else:
                print("✅ TG SENT OK")
    except Exception as e: 
        print(f"❌ TG EX: {e}")

async def tg_sticker(session):
    try:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker",
                                json={"chat_id":CHAT_ID,"sticker":WIN_STICKER_ID},
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            await r.text()
    except Exception as e: print(f"Sticker: {e}")

# ==================== API ====================
async def fetch_data(session):
    for att in range(1,4):
        try:
            async with session.get(API_URL,headers={"User-Agent":"Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status==200:
                    d=await r.json()
                    if isinstance(d,dict):
                        if "data" in d and isinstance(d["data"],dict) and "list" in d["data"]: return d["data"]["list"]
                        if "list" in d: return d["list"]
                    elif isinstance(d,list): return d
        except Exception as e: print(f"API {att}: {e}")
        await asyncio.sleep(2**att)
    return None

# ==================== BOT LOOP ====================
class Bot:
    def __init__(self): self.pending=None

    async def run(self, session):
        print("🚀 QUANTUM V31 STARTED (Fixed OBS + Contrarian + Recent-Window)")
        while True:
            try: await self.step(session)
            except Exception as e: print(f"Loop: {e}"); import traceback; traceback.print_exc()
            await asyncio.sleep(5)

    async def step(self, session):
        if time.time()<STATE.get("cooldown_until",0): await asyncio.sleep(5); return
        raw=await fetch_data(session)
        if not raw: return
        history=validate(raw)
        if not history or len(history)<30: return
        last=history[-1]; li=last["issue"]
        if li==STATE.get("last_processed_issue",0): return
        arr=[h["size"] for h in history]
        regime=detect_regime(arr)

        if self.pending and self.pending["next_issue"]==li:
            ab=last["size"]==1
            actual_s="BIGGG" if ab else "SMALL"
            win=actual_s==self.pending["pred_size"]
            lv=STATE.get("current_level",1)
            if lv==4 and win: STATE["level4_hits"]=STATE.get("level4_hits",0)+1

            pred_dir = 1 if self.pending["pred_size"]=="BIGGG" else 0
            STATE.setdefault("recent_pred_dir",[]).append(pred_dir)
            STATE.setdefault("recent_actual_dir",[]).append(1 if ab else 0)
            STATE["recent_pred_dir"] = STATE["recent_pred_dir"][-40:]
            STATE["recent_actual_dir"] = STATE["recent_actual_dir"][-40:]

            upd_eng_stats(self.pending["probs"],ab,regime)
            grad_update(self.pending["probs"],ab)
            MS.update(self.pending["probs"], ab)
            OBS.update(self.pending["probs"], 1.0 if ab else 0.0)
            upd_global(win,lv)

            if len(arr)>=2: update_pattern_accuracy(arr[:-1],ab)
            if self.pending.get("best_sig") and self.pending["best_sig"]!="N/A":
                sig_part = self.pending["best_sig"].split("-",1)[-1]
                if sig_part and sig_part != "N/A":
                    upd_blacklist(sig_part, win)
            if win: asyncio.create_task(tg_sticker(session))
            self.pending=None

        if not self.pending or self.pending["last_issue"]!=li:
            pred=predict_next(history)
            ni=li+1
            pn=predict_number(history,pred["pred_size"])
            self.pending={"last_issue":li,"next_issue":ni,
                          "pred_size":"BIGGG" if pred["pred_size"]==1 else "SMALL",
                          "pred_number":pn,"probs":pred["probs"],"best_sig":pred["best_sig"]}
            STATE.setdefault("prediction_memory",{})[str(ni)]={"size":self.pending["pred_size"],"number":pn}

            hb=fmt_history(history)
            ep=pred["probs"]
            lv=STATE.get("current_level",1)
            fund=f"{BET_LEVELS[min(lv-1,len(BET_LEVELS)-1)]}X"

            cons1=f"BAY:{ep['bayes_pattern']:.2f} MKV:{ep['markov_bic']:.2f} KLM:{ep['kalman_trend']:.2f} ACF:{ep['acf_engine']:.2f}"
            cons2=f"SPC:{ep['spectral']:.2f} NSQ:{ep['number_seq']:.2f} HOT:{ep['hot_number']:.2f} BRK:{ep['streak_break']:.2f}"
            cons3=f"HCD:{ep['hot_cold']:.2f} GMB:{ep['gambler_instinct']:.2f} JCK:{ep['jack_pressure']:.2f} FTG:{ep['trend_fatigue']:.2f}"
            cons4=f"FLW:{ep['number_flow']:.2f} BYF:{ep['bayes_freq']:.2f}"

            tw_sorted = sorted(pred["wts"].items(), key=lambda kv: -kv[1])[:4]
            top_w_str = " | ".join(f"`{e[:4]}:{w:.2f}`" for e,w in tw_sorted)
            obs_a = pred["obs_alpha"]

            pat_display=""
            if pred["best_label"]:
                pat_display=f"\n🎯 *Best Sig:* `{pred['best_sig']}`\n"
                pat_display+=f"💪 *Pattern:* `{pred['best_label']}`"
                if pred["best_note"]: pat_display+=f"\n   `{pred['best_note']}`"

            hot=f"\n🔥 *Hot:* `{pred['hot'][0][0]}` ({pred['hot'][0][1]}x)" if pred["hot"] else ""

            rp = pred["regime_p"]
            rp_top = sorted(rp.items(), key=lambda kv:-kv[1])[:2]
            rp_str = " ".join(f"{k[:4]}:{v:.2f}" for k,v in rp_top)

            contrarian_flag = " ⚠️FLIP" if pred["contrarian"] else ""

            msg=(f"🎯 *QUANTUM V31 — FIXED* 🎯\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"📌 *Period:* `{ni}`\n"
                 f"🎲 *Number:* `{pn}`\n"
                 f"🔥 *Target:* *{'BIGGG 🟢' if pred['pred_size']==1 else 'SMALL 🔴'}*{contrarian_flag}\n"
                 f"📊 *Conf:* `{pred['conf']*100:.1f}%` | {pred['lab']}\n"
                 f"🎰 *Form:* {pred['form']}\n"
                 f"🎯 *Agree:* `{pred['agree']}/14` | ⚙️ *OBS:* `{obs_a*100:.0f}%`\n"
                 f"📈 *Regime:* `{pred['regime']}` | `{rp_str}`\n"
                 f"📉 *Hurst:* `{pred['hurst']:.3f}` | *CE:* `{pred['cond_ent']:.3f}`\n"
                 f"💰 *Fund:* `{fund}` (Level {lv})"
                 f"{pat_display}"
                 f"{hot}\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"🧠 *14-Engine:*\n`{cons1}`\n`{cons2}`\n`{cons3}`\n`{cons4}`\n"
                 f"⚖️ *Top Adaptive:* `{top_w_str}`\n"
                 f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 f"📜 *TREND (8):*\n{hb}"
                 f"{fmt_footer()}")
            
            print(f"[{time.strftime('%H:%M:%S')}] PREDICTION for {ni}: {self.pending['pred_size']} (Conf: {pred['conf']*100:.1f}%)")
            asyncio.create_task(tg_send(session, msg))

        STATE["last_processed_issue"]=li
        save_state()

# ==================== WARMUP ====================
async def warmup(session):
    print("🔄 Warmup V31 starting...")
    raw = await fetch_data(session)
    if not raw: 
        print("⚠️ Warmup failed: No API data")
        return
    history = validate(raw)
    if not history or len(history) < 50: 
        print(f"⚠️ Warmup failed: history length {len(history) if history else 0}")
        return

    arr = [h["size"] for h in history]
    for i in range(5, len(arr)):
        update_pattern_accuracy(arr[:i], arr[i])
        if i % 20 == 0:
            await asyncio.sleep(0.01)

    # Only warmup on last 150 points to save time
    start_idx = max(40, len(history) - 150)
    print(f"⚙️ Warming up from index {start_idx} to {len(history)-1}...")

    for i in range(start_idx, len(history)-1):
        part = history[:i]
        a = [h["size"] for h in part]
        try:
            pr = predict_next(part)
            ab = history[i]["size"] == 1
            upd_eng_stats(pr["probs"], ab, detect_regime(a))
            grad_update(pr["probs"], ab)
            MS.update(pr["probs"], ab)
            OBS.update(pr["probs"], 1.0 if ab else 0.0)
        except Exception as ex:
            print(f"Warmup step {i}: {ex}")
            continue
        if i % 5 == 0:
            await asyncio.sleep(0.01)

    save_state()
    print(f"✅ Warmup done. Sig-DB:{len(STATE.get('pattern_accuracy',{}))} OBS-updates:{OBS.updates}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="QUANTUM V31 ACTIVE",status=200)

async def main():
    app=web.Application(); app.router.add_get("/",health)
    runner=web.AppRunner(app); await runner.setup()
    port=int(os.environ.get("PORT",10000))
    await web.TCPSite(runner,"0.0.0.0",port).start()
    print(f"✅ Port {port}")
    
    async with aiohttp.ClientSession() as session:
        # Send startup confirmation
        await tg_send(session, "🤖 *QUANTUM V31* Booting up...\nWarmup starting (takes ~30-60s)")
        await warmup(session)
        await tg_send(session, "✅ *QUANTUM V31* Online! Waiting for next prediction...")
        await Bot().run(session)

if __name__=="__main__":
    asyncio.run(main())

