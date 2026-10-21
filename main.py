import asyncio
import contextlib
import gc
import html
import logging
import math
import os
import signal
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA"
    chat_id_raw: str = "1264164655"
    admin_ids_raw: str = "1264164655"
    source_api_url: str = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"
    
    db_path: str = "predictor.db"
    log_dir: str = "logs"
    fetch_every: float = 1.5
    request_timeout: float = 5
    bot_name: str = "V103 ULTIMATE RECOVERY"

    def parse_chat_id(self):
        return int(self.chat_id_raw) if self.chat_id_raw.isdigit() else self.chat_id_raw

    def admin_ids(self):
        return set(int(x.strip()) for x in self.admin_ids_raw.split(",") if x.strip().isdigit())

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
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "bot.log", maxBytes=1_000_000, backupCount=1)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("v103_recovery")

def h(x: Any) -> str:
    return html.escape(str(x))

def safe_mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY,
                    size TEXT,
                    num INTEGER,
                    status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER,
                    actual_size TEXT,
                    logic_used TEXT,
                    created_at REAL
                )
            """)

    def prediction_exists(self, issue):
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
        return row is not None

    def save_prediction(self, issue, size, num, logic):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, logic_used, created_at) VALUES (?, ?, ?, ?, ?)", 
                      (issue, size, num, logic, time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, logic_used FROM predictions WHERE status='PENDING'").fetchall()
            
        for issue, p_size, p_num, logic in rows:
            if issue in history_dict:
                act = history_dict[issue]
                act_size_str = "BIG" if act['size'] == 1 else "SMALL"
                win = (p_size == act_size_str)
                
                if win:
                    status = "JACK 🎰" if p_num == act['num'] else "WIN ✅"
                else:
                    status = "LOSS ❌"
                
                with self._conn() as c:
                    c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", 
                              (status, act['num'], act_size_str, issue))
                resolved.append({"issue": issue, "size": p_size, "num": p_num, "actual_num": act['num'], "actual_size": act_size_str, "status": status, "logic": logic})
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, actual_size, actual_num, status 
                FROM predictions 
                WHERE status!='PENDING' 
                ORDER BY CAST(issue AS INTEGER) DESC 
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# =========================
# V103 RECOVERY ENGINE
# =========================
class RecoveryPredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.is_recovery_mode = False

    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200: return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])
                
                items.sort(key=lambda x: int(x["issueNumber"]))
                items = items[-300:] # Memory Safe
                
                out = []
                seen = set()
                for item in items:
                    try:
                        issue = str(item["issueNumber"])
                        if issue in seen: continue
                        seen.add(issue)
                        num = int(item["number"])
                        out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                    except: continue
                self.history = out
                return True
        except Exception:
            return False

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        votes = {"BIG": 0.0, "SMALL": 0.0}
        logic = []
        
        # Calculate live streak
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break

        # ==========================================
        # 🚨 RECOVERY MODE (If last round was a loss)
        # ==========================================
        if self.is_recovery_mode:
            logic.append("RECOVERY")
            
            # 1. Ride the current streak (Do not fight)
            if streak >= 2:
                if sizes[-1] == 1: votes["BIG"] += 1.5
                else: votes["SMALL"] += 1.5
                logic.append(f"R_Trend{streak}")
            
            # 2. Order-4 Markov (Deep Search)
            if len(sizes) > 4:
                state = tuple(sizes[-4:])
                trans = Counter()
                for i in range(len(sizes)-4):
                    if tuple(sizes[i:i+4]) == state: trans[sizes[i+4]] += 1
                if trans:
                    total_t = sum(trans.values())
                    pb = (trans.get(1, 0) + 1) / (total_t + 2)
                    if pb > 0.55: 
                        votes["BIG"] += 0.8
                        logic.append("R_Markov4")
                    elif pb < 0.45: 
                        votes["SMALL"] += 0.8
                        logic.append("R_Markov4")

            # 3. Fallback: Flip the last result (Revenge)
            if votes["BIG"] == 0 and votes["SMALL"] == 0:
                if sizes[-1] == 1: votes["SMALL"] += 1.0
                else: votes["BIG"] += 1.0
                logic.append("R_Flip")

        # ==========================================
        # 🧠 NORMAL MODE
        # ==========================================
        else:
            # 1. Pattern Matching
            for k in range(10, 3, -1):
                if len(sizes) < k + 1: continue
                pat = tuple(sizes[-k:])
                big_f, small_f = 0, 0
                for i in range(len(sizes) - k):
                    if tuple(sizes[i:i+k]) == pat:
                        if sizes[i+k] == 1: big_f += 1
                        else: small_f += 1
                t = big_f + small_f
                if t >= (1 if k >= 8 else 2):
                    pb = (big_f + 1) / (t + 2)
                    votes["BIG"] += pb * 0.3
                    votes["SMALL"] += (1-pb) * 0.3
                    logic.append(f"P{k}")
                    break
            
            # 2. Markov 3
            if len(sizes) > 3:
                state = tuple(sizes[-3:])
                trans = Counter()
                for i in range(len(sizes)-3):
                    if tuple(sizes[i:i+3]) == state: trans[sizes[i+3]] += 1
                if trans:
                    pb = (trans.get(1, 0) + 1) / (sum(trans.values()) + 2)
                    votes["BIG"] += pb * 0.3
                    votes["SMALL"] += (1-pb) * 0.3
                    logic.append("M3")

            # 3. Streak (Trend)
            if streak >= 3:
                if sizes[-1] == 1: votes["BIG"] += 0.4
                else: votes["SMALL"] += 0.4
                logic.append(f"S{streak}")

        # Final Probability
        total = votes["BIG"] + votes["SMALL"]
        if total == 0: return None
            
        p_big = votes["BIG"] / total
        pred_size = 1 if p_big >= 0.5 else 0
        
        # Confidence
        confidence = int(max(p_big, 1 - p_big) * 100)
        if self.is_recovery_mode: confidence += 10 # Boost confidence in recovery
        confidence = min(95, max(52, confidence))

        # TARGET NUMBER
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i
            
        freq = Counter(nums[-50:])
        best_num = pool[0]
        best_score = -1
        for n in pool:
            score = (last_seen[n] * 0.2) + (freq.get(n, 0) * 1.5)
            if score > best_score:
                best_score = score
                best_num = n

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": " + ".join(logic)
        }

PREDICTOR = RecoveryPredictor()

# =========================
# FORMATTING & SENDING
# =========================
def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        issue = r[0][-4:]
        act_size = r[1] or "?"
        act_num = r[2] if r[2] is not None else "?"
        status = r[3]
        recent_text += f"➜ <code>{h(issue)}</code> Actual: {h(act_size)} {h(act_num)} → <b>{h(status)}</b>\n"

    mode_str = "🚨 RECOVERY MODE" if PREDICTOR.is_recovery_mode else "🧠 NORMAL MODE"

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Logic</b>: {h(p['logic'])}
⚖️ <b>Status</b>: {mode_str}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V103 ULTIMATE RECOVERY</i>
"""

async def send_msg(app, text):
    try:
        await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
    except Exception as e:
        log.error(f"Send error: {e}")

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 V103 Ultimate Recovery Active!\nLoss Recovery Mode is armed.")

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info("V103 Worker loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                # 1. Resolve Pending
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']:
                        PREDICTOR.loss_streak += 1
                        PREDICTOR.is_recovery_mode = True  # 🚨 Turn on Recovery
                    else:
                        PREDICTOR.loss_streak = 0
                        PREDICTOR.is_recovery_mode = False # 🟢 Turn off Recovery
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                # 2. Generate New Prediction
                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if not STORE.prediction_exists(next_issue):
                    p = PREDICTOR.generate_prediction()
                    if p:
                        STORE.save_prediction(p['issue'], p['size'], p['num'], p['logic'])
                        recent = STORE.recent_outcomes(12)
                        await send_msg(app, format_prediction(p, recent))
                        log.info(f"Predicted {p['issue']} | Recovery: {PREDICTOR.is_recovery_mode} | {p['logic']}")
            
            gc.collect() 
            await asyncio.sleep(CFG.fetch_every)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            await asyncio.sleep(2)

# =========================
# DUMMY WEB SERVER & MAIN
# =========================
async def handle_ping(request):
    return web.Response(text="Bot is running perfectly!")

async def main():
    CFG.validate()
    
    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Dummy web server running on port {port}")

    app = Application.builder().token(CFG.telegram_token).build()
    app.add_handler(CommandHandler("start", cmd_start))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        
        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())








