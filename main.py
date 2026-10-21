import asyncio
import contextlib
import gc
import html
import logging
import math
import os
import signal
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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
    bot_name: str = "V101 ULTIMATE HYBRID"

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
log = logging.getLogger("v101_hybrid")

def h(x: Any) -> str:
    return html.escape(str(x))

def safe_mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

# =========================
# STORAGE (Fixed)
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
                    created_at REAL
                )
            """)

    def prediction_exists(self, issue):
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
        return row is not None

    def save_prediction(self, issue, size, num):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, created_at) VALUES (?, ?, ?, ?)", 
                      (issue, size, num, time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num FROM predictions WHERE status='PENDING'").fetchall()
            
        for issue, p_size, p_num in rows:
            if issue in history_dict:
                act = history_dict[issue]
                act_size_str = "BIG" if act['size'] == 1 else "SMALL"
                
                # Strict Matching Logic
                win = (p_size == act_size_str)
                if win:
                    status = "JACK 🎰" if p_num == act['num'] else "WIN ✅"
                else:
                    status = "LOSS ❌"
                
                with self._conn() as c:
                    c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", 
                              (status, act['num'], act_size_str, issue))
                resolved.append({"issue": issue, "size": p_size, "num": p_num, "actual_num": act['num'], "actual_size": act_size_str, "status": status})
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            # Strict Actual Result Fetch (Integer Cast for sorting)
            rows = c.execute("""
                SELECT issue, actual_size, actual_num, status 
                FROM predictions 
                WHERE status!='PENDING' 
                ORDER BY CAST(issue AS INTEGER) DESC 
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1] # Return Oldest -> Newest

STORE = Storage(CFG.db_path)

# =========================
# PREDICTION ENGINE
# =========================
class UltimatePredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0

    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200: return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])
                
                # Deduplicate & Sort
                items.sort(key=lambda x: int(x["issueNumber"]))
                items = items[-300:] # Last 300
                
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
        except Exception as e:
            log.error(f"Fetch error: {e}")
            return False

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        prob_big, prob_small = 0.5, 0.5
        logic = []

        # 1. BAYESIAN PATTERN
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
                pb = (big_f + 1) / (t + 2)
                ps = (small_f + 1) / (t + 2)
                weight = min(0.35, k * 0.03)
                prob_big += (pb - 0.5) * weight * 2
                prob_small += (ps - 0.5) * weight * 2
                logic.append(f"P{k}")
                break

        # 2. ORDER-3 MARKOV
        if len(sizes) > 3:
            state = tuple(sizes[-3:])
            trans = Counter()
            for i in range(len(sizes)-3):
                if tuple(sizes[i:i+3]) == state: trans[sizes[i+3]] += 1
            if trans:
                total_t = sum(trans.values())
                pb = (trans.get(1, 0) + 1) / (total_t + 2)
                prob_big += (pb - 0.5) * 0.3
                prob_small += ((1-pb) - 0.5) * 0.3
                logic.append("M3")

        # 3. MEAN REVERSION
        big_count = sizes[-20:].count(1)
        if big_count >= 15:
            prob_small += 0.15; logic.append("MR-S")
        elif big_count <= 5:
            prob_big += 0.15; logic.append("MR-B")

        # 4. LIVE STREAK
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
        if streak >= 4:
            if sizes[-1] == 1: prob_big += 0.25
            else: prob_small += 0.25
            logic.append(f"S{streak}")
            
        # 5. ANTI-LOSS
        if self.loss_streak >= 2:
            if sizes[-1] == 1: prob_small += 0.2
            else: prob_big += 0.2
            logic.append("ANTI-L")

        # Final Probability
        total = prob_big + prob_small
        p_big = prob_big / total
        pred_size = 1 if p_big >= 0.5 else 0
        confidence = int(max(p_big, 1 - p_big) * 100)
        confidence = min(92, max(52, confidence))

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

    def analyze_market(self):
        if len(self.history) < 50: return "Not enough data."
        sizes = [x["size"] for x in self.history[-120:]]
        short = safe_mean(sizes[-12:])
        long = safe_mean(sizes[-48:])
        delta = short - long
        
        regime = "STABLE 🟢"
        if abs(delta) >= 0.20: regime = "STRONG SHIFT 🔴"
        elif abs(delta) >= 0.10: regime = "MODERATE SHIFT 🟡"
        
        trend = "UP (BIG)" if delta > 0 else "DOWN (SMALL)" if delta < 0 else "FLAT"
        
        return f"📊 <b>Market Analytics</b>\nRegime: {regime}\nTrend: {trend}\nShort Rate: {short*100:.1f}%\nLong Rate: {long*100:.1f}%"

PREDICTOR = UltimatePredictor()

# =========================
# FORMATTING (Strict Actual Result)
# =========================
def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        issue = r[0][-4:]
        act_size = r[1] or "?"
        act_num = r[2] if r[2] is not None else "?"
        status = r[3]
        # Explicitly show "Actual:"
        recent_text += f"➜ <code>{h(issue)}</code> Actual: {h(act_size)} {h(act_num)} → <b>{h(status)}</b>\n"

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Logic</b>: {h(p['logic'])}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V101 ULTIMATE HYBRID</i>
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
    await update.message.reply_text("🤖 V101 Ultimate Hybrid Active!\nPredictions + Analytics.")

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent_outcomes(15)
    if not rows:
        return await update.message.reply_text("No history yet.")
    
    text = "🧾 <b>Recent Outcomes</b>\n\n"
    for r in rows:
        issue = r[0][-4:]
        act_size = r[1] or "?"
        act_num = r[2] if r[2] is not None else "?"
        status = r[3]
        text += f"<code>{h(issue)}</code> Actual: {h(act_size)} {h(act_num)} → <b>{h(status)}</b>\n"
    await update.message.reply_text(text, parse_mode="HTML")

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info("Worker loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                # 1. Resolve pending
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']: PREDICTOR.loss_streak += 1
                    else: PREDICTOR.loss_streak = 0
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                # 2. Generate new prediction
                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if not STORE.prediction_exists(next_issue):
                    p = PREDICTOR.generate_prediction()
                    if p:
                        STORE.save_prediction(p['issue'], p['size'], p['num'])
                        recent = STORE.recent_outcomes(12)
                        await send_msg(app, format_prediction(p, recent))
                        log.info(f"Predicted for {p['issue']}")
            
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
    
    # Web Server Fix for Render
    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Dummy web server running on port {port}")

    app = Application.builder().token(CFG.telegram_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("history", cmd_history))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        
        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())








