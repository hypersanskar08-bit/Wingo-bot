import json
import time
import math
import os
import asyncio
from collections import defaultdict
import aiohttp
from aiohttp import web

# ==================== CONFIGURATION ====================
API_URL   = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-500"
BOT_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID   = "1264164655"
WIN_STICKER_ID = "CAACAgIAAxkBAAEK941l-2E5L8X8u3X8g9X8g9X8g9X8gAACSAADw2m4HEX8_X3I1_34MAQ"
# =======================================================

# ─────────────────────────────────────────────
# WEB SERVER
# ─────────────────────────────────────────────
async def handle_health(request):
    return web.Response(text="QUANTUM V22 ACTIVE", status=200)

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────
async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            await r.text()
    except Exception as e:
        print(f"Telegram error: {e}")

async def send_sticker(session):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
    try:
        async with session.post(url,
                                json={"chat_id": CHAT_ID, "sticker": WIN_STICKER_ID},
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            await r.text()
    except Exception as e:
        print(f"Sticker error: {e}")

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
async def fetch_data(session):
    try:
        async with session.get(API_URL,
                               headers={"User-Agent": "Mozilla/5.0"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                lst = (data.get("data", {}) or {}).get("list") or data if isinstance(data, list) else None
                if lst:
                    return lst
    except Exception as e:
        print(f"Fetch error: {e}")
    return None

# ─────────────────────────────────────────────
# MATH UTILS
# ─────────────────────────────────────────────
def logit(p):
    p = max(1e-9, min(1 - 1e-9, p))
    return math.log(p / (1 - p))

def sigmoid(x):
    return 1 / (1 + math.exp(-max(-15, min(15, x))))

def acf(arr, lag):
    n = len(arr)
    if n < lag + 8: return 0.0
    m = sum(arr) / n
    v = sum((x - m) ** 2 for x in arr) / n
    if v < 1e-9: return 0.0
    cov = sum((arr[i] - m) * (arr[i - lag] - m) for i in range(lag, n)) / (n - lag)
    return cov / v

def hurst_exp(arr):
    if len(arr) < 20: return 0.5
    n = len(arr)
    m = sum(arr) / n
    Z = []
    s = 0
    for v in arr:
        s += v - m
        Z.append(s)
    R = max(Z) - min(Z)
    S = math.sqrt(sum((x - m) ** 2 for x in arr) / n)
    return math.log(R / S + 1e-9) / math.log(n) if S > 1e-9 else 0.5

def cond_entropy(arr, order=1):
    if len(arr) < order + 8: return 1.0
    counts = defaultdict(lambda: {0: 0, 1: 0})
    for i in range(order, len(arr)):
        k = tuple(arr[i - order:i])
        counts[k][arr[i]] += 1
    H = 0.0
    tot = 0
    for k, d in counts.items():
        n = d[0] + d[1]
        tot += n
        for c in d.values():
            if c > 0:
                p = c / n
                H -= n * (p * math.log2(p))
    return H / max(tot, 1)

# ═══════════════════════════════════════════════════════════
# ENGINE 1 — KALMAN FILTER (tracks hidden probability state)
# ═══════════════════════════════════════════════════════════
def eng_kalman(arr):
    if len(arr) < 15: return 0.5, 0.0
    x, P = 0.5, 0.25
    Q, R = 0.004, 0.25
    for v in reversed(arr):
        P += Q
        K = P / (P + R)
        x += K * (v - x)
        P = (1 - K) * P
    bias = abs(x - 0.5)
    if bias < 0.04: return 0.5, 0.0
    conf = min(bias * 5.0 + (1 - P) * 0.25, 0.92)
    return max(0.05, min(0.95, x)), conf

# ═══════════════════════════════════════════════════════════
# ENGINE 2 — MARKOV (1st–4th order, recency-weighted, BIC)
# ═══════════════════════════════════════════════════════════
def bic_order(arr, max_o=4):
    n = len(arr)
    if n < 20: return 1
    best, bo = float("inf"), 1
    for o in range(1, max_o + 1):
        if n < o + 8: break
        cnt = defaultdict(lambda: {0: 0, 1: 0})
        for i in range(o, n):
            cnt[tuple(arr[i - o:i])][arr[i]] += 1
        ll = 0.0
        for k, d in cnt.items():
            t = d[0] + d[1]
            for c in d.values():
                if c > 0: ll += c * math.log(c / t)
        bic = -2 * ll + (2 ** o) * math.log(n)
        if bic < best: best, bo = bic, o
    return bo

def eng_markov(arr):
    if len(arr) < 15: return 0.5, 0.0
    n = len(arr)
    best_o = bic_order(arr)
    results = []
    for o in range(1, min(best_o + 3, 6)):
        if n < o + 8: continue
        tr = defaultdict(lambda: {0: 0.0, 1: 0.0})
        for i in range(o, n):
            state = tuple(arr[i - o:i])
            age = n - 1 - i
            w = math.exp(-age * 0.022)
            tr[state][arr[i]] += w
        cur = tuple(arr[-o:])
        if cur not in tr: continue
        c = tr[cur]
        tot = c[0] + c[1]
        if tot < 0.5: continue
        prob = c[1] / tot
        bias = abs(prob - 0.5)
        if bias < 0.04: continue
        ev = min(math.log(tot + 1) / 2.5, 1.0)
        eb = 0.10 if o == best_o else 0.0
        conf = min(bias * 5.2 + ev * 0.28 + eb, 0.92)
        ow = math.exp(-abs(o - best_o) * 0.25) * (1 + min(tot, 10) * 0.04)
        results.append((prob, conf, ow))
    if not results: return 0.5, 0.0
    tw = sum(ow for _, _, ow in results)
    prob = sum(p * ow for p, _, ow in results) / tw
    conf = sum(c * ow for _, c, ow in results) / tw
    if abs(prob - 0.5) < 0.04: return 0.5, 0.0
    return max(0.05, min(0.95, prob)), min(conf, 0.92)

# ═══════════════════════════════════════════════════════════
# ENGINE 3 — AUTOCORRELATION (lag 1–6)
# ═══════════════════════════════════════════════════════════
def eng_autocorr(arr):
    if len(arr) < 25: return 0.5, 0.0
    w = arr[:min(100, len(arr))]
    sigs = []
    for lag in range(1, 7):
        ac = acf(w, lag)
        if abs(ac) < 0.09: continue
        last_k = w[-lag]
        prob = (0.5 + abs(ac) * 0.45 if last_k else 0.5 - abs(ac) * 0.45) if ac > 0 else \
               (0.5 - abs(ac) * 0.45 if last_k else 0.5 + abs(ac) * 0.45)
        lag_w = math.exp(-lag * 0.28)
        conf = min(abs(ac) * 3.2 * lag_w, 0.85)
        if abs(prob - 0.5) > 0.04:
            sigs.append((prob, conf, abs(ac) * lag_w))
    if not sigs: return 0.5, 0.0
    tw = sum(ow for _, _, ow in sigs)
    prob = sum(p * ow for p, _, ow in sigs) / tw
    conf = sum(c * ow for _, c, ow in sigs) / tw
    if abs(prob - 0.5) < 0.04: return 0.5, 0.0
    return max(0.05, min(0.95, prob)), min(conf, 0.85)

# ═══════════════════════════════════════════════════════════
# ENGINE 4 — BAYESIAN FREQUENCY (Beta-Binomial multi-window)
# ═══════════════════════════════════════════════════════════
def eng_bayes(arr):
    if len(arr) < 10: return 0.5, 0.0
    n = len(arr)
    sigs = []
    for ws, wt in [(10, 0.40), (20, 0.28), (40, 0.18), (80, 0.10), (120, 0.04)]:
        if n < ws: continue
        w = arr[:ws]
        k = sum(w)
        a, b = k + 1.0, (ws - k) + 1.0
        pm = a / (a + b)
        ps = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        bias = abs(pm - 0.5)
        if bias < 0.07: continue
        conf = min(bias * 3.5 * (1 - min(ps * 5, 0.9)), 0.80) * wt * 5
        if conf < 0.05: continue
        sigs.append((pm, conf, wt * math.exp(-(n - ws) * 0.005)))
    if not sigs: return 0.5, 0.0
    tw = sum(wt for _, _, wt in sigs)
    prob = sum(p * wt for p, _, wt in sigs) / tw
    conf = sum(c for _, c, _ in sigs) / len(sigs)
    if abs(prob - 0.5) < 0.05: return 0.5, 0.0
    return max(0.05, min(0.95, prob)), min(conf, 0.82)

# ═══════════════════════════════════════════════════════════
# ENGINE 5 — REGIME + STREAK ANALYZER
# ═══════════════════════════════════════════════════════════
def eng_regime_streak(arr):
    if len(arr) < 20: return 0.5, 0.0
    w = arr[:min(40, len(arr))]
    n = len(w)
    h = hurst_exp(w)
    alt = sum(1 for i in range(1, n) if w[i] != w[i - 1]) / (n - 1)
    rl, cr = [], 1
    for i in range(1, n):
        if w[i] == w[i - 1]: cr += 1
        else: rl.append(cr); cr = 1
    rl.append(cr)
    avg_run = sum(rl) / len(rl)
    last = w[0]
    cs = 0
    for v in arr:
        if v == last: cs += 1
        else: break

    sigs = []
    # Regime signal
    if h > 0.58 and alt < 0.44:
        prob = float(last)
        conf = min((h - 0.50) * 3.0 + (avg_run - 2.0) * 0.15, 0.82)
        if abs(prob - 0.5) > 0.04: sigs.append((prob, conf, 1.2))
    elif h < 0.42 and alt > 0.62:
        prob = 1.0 - float(last)
        conf = min((0.50 - h) * 3.0 + (alt - 0.50) * 1.5, 0.80)
        if abs(prob - 0.5) > 0.04: sigs.append((prob, conf, 1.2))

    # Streak empirical
    fn = len(arr)
    if cs >= 3 and fn > cs + 5:
        cont, rev = 0, 0
        for i in range(1, fn - cs):
            if all(arr[i + j] == arr[i] for j in range(cs)):
                if i + cs < fn:
                    if arr[i + cs] == arr[i]: cont += 1
                    else: rev += 1
        tot = cont + rev
        if tot >= 4:
            cr2 = cont / tot
            bias = abs(cr2 - 0.5)
            if bias > 0.10:
                prob = (0.5 + bias * 0.7) if (last and cr2 > 0.5) or (not last and cr2 < 0.5) else (0.5 - bias * 0.7)
                conf = min(bias * 3.5 * min(tot / 10, 1.0), 0.80)
                if abs(prob - 0.5) > 0.04: sigs.append((prob, conf, 1.0))

    if not sigs: return 0.5, 0.0
    tw = sum(ow for _, _, ow in sigs)
    prob = sum(p * ow for p, _, ow in sigs) / tw
    conf = sum(c * ow for _, c, ow in sigs) / tw
    return max(0.05, min(0.95, prob)), min(conf, 0.85)

# ═══════════════════════════════════════════════════════════
# ENGINE 6 — DEEP PATTERN MINER (upgraded, recency-weighted)
# ═══════════════════════════════════════════════════════════
def eng_pattern_miner(arr):
    n = len(arr)
    if n < 20: return 0.5, 0.0
    best_prob, best_conf, best_len = 0.5, 0.0, 0
    for L in range(10, 2, -1):
        if n <= L: continue
        tail = tuple(arr[-L:])
        big_w, sml_w = 0.0, 0.0
        for i in range(n - L):
            if tuple(arr[i:i + L]) == tail:
                age = n - L - i
                w = math.exp(-age * 0.025)
                if arr[i + L] == 1: big_w += w
                else: sml_w += w
        if big_w + sml_w > 0.3:
            prob = big_w / (big_w + sml_w)
            bias = abs(prob - 0.5)
            if bias < 0.06: continue
            ev = min(math.log(big_w + sml_w + 1) / 2.0, 1.0)
            conf = min(bias * 5.0 + ev * 0.30, 0.92)
            if conf > best_conf:
                best_prob, best_conf, best_len = prob, conf, L
    if best_conf < 0.05: return 0.5, 0.0
    return max(0.05, min(0.95, best_prob)), best_conf

# ═══════════════════════════════════════════════════════════
# REGIME-CONDITIONAL WEIGHTS
# ═══════════════════════════════════════════════════════════
REGIME_BONUS = {
    "TREND": {"KALMAN": 1.1, "MARKOV": 1.0, "AUTOCORR": 1.4, "BAYES": 0.7, "REGIME": 1.5, "PATTERN": 0.9},
    "ANTI":  {"KALMAN": 1.0, "MARKOV": 1.0, "AUTOCORR": 1.5, "BAYES": 0.8, "REGIME": 1.3, "PATTERN": 1.0},
    "RAND":  {"KALMAN": 1.2, "MARKOV": 1.3, "AUTOCORR": 0.9, "BAYES": 1.4, "REGIME": 0.9, "PATTERN": 1.2},
}

def detect_regime(arr):
    if len(arr) < 15: return "RAND"
    w = arr[:min(40, len(arr))]
    h = hurst_exp(w)
    alt = sum(1 for i in range(1, len(w)) if w[i] != w[i - 1]) / (len(w) - 1)
    if h > 0.58 and alt < 0.44: return "TREND"
    if h < 0.42 and alt > 0.62: return "ANTI"
    return "RAND"

# ═══════════════════════════════════════════════════════════
# META FUSION (entropy-gated log-odds combination)
# ═══════════════════════════════════════════════════════════
def meta_fuse(engines, regime, entropy):
    rb = REGIME_BONUS.get(regime, REGIME_BONUS["RAND"])
    gate = max(0.4, 1 - entropy * 0.5)
    lo_sum, wt_sum = 0.0, 0.0
    votes = []
    for name, (prob, conf) in engines.items():
        if conf < 0.04 or prob == 0.5: continue
        cw = conf * rb.get(name, 1.0) * gate
        lo_sum += logit(prob) * cw
        wt_sum += cw
        votes.append((name, prob, conf, "BIG" if prob > 0.5 else "SMALL"))
    if wt_sum < 0.01: return 0.5, 0.0, votes
    fused = sigmoid(lo_sum / wt_sum)
    bv = sum(1 for _, _, _, d in votes if d == "BIG")
    sv = len(votes) - bv
    agree = max(bv, sv) / len(votes) if votes else 0.5
    return fused, agree, votes

# ═══════════════════════════════════════════════════════════
# NUMBER PREDICTOR (Gap + EMA + Markov + Pattern next-num)
# ═══════════════════════════════════════════════════════════
def predict_number(history_list, direction):
    rang = [5, 6, 7, 8, 9] if direction == "BIGGG" else [0, 1, 2, 3, 4]
    nums = [int(item.get("number", -1)) for item in history_list if str(item.get("number","")).isdigit()]
    if len(nums) < 8:
        return rang[0], {v: 1 for v in rang}
    scores = {v: 0.0 for v in rang}
    n = len(nums)

    # 1. GAP — overdue numbers score higher
    last_seen = {}
    for i, v in enumerate(nums):
        if v not in last_seen: last_seen[v] = i
    max_gap = max((last_seen.get(v, n) for v in rang), default=n)
    for v in rang:
        scores[v] += (last_seen.get(v, n) / max_gap) * 30

    # 2. EMA — cold numbers preferred
    ema = {i: 0.1 for i in range(10)}
    for v in reversed(nums[:80]):
        for k in range(10):
            ema[k] = ema[k] * 0.88 + (0.12 if v == k else 0)
    mx_ema = max(ema[v] for v in rang) or 0.1
    for v in rang:
        scores[v] += (1 - ema[v] / mx_ema) * 22

    # 3. Markov pair (what number follows last number)
    pair = defaultdict(lambda: defaultdict(float))
    for i in range(1, min(n, 100)):
        age = min(n, 100) - 1 - i
        w = math.exp(-age * 0.04)
        pair[nums[i - 1]][nums[i]] += w
    if nums[0] in pair:
        tot = sum(pair[nums[0]].values()) or 1
        for v in rang:
            scores[v] += pair[nums[0]][v] / tot * 55

    # 4. Tri-gram
    if n >= 2 and nums[1] in pair and nums[0] in pair.get(nums[1], {}):
        # Use bigram lookback
        tri = defaultdict(lambda: defaultdict(float))
        for i in range(2, min(n, 80)):
            age = min(n, 80) - 1 - i
            w = math.exp(-age * 0.04)
            tri[(nums[i - 2], nums[i - 1])][nums[i]] += w
        k3 = (nums[1], nums[0])
        if k3 in tri:
            tot3 = sum(tri[k3].values()) or 1
            for v in rang:
                scores[v] += tri[k3][v] / tot3 * 35

    # 5. Recency — number appeared in last 5 → penalise
    recent5 = nums[:5]
    for v in rang:
        if v in recent5: scores[v] -= 60

    sorted_nums = sorted(rang, key=lambda v: -scores[v])
    return sorted_nums[0], scores

# ═══════════════════════════════════════════════════════════
# MAIN PREDICTION ENGINE
# ═══════════════════════════════════════════════════════════
def v22_engine(history_list, current_level):
    # Parse outcomes
    arr = []
    for item in history_list:
        s = str(item.get("size", "")).upper()
        arr.append(1 if s in ["BIG", "BIGGG"] else 0)
    if len(arr) < 20: return None

    # Reverse so arr[0] = latest
    arr = list(reversed(arr))

    # Entropy + regime
    entropy = cond_entropy(arr[:40], order=1)
    regime  = detect_regime(arr)

    # Run all engines
    eK  = eng_kalman(arr)
    eMk = eng_markov(arr)
    eAC = eng_autocorr(arr)
    eBay= eng_bayes(arr)
    eRS = eng_regime_streak(arr)
    ePM = eng_pattern_miner(arr)

    engines = {
        "KALMAN":  eK,
        "MARKOV":  eMk,
        "AUTOCORR":eAC,
        "BAYES":   eBay,
        "REGIME":  eRS,
        "PATTERN": ePM,
    }

    fused_prob, agree, votes = meta_fuse(engines, regime, entropy)

    if fused_prob >= 0.50:
        direction = "BIGGG"
        raw_conf  = fused_prob
    else:
        direction = "SMALL"
        raw_conf  = 1.0 - fused_prob

    # Honest confidence (not fake 95%)
    # Base 55–80 range, scaled by signal strength
    dom = abs(fused_prob - 0.5)
    base_conf = 55 + dom * 60 + agree * 8
    # Entropy adjustment: high entropy = less predictable
    base_conf -= entropy * 6
    display_conf = round(max(54, min(80, base_conf)), 1)

    # Predict number
    best_num, num_scores = predict_number(history_list, direction)

    # Bet advice
    bet_map = {1: "1.0X 🎯", 2: "2.5X 🔥", 3: "6.0X ⚡", 4: "12.0X 🛡"}
    bet_advice = bet_map.get(current_level, "1.0X 🎯")

    # Engine vote summary (top 3 active)
    active_votes = [(n, d) for n, _, _, d in votes]
    vote_str = " | ".join(f"{n}:{d}" for n, d in active_votes[:4]) if active_votes else "LEARNING"

    last_issue = int(history_list[-1]["issueNumber"])

    return {
        "last_issue":   last_issue,
        "next_issue":   last_issue + 1,
        "direction":    direction,
        "number":       best_num,
        "confidence":   display_conf,
        "bet_advice":   bet_advice,
        "regime":       regime,
        "entropy":      round(entropy, 3),
        "vote_str":     vote_str,
        "active_eng":   len(votes),
        "current_level":current_level,
    }

# ─────────────────────────────────────────────
# HISTORY FORMATTER (with proper emojis)
# WIN = ✅✅✅  |  JACKPOT = ☠️☠️☠️  |  LOSS = (blank)
# ─────────────────────────────────────────────
def format_history(server_list, pred_log):
    """
    pred_log: list of {issue, direction, number} for tracking results
    """
    lines = ""
    shown = server_list[:8] if len(server_list) >= 8 else server_list
    for item in shown:
        issue_short = str(item["issueNumber"])[-3:]
        size = str(item.get("size", "")).upper()
        size_str = "BIGGG" if size in ["BIG", "BIGGG"] else "SMALL"
        num = item.get("number", "?")

        # Find matching prediction
        matched = next((p for p in pred_log if str(p["issue"]) == str(item["issueNumber"])), None)
        if matched:
            hit_size = (matched["direction"] == size_str)
            hit_num  = (str(matched["number"]) == str(num))
            if hit_num:
                emoji = "☠️☠️☠️"   # JACKPOT
            elif hit_size:
                emoji = "✅✅✅"    # WIN
            else:
                emoji = ""          # LOSS — blank
        else:
            emoji = ""  # no prediction recorded

        lines += f"`{issue_short}` *{size_str}* `{num}` {emoji}\n"
    return lines

# ─────────────────────────────────────────────
# BOT MAIN LOOP
# ─────────────────────────────────────────────
async def bot_loop(session):
    current_level = 1
    pending_pred  = None
    pred_log      = []  # [{issue, direction, number}, ...]
    print("🚀 QUANTUM V22 STARTED")

    while True:
        try:
            raw_list = await fetch_data(session)
            if raw_list:
                history = list(reversed(raw_list))
                last_item   = history[-1]
                last_issue  = int(last_item["issueNumber"])
                actual_size = "BIGGG" if str(last_item.get("size","")).upper() in ["BIG","BIGGG"] else "SMALL"
                actual_num  = str(last_item.get("number",""))

                # ── RESULT CHECK ──────────────────────────────
                if pending_pred and str(pending_pred["next_issue"]) == str(last_issue):
                    hit_size = (actual_size == pending_pred["direction"])
                    hit_num  = (str(pending_pred["number"]) == actual_num)

                    if hit_num:
                        # JACKPOT
                        current_level = 1
                        asyncio.create_task(send_sticker(session))
                        result_msg = (
                            f"☠️☠️☠️ *JACKPOT!* `{actual_num}` ☠️☠️☠️\n"
                            f"Period `{last_issue}` → *{actual_size}* `{actual_num}`"
                        )
                        asyncio.create_task(send_telegram(session, result_msg))
                    elif hit_size:
                        # WIN
                        current_level = 1
                        asyncio.create_task(send_sticker(session))
                        result_msg = (
                            f"✅✅✅ *WIN!* ✅✅✅\n"
                            f"Period `{last_issue}` → *{actual_size}* `{actual_num}`"
                        )
                        asyncio.create_task(send_telegram(session, result_msg))
                    else:
                        # LOSS — no emoji, just info
                        current_level = min(current_level + 1, 4)

                    # Log result
                    pred_log.append({
                        "issue":     last_issue,
                        "direction": pending_pred["direction"],
                        "number":    pending_pred["number"],
                    })
                    if len(pred_log) > 100: pred_log = pred_log[-100:]
                    pending_pred = None

                # ── NEW PREDICTION ────────────────────────────
                if not pending_pred or pending_pred["last_issue"] != last_issue:
                    p = v22_engine(history, current_level)
                    if p:
                        pending_pred = p
                        hist_block   = format_history(history, pred_log)
                        dir_emoji    = "🟢" if p["direction"] == "BIGGG" else "🔴"

                        msg = (
                            f"🎯 *QUANTUM V22 — DEEP STRIKE* 🎯\n\n"
                            f"📌 *Period:* `{p['next_issue']}`\n"
                            f"🔥 *Size:* *{p['direction']}* {dir_emoji}\n"
                            f"🎰 *Number:* `{p['number']}`\n"
                            f"📊 *Confidence:* `{p['confidence']:.1f}%`\n"
                            f"💰 *Bet:* `{p['bet_advice']}`\n\n"
                            f"🚩 *Level:* `{p['current_level']}`  "
                            f"🌐 *Regime:* `{p['regime']}`\n"
                            f"⚙️ *Engines ({p['active_eng']}):* `{p['vote_str']}`\n"
                            f"📉 *Entropy:* `{p['entropy']}`\n"
                            f"───────────────────────\n"
                            f"📜 *RECENT (8):*\n"
                            f"{hist_block}"
                        )
                        asyncio.create_task(send_telegram(session, msg))

        except Exception as e:
            print(f"Loop error: {e}")

        await asyncio.sleep(5)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web server on port {port}")
    async with aiohttp.ClientSession() as session:
        await bot_loop(session)

if __name__ == "__main__":
    asyncio.run(main())


