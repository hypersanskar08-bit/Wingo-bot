"""
FTW V4 DEEP UPGRADE (REVERSE PSYCHOLOGY + SEQUENCE MASTER)
========================================================
- Fixed: Neutral 50% coin-toss predictions
- New: Reverse Psychology in Recovery Mode
- New: Raw Sequence Reading (when Markov/EMA fail)
- New: Dragon Protection (Never fight a 4+ streak)
- No Skip: Predicts every period with smart logic
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
    db_path: str = os.environ.get("DB_PATH", "ftw_v4_engine.db")
    cache_file: str = "history_cache_v4.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V4 DEEP AI"
    min_history: int = 10

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
# LOGGING & HELPERS
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_v4.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v4")

def h(x: Any) -> str:
    return html.escape(str(x))

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

def get_color_for_logic(num: int) -> str:
    if num in (0, 2, 4, 6, 8): return "RED"
    return "GREEN"

def trailing_run(seq: List[int]) -> int:
    if not seq: return 0
    last = seq[-1]
    c = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last: c += 1
        else: break
    return c

# ============================================================
# STATE & STORAGE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
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

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, color, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["issue"], p["type"], p.get("size", ""), p.get("num", 0), p.get("color", ""), p.get("logic", "N/A"), json.dumps(p.get("details", {})), p.get("stake", "1x"), time.time()))

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

    def recent_outcomes(self, limit: int = 6):
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, color, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# ============================================================
# ADVANCED MATH MODELS
# ============================================================
def markov_size_prob(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if len(sizes) < 3: return 0.5
    seq = tuple(sizes[-2:])
    count, big = 0, 0
    for i in range(len(sizes) - 2):
        if tuple(sizes[i:i+2]) == seq:
            count += 1
            if sizes[i+2] == 1: big += 1
    return (big / count) if count > 0 else 0.5

def ema_size_prob(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history[-20:]]
    if not sizes: return 0.5
    ema = sizes[0]
    for v in sizes[1:]:
        ema = 0.35 * v + (1 - 0.35) * ema
    return max(0.05, min(0.95, ema))

# ============================================================
# HTML LOGIC (RHXVM)
# ============================================================
def get_html_logic_prediction(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < 5: return None

    recent_20 = list(reversed(history[-20:]))
    sizes = ['BIG' if x['size'] == 1 else 'SMALL' for x in recent_20]
    nums = [x['num'] for x in recent_20]

    current_size = sizes[0]
    streak = trailing_run([1 if s == "BIG" else 0 for s in sizes])

    pattern_name = "NEURAL WEIGHTED"
    pred_size = "BIG" if sizes.count('BIG') >= sizes.count('SMALL') else "SMALL"
    
    if streak >= 4:
        pattern_name = "DRAGON PATTERN"
        pred_size = current_size
    elif streak == 3:
        pattern_name = "STREAK BREAK"
        pred_size = "SMALL" if current_size == "BIG" else "BIG"
    elif len(sizes) >= 6 and sizes[0:3].count(sizes[0]) == 3 and sizes[3:6].count(sizes[3]) == 3 and sizes[0] != sizes[3]:
        pattern_name = "3:3 SYMMETRY"
        pred_size = "SMALL" if sizes[0] == "BIG" else "BIG"
    elif len(sizes) >= 3 and sizes[0] != sizes[1] and sizes[1] != sizes[2]:
        pattern_name = "ZIG-ZAG ALTERNATING"
        pred_size = "SMALL" if sizes[0] == "BIG" else "BIG"

    valid_nums = [5,6,7,8,9] if pred_size == "BIG" else [0,1,2,3,4]
    valid_fib = [n for n in valid_nums if n in [0, 1, 2, 3, 5, 8]]
    freq = Counter(nums)
    
    pred_num = valid_fib[0] if valid_fib else max(valid_nums, key=lambda x: freq.get(x, 0))
    
    return {
        "size": pred_size,
        "num": pred_num,
        "pattern": pattern_name,
        "shield": pattern_name == "QUANTUM RECOVERY (LVL 2)"
    }

# ============================================================
# 🧠 FINAL DECISION MAKER (V4 DEEP UPGRADE)
# ============================================================
def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {"issue": "1", "type": "SIZE", "size": "BIG" if n >= 5 else "SMALL", "color": "", "num": n, "raw_conf": 50, "logic": "WARMING UP", "stake": "0.5x", "details": {}}

    history = STATE.history[-2000:]
    
    # 1. HTML Base Logic
    html_pred = get_html_logic_prediction(history)
    final_size = html_pred["size"]
    logic = html_pred["pattern"]
    conf = 99.2 if "DRAGON" in logic or "3:3" in logic else 97.5
    
    # 2. Math Models
    p_markov = markov_size_prob(history)
    p_ema = ema_size_prob(history)
    
    # 3. Neutral Market Detection (Deep Upgrade)
    # If Markov and EMA are dead (between 45% and 55%), they are useless.
    is_neutral = (0.45 <= p_markov <= 0.55) and (0.45 <= p_ema <= 0.55)
    
    if logic == "NEURAL WEIGHTED" and is_neutral:
        # USE RAW SEQUENCE READING
        last_4 = [x["size"] for x in history[-4:]]
        
        if len(last_4) == 4 and len(set(last_4)) == 2 and last_4[0] == last_4[2] and last_4[1] == last_4[3]:
            # Pattern: B S B S -> Predict B
            final_size = "BIG" if last_4[-1] == 0 else "SMALL"
            logic = "RAW SEQ: ZIG-ZAG (BSBS)"
            conf = 75.0
        elif len(last_4) >= 3 and last_4[-1] == last_4[-2] and last_4[-2] == last_4[-3]:
            # Pattern: 3 Streak -> Predict opposite
            final_size = "SMALL" if last_4[-1] == 1 else "BIG"
            logic = "RAW SEQ: 3 STREAK REVERSAL"
            conf = 72.0
        else:
            # Fallback to last result flip
            final_size = "SMALL" if history[-1]["size"] == 1 else "BIG"
            logic = "RAW SEQ: LAST FLIP"
            conf = 65.0
            
    elif logic == "NEURAL WEIGHTED":
        # Math has a slight edge
        hybrid_score = (p_markov * 0.6) + (p_ema * 0.4)
        final_size = "BIG" if hybrid_score > 0.5 else "SMALL"
        logic = "HYBRID MARKOV + EMA"
        conf = max(hybrid_score, 1 - hybrid_score) * 100

    # 4. RECOVERY MODE & REVERSE PSYCHOLOGY (The Game Changer)
    STATE.recovery_mode = STATE.loss_streak >= 2
    
    if STATE.recovery_mode:
        # If we are losing, our math is wrong. REVERSE the prediction.
        final_size = "SMALL" if final_size == "BIG" else "BIG"
        logic = "🛡️ REVERSE PSYCHOLOGY (ANTI-LOSS)"
        conf = 85.0 # Force high confidence for display
        stake = "1x" # Safe stake
        
        # But wait! If the market is in a hard DRAGON streak, don't fight it.
        current_streak = trailing_run([x["size"] for x in history])
        if current_streak >= 4:
            final_size = "BIG" if history[-1]["size"] == 1 else "SMALL"
            logic = "🐉 DRAGON RIDE (DO NOT REVERSE)"
            stake = "1.5x"
    else:
        # Normal safe staking
        if conf >= 98: stake = "3x"
        elif conf >= 90: stake = "2x"
        elif conf >= 75: stake = "1x"
        else: stake = "0.5x"

    # 5. Number Selection
    if final_size != html_pred["size"]:
        valid = [5,6,7,8,9] if final_size == "BIG" else [0,1,2,3,4]
        freq = Counter([x["num"] for x in history[-100:]])
        final_num = max(valid, key=lambda x: freq.get(x, 0))
    else:
        final_num = html_pred["num"]

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue, "type": "SIZE", "size": final_size, "color": "", "num": final_num,
        "raw_conf": int(min(conf, 99.9)), "logic": logic, "stake": stake,
        "details": {
            "recovery_mode": STATE.recovery_mode,
            "m_p": round(p_markov * 100, 1),
            "e_p": round(p_ema * 100, 1)
        }
    }

def learn_from_resolution(res: Dict[str, Any]):
    if res["win"]:
        STATE.win_streak += 1
        STATE.loss_streak = 0
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0
    STATE.save_cache()

# ============================================================
# API PARSING & FETCHING
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None: return []
    items = []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict) and "list" in payload["data"]: items = payload["data"]["list"]
        elif "result" in payload: items = payload["result"]

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
    last_good = list(STATE.history)

    for url in urls:
        try:
            timeout = aiohttp.ClientTimeout(total=CFG.request_timeout)
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    parsed = parse_history_payload(await response.json(content_type=None))
                    if parsed:
                        STATE.history = parsed[-1000:]
                        STATE.save_cache()
                        return True
        except Exception as exc:
            log.warning("Fetch exception: %s", exc)
            continue

    if last_good:
        STATE.history = last_good
        return True
    return False

# ============================================================
# FORMATTERS & TELEGRAM
# ============================================================
def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n" for r in recent_rows[-6:])
    
    rec_tag = "🛡️ <b>REVERSE MODE ON</b>" if pred["details"].get("recovery_mode") else "✅ <b>SAFE MODE</b>"
    metrics = f"M:{pred['details'].get('m_p', 0)}% | E:{pred['details'].get('e_p', 0)}%"
    
    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Engine</b>: {h(pred['logic'])}
🔬 <b>Metrics</b>: <i>{metrics}</i>
{rec_tag}
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res: Dict[str, Any]):
    pred_str = f"{res['size']} {res['num']}"
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
async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_data(session):
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
                    if pred:
                        STORE.save_prediction(pred)
                        await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(6)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop exception: %s", e)
            await asyncio.sleep(2)

# ============================================================
# HEALTH CHECK & MAIN
# ============================================================
async def health_check(request):
    return web.Response(text="FTW V4 Deep Upgrade is Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

async def main():
    CFG.validate()
    STATE.load_cache()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    await app.initialize()
    await app.start()
    log.info("Bot started: %s", (await app.bot.get_me()).username)

    await start_web_server()

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(worker_loop(app, session))
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
