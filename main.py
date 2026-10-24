"""
V900 RHYTHM-MASTER
==================
- Rhythm Detector: Identifies Zig-Zag (B-S-B-S) and Block (B-B-S-S) patterns.
- Double-Loss Contrarian: Flips logic if bot loses twice in a row to dodge traps.
- Strict No-Trap: Disables trend following after 3+ streaks to avoid house traps.
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
    db_path: str = os.environ.get("DB_PATH", "predictor_v900.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V900 RHYTHM-MASTER"
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
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v900")

def h(x: Any) -> str:
    return html.escape(str(x))

# =========================
# AI & RHYTHM DETECTORS
# =========================
def detect_rhythm(sizes):
    """Detects if the market is in ZigZag (BSBS) or Block (BBSS) rhythm."""
    if len(sizes) < 10: return None
    last_10 = sizes[-10:]
    
    # Check Zig-Zag (Alternating)
    is_zigzag = True
    for i in range(1, len(last_10)):
        if last_10[i] == last_10[i-1]:
            is_zigzag = False
            break
    if is_zigzag:
        return {"dir": 1 - sizes[-1], "conf": 75, "name": "ZigZag-Rhythm"}

    # Check Block 2-2 (BBSS)
    if last_10[-1] == last_10[-2] and last_10[-3] != last_10[-2]:
        return {"dir": 1 - sizes[-1], "conf": 65, "name": "BlockBreak-2"}
        
    return None

def agent_anti_trap(sizes):
    """If 3+ streak, predict the break."""
    if len(sizes) < 20: return None
    last = sizes[-1]
    run = 1
    for i in range(len(sizes)-2, -1, -1):
        if sizes[i] == last: run += 1
        else: break
        
    if run == 3:
        # 3 streak usually breaks (Trap)
        return {"dir": 1 - last, "conf": 65, "name": "AntiTrap-3"}
    elif run >= 5:
        # 5 streak is a strong trend, continue
        return {"dir": last, "conf": 70, "name": "Dragon-5"}
    return None

def agent_safe_trend(sizes):
    if len(sizes) < 5: return None
    last = sizes[-1]
    run = 1
    for i in range(len(sizes)-2, -1, -1):
        if sizes[i] == last: run += 1
        else: break
    if run == 1 or run == 2: 
        return {"dir": last, "conf": 58, "name": "SafeTrend"}
    return None

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
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.win_streak = 0

STATE = BotState()

# =========================
# PREDICTOR
# =========================
def generate_prediction():
    if len(STATE.history) < CFG.min_history: return None
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    
    # Get agents
    a_rhythm = detect_rhythm(sizes)
    a_anti_trap = agent_anti_trap(sizes)
    a_safe_trend = agent_safe_trend(sizes)
    
    agents = [
        ("RhythmAgent", a_rhythm),
        ("TrapAgent", a_anti_trap),
        ("TrendAgent", a_safe_trend)
    ]
    
    active_agents = [(name, pred) for name, pred in agents if pred is not None]
    
    # 🔥 DOUBLE LOSS CONTRARIAN OVERRIDE
    # If bot lost 2 times, it assumes the house is tricking it. Flip the signal.
    if STATE.loss_streak >= 2 and active_agents:
        # Reverse the first available signal
        name, pred = active_agents[0]
        active_agents = [("ContrarianAgent", {"dir": 1 - pred["dir"], "conf": 70, "name": "Contrarian-Flip"})]
        mode_override = "CHOPPY"
    else:
        mode_override = "DYNAMIC"

    if not active_agents:
        # Smart fallback: Follow last 3 majority
        last_3 = sizes[-3:]
        fallback = 1 if sum(last_3) >= 2 else 0
        active_agents.append(("FallbackAgent", {"dir": fallback, "conf": 55, "name": "Fallback"}))

    # Calculate combined direction (Majority Vote)
    votes_big = sum(1 for _, p in active_agents if p["dir"] == 1)
    votes_small = sum(1 for _, p in active_agents if p["dir"] == 0)
    
    pred_dir = 1 if votes_big > votes_small else 0
    
    # Confidence calculation
    total_votes = votes_big + votes_small
    p_big = votes_big / total_votes if total_votes > 0 else 0.5

    stake = "1x"
    if STATE.loss_streak >= 2: stake = "0.5x"
    elif STATE.win_streak >= 2: stake = "2x"

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    freq = Counter(nums[-15:])
    max_freq = max([freq[x] for x in pool]) if freq else 0
    best_nums = [x for x in pool if freq[x] == max_freq]
    num = random.choice(best_nums)

    issue = str(int(STATE.history[-1]["issue"]) + 1)
    active_signals = [f"{name} ({'BIG' if p['dir']==1 else 'SMALL'})" for name, p in active_agents]

    return {
        "issue": issue, "size": "BIG" if pred_dir == 1 else "SMALL", "num": num,
        "p_big": p_big, "confidence": int(50 + abs(p_big - 0.5)*100), "raw_conf": int(50 + abs(p_big - 0.5)*100),
        "consensus": max(p_big, 1-p_big), "mode": mode_override, "regime": mode_override,
        "logic": ", ".join(active_signals), "sigs": [{"name": s, "dir": pred_dir} for s in active_signals],
        "stake": stake, "details": {"agents": {a: p for a, p in active_agents}}
    }

def learn_from_resolution(res: dict):
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

🧠 <b>Mode</b>: <b>{h(p['mode'])}</b>
🎲 <b>P(BIG)</b>: <b>{p['p_big']*100:.1f}%</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

🔎 <b>Active Agents</b>:
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

async def handle_ping(request): return web.Response(text="V900 is running!")

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
