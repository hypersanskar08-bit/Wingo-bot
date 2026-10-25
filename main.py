"""
V916 HYPER PRO PREDICTOR (DEEP UPGRADE: NO SKIP + STRICT STAKE)
======================================================================
- NO SKIP: Predicts every period.
- Dynamic Stake: Low confidence = Low stake (0.5x).
- Strict Recovery: Recovery mode limits max stake to 1x.
- Anti-Trap Logic: Reverses prediction on 5+ streak.
- Fixed: 95% fake confidence (capped at 85%)
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
# CONFIG
# ============================================================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo"
    chat_id_raw: str = "1264164655"
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_hyper_fixed.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "V916 HYPER PRO"
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
# LOGGING
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916_hyper_fixed.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("v916")

def h(x: Any) -> str:
    return html.escape(str(x))

# ============================================================
# HEALTH CHECK FOR RENDER
# ============================================================
async def health_check(request):
    return web.Response(text="V916 Hyper Pro Deep Upgraded is Running")

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
# HELPERS
# ============================================================
def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def normalize_issue(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        if value.isdigit():
            return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            return int(digits)
    try:
        return int(value)
    except Exception:
        return None

def get_color_for_logic(num: int) -> str:
    if num in (0, 2, 4, 6, 8):
        return "RED"
    return "GREEN"

def trailing_run(seq: List[int]) -> int:
    if not seq:
        return 0
    last = seq[-1]
    c = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            c += 1
        else:
            break
    return c

def ratio_big(seq: List[int], window: int = 20) -> float:
    if not seq:
        return 0.5
    arr = seq[-window:]
    return sum(arr) / len(arr)

def alternating_score(seq: List[int], window: int = 12) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    changes = sum(1 for i in range(1, len(arr)) if arr[i] != arr[i - 1])
    return changes / max(1, len(arr) - 1)

def block_pattern(seq: List[int], window: int = 10) -> bool:
    if len(seq) < 4:
        return False
    arr = seq[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i + 4]
        if a == b and c == d and a != c:
            return True
    return False

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8:
        return "COLD"

    run = trailing_run(seq)
    alt = alternating_score(seq, 10)
    bp = block_pattern(seq, 10)
    ratio = ratio_big(seq, 10)

    if alt >= 0.65:
        return "ALTERNATING"
    if bp:
        return "BLOCK"
    if run >= 5: # Trend check before Trap
        return "TREND"
    if run >= 3:
        return "TRAP"
    if abs(ratio - 0.5) <= 0.12:
        return "CHOPPY"
    return "CYCLE"

# ============================================================
# BOT STATE & DB
# ============================================================
class BayesianWeights:
    def __init__(self):
        self.size = {"Markov": 1.0, "EMA": 1.0, "Streak": 1.0, "Trend": 1.0, "Alternation": 1.0, "Bias": 1.0, "ReverseTrap": 1.0}
        self.color = {"ColorMarkov": 1.0, "ColorStreak": 1.0, "ColorFreq": 1.0, "ColorAlternation": 1.0, "ReverseTrap": 1.0}

    def adjust(self, winner: str, actual_side: str, is_size: bool):
        if is_size:
            for name in self.size: self.size[name] = clamp(self.size[name] * (1.08 if winner == actual_side else 0.92), 0.25, 3.0)
        else:
            for name in self.color: self.color[name] = clamp(self.color[name] * (1.08 if winner == actual_side else 0.92), 0.25, 3.0)

class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.weights = BayesianWeights()
        self.loss_streak = 0
        self.win_streak = 0
        self.recovery_mode = False

    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-2000:], f)
        except Exception: pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list): self.history = data
        except Exception: pass

STATE = BotState()

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
                    issue TEXT PRIMARY KEY, pred_type TEXT, size TEXT, num INTEGER, color TEXT,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT,
                    actual_color TEXT, logic TEXT, details TEXT, stake TEXT, created_at REAL
                )
            """)
            c.execute("CREATE TABLE IF NOT EXISTS pattern_stats (pattern_key TEXT PRIMARY KEY, window INTEGER, big_count INTEGER DEFAULT 0, small_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0)")
            c.execute("CREATE TABLE IF NOT EXISTS color_stats (color TEXT PRIMARY KEY, red_count INTEGER DEFAULT 0, green_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0)")

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, pred_type, size, num, color, logic, details, stake, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p["issue"], p["type"], p.get("size", ""), p.get("num", 0), p.get("color", ""), p.get("logic", "N/A"), json.dumps(p.get("details", {})), p.get("stake", "1x"), time.time()))

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, pred_type, size, num, color, details, stake FROM predictions WHERE status='PENDING'").fetchall()

        for row in rows:
            issue, pred_type, p_size, p_num, p_color, details_json, stake = row
            act = history_by_issue.get(str(issue))
            if not act: continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            actual_color = get_color_for_logic(act["num"])

            win_size = (p_size == actual_size) if p_size else True
            win_color = (p_color == actual_color) if p_color else True
            win = win_size and win_color

            if win and p_num == act["num"]: status = "JACK 🏆"
            elif win: status = "WIN ✅"
            else: status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?", (status, act["num"], actual_size, actual_color, issue))

            resolved.append({
                "issue": issue, "type": pred_type or "SIZE", "size": p_size, "num": p_num, "color": p_color, "stake": stake,
                "actual_num": act["num"], "actual_size": actual_size, "actual_color": actual_color, "status": status, "win": win,
                "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 8):
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, color, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def update_pattern_memory(self, seq: List[int], actual_size: int):
        for w in [5, 6, 7, 8, 9, 10]:
            if len(seq) < w: continue
            key = f"{w}:{json.dumps(list(seq[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)", (key, w, time.time()))
                    row = (0, 0, 0)
                b, s, total = row
                if actual_size == 1: b += 1
                else: s += 1
                c.execute("UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?", (b, s, total+1, time.time(), key))

    def update_color_memory(self, colors: List[str], actual_color: str):
        for w in [5, 6, 7, 8, 9, 10]:
            if len(colors) < w: continue
            key = f"{w}:{json.dumps(list(colors[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT red_count, green_count, total_count FROM color_stats WHERE color=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO color_stats (color, red_count, green_count, total_count, last_seen) VALUES (?, 0, 0, 0, ?)", (key, time.time()))
                    row = (0, 0, 0)
                r, g, total = row
                if actual_color == "RED": r += 1
                else: g += 1
                c.execute("UPDATE color_stats SET red_count=?, green_count=?, total_count=?, last_seen=? WHERE color=?", (r, g, total+1, time.time(), key))

    def get_pattern_prob(self, seq: List[int]) -> float:
        matches = []
        for w in [5, 6, 7, 8, 9, 10]:
            if len(seq) < w: continue
            key = f"{w}:{json.dumps(list(seq[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
            if row and row[2] > 0: matches.append((row[0] / row[2], row[2]))
        if not matches: return 0.5
        return sum(p * cnt for p, cnt in matches) / sum(cnt for _, cnt in matches)

    def get_color_prob(self, colors: List[str]) -> Dict[str, float]:
        matches = []
        for w in [5, 6, 7, 8, 9, 10]:
            if len(colors) < w: continue
            key = f"{w}:{json.dumps(list(colors[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT red_count, green_count, total_count FROM color_stats WHERE color=?", (key,)).fetchone()
            if row and row[2] > 0: matches.append((row[0] / row[2], row[1] / row[2], row[2]))
        if not matches: return {"RED": 0.5, "GREEN": 0.5}
        total = sum(cnt for _, _, cnt in matches)
        return {"RED": sum(r * cnt for r, _, cnt in matches) / total, "GREEN": sum(g * cnt for _, g, cnt in matches) / total}

STORE = Storage(CFG.db_path)

# ============================================================
# MODELS
# ============================================================
def model_markov_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if len(sizes) < 3: return 0.5
    seq = tuple(sizes[-2:]); count = 0; big = 0
    for i in range(len(sizes) - 2):
        if tuple(sizes[i:i + 2]) == seq:
            count += 1
            if sizes[i + 2] == 1: big += 1
    return big / count if count > 0 else 0.5

def model_ema_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history[-20:]]
    if not sizes: return 0.5
    ema = sizes[0]; alpha = 0.35
    for v in sizes[1:]: ema = alpha * v + (1 - alpha) * ema
    return clamp(ema, 0.05, 0.95)

def model_streak_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]; run = trailing_run(sizes)
    if run >= 3: return 0.95 if sizes[-1] == 1 else 0.05
    if run == 2: return 0.80 if sizes[-1] == 1 else 0.20
    return 0.5

def model_alternation_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]; alt = alternating_score(sizes, 12)
    return 1.0 - sizes[-1] if alt >= 0.6 else 0.5

def compute_size_score(history: List[Dict[str, Any]]) -> float:
    seq = [x["size"] for x in history]
    base = {
        "Markov": model_markov_size(history),
        "EMA": model_ema_size(history),
        "Streak": model_streak_size(history),
        "Alternation": model_alternation_size(history),
        "Exact": STORE.get_pattern_prob(seq)
    }

    # STRICT RECOVERY MODE LOGIC
    if STATE.recovery_mode:
        base["EMA"] = 0.5
        base["Exact"] = clamp(0.5 + (base["Exact"] - 0.5) * 0.5, 0.05, 0.95) 

    weights = {k: STATE.weights.size.get(k, 1.0) for k in base}
    weighted = sum(v * weights[k] for k, v in base.items())
    return clamp(weighted / sum(weights.values()), 0.05, 0.95)

def compute_color_score(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [get_color_for_logic(x["num"]) for x in history]
    exact = STORE.get_color_prob(colors)
    base = {"ColorMarkov": 0.5, "ExactRed": exact["RED"], "ExactGreen": exact["GREEN"]}
    
    if STATE.recovery_mode:
        base["ExactRed"] = 0.5
        base["ExactGreen"] = 0.5

    red_score = (base["ExactRed"] * 1.2) / 2.2
    green_score = (base["ExactGreen"] * 1.2) / 2.2
    return {"RED": clamp(red_score, 0.05, 0.95), "GREEN": clamp(green_score, 0.05, 0.95)}

def choose_final_winner(history: List[Dict[str, Any]]):
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    size_p = compute_size_score(history)
    color_p = compute_color_score(history)

    # ANTI-TRAP LOGIC
    run = trailing_run(seq)
    if run >= 5:
        size_p = 0.20 if seq[-1] == 1 else 0.80 
    elif regime == "ALTERNATING":
        size_p = 1.0 - seq[-1] 

    size_edge = abs(size_p - 0.5)
    color_edge = abs(max(color_p["RED"], color_p["GREEN"]) - 0.5)

    if color_edge > size_edge + 0.04:
        pred_type = "COLOR"
        winner = "RED" if color_p["RED"] >= color_p["GREEN"] else "GREEN"
        conf = max(color_p["RED"], color_p["GREEN"])
    else:
        pred_type = "SIZE"
        winner = "BIG" if size_p > 0.5 else "SMALL"
        conf = size_p if winner == "BIG" else 1.0 - size_p

    return pred_type, winner, conf

def choose_number_for_winner(winner: str, history: List[Dict[str, Any]]) -> int:
    if winner == "BIG": valid = [n for n in range(10) if n >= 5]
    elif winner == "SMALL": valid = [n for n in range(10) if n < 5]
    elif winner == "RED": valid = [n for n in range(10) if get_color_for_logic(n) == "RED"]
    else: valid = [n for n in range(10) if get_color_for_logic(n) == "GREEN"]

    nums = [x["num"] for x in history[-250:]]
    freq = Counter(nums)
    score = {n: freq.get(n, 0) * 3.0 for n in valid}
    return max(score, key=score.get) if score else random.choice(valid)

def generate_prediction() -> Optional[Dict[str, Any]]:
    if len(STATE.history) < CFG.min_history: return None

    history = STATE.history[-2000:]
    pred_type, winner, conf = choose_final_winner(history)
    
    # NO SKIP LOGIC - Always predicts, just manages stake
    num = choose_number_for_winner(winner, history)
    
    # DYNAMIC STAKE MANAGEMENT
    if pred_type == "SIZE":
        size_value, color_value = winner, ""
        if STATE.recovery_mode:
            stake = "1x" if conf >= 0.60 else "0.5x"
        else:
            stake = "3x" if conf > 0.80 else "2x" if conf > 0.70 else "1x" if conf > 0.60 else "0.5x"
    else:
        size_value, color_value = "", winner
        if STATE.recovery_mode:
            stake = "1x" if conf >= 0.60 else "0.5x"
        else:
            stake = "2x" if conf > 0.80 else "1x" if conf > 0.60 else "0.5x"

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)
    conf_display = int(clamp(conf * 100, 0, 85)) # Cap at 85% to avoid fake 95%

    return {
        "issue": issue, "type": pred_type, "size": size_value, "color": color_value, "num": num,
        "raw_conf": conf_display, "regime": detect_regime([x["size"] for x in history]),
        "logic": f"{pred_type}:{winner}; recovery={STATE.recovery_mode}", "stake": stake,
        "details": {"size_p": compute_size_score(history), "recovery_mode": STATE.recovery_mode}
    }

def learn_from_resolution(res: Dict[str, Any]):
    if STATE.history:
        STORE.update_pattern_memory([x["size"] for x in STATE.history], 1 if res["actual_size"] == "BIG" else 0)
        STORE.update_color_memory([get_color_for_logic(x["num"]) for x in STATE.history], get_color_for_logic(res["actual_num"]))

    if res["win"]:
        STATE.win_streak += 1
        STATE.loss_streak = 0
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    STATE.recovery_mode = STATE.loss_streak >= 2
    STATE.save_cache()

# ============================================================
# PARSING & FETCHING
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    items = []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict) and "list" in payload["data"]: items = payload["data"]["list"]
        elif "result" in payload: items = payload["result"]
    
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None: continue
        if str(issue) in seen: continue
        seen.add(str(issue))
        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if 0 <= num <= 9:
            out.append({"issue": str(issue), "num": num, "size": 1 if num >= 5 else 0})
    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data():
    urls = CFG.api_urls()
    for url in urls:
        try:
            timeout = aiohttp.ClientTimeout(total=CFG.request_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as r:
                    if r.status == 200:
                        parsed = parse_history_payload(await r.json(content_type=None))
                        if parsed:
                            STATE.history = parsed[-1000:]
                            STATE.save_cache()
                            return True
        except Exception:
            pass
    return bool(STATE.history)

# ============================================================
# FORMATTING & TELEGRAM
# ============================================================
def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n" for r in recent_rows[-6:])
    target_line = f"📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>" if pred["type"] == "SIZE" else f"🎨 <b>Target Color</b>: <b>{h(pred['color'])}</b>"
    rec_text = " (Recovery ON)" if pred["details"].get("recovery_mode") else ""

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
{target_line}
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Pattern</b>: {h(pred.get('logic', 'N/A'))}{rec_text}
💡 <b>Mode</b>: <b>{h(pred['type'])}</b>
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res: Dict[str, Any]):
    pred_str = f"{res['size']} {res['num']}" if res["type"] == "SIZE" else f"{res['color']} {res['num']}"
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'])}</code>\nPred: <b>{h(pred_str)}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(bot, text: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await asyncio.wait_for(bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"), timeout=CFG.request_timeout)
            return True
        except Exception:
            await asyncio.sleep(1)
    return False

# ============================================================
# WORKER LOOP
# ============================================================
async def worker_loop(app):
    last_processed_issue = None
    while True:
        try:
            if await asyncio.wait_for(fetch_data(), timeout=CFG.request_timeout + 5):
                history_by_issue = {str(x["issue"]): x for x in STATE.history}
                for res in STORE.resolve_pending(history_by_issue):
                    learn_from_resolution(res)
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = generate_prediction()
                    if pred: # Will always be true now (unless history is empty)
                        STORE.save_prediction(pred)
                        await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(6)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop exception: %s", e)
            await asyncio.sleep(2)

async def main():
    CFG.validate()
    STATE.load_cache()
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    await app.initialize()
    await app.start()
    log.info("Bot started: %s", (await app.bot.get_me()).username)
    await start_web_server()
    asyncio.create_task(worker_loop(app))
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
