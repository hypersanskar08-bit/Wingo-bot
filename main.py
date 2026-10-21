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
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web # 🔥 Render Web Server
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
    fetch_every: float = 1.5 # Fast loop
    request_timeout: float = 5
    bot_name: str = "V100 HYBRID PREDICTOR"

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
log = logging.getLogger("hybrid")

def h(x: Any) -> str:
    return html.escape(str(x))

# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
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
                    created_at REAL
                )
            """)

    def save_prediction(self, issue, size, num):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO predictions (issue, size, num, created_at) VALUES (?, ?, ?, ?)", 
                      (issue, size, num, time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num FROM predictions WHERE status='PENDING'").fetchall()
            
        for issue, p_size, p_num in rows:
            if issue in history_dict:
                act = history_dict[issue]
                act_size_str = "BIG" if act['size'] == 1 else "SMALL"
                win = (p_size == act_size_str)
                status = ("JACK 🎰" if p_num == act['num'] else "WIN ✅") if win else "LOSS ❌"
                
                with self._conn() as c:
                    c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", 
                              (status, act['num'], act_size_str, issue))
                resolved.append({"issue": issue, "size": p_size, "num": p_num, "actual_num": act['num'], "actual_size": act_size_str, "status": status})
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            return c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

STORE = Storage(CFG.db_path)

# =========================
# PREDICTION ENGINE (Hybrid)
# =========================
class HybridPredictor:
    def __init__(self):
        self.history = []
        self.last_predicted_issue = None

    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200: return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])[-300:] # Last 300 only to save memory
                
                out = []
                for item in items:
                    try:
                        issue = str(item["issueNumber"])
                        num = int(item["number"])
                        out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                    except: continue
                out.sort(key=lambda x: int(x["issue"]))
                self.history = out
                return True
        except Exception:
            return False

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        prob_big = 0.5
        prob_small = 0.5
        logic = []

        # 1. Pattern Matching (12 to 4)
        for k in range(12, 3, -1):
            if len(sizes) < k + 1: continue
            pat = tuple(sizes[-k:])
            big_f, small_f = 0, 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k] == 1: big_f += 1
                    else: small_f += 1
            t = big_f + small_f
            if t >= (1 if k >= 8 else 2):
                # Bayesian Smoothing
                pb = (big_f + 1) / (t + 2)
                ps = (small_f + 1) / (t + 2)
                weight = min(0.35, k * 0.03)
                prob_big += (pb - 0.5) * weight * 2
                prob_small += (ps - 0.5) * weight * 2
                logic.append(f"P{k}({big_f}B/{small_f}S)")
                break

        # 2. Mean Reversion (Balance)
        last_20 = sizes[-20:]
        big_count = last_20.count(1)
        if big_count >= 15:
            prob_small += 0.15; logic.append("MeanRev(S)")
        elif big_count <= 5:
            prob_big += 0.15; logic.append("MeanRev(B)")

        # 3. Live Trend (Streak)
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
        
        if streak >= 3:
            if sizes[-1] == 1: prob_big += 0.2; prob_small -= 0.1
            else: prob_small += 0.2; prob_big -= 0.1
            logic.append(f"Trend{streak}x")

        # Final Probability
        total = prob_big + prob_small
        p_big = prob_big / total
        pred_size = 1 if p_big >= 0.5 else 0
        confidence = int(max(p_big, 1 - p_big) * 100)

        # Target Number
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
            "confidence": min(88, max(51, confidence)),
            "logic": " + ".join(logic[:2]) if logic else "Neutral"
        }

PREDICTOR = HybridPredictor()

# =========================
# TELEGRAM FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = "".join(
        f"➜ `{h(r[0][-4:])}` {h(r[1])} {h(r[2])} → **{h(r[3])}**\n"
        for r in recent_rows
    )
    return f"""
📌 **Period No**: `{p['issue'][-4:]}`
🎯 **Target No**: `{p['num']}`
📊 **Size**: **{p['size']}**

📈 **Confidence**: {p['confidence']}%
🧠 **Logic**: {p['logic']}

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ *V100 HYBRID PREDICTOR*
"""

async def send_msg(app, text):
    try:
        await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Send error: {e}")

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 V100 Hybrid Predictor Active!\nBot will send predictions automatically.")

async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent_outcomes(12)
    if not rows: 
        return await update.message.reply_text("No history yet.")
    
    text = "🧾 **Last 12 Outcomes**\n\n"
    for r in rows[::-1]:
        text += f"`{r[0][-4:]}` {r[1]} {r[2]} → {r[3]}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# =========================
# WORKER LOOP & SERVER
# =========================
async def worker_loop(app, session):
    log.info("Worker loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                # 1. Resolve Pending
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    text = f"🎯 **Result**\nPeriod: `{res['issue'][-4:]}`\nPred: {res['size']} {res['num']}\nAct: {res['actual_size']} {res['actual_num']}\n**{res['status']}**"
                    await send_msg(app, text)

                # 2. New Prediction
                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if PREDICTOR.last_predicted_issue != next_issue:
                    p = PREDICTOR.generate_prediction()
                    if p:
                        STORE.save_prediction(p['issue'], p['size'], p['num'])
                        PREDICTOR.last_predicted_issue = next_issue
                        
                        recent = STORE.recent_outcomes(12)
                        await send_msg(app, format_prediction(p, recent))
                        log.info(f"Predicted for {p['issue']}")
            
            gc.collect() # RAM Cleanup
            await asyncio.sleep(CFG.fetch_every)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            await asyncio.sleep(2)

async def handle_ping(request):
    return web.Response(text="Bot is running perfectly!")

async def main():
    CFG.validate()
    
    # Dummy Web Server for Render
    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server running on port {port}")

    app = Application.builder().token(CFG.telegram_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("last", cmd_last))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        
        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait() # Run forever

if __name__ == "__main__":
    asyncio.run(main())








