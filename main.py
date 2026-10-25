"""
V916 ULTRA ADVANCED PREDICTOR (HARDCODED TOKEN VERSION)
=======================================================
- Original Ensemble Logic (Intact)
- HTML Dragon Streak (Blended as a model)
- Markov Chain Transition Analysis
- Bayesian Weight Updating
- Kelly Criterion Stake Sizing
"""

import asyncio
import gc
import html
import json
import logging
import math
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from telegram.ext import Application


# ============================================================
# CONFIG (TOKEN DIRECTLY APPLIED HERE)
# ============================================================
@dataclass
class Config:
    # Maine aapka token yahan direct daal diya hai
    telegram_token: str = "8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM"
    chat_id_raw: str = "1264164655"
    
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_ultra.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 30.0
    bot_name: str = "V916 ULTRA"
    min_history: int = 50

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls if urls else [
            "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
        ]

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()


# ============================================================
# LOGGING + HEALTH
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916_ultra.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("v916")

def h(x: Any) -> str: return html.escape(str(x))

async def health_check(request):
    return web.Response(text="V916 Ultra Advanced Predictor is Running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

# ============================================================
# HELPERS & MATH
# ============================================================
def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))

def safe_int(x, default=0):
    try: return int(x)
    except Exception: return default

def normalize_issue(value: Any) -> Optional[int]:
    if value is None: return None
    if isinstance(value, str):
        value = value.strip()
        if value == "": return None
        if value.isdigit(): return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits: return int(digits)
    try: return int(value)
    except Exception: return None

def get_color_from_num(num: int) -> str:
    if num in (0, 5): return "VIOLET"
    if num in (2, 4, 6, 8): return "RED"
    if num in (1, 3, 7, 9): return "GREEN"
    return "UNKNOWN"

# ============================================================
# ADVANCED MATH MODELS
# ============================================================
def trailing_run(values: List[int], target: Optional[int] = None) -> int:
    if not values: return 0
    last = values[-1] if target is None else target
    run = 1
    for i in range(len(values) - 2, -1, -1):
        if values[i] == last: run += 1
        else: break
    return run

def ratio_big(values: List[int], window: int = 20) -> float:
    if not values: return 0.5
    arr = values[-window:]
    return sum(arr) / len(arr)

def exponential_moving_average(values: List[int], alpha: float = 0.3) -> float:
    if not values: return 0.5
    ema = values[0]
    for val in values[1:]:
        ema = alpha * val + (1 - alpha) * ema
    return ema

def alternating_score(values: List[int], window: int = 12) -> float:
    if len(values) < 2: return 0.0
    arr = values[-window:]
    changes = sum(1 for i in range(1, len(arr)) if arr[i] != arr[i - 1])
    return changes / max(1, len(arr) - 1)

def block_pattern(values: List[int], window: int = 10) -> bool:
    if len(values) < 4: return False
    arr = values[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i + 4]
        if a == b and c == d and a != c: return True
    return False

def volatility(values: List[int], window: int = 20) -> float:
    if len(values) < 2: return 0.0
    arr = values[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

def detect_regime(sizes: List[int]) -> str:
    if len(sizes) < 8: return "COLD"
    run = trailing_run(sizes)
    alt = alternating_score(sizes, 10)
    bp = block_pattern(sizes, 10)
    ratio = ratio_big(sizes, 10)

    if alt >= 0.65: return "ALTERNATING"
    if bp: return "BLOCK"
    if run >= 3: return "TRAP"
    if run >= 5: return "TREND"
    if abs(ratio - 0.5) <= 0.12: return "CHOPPY"
    return "CYCLE"

def detect_trend_shift(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < 20: return {"direction": "NONE", "confidence": 0.0}
    recent = [x["size"] for x in history[-12:]]
    prev = [x["size"] for x in history[-24:-12]]
    diff = ratio_big(recent, 12) - ratio_big(prev, 12)
    direction = "SHIFT_TO_BIG" if diff > 0.22 else ("SHIFT_TO_SMALL" if diff < -0.22 else "NONE")
    return {"direction": direction, "confidence": clamp(abs(diff) * 2.5, 0.0, 1.0)}

# ============================================================
# STATE & BAYESIAN WEIGHTS
# ============================================================
class BayesianWeights:
    def __init__(self):
        self.size = {
            "Markov": 1.0, "EMA": 1.0, "DragonStreak": 1.0, "Exact": 1.0, 
            "Trend": 1.0, "Alternation": 1.0, "Block": 1.0, 
            "Run": 1.0, "Volatility": 1.0, "Bias": 1.0, "Parity": 1.0
        }
        self.color = {"Freq": 1.0, "Streak": 1.0, "Similarity": 1.0}
        
    def update(self, model_votes: Dict[str, str], actual_size: str, actual_color: str):
        for model, vote in model_votes.items():
            if model in self.size and vote in ("BIG", "SMALL"):
                if vote == actual_size:
                    self.size[model] = clamp(self.size[model] * 1.05, 0.5, 3.0)
                else:
                    self.size[model] = clamp(self.size[model] * 0.92, 0.2, 2.0)

class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.loss_streak = 0
        self.win_streak = 0
        self.weights = BayesianWeights()
        self.conf_bins = defaultdict(lambda: {"wins": 0, "total": 0})
        
    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-2000:], f)
        except Exception as e:
            log.warning(f"Cache save failed: {e}")

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data: self.history = data
        except Exception: pass

STATE = BotState()

# ============================================================
# STORAGE
# ============================================================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER, color TEXT, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, actual_color TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT, p_big REAL, calibrated_conf INTEGER,
                    consensus REAL, stake TEXT, created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_stats (
                    pattern_key TEXT PRIMARY KEY, window INTEGER, big_count INTEGER DEFAULT 0,
                    small_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0
                )
            """)

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, color, mode, logic, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["color"], p["mode"], p["logic"], json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "UNKNOWN"), json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50), p.get("consensus", 0.5), p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict: Dict[str, Dict[str, Any]]):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, color, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()

        for row in rows:
            issue, p_size, p_num, p_color, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake = row
            act = history_dict.get(str(issue)) or history_dict.get(issue)
            if act is None: continue

            act_size = "BIG" if act["size"] == 1 else "SMALL"
            act_color = get_color_from_num(act["num"])
            win = (p_size == act_size) if p_size else True

            if win and p_num == act["num"]: status = "JACK 🏆"
            elif win: status = "WIN ✅"
            else: status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?",
                    (status, act["num"], act_size, act_color, issue))

            details = json.loads(details_json) if details_json else {}
            resolved.append({
                "issue": issue, "size": p_size, "num": p_num, "color": p_color,
                "actual_num": act["num"], "actual_size": act_size, "actual_color": act_color,
                "status": status, "mode": mode, "win": win, "raw_conf": raw_conf,
                "regime": regime, "details": details, "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5, "calibrated_conf": cal_conf or 50,
                "consensus": consensus or 0.5, "stake": stake or "1x"
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, color, status FROM predictions
                WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

    def update_pattern_memory(self, sizes: List[int], actual_size: int):
        for window in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(sizes) < window: continue
            seq = tuple(sizes[-window:])
            key = f"{window}:{json.dumps(list(seq))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)", (key, window, time.time()))
                    row = (0, 0, 0)
                big_count, small_count, total_count = row
                if actual_size == 1: big_count += 1
                else: small_count += 1
                total_count += 1
                c.execute("UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?",
                    (big_count, small_count, total_count, time.time(), key))

    def get_exact_pattern_probability(self, sizes: List[int]) -> float:
        matches = []
        for window in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(sizes) < window: continue
            key = f"{window}:{json.dumps(list(sizes[-window:]))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
            if row and row[2] > 0: matches.append({"p_big": row[0] / row[2], "count": int(row[2])})
        if not matches: return 0.5
        total = sum(m["count"] for m in matches)
        return sum(m["p_big"] * m["count"] for m in matches) / total

STORE = Storage(CFG.db_path)

# ============================================================
# INDIVIDUAL MODELS
# ============================================================
def model_markov(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if len(sizes) < 4: return 0.5
    seq = tuple(sizes[-2:]) 
    transitions = {0: 0, 1: 0}
    for i in range(len(sizes) - 3, -1, -1):
        if tuple(sizes[i:i+2]) == seq:
            transitions[sizes[i+2]] += 1
    total = transitions[0] + transitions[1]
    if total == 0: return 0.5
    return transitions[1] / total 

def model_ema(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history[-15:]]
    ema = exponential_moving_average(sizes, alpha=0.4)
    if ema > 0.55: return 0.85 
    if ema < 0.45: return 0.15 
    return ema

def model_dragon_streak(history: List[Dict[str, Any]]) -> Tuple[float, str]:
    sizes = [x["size"] for x in history]
    current = sizes[-1]
    streak = trailing_run(sizes)
    if streak >= 3: return (0.95 if current == 1 else 0.05), f"Dragon Streak (x{streak})"
    if streak == 2: return (0.80 if current == 1 else 0.20), "Double Repeat (x2)"
    return 0.5, "None"

def model_trend(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    run = trailing_run(sizes)
    if run >= 5: return 1.0 if sizes[-1] == 1 else 0.0
    if run >= 3: return 1.0 if sizes[-1] == 0 else 0.0
    return 0.5

def model_alternation(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    return 1.0 - sizes[-1] if alternating_score(sizes, 12) >= 0.6 else 0.5

def model_block(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    return 1.0 - sizes[-1] if block_pattern(sizes, 10) else 0.5

def model_run(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    return 1.0 if sizes[-1] == 1 else 0.0 if trailing_run(sizes) >= 3 else 0.5

def model_volatility(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    return 1.0 - sizes[-1] if volatility(sizes, 20) > 0.5 else 0.5

def model_bias(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    ratio = ratio_big(sizes, 20)
    return (1.0 if ratio >= 0.5 else 0.0) if abs(ratio - 0.5) > 0.18 else 0.5

def model_parity(history: List[Dict[str, Any]]) -> float:
    nums = [x["num"] for x in history[-30:]]
    if not nums: return 0.5
    even = sum(1 for n in nums if n % 2 == 0)
    return 1.0 if even > (len(nums) - even) else 0.0

# ============================================================
# ADVANCED ENSEMBLE
# ============================================================
def ensemble_size_probability(history: List[Dict[str, Any]]) -> Tuple[float, Dict[str, str]]:
    sizes = [x["size"] for x in history]
    streak_p, streak_name = model_dragon_streak(history)
    
    base = {
        "Markov": model_markov(history),
        "EMA": model_ema(history),
        "DragonStreak": streak_p,
        "Exact": STORE.get_exact_pattern_probability(sizes),
        "Trend": model_trend(history),
        "Alternation": model_alternation(history),
        "Block": model_block(history),
        "Run": model_run(history),
        "Volatility": model_volatility(history),
        "Bias": model_bias(history),
        "Parity": model_parity(history)
    }

    votes = {k: ("BIG" if v > 0.5 else "SMALL") for k, v in base.items()}

    weighted_sum = 0.0
    total_weight = 0.0
    for name, prob in base.items():
        w = STATE.weights.size.get(name, 1.0)
        weighted_sum += prob * w
        total_weight += w

    final_p_big = weighted_sum / total_weight

    shift = detect_trend_shift(history)
    if shift["direction"] == "SHIFT_TO_BIG": final_p_big = clamp(final_p_big + 0.08, 0.05, 0.95)
    elif shift["direction"] == "SHIFT_TO_SMALL": final_p_big = clamp(final_p_big - 0.08, 0.05, 0.95)

    return clamp(final_p_big, 0.05, 0.95), votes

def ensemble_color_probability(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [get_color_from_num(x["num"]) for x in history[-30:]]
    red_prob = colors.count("RED") / len(colors) if colors else 0.5
    return {"RED": red_prob, "GREEN": 1.0 - red_prob}

def choose_best_number(valid_nums: List[int], history: List[Dict[str, Any]]) -> int:
    if not valid_nums: return random.randint(0, 9)
    nums = [x["num"] for x in history[-250:]]
    freq = Counter(nums)
    score = {}
    for n in valid_nums:
        s = freq.get(n, 0) * 3.0
        if nums and nums[-1] == n: s *= 0.7
        if n in (1, 3, 7, 9): s += 1.2
        if n in (2, 4, 6, 8): s += 1.0
        score[n] = s
    return max(score, key=score.get)

def calculate_kelly_stake(prob: float, odds: float = 1.0) -> Tuple[str, int]:
    if prob <= 0.5: return "0.25x", 56
    edge = (prob * 2) - 1
    if edge > 0.35: return "3x", 95
    if edge > 0.25: return "2x", 85
    if edge > 0.15: return "1.5x", 75
    if edge > 0.05: return "1x", 65
    return "0.5x", 60

# ============================================================
# FINAL PREDICTION
# ============================================================
def generate_prediction() -> Optional[Dict[str, Any]]:
    if len(STATE.history) < CFG.min_history:
        return None 

    history = STATE.history[-2000:]
    sizes = [x["size"] for x in history]

    p_big, votes = ensemble_size_probability(history)
    p_small = 1.0 - p_big
    color_probs = ensemble_color_probability(history)

    if p_big > p_small:
        final_size, final_color = "BIG", ""
        valid_nums = [n for n in range(10) if n >= 5]
        best_conf = p_big
    else:
        final_size, final_color = "SMALL", ""
        valid_nums = [n for n in range(10) if n < 5]
        best_conf = p_small

    if votes["DragonStreak"] == "BIG" and p_big < 0.6:
        final_size, best_conf = "BIG", 0.75
        valid_nums = [n for n in range(10) if n >= 5]
    elif votes["DragonStreak"] == "SMALL" and p_small < 0.6:
        final_size, best_conf = "SMALL", 0.75
        valid_nums = [n for n in range(10) if n < 5]

    best_num = choose_best_number(valid_nums, history)

    stake, confidence = calculate_kelly_stake(best_conf)

    pattern = "ULTRA ENSEMBLE (Markov+EMA+Bayes)"
    if votes.get("DragonStreak") == final_size:
        _, streak_name = model_dragon_streak(history)
        if streak_name != "None":
            pattern = f"{streak_name} + Ensemble"

    issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue, "size": final_size, "num": best_num, "color": final_color, "winner": final_size,
        "p_big": round(p_big, 4), "size_conf": round(best_conf, 4), "color_conf": 0.5,
        "confidence": confidence, "raw_conf": confidence, "consensus": best_conf, "mode": "ULTRA-ADV",
        "regime": detect_regime(sizes), "logic": f"Models Consensus: {final_size}", "pattern": pattern,
        "sigs": [{"name": k, "dir": 1 if v == "BIG" else 0} for k, v in votes.items()], "stake": stake,
        "details": {
            "winner": final_size, "best_conf": best_conf, "p_big": p_big, "votes": votes,
            "regime": detect_regime(sizes), "trend_shift": detect_trend_shift(history),
            "loss_streak": STATE.loss_streak
        },
    }

# ============================================================
# LIVE LEARNING
# ============================================================
def learn_from_resolution(res: dict):
    if res["win"]: STATE.loss_streak = 0
    else: STATE.loss_streak += 1

    if "votes" in res.get("details", {}):
        STATE.weights.update(res["details"]["votes"], res["actual_size"], res["actual_color"])

    actual_size = 1 if res["actual_size"] == "BIG" else 0
    STORE.update_pattern_memory([x["size"] for x in STATE.history], actual_size)

    STATE.save_cache()

# ============================================================
# API PARSING & FETCH
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None: return []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict) and "list" in payload["data"]: items = payload["data"]["list"]
        else: items = []
    else: items = []

    out, seen = [], set()
    for item in items:
        try:
            issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
            if issue is None or str(issue) in seen: continue
            seen.add(str(issue))
            num = safe_int(item.get("number", item.get("value", 0)), 0)
            if not (0 <= num <= 9): continue
            out.append({"issue": str(issue), "num": num, "size": 1 if num >= 5 else 0})
        except Exception: continue
    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data(session):
    urls = CFG.api_urls()
    if not urls: return False
    last_good = [x for x in STATE.history]
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout), headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status == 200:
                    payload = await r.json(content_type=None)
                    parsed = parse_history_payload(payload)
                    if parsed:
                        STATE.history = parsed[-1000:]
                        STATE.save_cache()
                        return True
        except Exception: pass
    if last_good: STATE.history = last_good; return True
    return False

# ============================================================
# FORMATTING & TELEGRAM
# ============================================================
def format_prediction(p, recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n" for r in recent_rows[-10:])
    stake_emoji = {"3x": "🚀 3x", "2x": "🔥 2x", "1.5x": "🟢 1.5x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x", "0.25x": "🧯 0.25x"}.get(p["stake"], "1x")

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Winner</b>: <b>{h(p['winner'])}</b>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>

🧠 <b>Pattern</b>: {h(p.get('pattern', 'N/A'))}
📊 <b>Size Conf</b>: <b>{p['size_conf']*100:.1f}%</b>
💰 <b>Kelly Stake</b>: <b>{stake_emoji}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: <b>{h(res['size'])} {h(res['num'])}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(app, text, retries=3):
    for attempt in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"); return True
        except Exception: await asyncio.sleep(1)
    return False

async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_data(session):
                hist_dict = {str(x["issue"]): x for x in STATE.history}
                for res in STORE.resolve_pending(hist_dict):
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                if STATE.history:
                    latest_issue = STATE.history[-1]["issue"]
                    next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)
                    
                    if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            await send_msg(app, format_prediction(p, STORE.recent_outcomes(10)))
                        last_processed_issue = latest_issue
            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop error: %s", e); await asyncio.sleep(2)

# ============================================================
# MAIN
# ============================================================
async def main():
    CFG.validate()
    STATE.load_cache()
    
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    async with app:
        await app.start()
        me = await app.bot.get_me()
        log.info("Bot verified successfully: %s", me.username)
        
        await start_web_server()
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
