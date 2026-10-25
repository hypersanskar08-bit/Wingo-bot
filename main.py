"""
V916 MAX PROBABILITY (Pick Highest of 4)
========================================
- Calculates absolute probability for BIG, SMALL, RED, and GREEN.
- Directly picks the option with the highest probability among these 4.
- Only RED and GREEN are considered (No Violet).
- Recent Outcomes shows exactly what was predicted.
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
from collections import Counter, defaultdict
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
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_maxprob.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 30.0
    bot_name: str = "V916 MAX PROB"
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
# LOGGING & WEB SERVER
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("v916")

def h(x: Any) -> str:
    return html.escape(str(x))

async def health_check(request):
    return web.Response(text="V916 Bot is Running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Web Server started on port {port} (Render Health Check OK)")

# ============================================================
# HELPERS & COLOR LOGIC (RED/GREEN ONLY)
# ============================================================
def get_color_from_num(num: int) -> str:
    if num in (0, 2, 4, 6, 8): return "RED"
    if num in (1, 3, 5, 7, 9): return "GREEN"
    return "UNKNOWN"

def get_num_colors_list(num: int) -> List[str]:
    if num in (0, 2, 4, 6, 8): return ["RED"]
    if num in (1, 3, 5, 7, 9): return ["GREEN"]
    return []

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

def majority_dir(values: List[int], window: int = 6) -> int:
    if not values: return 1
    arr = values[-window:]
    return 1 if sum(arr) >= (len(arr) / 2) else 0

def alternating_score(values: List[int], window: int = 12) -> float:
    if len(values) < 2: return 0.0
    arr = values[-window:]
    changes = 0
    for i in range(1, len(arr)):
        if arr[i] != arr[i - 1]: changes += 1
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

# ============================================================
# STATE & CACHE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.loss_streak = 0
        self.win_streak = 0

    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-1000:], f)
        except Exception as e:
            log.warning(f"Failed to save cache: {e}")

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        self.history = data
                        log.info(f"Loaded {len(self.history)} records from local cache.")
        except Exception as e:
            log.warning(f"Failed to load cache: {e}")

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
                    pattern_key TEXT PRIMARY KEY, window INTEGER, big_count INTEGER DEFAULT 0, small_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0
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
            
            win_size = (p_size == act_size) if p_size else True
            win_color = (p_color == act_color) if p_color else True
            win = win_size and win_color

            if win and p_num == act["num"]: status = "JACK 🏆"
            elif win: status = "WIN ✅"
            else: status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?", (status, act["num"], act_size, act_color, issue))

            resolved.append({
                "issue": issue, "size": p_size, "num": p_num, "color": p_color, "actual_num": act["num"],
                "actual_size": act_size, "actual_color": act_color, "status": status, "mode": mode, "win": win,
                "raw_conf": raw_conf, "regime": regime,
                "details": json.loads(details_json) if details_json else {},
                "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5,
                "calibrated_conf": cal_conf or 50, "consensus": consensus or 0.5, "stake": stake or "1x"
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, color, status 
                FROM predictions 
                WHERE status!='PENDING' 
                ORDER BY CAST(issue AS INTEGER) DESC 
                LIMIT ?
            """, (limit,)).fetchall()
        
        if not rows and STATE.history:
            fallback = []
            for x in STATE.history[-limit:]:
                sz = "BIG" if x["size"] == 1 else "SMALL"
                col = get_color_from_num(x["num"])
                fallback.append((x["issue"], sz, col, "PAST"))
            return fallback
            
        return rows[::-1]

    def update_pattern_memory(self, sizes: List[int], actual_size: int):
        for window in [3, 5, 8, 12, 20, 30, 50, 100]:
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
                c.execute("UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?", (big_count, small_count, total_count, time.time(), key))

    def get_exact_pattern_probability(self, sizes: List[int]) -> float:
        matches = []
        for window in [3, 5, 8, 12, 20, 30, 50, 100]:
            if len(sizes) < window: continue
            seq = tuple(sizes[-window:])
            key = f"{window}:{json.dumps(list(seq))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
            if row and row[2] > 0:
                matches.append({"p_big": row[0] / row[2], "count": int(row[2])})
        if not matches: return 0.5
        total_count = sum(m["count"] for m in matches)
        return sum(m["p_big"] * m["count"] for m in matches) / total_count

STORE = Storage(CFG.db_path)

# ============================================================
# PREDICTION LOGIC
# ============================================================
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

def model_volatility(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    return 1.0 - sizes[-1] if volatility(sizes, 20) > 0.5 else 0.5

def model_bias(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    ratio = ratio_big(sizes, 20)
    if abs(ratio - 0.5) > 0.18: return 1.0 if ratio >= 0.5 else 0.0
    return 0.5

def ensemble_probability(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    sizes = [x["size"] for x in history]
    scores = [
        ("Exact", STORE.get_exact_pattern_probability(sizes)),
        ("Trend", model_trend(history)),
        ("Alternation", model_alternation(history)),
        ("Block", model_block(history)),
        ("Volatility", model_volatility(history)),
        ("Bias", model_bias(history)),
    ]
    weights = {"Exact": 0.35, "Trend": 0.15, "Alternation": 0.15, "Block": 0.10, "Volatility": 0.10, "Bias": 0.15}
    regime = detect_regime(sizes)
    if regime == "TRAP": weights["Exact"] += 0.10; weights["Alternation"] += 0.10
    if regime == "TREND": weights["Trend"] += 0.15
    if regime == "ALTERNATING": weights["Alternation"] += 0.15
    if regime == "BLOCK": weights["Block"] += 0.12

    total_weight = sum(weights.values())
    final = sum(score * weights.get(name, 0.10) for name, score in scores) / total_weight
    return {"p_big": clamp(final, 0.05, 0.95)}

def calculate_color_probs(history: List[Dict[str, Any]]) -> Dict[str, float]:
    recent_colors = []
    for x in history[-20:]:
        n = x["num"]
        if n in (0, 2, 4, 6, 8): recent_colors.append("RED")
        else: recent_colors.append("GREEN")

    if not recent_colors:
        return {"RED": 0.5, "GREEN": 0.5}

    last_c = recent_colors[-1]
    streak = 1
    for c in reversed(recent_colors[:-1]):
        if c == last_c: streak += 1
        else: break

    last_10 = recent_colors[-10:]
    red_freq = last_10.count("RED") / 10.0
    green_freq = last_10.count("GREEN") / 10.0

    p_red, p_green = 0.5, 0.5

    if streak >= 3:
        if last_c == "RED": p_green += 0.20
        else: p_red += 0.20

    if red_freq > 0.7: p_green += 0.15
    elif green_freq > 0.7: p_red += 0.15

    total = p_red + p_green
    p_red /= total
    p_green /= total

    return {"RED": p_red, "GREEN": p_green}

def pick_best_number(valid_nums: List[int], history: List[Dict[str, Any]]) -> int:
    if not valid_nums: return random.randint(0, 9)
    nums = [x["num"] for x in history[-250:]]
    freq = Counter(nums)
    score = {}
    
    for n in valid_nums:
        s = freq.get(n, 0) * 2.5
        if nums and nums[-1] == n: s *= 0.5
        if n in (1, 3, 7, 9, 2, 6, 8): s += 1.0
        score[n] = s
        
    return max(score, key=score.get)

def generate_prediction() -> Optional[Dict[str, Any]]:
    if len(STATE.history) < CFG.min_history:
        issue = str(int(STATE.history[-1]["issue"]) + 1) if STATE.history else "1"
        num = random.randint(0, 9)
        return {
            "issue": issue, "size": "BIG" if num >=5 else "SMALL", "num": num, "color": "",
            "p_big": 0.5, "size_conf": 0.5, "color_conf": 0.5,
            "confidence": 55, "raw_conf": 55, "consensus": 0.5,
            "mode": "FALLBACK", "regime": "COLD", "logic": "Fallback",
            "sigs": [{"name": "Fallback", "dir": 1 if num>=5 else 0}], "stake": "0.25x",
            "details": {"fallback": True},
        }

    history = STATE.history[-2000:]
    sizes = [x["size"] for x in history]
    
    # 1. Analyze Size
    p_big = ensemble_probability(history)["p_big"]
    p_small = 1.0 - p_big
    
    # 2. Analyze Color
    color_probs = calculate_color_probs(history)
    p_red = color_probs["RED"]
    p_green = color_probs["GREEN"]

    # 3. Find the ABSOLUTE HIGHEST probability among all 4
    candidates = {
        "BIG": p_big,
        "SMALL": p_small,
        "RED": p_red,
        "GREEN": p_green
    }
    
    best_choice = max(candidates, key=candidates.get)
    final_conf = candidates[best_choice]

    valid_nums = []
    
    # 4. Setup Final Prediction based on Winner
    if best_choice in ("BIG", "SMALL"):
        final_size = best_choice
        final_color = "" 
        valid_nums = [n for n in range(10) if ("BIG" if n >= 5 else "SMALL") == final_size]
        size_pred = final_size
        color_pred = ""
    else:
        final_size = "" 
        final_color = best_choice
        valid_nums = [n for n in range(10) if get_color_from_num(n) == final_color]
        size_pred = ""
        color_pred = final_color

    # 5. Pick Number
    best_num = pick_best_number(valid_nums, history)

    # Confidence & Stake
    if final_conf > 0.65:
        confidence, stake = 75, "1x"
    elif final_conf > 0.55:
        confidence, stake = 65, "0.5x"
    else:
        confidence, stake = 55, "0.25x"

    issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)
    
    return {
        "issue": issue, 
        "size": size_pred, 
        "num": best_num, 
        "color": color_pred,
        "p_big": round(p_big, 4), 
        "size_conf": round(max(p_big, p_small), 4),
        "color_conf": round(max(p_red, p_green), 4),
        "confidence": confidence, 
        "raw_conf": confidence, 
        "consensus": max(p_big, 1 - p_big), 
        "mode": "NORMAL", 
        "regime": detect_regime(sizes), 
        "logic": "Max Prob", 
        "sigs": [{"name": "Max Logic", "dir": 1 if (size_pred == "BIG" or best_num>=5) else 0}],
        "stake": stake, 
        "details": {"loss_streak": STATE.loss_streak}
    }

def learn_from_resolution(res: dict):
    if res["win"]:
        STATE.loss_streak = 0
        STATE.win_streak += 1
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    if STATE.history:
        actual_size = 1 if res["actual_size"] == "BIG" else 0
        STORE.update_pattern_memory([x["size"] for x in STATE.history], actual_size)

# ============================================================
# API & TELEGRAM
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None: return []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            items = payload["data"].get("list", [])
        elif "data" in payload and isinstance(payload["data"], list):
            items = payload["data"]
        else:
            items = payload.get("result", payload.get("items", payload.get("history", [])))
    else: items = []

    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None or str(issue) in seen: continue
        seen.add(str(issue))
        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if 0 <= num <= 9:
            out.append({"issue": str(issue), "num": num, "size": 1 if num >= 5 else 0})
            
    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data(session):
    urls = CFG.api_urls()
    for url in urls:
        for retry in range(5):
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                    if r.status == 429:
                        await asyncio.sleep(min((retry + 1) * 2, 10))
                        continue
                    if r.status != 200: break
                    try: payload = await r.json(content_type=None)
                    except: break
                    parsed = parse_history_payload(payload)
                    if parsed:
                        STATE.history = parsed[-1000:]
                        STATE.save_cache()
                        return True
                    break
            except asyncio.TimeoutError:
                await asyncio.sleep((retry + 1) * 3)
            except Exception:
                await asyncio.sleep((retry + 1) * 3)

    if STATE.history: return True
    return False

def format_prediction(p, recent_rows):
    recent_lines = []
    for r in recent_rows:
        issue, p_size, p_color, status = r
        
        if p_size and p_size != "":
            recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> <b>{h(p_size)}</b> → <b>{h(status)}</b>")
        elif p_color and p_color != "":
            recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> <b>{h(p_color)}</b> → <b>{h(status)}</b>")
        else:
            recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> <b>{h(status)}</b>")

    recent_text = "\n".join(recent_lines) + "\n" if recent_lines else "No past outcomes\n"
    stake_emoji = {"2x": "🔥 2x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x", "0.25x": "🧯 0.25x"}.get(p["stake"], "1x")

    info_lines = []
    if p.get('size') and p.get('size') != "":
        info_lines.append(f"📊 <b>Size</b>: <b>{h(p['size'])}</b> ({p['size_conf']*100:.1f}%)")
    if p.get('color') and p.get('color') != "":
        info_lines.append(f"🎨 <b>Color</b>: <b>{h(p['color'])}</b> ({p['color_conf']*100:.1f}%)")
    
    info_str = "\n".join(info_lines)

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
{info_str}

🧠 <b>Mode</b>: <b>{h(p['mode'])}</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    if res['size'] and res['size'] != "": 
        pred_str = f"{res['size']} {res['num']}"
    elif res['color'] and res['color'] != "": 
        pred_str = f"{res['color']} {res['num']}"
    else:
        pred_str = f"Num {res['num']}"
        
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: <b>{h(pred_str)}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(app, text, retries=3):
    for attempt in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except Exception:
            await asyncio.sleep(1)
    return False

async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_data(session):
                hist_dict = {str(x["issue"]): x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    p = generate_prediction()
                    if p:
                        STORE.save_prediction(p)
                        await send_msg(app, format_prediction(p, STORE.recent_outcomes(12)))
                    last_processed_issue = latest_issue
                    
            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            await asyncio.sleep(2)

async def main():
    CFG.validate()
    STATE.load_cache()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    async with app:
        await app.start()
        try:
            await app.bot.get_me()
        except Exception:
            return

        await start_web_server()

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
