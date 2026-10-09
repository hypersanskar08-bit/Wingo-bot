import json, time, math, os, asyncio, traceback
from collections import defaultdict
from math import lgamma, log, exp
import aiohttp
from aiohttp import web

# ==================== CONFIG ====================
API_URL = os.environ.get("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk")
CHAT_ID = os.environ.get("CHAT_ID", "1264164655")
WIN_STICKER_ID = os.environ.get("STICKER_ID", "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ")

BET_LEVELS = [1.0, 2.5, 6.0, 12.0]
MAX_LEVEL = 4
WARMUP_POINTS = 130
MAX_LOSS_STREAK_HARD = 5
LEVEL4_COOLDOWN = 900

STATE_FILE = "engine_state_v30.json"
ENGINES = ["ppm", "bocpd", "hmm", "kalman", "streak", "acf", "freq", "momentum", "spectral", "runs"]

# ==================== STATE ====================
STATE = {
    "total_wins": 0, "total_losses": 0,
    "current_level": 1, "current_loss_streak": 0, "max_b2b_loss": 0,
    "bot_recent_form": [], "prediction_memory": {},
    "last_processed_issue": 0, "cooldown_until": 0,
}

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            for k in STATE:
                if k not in s: s[k] = STATE[k]
            STATE = s
        except Exception as ex:
            print(f"State load err: {ex}")

def save_state():
    try:
        for key, cap in [("prediction_memory", 500), ("bot_recent_form", 20)]:
            if len(STATE.get(key, {})) > cap:
                if isinstance(STATE[key], list): STATE[key] = STATE[key][-cap:]
                else:
                    for k in sorted(STATE[key].keys())[:-cap]: del STATE[key][k]
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f)
    except Exception as e:
        print(f"Save err: {e}")

load_state()

# ==================== KT (KRICHEVSKY-TROFIMOV) CORE ====================
_NORM = 2.0 * lgamma(0.5) - lgamma(1.0)

def kt_mean(ones, zeros):
    return (ones + 0.5) / (ones + zeros + 1.0)

def kt_log(ones, zeros):
    return lgamma(ones + 0.5) + lgamma(zeros + 0.5) - lgamma(ones + zeros + 1.0) - _NORM

# ---------- PPM-C: Prediction by Partial Matching ----------
def ppm_predict(seq, max_order=10):
    """PPM method-C blended predictor. Real variable-order context model."""
    n = len(seq)
    if n < 4:
        return 0.5
    max_order = min(max_order, n - 1)

    def ctx_counts(k):
        ctx = seq[n - k:]
        ones = zeros = 0
        for i in range(k, n):
            if seq[i - k:i] == ctx:
                if seq[i] == 1: ones += 1
                else: zeros += 1
        return ones, zeros

    def rec(k):
        if k == 0:
            o = seq.count(1)
            return kt_mean(o, n - o)
        o, z = ctx_counts(k)
        t = o + z
        if t == 0:
            return rec(k - 1)
        d = (1 if o > 0 else 0) + (1 if z > 0 else 0)
        p_here = o / t
        p_low = rec(k - 1)
        return (t / (t + d)) * p_here + (d / (t + d)) * p_low

    return rec(max_order)

# ==================== BOCPD (Bayesian Online Changepoint Detection) ====================
class BOCPD:
    __slots__ = ("hazard", "a", "b", "R")
    def __init__(self, hazard=1.0 / 45.0):
        self.hazard = hazard
        self.a = [1.0]   # Beta alpha per run-length
        self.b = [1.0]   # Beta beta per run-length
        self.R = [1.0]   # run-length posterior

    def predict(self):
        return sum(self.R[i] * (self.a[i] / (self.a[i] + self.b[i])) for i in range(len(self.R)))

    def update(self, x):
        m = len(self.R)
        preds = [self.a[i] / (self.a[i] + self.b[i]) for i in range(m)]
        Rnew = [0.0] * (m + 1)
        cp = 0.0
        for i in range(m):
            p = preds[i] if x == 1 else (1.0 - preds[i])
            w = self.R[i] * p
            Rnew[i + 1] = w * (1.0 - self.hazard)
            cp += w * self.hazard
        Rnew[0] = cp
        s = sum(Rnew)
        Rnew = [v / s for v in Rnew] if s > 0 else [1.0] + [0.0] * m
        an, bn = [1.0], [1.0]
        for i in range(m):
            an.append(self.a[i] + (1.0 if x == 1 else 0.0))
            bn.append(self.b[i] + (0.0 if x == 1 else 1.0))
        if len(Rnew) > 240:
            Rnew = Rnew[-120:]; s = sum(Rnew)
            Rnew = [v / s for v in Rnew] if s > 0 else Rnew
            an = an[-120:]; bn = bn[-120:]
        self.R, self.a, self.b = Rnew, an, bn

    def cp_prob(self):
        return self.R[0]

# ==================== HMM (2-state, online EM) ====================
class HMM2:
    __slots__ = ("trans", "emit", "alpha", "lr")
    def __init__(self):
        self.trans = [[0.9, 0.1], [0.1, 0.9]]
        self.emit = [[0.42, 0.58], [0.58, 0.42]]
        self.alpha = [0.5, 0.5]
        self.lr = 0.02

    def predict(self):
        s0 = self.alpha[0] * self.trans[0][0] + self.alpha[1] * self.trans[1][0]
        s1 = self.alpha[0] * self.trans[0][1] + self.alpha[1] * self.trans[1][1]
        return s0 * self.emit[0][1] + s1 * self.emit[1][1]

    def update(self, x):
        new = [0.0, 0.0]
        for j in range(2):
            acc = self.alpha[0] * self.trans[0][j] + self.alpha[1] * self.trans[1][j]
            new[j] = acc * self.emit[j][x]
        s = sum(new)
        if s <= 0: return
        g = [v / s for v in new]
        for j in range(2):
            for y in (0, 1):
                tgt = 1.0 if y == x else 0.0
                self.emit[j][y] += self.lr * g[j] * (tgt - self.emit[j][y])
            tot = self.emit[j][0] + self.emit[j][1]
            if tot > 0:
                self.emit[j][0] /= tot; self.emit[j][1] /= tot
        self.alpha = g

# ==================== HEDGE (exponential weights / no-regret) ====================
class Hedge:
    __slots__ = ("eta", "loss", "engines")
    def __init__(self, engines, eta=0.35):
        self.eta = eta
        self.engines = list(engines)
        self.loss = {e: 0.0 for e in self.engines}

    def weights(self):
        mn = min(self.loss.values())
        raw = {e: exp(-self.eta * (self.loss[e] - mn)) for e in self.engines}
        s = sum(raw.values()) or 1.0
        return {e: v / s for e, v in raw.items()}

    def combine(self, probs):
        w = self.weights()
        return sum(w[e] * probs[e] for e in self.engines if e in probs)

    def update(self, probs, x):
        for e in self.engines:
            if e not in probs: continue
            p = min(max(probs[e], 1e-6), 1.0 - 1e-6)
            self.loss[e] += -log(p) if x == 1 else -log(1.0 - p)

# ==================== CALIBRATOR (Platt + metrics) ====================
class Calibrator:
    __slots__ = ("a", "b", "n", "brier", "logloss", "lr")
    def __init__(self):
        self.a, self.b = 1.0, 0.0
        self.n, self.brier, self.logloss = 0, 0.0, 0.0
        self.lr = 0.03

    def apply(self, p):
        p = min(max(p, 1e-6), 1.0 - 1e-6)
        z = self.a * log(p / (1.0 - p)) + self.b
        return 1.0 / (1.0 + exp(-z))

    def update(self, p_raw, x):
        p_raw = min(max(p_raw, 1e-6), 1.0 - 1e-6)
        pc = self.apply(p_raw)
        err = pc - x
        self.a -= self.lr * err * log(p_raw / (1.0 - p_raw))
        self.b -= self.lr * err
        self.a = max(0.1, min(5.0, self.a))
        self.b = max(-3.0, min(3.0, self.b))
        self.brier += (pc - x) ** 2
        self.logloss += -(x * log(pc) + (1 - x) * log(1 - pc))
        self.n += 1

    def avg_brier(self):
        return self.brier / self.n if self.n else 0.0

    def avg_logloss(self):
        return self.logloss / self.n if self.n else 0.0

# ==================== ENGINES ====================
def eng_kalman(arr):
    if len(arr) < 20: return 0.5
    x, P = 0.5, 1.0
    Q, R = 0.02, 0.15
    for z in arr[-30:]:
        Pp = P + Q
        K = Pp / (Pp + R)
        x = x + K * (z - x)
        P = (1 - K) * Pp
    return max(0.15, min(0.85, 0.5 + (x - 0.5) * 1.2))

def eng_streak(arr):
    if len(arr) < 6: return 0.5
    rec = arr[-12:]
    sv, L = rec[-1], 1
    for i in range(len(rec) - 2, -1, -1):
        if rec[i] == sv: L += 1
        else: break
    raw = 1.0 / (1.0 + exp(-(L - 4.5) * 0.8))
    break_conf = 0.5 + (raw - 0.5) * 0.7
    p1 = (1.0 - break_conf) if sv == 1 else break_conf
    return max(0.1, min(0.9, p1))

def eng_acf(arr):
    n = len(arr)
    if n < 30: return 0.5
    rec = arr[-40:]; m = len(rec)
    mean = sum(rec) / m
    var = sum((v - mean) ** 2 for v in rec)
    if var <= 0: return 0.5
    best_lag, best_r = 0, 0.0
    for lag in range(1, 8):
        cov = sum((rec[i] - mean) * (rec[i + lag] - mean) for i in range(m - lag))
        r = cov / var
        if abs(r) > abs(best_r):
            best_r, best_lag = r, lag
    if best_lag == 0 or abs(best_r) < 0.15: return 0.5
    last = rec[-best_lag]
    pred = last if best_r > 0 else 1 - last
    strength = min(0.35, abs(best_r) * 0.45)
    return 0.5 + strength if pred == 1 else 0.5 - strength

def eng_freq(arr):
    if len(arr) < 15: return 0.5
    rec = arr[-30:]
    wsum = wtot = 0.0
    for i, v in enumerate(rec):
        w = 0.93 ** (len(rec) - 1 - i)
        wsum += w * v; wtot += w
    if wtot <= 0: return 0.5
    return (wsum + 0.5) / (wtot + 1.0)

def eng_momentum(nums):
    if len(nums) < 12: return 0.5
    l3 = nums[-3:]
    if len(l3) < 3: return 0.5
    if l3[0] < l3[1] < l3[2]: return 0.60
    if l3[0] > l3[1] > l3[2]: return 0.40
    return 0.5

def eng_spectral(arr):
    if len(arr) < 24: return 0.5
    N = 16
    seq = arr[-N:]
    best_k, best_mag = 0, 0.0
    for k in range(1, N // 2):
        re = sum(seq[t] * math.cos(2 * math.pi * k * t / N) for t in range(N))
        im = sum(seq[t] * math.sin(-2 * math.pi * k * t / N) for t in range(N))
        mag = math.hypot(re, im)
        if mag > best_mag:
            best_mag, best_k = mag, k
    if best_k == 0: return 0.5
    amp = min(1.0, best_mag / (N / 2))
    period = N / best_k
    phase = (N % period) / period
    return max(0.2, min(0.8, 0.5 + 0.25 * amp * math.sin(2 * math.pi * phase)))

def eng_runs(arr):
    """Wald-Wolfowitz runs test -> real statistical significance engine."""
    n = len(arr)
    if n < 30: return 0.5
    seq = arr[-60:]; m = len(seq)
    ones = sum(seq); zeros = m - ones
    if ones == 0 or zeros == 0: return 0.5
    runs = 1
    for i in range(1, m):
        if seq[i] != seq[i - 1]: runs += 1
    exp_runs = 2.0 * ones * zeros / m + 1.0
    var_runs = (2.0 * ones * zeros * (2.0 * ones * zeros - m)) / (m * m * (m - 1))
    if var_runs <= 0: return 0.5
    z = (runs - exp_runs) / math.sqrt(var_runs)
    last = seq[-1]
    strength = min(0.30, abs(z) * 0.08)
    if z > 0:      # too many runs -> alternating tendency
        return 0.5 + strength if last == 0 else 0.5 - strength
    else:          # too few runs -> streaky tendency
        return 0.5 + strength if last == 1 else 0.5 - strength

# ==================== MODEL INSTANCES ====================
BOCPD_MODEL = BOCPD()
HMM_MODEL = HMM2()
HEDGE = Hedge(ENGINES)
CAL = Calibrator()

def engine_probs(arr, nums):
    a = arr[-300:] if len(arr) > 300 else arr
    return {
        "ppm":      ppm_predict(a, 10),
        "bocpd":    BOCPD_MODEL.predict(),
        "hmm":      HMM_MODEL.predict(),
        "kalman":   eng_kalman(a),
        "streak":   eng_streak(a),
        "acf":      eng_acf(a),
        "freq":     eng_freq(a),
        "momentum": eng_momentum(nums),
        "spectral": eng_spectral(a),
        "runs":     eng_runs(a),
    }

def make_prediction(arr, nums):
    probs = engine_probs(arr, nums)
    p_raw = HEDGE.combine(probs)
    p_cal = CAL.apply(p_raw)
    direction = 1 if p_cal >= 0.5 else 0
    conf = max(p_cal, 1.0 - p_cal)
    return probs, p_raw, p_cal, direction, conf

# ==================== VALIDATION / API ====================
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
            nr = item.get("number", None)
            num = -1 if (nr is None or nr == "") else int(float(nr))
            out.append({"issue": iss, "size": sz, "number": num})
            seen.add(iss)
        except Exception:
            continue
    return out

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
        except Exception as e:
            print(f"API att {att}: {e}")
        await asyncio.sleep(2 ** att)
    return None

# ==================== WARMUP (rebuild all stateful models) ====================
def rebuild_models(history):
    global BOCPD_MODEL, HMM_MODEL, HEDGE, CAL
    BOCPD_MODEL, HMM_MODEL = BOCPD(), HMM2()
    HEDGE, CAL = Hedge(ENGINES), Calibrator()

    arr = [h["size"] for h in history]
    nums = [h["number"] for h in history]
    n = len(arr)
    if n < 60: return
    start = max(30, n - WARMUP_POINTS)

    for i in range(start):
        BOCPD_MODEL.update(arr[i]); HMM_MODEL.update(arr[i])

    for i in range(start, n):
        probs = engine_probs(arr[:i], nums[:i])
        p_comb = HEDGE.combine(probs)
        CAL.update(p_comb, arr[i])
        HEDGE.update(probs, arr[i])
        BOCPD_MODEL.update(arr[i]); HMM_MODEL.update(arr[i])

    STATE["last_processed_issue"] = history[-1]["issue"]
    print(f"✅ Models rebuilt from {n} points. Brier={CAL.avg_brier():.3f} LogLoss={CAL.avg_logloss():.3f}")

# ==================== STATS / FORMAT ====================
def upd_global(win, level):
    if win:
        STATE["total_wins"] = STATE.get("total_wins", 0) + 1
        STATE["current_loss_streak"] = 0
        STATE["current_level"] = 1
    else:
        STATE["total_losses"] = STATE.get("total_losses", 0) + 1
        STATE["current_loss_streak"] = STATE.get("current_loss_streak", 0) + 1
        STATE["max_b2b_loss"] = max(STATE.get("max_b2b_loss", 0), STATE["current_loss_streak"])
        if level == MAX_LEVEL:
            STATE["cooldown_until"] = time.time() + LEVEL4_COOLDOWN
            STATE["current_level"] = 1
            STATE["current_loss_streak"] = 0
        else:
            STATE["current_level"] = min(STATE.get("current_level", 1) + 1, MAX_LEVEL)
            if STATE["current_loss_streak"] >= MAX_LOSS_STREAK_HARD:
                STATE["cooldown_until"] = time.time() + 300
                STATE["current_level"] = 1
                STATE["current_loss_streak"] = 0
    f = STATE.setdefault("bot_recent_form", [])
    f.append(1 if win else 0)
    STATE["bot_recent_form"] = f[-20:]

def fmt_history(history):
    out = ""
    for item in history[-8:]:
        sp = str(item["issue"])[-3:]
        ss = "BIGGG" if item["size"] == 1 else "SMALL"
        nd = str(item["number"]) if item["number"] != -1 else "?"
        p = STATE.get("prediction_memory", {}).get(str(item["issue"]))
        icon = "  ✅✅✅" if (p and p["size"] == ss) else ""
        out += f"`{sp}` *{ss}* ({nd}){icon}\n"
    return out

def fmt_footer():
    w, l = STATE.get("total_wins", 0), STATE.get("total_losses", 0)
    mb, cs = STATE.get("max_b2b_loss", 0), STATE.get("current_loss_streak", 0)
    lv = STATE.get("current_level", 1)
    t = w + l; wr = (w / t * 100) if t > 0 else 0.0
    lvl_s = f"{BET_LEVELS[min(lv - 1, len(BET_LEVELS) - 1)]}X"
    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *LIFETIME*\n"
            f"✅ *W:* `{w}` | ❌ *L:* `{l}`\n"
            f"📉 *Max B2B:* `{mb}` | 🔥 *Streak:* `{cs}`\n"
            f"🎯 *WR:* `{wr:.1f}%` | 💰 *Level:* `{lv}` ({lvl_s})")

def predict_number(history, direction):
    nums = [h["number"] for h in history if h["number"] >= 0]
    if len(nums) < 40: return 8 if direction == 1 else 2
    cands = [5, 6, 7, 8, 9] if direction == 1 else [0, 1, 2, 3, 4]
    freq = defaultdict(int)
    for v in nums[-120:]: freq[v] += 1
    return max(cands, key=lambda n: freq.get(n, 0))

def sig_label(conf):
    d = conf - 0.50
    if d < 0.02: return "🟥 NONE"
    if d < 0.05: return "🟧 WEAK"
    if d < 0.10: return "🟨 MODERATE"
    if d < 0.15: return "🟩 STRONG"
    return "🟢 V.STRONG"

def top_weights(n=4):
    w = HEDGE.weights()
    return sorted(w.items(), key=lambda kv: -kv[1])[:n]

# ==================== TELEGRAM ====================
async def tg_send(session, msg):
    try:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200: print(f"TG err: {await r.text()}")
    except Exception as e:
        print(f"TG: {e}")

async def tg_sticker(session):
    try:
        async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker",
                                json={"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID},
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            await r.text()
    except Exception as e:
        print(f"Sticker: {e}")

# ==================== BOT ====================
class Bot:
    def __init__(self): self.pending = None

    async def run(self, session):
        print("🚀 QUANTUM V30 — CTW · BOCPD · HEDGE STARTED")
        while True:
            try:
                await self.step(session)
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(5)

    async def step(self, session):
        if time.time() < STATE.get("cooldown_until", 0):
            return
        raw = await fetch_data(session)
        if not raw: return
        history = validate(raw)
        if not history or len(history) < 60: return

        last = history[-1]
        li = last["issue"]
        if li == STATE.get("last_processed_issue", 0): return

        arr = [h["size"] for h in history]
        nums = [h["number"] for h in history]

        # ---- LEARN from resolved prediction ----
        if self.pending and self.pending["next_issue"] == li:
            actual = last["size"]
            BOCPD_MODEL.update(actual)
            HMM_MODEL.update(actual)
            HEDGE.update(self.pending["probs"], actual)
            CAL.update(self.pending["p_raw"], actual)
            win = (actual == 1) == (self.pending["pred_size"] == 1)
            upd_global(win, STATE.get("current_level", 1))
            if win:
                asyncio.create_task(tg_sticker(session))
            self.pending = None

        # ---- PREDICT next ----
        if not self.pending or self.pending["last_issue"] != li:
            probs, p_raw, p_cal, direction, conf = make_prediction(arr, nums)
            ni = li + 1
            pn = predict_number(history, direction)
            self.pending = {
                "last_issue": li, "next_issue": ni,
                "pred_size": "BIGGG" if direction == 1 else "SMALL",
                "pred_number": pn, "probs": probs, "p_raw": p_raw,
            }
            STATE.setdefault("prediction_memory", {})[str(ni)] = {"size": self.pending["pred_size"], "number": pn}

            lv = STATE.get("current_level", 1)
            fund = f"{BET_LEVELS[min(lv - 1, len(BET_LEVELS) - 1)]}X"
            tw = " | ".join(f"`{e}:{w:.2f}`" for e, w in top_weights(4))
            col = "🟢" if direction == 1 else "🔴"
            size_s = self.pending["pred_size"]

            msg = (f"🎯 *QUANTUM V30 — PPM · BOCPD · HEDGE* 🎯\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"📌 *Period:* `{ni}`\n"
                   f"🎲 *Number:* `{pn}`\n"
                   f"🔥 *Size:* *{size_s} {col}*\n"
                   f"📊 *Conf (calibrated):* `{conf*100:.1f}%` {sig_label(conf)}\n"
                   f"🧮 *BOCPD regime-shift P:* `{BOCPD_MODEL.cp_prob()*100:.1f}%`\n"
                   f"📉 *Brier:* `{CAL.avg_brier():.3f}` | *LogLoss:* `{CAL.avg_logloss():.3f}`\n"
                   f"💰 *Fund:* `{fund}` (Level {lv})\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"⚖️ *Hedge weights:*\n{tw}\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"📜 *TREND (8):*\n{fmt_history(history)}"
                   f"{fmt_footer()}")
            asyncio.create_task(tg_send(session, msg))

        STATE["last_processed_issue"] = li
        save_state()

# ==================== SERVER ====================
async def health(r): return web.Response(text="V30 ACTIVE", status=200)

async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Server on port {port}")

    async with aiohttp.ClientSession() as session:
        for _ in range(5):
            raw = await fetch_data(session)
            history = validate(raw) if raw else None
            if history and len(history) >= 60:
                rebuild_models(history)
                break
            await asyncio.sleep(4)
        await Bot().run(session)

if __name__ == "__main__":
    asyncio.run(main())

