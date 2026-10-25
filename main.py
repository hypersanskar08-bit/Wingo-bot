"""
V916 HYPER PRO PREDICTOR (FINAL ULTIMATE FIX)
=============================================
- Fixed KeyError: 'logic' (Database sync issue)
- Fixed KeyError: 'VIOLET' (Color logic)
- Dynamic Selection: Predicts only ONE (Size or Color) based on highest Edge.
- Strictly 1 Prediction per period.
- Token directly applied.
"""

import asyncio
import gc
import html
import json
import logging
import os
import random
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web
from telegram.ext import Application

# ============================================================
# CONFIG (TOKEN HARDCODED)
# ============================================================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM"
    chat_id_raw: str = "1264164655"
    source_api_urls: str = os.environ.get("API_URLS", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000")
    db_path: str = os.environ.get("DB_PATH", "v916_hyper.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 30.0
    bot_name: str = "V916 HYPER PRO"
    min_history: int = 50

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls if urls else ["https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"]

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# ============================================================
# LOGGING
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916_hyper.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v916")

def h(x: Any) -> str: return html.escape(str(x))

async def health_check(request):
    return web.Response(text="V916 Hyper Pro is Running!")

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
def clamp(v: float, low: float, high: float) -> float: return max(low, min(high, v))
def safe_int(x, default=0):
    try: return int(x)
    except: return default

def normalize_issue(value: Any) -> Optional[int]:
    if value is None: return None
    if isinstance(value, str):
        value = value.strip()
        if value == "": return None
        if value.isdigit(): return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits: return int(digits)
    try: return int(value)
    except: return None

def get_wingo_color(num: int) -> str:
    """For logic: 0 is Red, 5 is Green. Returns pure RED or GREEN."""
    if num in (0, 2, 4, 6, 8): return "RED"
    return "GREEN"

def get_display_color(num: int) -> str:
    """For display in Telegram: 0 and 5 are Violet"""
    if num in (0, 5): return "VIOLET"
    if num in (2, 4, 6, 8): return "RED"
    return "GREEN"

def trailing_run(values: List[Any]) -> int:
    if not values: return 0
    last, run = values[-1], 1
    for i in range(len(values) - 2, -1, -1):
        if values[i] == last: run += 1
        else: break
    return run

def ratio_big(values: List[int], window: int = 20) -> float:
    if not values: return 0.5
    return sum(values[-window:]) / len(values[-window:])

def exponential_moving_average(values: List[int], alpha: float = 0.3) -> float:
    if not values: return 0.5
    ema = values[0]
    for val in values[1:]: ema = alpha * val + (1 - alpha) * ema
    return ema

def alternating_score(values: List[Any], window: int = 12) -> float:
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

def detect_regime(sizes: List[int]) -> str:
    if len(sizes) < 8: return "COLD"
    run, alt, bp, ratio = trailing_run(sizes), alternating_score(sizes, 10), block_pattern(sizes, 10), ratio_big(sizes, 10)
    if alt >= 0.65: return "ALTERNATING"
    if bp: return "BLOCK"
    if run >= 3: return "TRAP"
    if run >= 5: return "TREND"
    if abs(ratio - 0.5) <= 0.12: return "CHOPPY"
    return "CYCLE"

# ============================================================
# WEIGHTS & STATE
# ============================================================
class BayesianWeights:
    def __init__(self):
        self.size = {"Markov": 1.0, "EMA": 1.0, "Streak": 1.0, "Trend": 1.0, "Alternation": 1.0, "Bias": 1.0}
        self.color = {"ColorMarkov": 1.0, "ColorStreak": 1.0, "ColorFreq": 1.0, "ColorAlternation": 1.0}
        
    def update(self, size_votes: Dict[str, str], color_votes: Dict[str, str], actual_size: str, actual_color: str):
        for model, vote in size_votes.items():
            if model in self.size:
                self.size[model] = clamp(self.size[model] * (1.05 if vote == actual_size else 0.92), 0.2, 3.0)
        for model, vote in color_votes.items():
            if model in self.color:
                self.color[model] = clamp(self.color[model] * (1.05 if vote == actual_color else 0.92), 0.2, 3.0)

class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.weights = BayesianWeights()
        
    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f: json.dump(self.history[-2000:], f)
        except: pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data: self.history = data
        except: pass

STATE = BotState()

# ============================================================
# STORAGE (FIXED KEY ERROR)
# ============================================================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20); c.execute("PRAGMA journal_mode=WAL"); return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, pred_type TEXT, size TEXT, num INTEGER, color TEXT, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, actual_color TEXT, logic TEXT, details TEXT, stake TEXT, created_at REAL
                )
            """)
            # Safe migration for older databases
            try: c.execute("ALTER TABLE predictions ADD COLUMN pred_type TEXT")
            except sqlite3.OperationalError: pass

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c: return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions (issue, pred_type, size, num, color, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("issue", ""), p.get("type", "SIZE"), p.get("size", ""), p.get("num", 0), 
                p.get("color", ""), p.get("pattern", "N/A"), json.dumps(p.get("details", {})), 
                p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict: Dict[str, Dict[str, Any]]):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, pred_type, size, num, color, details, stake FROM predictions WHERE status='PENDING'").fetchall()

        for row in rows:
            issue, p_type, p_size, p_num, p_color, details_json, stake = row
            act = history_dict.get(str(issue))
            if not act: continue

            act_size = "BIG" if act["size"] == 1 else "SMALL"
            act_color = get_wingo_color(act["num"]) 
            
            win_size = (p_size == act_size) if p_size else True
            win_color = (p_color == act_color) if p_color else True
            win = win_size and win_color

            if win and p_num == act["num"]: status = "JACK 🏆"
            elif win: status = "WIN ✅"
            else: status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?", (status, act["num"], act_size, act_color, issue))

            resolved.append({
                "issue": issue, "type": p_type or "SIZE", "size": p_size, "num": p_num, "color": p_color, "stake": stake,
                "actual_num": act["num"], "actual_size": act_size, "actual_color": act_color,
                "status": status, "win": win, "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 10):
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, color, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# ============================================================
# SIZE MODELS
# ============================================================
def model_markov(history):
    sizes = [x["size"] for x in history]
    if len(sizes) < 4: return 0.5
    seq, transitions = tuple(sizes[-2:]), {0: 0, 1: 0}
    for i in range(len(sizes) - 3, -1, -1):
        if tuple(sizes[i:i+2]) == seq: transitions[sizes[i+2]] += 1
    total = transitions[0] + transitions[1]
    return transitions[1] / total if total > 0 else 0.5

def model_ema(history):
    sizes = [x["size"] for x in history[-15:]]
    ema = exponential_moving_average(sizes, alpha=0.4)
    return 0.85 if ema > 0.55 else (0.15 if ema < 0.45 else ema)

def model_streak(history):
    sizes = [x["size"] for x in history]
    streak = trailing_run(sizes)
    current = sizes[-1]
    if streak >= 3: return (0.95 if current == 1 else 0.05), f"Size Streak (x{streak})"
    if streak == 2: return (0.80 if current == 1 else 0.20), "Size Double (x2)"
    return 0.5, "None"

def model_trend(history):
    sizes = [x["size"] for x in history]
    run = trailing_run(sizes)
    if run >= 5: return 1.0 if sizes[-1] == 1 else 0.0
    if run >= 3: return 1.0 if sizes[-1] == 0 else 0.0
    return 0.5

def model_alternation(history):
    sizes = [x["size"] for x in history]
    return 1.0 - sizes[-1] if alternating_score(sizes, 12) >= 0.6 else 0.5

def model_bias(history):
    sizes = [x["size"] for x in history]
    ratio = ratio_big(sizes, 20)
    return (1.0 if ratio >= 0.5 else 0.0) if abs(ratio - 0.5) > 0.18 else 0.5

# ============================================================
# COLOR MODELS
# ============================================================
def get_colors(history): return [get_wingo_color(x["num"]) for x in history]

def model_color_markov(history):
    colors = get_colors(history)
    if len(colors) < 4: return 0.5
    seq, transitions = tuple(colors[-2:]), {"RED": 0, "GREEN": 0}
    for i in range(len(colors) - 3, -1, -1):
        if tuple(colors[i:i+2]) == seq: transitions[colors[i+2]] += 1
    total = transitions["RED"] + transitions["GREEN"]
    return transitions["RED"] / total if total > 0 else 0.5

def model_color_streak(history):
    colors = get_colors(history)
    streak = trailing_run(colors)
    current = colors[-1]
    if streak >= 3: return (0.90 if current == "RED" else 0.10), f"Color Streak (x{streak})"
    return 0.5, "None"

def model_color_freq(history):
    colors = get_colors(history[-20:])
    if not colors: return 0.5
    return colors.count("RED") / len(colors)

def model_color_alternation(history):
    colors = get_colors(history)
    if alternating_score(colors, 12) >= 0.6:
        return 1.0 if colors[-1] == "GREEN" else 0.0 
    return 0.5

# ============================================================
# ENSEMBLES
# ============================================================
def ensemble_size_probability(history):
    streak_p, streak_name = model_streak(history)
    base = {"Markov": model_markov(history), "EMA": model_ema(history), "Streak": streak_p, "Trend": model_trend(history), "Alternation": model_alternation(history), "Bias": model_bias(history)}
    votes = {k: ("BIG" if v > 0.5 else "SMALL") for k, v in base.items()}
    
    weighted_sum = sum(prob * STATE.weights.size.get(name, 1.0) for name, prob in base.items())
    total_weight = sum(STATE.weights.size.get(name, 1.0) for name in base)
    final_p_big = weighted_sum / total_weight
    return clamp(final_p_big, 0.05, 0.95), votes, streak_name

def ensemble_color_probability(history):
    streak_p, streak_name = model_color_streak(history)
    base = {"ColorMarkov": model_color_markov(history), "ColorStreak": streak_p, "ColorFreq": model_color_freq(history), "ColorAlternation": model_color_alternation(history)}
    votes = {k: ("RED" if v > 0.5 else "GREEN") for k, v in base.items()}
    
    weighted_sum = sum(prob * STATE.weights.color.get(name, 1.0) for name, prob in base.items())
    total_weight = sum(STATE.weights.color.get(name, 1.0) for name in base)
    final_p_red = weighted_sum / total_weight
    return clamp(final_p_red, 0.05, 0.95), votes, streak_name

# ============================================================
# FINAL PREDICTION (FIXED LOGIC KEY)
# ============================================================
def generate_prediction() -> Optional[Dict[str, Any]]:
    if len(STATE.history) < CFG.min_history: return None 

    history = STATE.history[-2000:]
    sizes = [x["size"] for x in history]

    p_big, size_votes, size_pattern = ensemble_size_probability(history)
    p_red, color_votes, color_pattern = ensemble_color_probability(history)

    size_edge = abs(p_big - 0.5)
    color_edge = abs(p_red - 0.5)

    if color_edge > size_edge and color_edge > 0.05:
        pred_type = "COLOR"
        final_color = "RED" if p_red > 0.5 else "GREEN"
        final_size = ""
        best_conf = 0.5 + color_edge
        pattern = color_pattern if color_pattern != "None" else "Color Ensemble"
        valid_nums = [n for n in range(10) if get_wingo_color(n) == final_color]
    else:
        pred_type = "SIZE"
        final_size = "BIG" if p_big > 0.5 else "SMALL"
        final_color = ""
        best_conf = 0.5 + size_edge
        pattern = size_pattern if size_pattern != "None" else "Size Ensemble"
        valid_nums = [n for n in range(10) if (n >= 5) == (final_size == "BIG")]

    nums = [x["num"] for x in history[-250:]]
    freq = Counter(nums)
    score = {}
    for n in valid_nums:
        s = freq.get(n, 0) * 3.0
        if nums and nums[-1] == n: s *= 0.7
        score[n] = s
    best_num = max(score, key=score.get) if score else random.choice(valid_nums)

    edge = (best_conf * 2) - 1
    if edge > 0.35: stake, confidence = "3x", 95
    elif edge > 0.25: stake, confidence = "2x", 85
    elif edge > 0.15: stake, confidence = "1.5x", 75
    elif edge > 0.05: stake, confidence = "1x", 65
    else: stake, confidence = "0.5x", 60

    issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue, "type": pred_type, "size": final_size, "color": final_color, "num": best_num,
        "p_big": round(p_big, 4), "p_red": round(p_red, 4), "confidence": confidence, "stake": stake,
        "regime": detect_regime(sizes), "pattern": pattern, "logic": pattern, # <--- FIXED: Added 'logic' key
        "details": {"size_votes": size_votes, "color_votes": color_votes, "size_edge": size_edge, "color_edge": color_edge}
    }

# ============================================================
# LIVE LEARNING
# ============================================================
def learn_from_resolution(res: dict):
    details = res.get("details", {})
    if details:
        STATE.weights.update(
            details.get("size_votes", {}), 
            details.get("color_votes", {}), 
            res["actual_size"], 
            res["actual_color"]
        )
    STATE.save_cache()

# ============================================================
# API & FORMATTING
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if not payload: return []
    items = payload if isinstance(payload, list) else payload.get("data", {}).get("list", [])
    out, seen = [], set()
    for item in items:
        try:
            issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
            if not issue or str(issue) in seen: continue
            seen.add(str(issue))
            num = safe_int(item.get("number", item.get("value", 0)))
            if 0 <= num <= 9: out.append({"issue": str(issue), "num": num, "size": 1 if num >= 5 else 0})
        except: continue
    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data(session):
    urls = CFG.api_urls()
    last_good = [x for x in STATE.history]
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout), headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status == 200:
                    parsed = parse_history_payload(await r.json(content_type=None))
                    if parsed: STATE.history = parsed[-1000:]; STATE.save_cache(); return True
        except: pass
    if last_good: STATE.history = last_good; return True
    return False

def format_prediction(p, recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n" for r in recent_rows[-8:])
    stake_emoji = {"3x": "🚀 3x", "2x": "🔥 2x", "1.5x": "🟢 1.5x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x"}.get(p["stake"], "1x")
    
    if p["type"] == "SIZE":
        target_text = f"📊 <b>Target Size</b>: <b>{h(p['size'])}</b>"
    else:
        target_text = f"🎨 <b>Target Color</b>: <b>{h(p['color'])}</b>"

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
{target_text}
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>

🧠 <b>Pattern</b>: {h(p.get('pattern', 'N/A'))}
💡 <b>Mode</b>: <b>{h(p['type'])} PREDICTION</b>
📊 <b>Confidence</b>: <b>{p['confidence']}%</b>
💰 <b>Kelly Stake</b>: <b>{stake_emoji}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    actual_display_color = get_display_color(res["actual_num"])
    pred_str = f"{res['size']} {res['num']}" if res['type'] == "SIZE" else f"{res['color']} {res['num']}"
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: <b>{h(pred_str)}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])} ({h(actual_display_color)})\n<b>{h(res['status'])}</b>"

async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try: await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"); return True
        except: await asyncio.sleep(1)
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
                    latest = STATE.history[-1]["issue"]
                    next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)
                    
                    if latest != last_processed_issue and not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            await send_msg(app, format_prediction(p, STORE.recent_outcomes(8)))
                        last_processed_issue = latest
            gc.collect(); await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Error: %s", e); await asyncio.sleep(2)

async def main():
    CFG.validate(); STATE.load_cache()
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    async with app:
        await app.start()
        log.info("Bot verified: %s", (await app.bot.get_me()).username)
        await start_web_server()
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
