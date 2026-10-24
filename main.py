"""
V800 TREND-ENFORCER
===================
- Smart Fallback: Follows the majority of the last 10 results (never contradicts trend).
- Strict Override: If HMM Regime is TRENDING_SMALL, final prediction MUST be SMALL.
- Streak Recovery: Mutes complex AI agents after losses to strictly follow the trend.
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
from collections import Counter, deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.error import TimedOut, NetworkError

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAH_GCZP4vLI4M-rF8knnPAartqucT51X7U")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    webhook_url: str = os.environ.get("WEBHOOK_URL", "https://wingo-bot-mcs3.onrender.com")
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "predictor_v800.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V800 TREND-ENFORCER"
    min_history: int = 50

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# =========================
# LOGGING
# =========================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v800.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v800")

def h(x: Any) -> str:
    return html.escape(str(x))

# =========================
# AI & MATH CLASSES
# =========================
class HMMRegime:
    def __init__(self):
        self.current_state = "CHOPPY"
    def update(self, sizes):
        n = len(sizes)
        if n < 10: return self.current_state
        last = sizes[-1]
        run = 1
        for i in range(n-2, -1, -1):
            if sizes[i] == last: run += 1
            else: break
        
        # Strict regime definition
        if run >= 3:
            self.current_state = "TRENDING_BIG" if last == 1 else "TRENDING_SMALL"
        elif run <= 1:
            self.current_state = "CHOPPY"
        return self.current_state

class MetaLearner:
    def __init__(self):
        self.rewards = {
            "TrapAgent": deque(maxlen=50),
            "ZigzagAgent": deque(maxlen=50),
            "TrendAgent": deque(maxlen=50),
            "FallbackAgent": deque(maxlen=50)
        }
    def record(self, agent, correct):
        if agent in self.rewards:
            self.rewards[agent].append(1 if correct else 0)
    def get_weights(self):
        weights = {}
        for agent, history in self.rewards.items():
            if len(history) < 5:
                weights[agent] = 1.0
                continue
            acc = sum(history) / len(history)
            weights[agent] = max(0.1, min(3.0, (acc * 4) - 1))
        return weights

# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT, p_big REAL, calibrated_conf INTEGER,
                    consensus REAL, stake TEXT, created_at REAL
                )
            """)
            c.execute("CREATE TABLE IF NOT EXISTS state_kv (key TEXT PRIMARY KEY, value TEXT)")

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, mode, logic, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["mode"], p["logic"], json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "CHOPPY"), json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50), p.get("consensus", 0.5), p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()
        for row in rows:
            (issue, p_size, p_num, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake) = row
            if issue not in history_dict: continue
            act = history_dict[issue]
            act_size_str = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size_str)
            status = ("JACK 🏆" if p_num == act["num"] else "WIN ✅") if win else "LOSS ❌"
            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", (status, act["num"], act_size_str, issue))
            resolved.append({
                "issue": issue, "size": p_size, "num": p_num, "actual_num": act["num"], "actual_size": act_size_str,
                "status": status, "mode": mode, "win": win, "raw_conf": raw_conf, "regime": regime,
                "details": json.loads(details_json) if details_json else {}, "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5, "calibrated_conf": cal_conf or 50, "consensus": consensus or 0.5, "stake": stake or "1x"
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# =========================
# STATE & AGENTS
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.hmm = HMMRegime()
        self.meta = MetaLearner()
        self.loss_streak = 0
        self.win_streak = 0

STATE = BotState()

def agent_trap(sizes):
    n = len(sizes)
    if n < 30: return None
    last = sizes[-1]
    streak = 1
    for i in range(n-2, -1, -1):
        if sizes[i] == last: streak += 1
        else: break
        
    if streak < 3: return None
    
    continuations = 0
    reversals = 0
    for i in range(3, n):
        temp_streak = 1
        for j in range(i-2, -1, -1):
            if sizes[j] == sizes[i-1]: temp_streak += 1
            else: break
        if temp_streak == streak:
            if sizes[i] == last: continuations += 1
            else: reversals += 1
            
    total = continuations + reversals
    if total >= 5:
        p_rev = reversals / total
        if p_rev > 0.60: return {"dir": 1 - last, "conf": 60 + p_rev*20, "name": "TrapReversal"}
        elif p_rev < 0.40: return {"dir": last, "conf": 60 + (1-p_rev)*20, "name": "TrapContinuation"}
    return None

def agent_zigzag(sizes):
    if len(sizes) < 10: return None
    alt = True
    for i in range(len(sizes)-4, len(sizes)):
        if sizes[i] == sizes[i-1]:
            alt = False
            break
    if alt:
        return {"dir": 1 - sizes[-1], "conf": 65, "name": "Zigzag"}
    return None

def agent_trend(sizes):
    if len(sizes) < 10: return None
    last = sizes[-1]
    run = 1
    for i in range(len(sizes)-2, -1, -1):
        if sizes[i] == last: run += 1
        else: break
    
    if run == 1 or run == 2: 
        return {"dir": last, "conf": 58, "name": "SafeTrend"}
    return None

# =========================
# PREDICTOR
# =========================
def generate_prediction():
    if len(STATE.history) < CFG.min_history: return None
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    
    regime = STATE.hmm.update(sizes)
    
    a_trap = agent_trap(sizes)
    a_zigzag = agent_zigzag(sizes)
    a_trend = agent_trend(sizes)
    
    agents = [
        ("TrapAgent", a_trap),
        ("ZigzagAgent", a_zigzag),
        ("TrendAgent", a_trend)
    ]
    
    active_agents = [(name, pred) for name, pred in agents if pred is not None]
    
    # 🔥 SMART FALLBACK LOGIC
    if not active_agents:
        # Find majority in last 10
        last_10 = sizes[-10:]
        majority = 1 if sum(last_10) > 5 else 0
        # If regime says trending, override majority
        if regime == "TRENDING_SMALL": majority = 0
        if regime == "TRENDING_BIG": majority = 1
        
        active_agents.append(("FallbackAgent", {"dir": majority, "conf": 60, "name": "SmartFallback"}))

    # 🔥 LOSS STREAK OVERRIDE
    if STATE.loss_streak >= 2:
        if regime == "TRENDING_SMALL":
            active_agents = [("StrictTrendAgent", {"dir": 0, "conf": 80, "name": "StrictTrend"})]
        elif regime == "TRENDING_BIG":
            active_agents = [("StrictTrendAgent", {"dir": 1, "conf": 80, "name": "StrictTrend"})]

    weights = STATE.meta.get_weights()
    total_log_odds = 0.0
    active_signals = []
    
    for agent_name, pred in active_agents:
        w = weights.get(agent_name, 1.0)
        p = pred["conf"] / 100.0
        p = max(0.01, min(0.99, p))
        
        if pred["dir"] == 1:
            log_odds = math.log(p / (1 - p))
        else:
            log_odds = -math.log(p / (1 - p))
            
        total_log_odds += log_odds * w
        active_signals.append(f"{pred['name']} ({'BIG' if pred['dir']==1 else 'SMALL'})")
    
    if total_log_odds == 0.0:
        total_log_odds = 0.1 if sizes[-1] == 0 else -0.1

    p_big = 1 / (1 + math.exp(-total_log_odds))
    pred_dir = 1 if p_big > 0.5 else 0

    # 🔥 STRICT REGIME OVERRIDE
    # If regime is trending but edge is weak, obey regime.
    if abs(p_big - 0.5) < 0.10:
        if regime == "TRENDING_SMALL": pred_dir = 0
        elif regime == "TRENDING_BIG": pred_dir = 1

    stake = "1x"
    if STATE.loss_streak >= 2: stake = "0.5x"
    elif STATE.win_streak >= 2: stake = "2x"

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    freq = Counter(nums[-15:])
    max_freq = max([freq[x] for x in pool]) if freq else 0
    best_nums = [x for x in pool if freq[x] == max_freq]
    num = random.choice(best_nums)

    issue = str(int(STATE.history[-1]["issue"]) + 1)
    
    return {
        "issue": issue, "size": "BIG" if pred_dir == 1 else "SMALL", "num": num,
        "p_big": p_big, "confidence": int(50 + abs(p_big - 0.5)*100), "raw_conf": int(50 + abs(p_big - 0.5)*100),
        "consensus": max(p_big, 1-p_big), "mode": regime, "regime": regime,
        "logic": ", ".join(active_signals), "sigs": [{"name": s, "dir": pred_dir} for s in active_signals],
        "stake": stake, "details": {"agents": {a: p for a, p in active_agents}}
    }

def learn_from_resolution(res: dict):
    actual_size = 1 if res["actual_size"] == "BIG" else 0
    details = res.get("details") or {}
    agents = details.get("agents", {})
    
    for agent_name, pred in agents.items():
        if pred:
            STATE.meta.record(agent_name, pred["dir"] == actual_size)
            
    if res["win"]:
        STATE.loss_streak = 0; STATE.win_streak += 1
    else:
        STATE.loss_streak += 1; STATE.win_streak = 0

async def fetch_data(session):
    try:
        async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
            if r.status != 200: return False
            data = await r.json()
            items = data.get("data", {}).get("list", [])
            if not items: return False
            items.sort(key=lambda x: int(x["issueNumber"]))
            out = []
            seen = set()
            for item in items:
                try:
                    issue = str(item["issueNumber"])
                    if issue in seen: continue
                    seen.add(issue)
                    num = int(item["number"])
                    out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                except Exception: continue
            STATE.history = out
            return True
    except Exception: return False

# =========================
# FORMATTING & MAIN
# =========================
def format_prediction(p, recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n" for r in recent_rows[-12:])
    sig_text = "\n".join(f"  🕵️ {h(s['name'])}" for s in p["sigs"])
    stake_emoji = {"2x": "🔥 2x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x"}.get(p['stake'], "1x")
    
    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

🧠 <b>AI Regime</b>: <b>{h(p['regime'])}</b>
🎲 <b>P(BIG)</b>: <b>{p['p_big']*100:.1f}%</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

🔎 <b>House Trackers</b>:
{sig_text}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except (TimedOut, NetworkError): await asyncio.sleep(2)
        except Exception as e: log.error(f"Send error: {e}"); break
    return False

async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_data(session):
                hist_dict = {x["issue"]: x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)

                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            await send_msg(app, format_prediction(p, STORE.recent_outcomes(12)))
                            last_processed_issue = latest_issue
            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception(f"Loop error: {e}"); await asyncio.sleep(2)

async def handle_ping(request): return web.Response(text="V800 is running!")

async def main():
    CFG.validate()
    
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    
    async with app:
        await app.start()
        port = int(os.environ.get("PORT", 8080))
        
        if CFG.webhook_url:
            webhook_path = f"/webhook/{CFG.telegram_token}"
            await app.bot.set_webhook(url=f"{CFG.webhook_url}{webhook_path}", drop_pending_updates=True)
            web_app = web.Application()
            web_app.router.add_get("/", handle_ping)
            web_app.router.add_post(webhook_path, app.update_queue.put)
            runner = web.AppRunner(web_app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
        else:
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.updater.start_polling(drop_pending_updates=True)

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
