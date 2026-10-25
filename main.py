"""
V916 FIXED STATE-BASED PREDICTOR
================================
- Fixed Render Health Check (Web Server added)
- Fixed Missing Imports (aiohttp, Application)
- Added Premium Telegram Formatting & Recent Outcomes
- Token & Chat ID added as default fallback
"""

import asyncio
import gc
import json
import os
import random
import sqlite3
import time
import html
from collections import defaultdict, Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

import aiohttp
from aiohttp import web
from telegram.ext import Application

# ============================================================
# CONFIG
# ============================================================
@dataclass
class Config:
    tg_token: str = os.environ.get("TG_TOKEN", "8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM")
    tg_chat: str = os.environ.get("TG_CHAT", "1264164655")
    api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_state_predictor.db")
    cache_file: str = "history_cache.json"
    fetch_every: float = 1.0
    timeout: float = 30.0
    min_history: int = 50
    bot_name: str = "V916 STATE PREDICTOR"

CFG = Config()

def h(x: Any) -> str:
    return html.escape(str(x))

# ============================================================
# DUMMY WEB SERVER (RENDER FIX)
# ============================================================
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
    print(f"Web Server started on port {port} (Render Health Check OK)")

# ============================================================
# HELPERS
# ============================================================
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def normalize_issue(x):
    if x is None: return None
    if isinstance(x, str):
        x = x.strip()
        if not x: return None
        if x.isdigit(): return int(x)
        digits = "".join(ch for ch in x if ch.isdigit())
        if digits: return int(digits)
    try: return int(x)
    except Exception: return None

def get_color(num):
    if num in (0, 2, 4, 6, 8): return "RED"
    if num in (1, 3, 5, 7, 9): return "GREEN"
    return "UNKNOWN"

def size_of_num(num):
    return 1 if num >= 5 else 0

# ============================================================
# PATTERN MATH
# ============================================================
def trailing_run(seq: List[int]):
    if not seq: return 0
    last = seq[-1]
    c = 1
    for i in range(len(seq)-2, -1, -1):
        if seq[i] == last: c += 1
        else: break
    return c

def ratio_big(seq: List[int], window=20):
    if not seq: return 0.5
    arr = seq[-window:]
    return sum(arr) / len(arr)

def alternation(seq: List[int], window=12):
    if len(seq) < 2: return 0.0
    arr = seq[-window:]
    changes = 0
    for i in range(1, len(arr)):
        if arr[i] != arr[i-1]: changes += 1
    return changes / max(1, len(arr)-1)

def block_pattern(seq: List[int], window=10):
    if len(seq) < 4: return False
    arr = seq[-window:]
    for i in range(len(arr)-3):
        a, b, c, d = arr[i:i+4]
        if a == b and c == d and a != c: return True
    return False

def detect_regime(seq: List[int]):
    if len(seq) < 8: return "COLD"
    run = trailing_run(seq)
    alt = alternation(seq, 10)
    bp = block_pattern(seq, 10)
    ratio = ratio_big(seq, 10)

    if alt >= 0.65: return "ALTERNATING"
    if bp: return "BLOCK"
    if run >= 3: return "TRAP"
    if run >= 5: return "TREND"
    if abs(ratio - 0.5) <= 0.12: return "CHOPPY"
    return "CYCLE"

# ============================================================
# STATE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.last_predicted = None

    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-2000:], f)
        except Exception:
            pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data
        except Exception:
            pass

STATE = BotState()

# ============================================================
# SQLITE STORAGE
# ============================================================
class Storage:
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_db()

    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=20)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init_db(self):
        with self.conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER, color TEXT, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, actual_color TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT, created_at REAL, stake TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_stats (
                    pattern_key TEXT PRIMARY KEY, window INTEGER, big_count INTEGER DEFAULT 0, small_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS color_stats (
                    color TEXT PRIMARY KEY, red_count INTEGER DEFAULT 0, green_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0
                )
            """)

    def save_prediction(self, p):
        with self.conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, color, mode, logic, sigs, raw_conf, regime, details, created_at, stake)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["color"], p["mode"], p["logic"],
                json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "UNKNOWN"),
                json.dumps(p.get("details", {})),
                time.time(), p.get("stake", "0.25x")
            ))

    def exists(self, issue):
        with self.conn() as c:
            row = c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
            return row is not None

    def update_pattern_memory(self, seq: List[int], actual_size: int):
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(seq) < w: continue
            suffix = tuple(seq[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self.conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)", (key, w, time.time()))
                    row = (0, 0, 0)
                b, s, total = row
                if actual_size == 1: b += 1
                else: s += 1
                total += 1
                c.execute("UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?", (b, s, total, time.time(), key))

    def update_color_memory(self, colors: List[str], actual_color: str):
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(colors) < w: continue
            suffix = tuple(colors[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self.conn() as c:
                row = c.execute("SELECT red_count, green_count, total_count FROM color_stats WHERE color=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO color_stats (color, red_count, green_count, total_count, last_seen) VALUES (?, 0, 0, 0, ?)", (key, time.time()))
                    row = (0, 0, 0)
                r, g, total = row
                if actual_color == "RED": r += 1
                else: g += 1
                total += 1
                c.execute("UPDATE color_stats SET red_count=?, green_count=?, total_count=?, last_seen=? WHERE color=?", (r, g, total, time.time(), key))

    def get_pattern_prob(self, seq: List[int]):
        matches = []
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(seq) < w: continue
            suffix = tuple(seq[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self.conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
            if row is not None and row[2] > 0:
                total = row[2]
                p_big = row[0] / total
                matches.append((p_big, total))
        if not matches: return 0.5
        weighted = 0.0
        total_count = 0
        for p_big, total in matches:
            weighted += p_big * total
            total_count += total
        return weighted / total_count if total_count else 0.5

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self.conn() as c:
            rows = c.execute("SELECT issue, size, num, color, mode, sigs, raw_conf, regime, details FROM predictions WHERE status='PENDING'").fetchall()
        for row in rows:
            issue, p_size, p_num, p_color, mode, sigs_json, raw_conf, regime, details_json = row
            act = history_by_issue.get(str(issue))
            if act is None: continue
            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            actual_color = get_color(act["num"])
            
            size_match = (p_size == actual_size) if p_size else True
            color_match = (p_color == actual_color) if p_color else True
            win = size_match and color_match

            status = "JACK 🏆" if win and p_num == act["num"] else ("WIN ✅" if win else "LOSS ❌")

            with self.conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?", (status, act["num"], actual_size, actual_color, issue))

            resolved.append({
                "issue": issue, "pred_size": p_size, "pred_color": p_color, "pred_num": p_num,
                "actual_num": act["num"], "actual_size": actual_size, "actual_color": actual_color,
                "win": win, "status": status,
            })
        return resolved

    def recent_outcomes(self, limit=10):
        with self.conn() as c:
            rows = c.execute("""
                SELECT issue, size, color, status
                FROM predictions
                WHERE status!='PENDING'
                ORDER BY CAST(issue AS INTEGER) DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# ============================================================
# STRONG STATE SCORING
# ============================================================
def size_score(seq: List[int], regime: str):
    exact = STORE.get_pattern_prob(seq)
    if regime == "TREND":
        p_big = exact * 1.3 + 0.2 if seq[-1] == 1 else exact * 0.7
    elif regime == "TRAP":
        p_big = exact * 0.7 + (0.25 if seq[-1] == 0 else 0.75)
    elif regime == "ALTERNATING":
        p_big = 1.0 - seq[-1]
    elif block_pattern(seq, 10):
        p_big = 1.0 - seq[-1]
    else:
        p_big = exact

    return clamp(p_big, 0.05, 0.95)

def color_score(seq_nums: List[int], regime: str):
    colors = [get_color(n) for n in seq_nums]
    red_count = colors.count("RED")
    green_count = colors.count("GREEN")
    total = max(1, len(colors))
    red_ratio = red_count / total
    green_ratio = green_count / total

    last = colors[-1]
    streak = 1
    for c in reversed(colors[:-1]):
        if c == last: streak += 1
        else: break

    if streak >= 3:
        if last == "RED": red_ratio += 0.18
        else: green_ratio += 0.18

    if len(colors) >= 3 and colors[-1] == colors[-2] == colors[-3]:
        if last == "RED": green_ratio += 0.18
        else: red_ratio += 0.18

    return {"RED": clamp(red_ratio, 0.05, 0.95), "GREEN": clamp(green_ratio, 0.05, 0.95)}

def pick_best_winner(history: List[Dict[str, Any]]):
    seq_sizes = [x["size"] for x in history]
    seq_nums = [x["num"] for x in history]
    regime = detect_regime(seq_sizes)

    p_big = size_score(seq_sizes, regime)
    p_small = 1.0 - p_big

    color_probs = color_score(seq_nums, regime)
    p_red = color_probs["RED"]
    p_green = color_probs["GREEN"]

    # anti-repeat
    if trailing_run(seq_sizes) >= 4:
        if seq_sizes[-1] == 1:
            p_small += 0.18
            p_big *= 0.8
        else:
            p_big += 0.18
            p_small *= 0.8

    if regime == "TREND":
        if seq_sizes[-1] == 1: p_big = clamp(p_big + 0.12, 0.05, 0.95)
        else: p_small = clamp(p_small + 0.12, 0.05, 0.95)

    if regime == "ALTERNATING":
        p_big = 1.0 - seq_sizes[-1]
        p_small = seq_sizes[-1]

    candidates = {"BIG": p_big, "SMALL": p_small, "RED": p_red, "GREEN": p_green}
    winner = max(candidates, key=candidates.get)
    return winner, candidates

def choose_num_from_winner(winner: str, history: List[Dict[str, Any]]):
    if winner == "BIG": valid = [n for n in range(10) if n >= 5]
    elif winner == "SMALL": valid = [n for n in range(10) if n < 5]
    elif winner == "RED": valid = [n for n in range(10) if get_color(n) == "RED"]
    else: valid = [n for n in range(10) if get_color(n) == "GREEN"]

    nums = [x["num"] for x in history[-200:]]
    freq = Counter(nums)
    score = {}
    for n in valid:
        s = freq.get(n, 0) * 3
        if nums and nums[-1] == n: s *= 0.7
        if n in (1, 3, 7, 9): s += 1.2
        if n in (2, 4, 6, 8): s += 1.0
        score[n] = s

    return max(score, key=score.get) if score else random.randint(0, 9)

def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {
            "issue": "1", "winner": "BIG" if n >= 5 else "SMALL", "size": "BIG" if n >= 5 else "SMALL",
            "num": n, "color": "", "raw_conf": 55, "mode": "FALLBACK", "logic": "fallback",
            "sigs": [{"name": "fallback"}], "regime": "COLD", "details": {}, "stake": "0.25x"
        }

    history = STATE.history[-2000:]
    winner, candidates = pick_best_winner(history)
    num = choose_num_from_winner(winner, history)
    best_conf = max(candidates.values())

    stake = "1x" if best_conf > 0.75 else ("0.5x" if best_conf > 0.62 else "0.25x")
    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    size = winner if winner in ("BIG", "SMALL") else ""
    color = winner if winner in ("RED", "GREEN") else ""

    return {
        "issue": issue, "winner": winner, "size": size, "num": num, "color": color,
        "raw_conf": int(best_conf * 100), "mode": "STATE-BASED", "logic": f"winner:{winner}",
        "sigs": [{"name": "state-base", "winner": winner}], "regime": detect_regime([x["size"] for x in history]),
        "details": {"candidates": candidates, "regime": detect_regime([x["size"] for x in history])},
        "stake": stake
    }

def learn_from_result(res):
    if not STATE.history: return
    actual_size = 1 if res["actual_size"] == "BIG" else 0
    actual_color = get_color(res["actual_num"])
    STORE.update_pattern_memory([x["size"] for x in STATE.history], actual_size)
    STORE.update_color_memory([get_color(x["num"]) for x in STATE.history], actual_color)

# ============================================================
# PARSE & FETCH
# ============================================================
def parse_history_payload(payload):
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

async def fetch_history(session):
    headers = {"User-Agent": "Mozilla/5.0"}
    for i in range(4):
        try:
            async with session.get(CFG.api_url, timeout=aiohttp.ClientTimeout(total=CFG.timeout), headers=headers) as r:
                if r.status != 200:
                    await asyncio.sleep(1)
                    continue
                payload = await r.json(content_type=None)
                parsed = parse_history_payload(payload)
                if parsed:
                    STATE.history = parsed[-1000:]
                    STATE.save_cache()
                    return True
        except Exception:
            await asyncio.sleep(1)
    
    if STATE.history: return True
    return False

# ============================================================
# TELEGRAM FORMATTING
# ============================================================
async def send_msg(bot, text, chat_id):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False

def format_prediction(pred):
    win = pred["winner"]
    conf = pred["raw_conf"]
    stake = pred["stake"]
    
    stake_emoji = {"2x": "🔥 2x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x", "0.25x": "🧯 0.25x"}.get(stake, "1x")
    
    # Recent Outcomes logic
    recent = STORE.recent_outcomes(12)
    recent_lines = []
    for r in recent:
        issue, p_size, p_color, status = r
        if p_size and p_size != "": recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> <b>{h(p_size)}</b> → <b>{h(status)}</b>")
        elif p_color and p_color != "": recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> <b>{h(p_color)}</b> → <b>{h(status)}</b>")
    
    recent_text = "\n".join(recent_lines) + "\n" if recent_lines else "No past outcomes\n"
    
    info_lines = []
    if pred.get('size'): info_lines.append(f"📊 <b>Size</b>: <b>{h(pred['size'])}</b> ({conf}%)")
    if pred.get('color'): info_lines.append(f"🎨 <b>Color</b>: <b>{h(pred['color'])}</b> ({conf}%)")
    info_str = "\n".join(info_lines)

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>
{info_str}

🧠 <b>Mode</b>: <b>{h(pred['mode'])}</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    pred_str = f"{res['pred_size']} {res['pred_num']}" if res['pred_size'] else f"{res['pred_color']} {res['pred_num']}"
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: <b>{h(pred_str)}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

# ============================================================
# BOT LOOP
# ============================================================
async def run_bot():
    # Start Web Server for Render
    await start_web_server()

    if not CFG.tg_token or not CFG.tg_chat:
        print("Set TG_TOKEN and TG_CHAT")
        return

    app = Application.builder().token(CFG.tg_token).connect_timeout(20).read_timeout(20).build()
    async with app:
        await app.start()
        try:
            await app.bot.get_me()
            print("Telegram verified. Bot running...")
        except Exception:
            print("Telegram verification failed. Check Token.")
            return

        chat_id = int(CFG.tg_chat)
        STATE.load_cache()

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    if await fetch_history(session):
                        history_by_issue = {str(x["issue"]): x for x in STATE.history}

                        # resolve pending predictions
                        pending = STORE.resolve_pending(history_by_issue)
                        for res in pending:
                            learn_from_result(res)
                            await send_msg(app.bot, format_result(res), chat_id)

                        # generate new prediction
                        last_issue = STATE.history[-1]["issue"]
                        next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)
                        
                        if not STORE.exists(next_issue):
                            pred = generate_prediction()
                            if pred:
                                STORE.save_prediction(pred)
                                await send_msg(app.bot, format_prediction(pred), chat_id)
                    gc.collect()
                    await asyncio.sleep(CFG.fetch_every)
                except Exception as e:
                    print(f"Loop Error: {e}")
                    await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(run_bot())
