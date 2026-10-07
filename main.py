import json
import time
import math
import os
import asyncio
from collections import defaultdict
import aiohttp
from aiohttp import web

# ==================== CONFIG ====================
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")

# 🔥 SAFE LEVELS
BET_LEVELS = [1.0, 2.5]
MAX_LEVEL = 2
FLIP_BLACKLIST_MIN = 3
FLIP_BLACKLIST_MAX_WR = 0.35

# ==================== STATE ====================
def _default_eng():
    return {"alpha": 1.0, "beta": 1.0, "hits": 0, "misses": 0, "total": 0,
            "recent_hits": 0, "recent_total": 0, "gradient_weight": 1.0}

ENGINES = ["pattern", "trend", "number_seq", "hot_number", "streak_break",
           "rhythm", "hot_cold", "gambler_instinct"]

STATE_FILE = "engine_state_v28_5_1.json"
STATE = {
    "engine_stats": {e: _default_eng() for e in ENGINES},
    "pattern_memory": {}, "number_memory": {}, "pattern_strength": {},
    "flip_stats": {}, "flip_blacklist": [], "total_wins": 0, "total_losses": 0,
    "current_level": 1, "current_loss_streak": 0, "max_b2b_loss": 0,
    "pattern_flips": 0, "pattern_flips_won": 0, "bot_recent_form": [],
    "prediction_memory": {}, "last_processed_issue": 0, "calibration_offset": 0.0,
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
        except Exception as ex:
            print(f"State load err: {ex}")

def save_state():
    try:
        for key, cap in [("prediction_memory", 500), ("pattern_memory", 5000),
                         ("number_memory", 5000), ("pattern_strength", 5000),
                         ("flip_stats", 2000), ("flip_blacklist", 500),
                         ("bot_recent_form", 20)]:
            if len(STATE.get(key, {})) > cap:
                if isinstance(STATE[key], list):
                    STATE[key] = STATE[key][-cap:]
                else:
                    for k in sorted(STATE[key].keys())[:-cap]: del STATE[key][k]
        with open(STATE_FILE, "w") as f: json.dump(STATE, f)
    except Exception as e: print(f"Save err: {e}")

load_state()

# ==================== HELPERS ====================
def pattern_signature(arr, n=14):
    if len(arr) < n: return None
    return "".join("B" if x else "S" for x in arr[-n:])

def short_sig(arr, n=6):
    if len(arr) < n: return None
    return "".join("B" if x else "S" for x in arr[-n:])

def num_signature(history, n=5):
    nums = [int(h["number"]) for h in history if str(h.get("number", "")).isdigit()]
    if len(nums) < n: return None
    return "-".join(str(x) for x in nums[-n:])

def hamming(a, b):
    if not a or not b or len(a) != len(b): return 999
    return sum(1 for x, y in zip(a, b) if x != y)

# ==================== DATA VALIDATION ====================
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
    big_rate = sum(r) / len(r)
    if alt >= 15: return "ALTERNATING"
    if big_rate >= 0.70: return "BIG_HEAVY"
    if big_rate <= 0.30: return "SMALL_HEAVY"
    if all(x == r[0] for x in r): return "LONG_STREAK"
    return "BALANCED"

def entropy(arr, w=40):
    r = arr[-w:]
    if len(r) < 15: return 1.0
    p1 = sum(r) / len(r)
    if p1 in (0, 1): return 0.0
    return max(0.0, 1.0 - (-(p1*math.log2(p1) + (1-p1)*math.log2(1-p1))))

# ==================== ENGINES ====================
def eng_pattern(arr):
    n = len(arr)
    if n < 15: return 0.5
    sig = "".join("B" if x else "S" for x in arr)
    res, wts = [], []
    for L in [4, 6, 8, 10, 12, 14]:
        if n < L + 3: continue
        tail = sig[-L:]
        b = s = 0
        for i in range(n - L):
            if sig[i:i+L] == tail:
                if arr[i+L] == 1: b += 1
                else: s += 1
        tot = b + s
        if tot >= 3:
            p = (b + 1) / (tot + 2)
            if abs(p - 0.5) < 0.10: continue
            w = (L ** 1.5) * math.log(tot + 1) * min(2.0, tot / 3.0)
            res.append(p); wts.append(w)
    p14 = pattern_signature(arr, 14)
    if p14 and p14 in STATE["pattern_memory"]:
        pm = STATE["pattern_memory"][p14]
        pb, ps = pm.get("next_big", 0), pm.get("next_small", 0)
        if pb + ps >= 3:
            p = (pb + 1) / (pb + ps + 2)
            if abs(p - 0.5) >= 0.10:
                res.append(p); wts.append(14 ** 1.5 * math.log(pb + ps + 1))
    ss = short_sig(arr, 6)
    if ss and ss in STATE["pattern_strength"]:
        st = STATE["pattern_strength"][ss]
        h, m = st.get("hits", 0), st.get("misses", 0)
        t = h + m
        if t >= 3:
            acc = (h + 1) / (t + 2)
            fq = min(1.0, t / 15.0)
            acf = max(0.0, (acc - 0.50) * 3.0)
            strength = fq * acf
            if strength > 0.05:
                boost = 1.0 + strength * 3.0
                amp = max(0.05, min(0.95, 0.5 + (acc - 0.5) * (1.0 + strength)))
                res.append(amp); wts.append(6 ** 1.7 * boost * math.log(t + 1))
    if not res: return 0.5
    return sum(p*w for p, w in zip(res, wts)) / sum(wts)

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
    from collections import Counter
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

# ==================== WEIGHTS ====================
REGIME_W = {
    "ALTERNATING": {"pattern": 0.22, "trend": 0.09, "number_seq": 0.14, "hot_number": 0.10, "streak_break": 0.17, "rhythm": 0.13, "hot_cold": 0.08, "gambler_instinct": 0.07},
    "BIG_HEAVY":   {"pattern": 0.19, "trend": 0.17, "number_seq": 0.13, "hot_number": 0.11, "streak_break": 0.15, "rhythm": 0.10, "hot_cold": 0.08, "gambler_instinct": 0.07},
    "SMALL_HEAVY": {"pattern": 0.19, "trend": 0.17, "number_seq": 0.13, "hot_number": 0.11, "streak_break": 0.15, "rhythm": 0.10, "hot_cold": 0.08, "gambler_instinct": 0.07},
    "LONG_STREAK": {"pattern": 0.16, "trend": 0.13, "number_seq": 0.10, "hot_number": 0.09, "streak_break": 0.26, "rhythm": 0.10, "hot_cold": 0.09, "gambler_instinct": 0.07},
    "BALANCED":    {"pattern": 0.22, "trend": 0.12, "number_seq": 0.13, "hot_number": 0.11, "streak_break": 0.15, "rhythm": 0.11, "hot_cold": 0.09, "gambler_instinct": 0.07},
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

# ==================== FLIP BLACKLIST ====================
def check_pat_flip(arr, p):
    sig = short_sig(arr, 6)
    if not sig: return p, False, ""
    if sig in STATE.get("flip_blacklist", []):
        return p, False, f"BLACKLISTED({sig})"
    st = STATE["pattern_strength"].get(sig)
    if not st: return p, False, ""
    h, m = st.get("hits", 0), st.get("misses", 0)
    t = h + m
    if t < 5: return p, False, ""
    acc = (h + 1) / (t + 2)
    if acc < 0.58:
        return 1.0 - p, True, f"ACC{acc*100:.0f}%<58%(n={t})"
    return p, False, ""

def update_flip_stat(sig, won):
    if not sig: return
    st = STATE.setdefault("flip_stats", {}).setdefault(sig, {"flips": 0, "wins": 0})
    st["flips"] += 1
    if won: st["wins"] += 1
    if st["flips"] >= FLIP_BLACKLIST_MIN:
        wr = st["wins"] / st["flips"]
        if wr < FLIP_BLACKLIST_MAX_WR:
            bl = STATE.setdefault("flip_blacklist", [])
            if sig not in bl:
                bl.append(sig)
                print(f"🚫 Blacklisted flip for {sig} (WR {wr*100:.0f}%)")

# ==================== ANTI-ERROR ====================
def check_ae(arr, p):
    sig = pattern_signature(arr)
    if not sig: return p, False, ""
    err = STATE.get("error_patterns", {}).get(sig)
    if err and err.get("fail_count", 0) >= 2:
        if err["fail_count"] / max(1, err.get("total", 1)) >= 0.6:
            return 1.0 - p, True, f"AE({err['fail_count']}x)"
    return p, False, ""

# ==================== SIGNAL LABEL ====================
def sig_label(c):
    d = c - 0.50
    if d < 0.02: return "🟥 NONE"
    if d < 0.05: return "🟧 WEAK"
    if d < 0.10: return "🟨 MODERATE"
    if d < 0.15: return "🟩 STRONG"
    return "🟢 V.STRONG"

# ==================== NUMBER PREDICTOR ====================
def predict_number(history, direction):
    from collections import Counter
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
    probs = {
        "pattern": eng_pattern(arr),
        "trend": eng_trend(arr),
        "number_seq": eng_number_seq(history),
        "hot_number": eng_hot_number(history),
        "streak_break": eng_streak_break(arr),
        "rhythm": eng_rhythm(arr),
        "hot_cold": eng_hot_cold(arr),
        "gambler_instinct": eng_gambler(history),
    }
    regime = detect_regime(arr)
    wts = get_weights(regime)
    p = combine(probs, wts)
    pred = entropy(arr)
    p = (0.5 + (p-0.5)*(0.5 + pred*0.5)) if p > 0.5 else (0.5 - (0.5-p)*(0.5 + pred*0.5))
    p, ae_a, ae_r = check_ae(arr, p)
    p, pf_a, pf_r = check_pat_flip(arr, p)
    base = max(p, 1-p)
    conf = max(0.50, min(0.95, base))
    lab = sig_label(conf)
    ps = 1 if p >= 0.5 else 0
    nums = [h["number"] for h in history if h["number"] >= 0]
    from collections import Counter
    hot = Counter(nums[-20:]).most_common(1) if len(nums) >= 20 else None
    p14 = pattern_signature(arr)
    ss = short_sig(arr, 6) or "N/A"
    snote = ""; slab = ""
    if ss != "N/A" and ss in STATE["pattern_strength"]:
        st = STATE["pattern_strength"][ss]
        h, m = st.get("hits", 0), st.get("misses", 0)
        t = h + m
        if t >= 3:
            acc = (h + 1) / (t + 2)
            snote = f"Acc:{acc*100:.0f}%(n={t})"
            acf = max(0.0, (acc - 0.50) * 3.0)
            stg = min(1.0, t/15.0) * acf
            if stg >= 0.6: slab = "🔥 SUPER"
            elif stg >= 0.3: slab = "⭐ STRONG"
            elif stg >= 0.1: slab = "○ WEAK"
            else: slab = "💤 FLAT"
    form = STATE.get("bot_recent_form", [])
    if form:
        r5 = form[-5:]
        fs = f"{sum(r5)}/{len(r5)}"
        hot_s = sum(1 for r in reversed(form) if r == 1)
        cold_s = sum(1 for r in reversed(form) if r == 0)
        if hot_s >= 3: fl = f"🔥 HOT ({fs})"
        elif cold_s >= 3: fl = f"❄️ COLD ({fs})"
        else: fl = f"😐 NEUTRAL ({fs})"
    else: fl = "N/A"
    return {"pred_size": ps, "conf": conf, "lab": lab,
            "probs": probs, "wts": wts, "regime": regime, "entropy": pred,
            "sig": p14, "ss": ss, "snote": snote, "slab": slab,
            "form": fl, "hot": hot, "ae_a": ae_a, "ae_r": ae_r,
            "pf_a": pf_a, "pf_r": pf_r,
            "ae_flipped": ae_a, "pf_flipped": pf_a}

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

def upd_global(win):
    if win:
        STATE["total_wins"] = STATE.get("total_wins", 0) + 1
        STATE["current_loss_streak"] = 0
        STATE["current_level"] = 1
    else:
        STATE["total_losses"] = STATE.get("total_losses", 0) + 1
        STATE["current_loss_streak"] = STATE.get("current_loss_streak", 0) + 1
        STATE["max_b2b_loss"] = max(STATE.get("max_b2b_loss", 0), STATE["current_loss_streak"])
        STATE["current_level"] = min(STATE.get("current_level", 1) + 1, MAX_LEVEL)
    f = STATE.setdefault("bot_recent_form", [])
    f.append(1 if win else 0)
    STATE["bot_recent_form"] = f[-20:]

def upd_pat_mem(sig, ab):
    if not sig: return
    pm = STATE.setdefault("pattern_memory", {}).setdefault(sig, {"next_big": 0, "next_small": 0})
    if ab: pm["next_big"] += 1
    else: pm["next_small"] += 1

def upd_pat_strength(sig, ab, pred_big):
    if not sig: return
    st = STATE.setdefault("pattern_strength", {}).setdefault(sig, {"hits": 0, "misses": 0})
    if ab == pred_big: st["hits"] += 1
    else: st["misses"] += 1

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
    pats = len(STATE.get("pattern_memory", {}))
    errs = len(STATE.get("error_patterns", {}))
    nums_db = len(STATE.get("number_memory", {}))
    strong = len(STATE.get("pattern_strength", {}))
    pflips = STATE.get("pattern_flips", 0)
    pw = STATE.get("pattern_flips_won", 0)
    pa = (pw/pflips*100) if pflips > 0 else 0.0
    bl = len(STATE.get("flip_blacklist", []))
    lvl_s = f"{BET_LEVELS[min(lv-1, len(BET_LEVELS)-1)]}X"
    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *LIFETIME*\n"
            f"✅ *W:* `{w}` | ❌ *L:* `{l}`\n"
            f"📉 *Max B2B:* `{mb}` | 🔥 *Streak:* `{cs}`\n"
            f"🎯 *WR:* `{wr:.1f}%`\n"
            f"💰 *Level:* `{lv}` ({lvl_s})\n"
            f"🧠 *Pat:* `{pats}` | 💪 *Strong:* `{strong}`\n"
            f"🔢 *Num:* `{nums_db}` | ⚠️ *Err:* `{errs}`\n"
            f"🔄 *Flips:* `{pflips}` ({pa:.0f}% won)\n"
            f"🚫 *Blacklisted:* `{bl}`")

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
                    # 🔥 ROBUST FETCH — handles multiple response formats
                    if isinstance(d, dict):
                        if "data" in d and isinstance(d["data"], dict) and "list" in d["data"]:
                            return d["data"]["list"]
                        if "list" in d:
                            return d["list"]
                    elif isinstance(d, list):
                        return d
        except Exception as e:
            print(f"API att {att}: {e}")
        await asyncio.sleep(2 ** att)
    return None

# ==================== BOT LOOP ====================
class Bot:
    def __init__(self): self.pending = None

    async def run(self, session):
        print("🚀 QUANTUM V28.5.1 BOT STARTED")
        while True:
            try: await self.step(session)
            except Exception as e:
                print(f"SM err: {e}")
                import traceback; traceback.print_exc()
            await asyncio.sleep(5)

    async def step(self, session):
        raw = await fetch_data(session)
        if not raw: return
        history = validate(raw)
        if not history or len(history) < 30: return
        last = history[-1]; li = last["issue"]
        if li == STATE.get("last_processed_issue", 0): return
        arr = [h["size"] for h in history]
        regime = detect_regime(arr)

        # EVALUATE
        if self.pending and self.pending["next_issue"] == li:
            ab = last["size"] == 1
            pred_big = self.pending["pred_size"] == "BIGGG"
            upd_eng_stats(self.pending["probs"], ab, regime)
            grad_update(self.pending["probs"], ab)
            actual_s = "BIGGG" if ab else "SMALL"
            win = actual_s == self.pending["pred_size"]
            upd_global(win)
            upd_pat_mem(self.pending.get("p14"), ab)
            if self.pending.get("ss"):
                upd_pat_strength(self.pending["ss"], ab, pred_big)
            if self.pending.get("pf_flipped"):
                STATE["pattern_flips"] = STATE.get("pattern_flips", 0) + 1
                if win: STATE["pattern_flips_won"] = STATE.get("pattern_flips_won", 0) + 1
                update_flip_stat(self.pending.get("ss"), win)
            if self.pending.get("ae_flipped"):
                STATE["learned_overrides"] = STATE.get("learned_overrides", 0) + 1
            if win: asyncio.create_task(tg_sticker(session))
            self.pending = None

        # PREDICT
        if not self.pending or self.pending["last_issue"] != li:
            pred = predict_next(history)
            ni = li + 1
            pn = predict_number(history, pred["pred_size"])
            self.pending = {
                "last_issue": li, "next_issue": ni,
                "pred_size": "BIGGG" if pred["pred_size"] == 1 else "SMALL",
                "pred_number": pn, "probs": pred["probs"], "p14": pred["sig"],
                "ss": pred["ss"], "ae_flipped": pred["ae_flipped"],
                "pf_flipped": pred["pf_flipped"]
            }
            STATE.setdefault("prediction_memory", {})[str(ni)] = {"size": self.pending["pred_size"], "number": pn}

            hb = fmt_history(history)
            ep = pred["probs"]
            cons1 = f"PAT:{ep['pattern']:.2f} TRD:{ep['trend']:.2f} NSQ:{ep['number_seq']:.2f} HOT:{ep['hot_number']:.2f}"
            cons2 = f"BRK:{ep['streak_break']:.2f} RHY:{ep['rhythm']:.2f} HCD:{ep['hot_cold']:.2f} GMB:{ep['gambler_instinct']:.2f}"
            wstr = " ".join(f"{k[:3].upper()}:{v:.2f}" for k, v in pred["wts"].items())

            ae = f"\n🚨 *AE-FLIP:* `{pred['ae_r']}`" if pred["ae_flipped"] else ""
            pf = ""
            if pred["pf_flipped"]: pf = f"\n🔄 *PAT-FLIP:* `{pred['pf_r']}`"
            elif "BLACKLISTED" in (pred["pf_r"] or ""): pf = f"\n🚫 *FLIP SKIPPED:* `{pred['pf_r']}`"
            hot = f"\n🔥 *Hot:* `{pred['hot'][0][0]}` ({pred['hot'][0][1]}x)" if pred["hot"] else ""
            stg = f"\n💪 *Pattern:* `{pred['slab']}` {pred['snote']}" if pred["snote"] else ""
            lv = STATE.get("current_level", 1)
            fund = f"{BET_LEVELS[min(lv-1, len(BET_LEVELS)-1)]}X"
            footer = fmt_footer()

            msg = (f"🎯 *QUANTUM V28.5.1 ADAPTIVE* 🎯\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"📌 *Period:* `{ni}`\n"
                   f"🎲 *Number:* `{pn}`\n"
                   f"🔥 *Target:* *{'BIGGG 🟢' if pred['pred_size'] == 1 else 'SMALL 🔴'}*\n"
                   f"📊 *Conf:* `{pred['conf']*100:.1f}%` | {pred['lab']}\n"
                   f"🎰 *Form:* {pred['form']}\n"
                   f"📈 *Regime:* `{pred['regime']}` | *Ent:* `{pred['entropy']:.2f}`\n"
                   f"💰 *Fund:* `{fund}` (Level {lv})"
                   f"{ae}{pf}\n"
                   f"🧩 *Size Sig:* `{pred['sig'] or 'N/A'}`\n"
                   f"🎯 *Short Sig:* `{pred['ss']}`{stg}\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"🧠 *8-Engine:*\n`{cons1}`\n`{cons2}`\n"
                   f"⚖️ *Weights:* `{wstr}`\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"📜 *TREND (8):*\n{hb}"
                   f"{footer}")
            asyncio.create_task(tg_send(session, msg))

        STATE["last_processed_issue"] = li
        save_state()

# ==================== WARMUP ====================
async def warmup(session):
    print("Warmup V28.5.1...")
    raw = await fetch_data(session)
    if not raw: return
    history = validate(raw)
    if len(history) < 100: return
    for i in range(14, len(history) - 1):
        part = history[:i]; a = [h["size"] for h in part]
        sg = pattern_signature(a, 14)
        if sg:
            nb = history[i]["size"] == 1
            pm = STATE.setdefault("pattern_memory", {}).setdefault(sg, {"next_big": 0, "next_small": 0})
            if nb: pm["next_big"] += 1
            else: pm["next_small"] += 1
    for i in range(6, len(history) - 1):
        part = history[:i]; a = [h["size"] for h in part]
        ss = short_sig(a, 6)
        if not ss: continue
        try:
            pr = predict_next(part)
            ab = history[i]["size"] == 1
            upd_pat_strength(ss, ab, pr["pred_size"] == 1)
        except: continue
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
    print(f"Warmup done. Pat:{len(STATE.get('pattern_memory', {}))} Strong:{len(STATE.get('pattern_strength', {}))}")

# ==================== MAIN ====================
async def health(r): return web.Response(text="V28.5.1 ADAPTIVE ACTIVE", status=200)

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

