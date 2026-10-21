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
from collections import Counter
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

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
    request_timeout: float = 6
    bot_name: str = "V105 TRUE DEEP"

    def parse_chat_id(self):
        return int(self.chat_id_raw) if self.chat_id_raw.isdigit() else self.chat_id_raw

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# =========================
# LOGGING & STORAGE
# =========================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir)/"bot.log", maxBytes=1_000_000, backupCount=1); fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v105")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, created_at REAL
                )
            """)
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

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
                win = (p_size == act_size_str)
                status = ("JACK 🎰" if p_num == act['num'] else "WIN ✅") if win else "LOSS ❌"
                with self._conn() as c:
                    c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", 
                              (status, act['num'], act_size_str, issue))
                resolved.append({"issue": issue, "size": p_size, "num": p_num, "actual_num": act['num'], "actual_size": act_size_str, "status": status})
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# =========================
# V105 DEEP MATRIX PREDICTOR
# =========================
class DeepMatrixPredictor:
    def __init__(self):
        self.history = []

    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200: return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])
                items.sort(key=lambda x: int(x["issueNumber"])) # Oldest to Newest
                
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

    # --- ENGINE 1: Deep N-Gram Ensemble (4 to 15) ---
    def engine_ngram(self, sizes):
        probs = []
        for k in range(15, 3, -1):
            if len(sizes) < k + 1: continue
            pat = tuple(sizes[-k:])
            big_f, small_f = 0, 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k] == 1: big_f += 1
                    else: small_f += 1
            t = big_f + small_f
            if t >= 2: # Need at least 2 historical matches
                p_big = (big_f + 1) / (t + 2) # Laplace Smoothing
                probs.append((p_big, k))
                
        if not probs: return 0.5, "N-Gram(None)"
        # Weighted by length: Longer pattern = higher confidence
        total_w = sum(w for _, w in probs)
        p = sum(prob * w for prob, w in probs) / total_w
        return p, f"N-Gram(k={probs[0][1]})"

    # --- ENGINE 2: Multi-Order Markov (2 to 5) ---
    def engine_markov(self, sizes):
        probs = []
        for o in [2, 3, 4, 5]:
            if len(sizes) < o + 1: continue
            state = tuple(sizes[-o:])
            big_f, small_f = 0, 0
            for i in range(len(sizes) - o):
                if tuple(sizes[i:i+o]) == state:
                    if sizes[i+o] == 1: big_f += 1
                    else: small_f += 1
            t = big_f + small_f
            if t >= 1:
                p_big = (big_f + 1) / (t + 2)
                probs.append((p_big, o))
                
        if not probs: return 0.5, "Markov(None)"
        total_w = sum(w for _, w in probs)
        p = sum(prob * w for prob, w in probs) / total_w
        return p, f"Markov(2-5)"

    # --- ENGINE 3: SMA Crossover (Trend Shift Detector) ---
    def engine_trend_shift(self, sizes):
        if len(sizes) < 20: return 0.5, "SMA(Wait)"
        sma5 = sum(sizes[-5:]) / 5.0
        sma20 = sum(sizes[-20:]) / 20.0
        
        diff = sma5 - sma20
        # Detect shift logic
        if diff > 0.15: return 1.0, "SMA_Bullish"
        elif diff < -0.15: return 0.0, "SMA_Bearish"
        return 0.5, "SMA_Flat"

    # --- ENGINE 4: Mean Reversion (Oscillator) ---
    def engine_mean_reversion(self, sizes):
        if len(sizes) < 30: return 0.5, "MR(Wait)"
        big_count = sizes[-30:].count(1)
        if big_count >= 22: return 0.0, "MR_Overbought" # Expect SMALL
        elif big_count <= 8: return 1.0, "MR_Oversold"   # Expect BIG
        return 0.5, "MR_Neutral"

    def generate_prediction(self):
        if len(self.history) < 100: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        # Run all deep engines
        p_ngram, l_ngram = self.engine_ngram(sizes)
        p_markov, l_markov = self.engine_markov(sizes)
        p_sma, l_sma = self.engine_trend_shift(sizes)
        p_mr, l_mr = self.engine_mean_reversion(sizes)
        
        # Combine Probabilities (Dynamic Weights based on Market Regime)
        # If SMA says Bull/Bear strongly, give it 40% weight. N-Gram 30%, Markov 20%, MR 10%
        weights = {"ngram": 0.3, "markov": 0.2, "sma": 0.4, "mr": 0.1}
        
        # If Market is choppy (SMA Flat), MR and Markov matter more
        if p_sma == 0.5:
            weights = {"ngram": 0.2, "markov": 0.3, "sma": 0.1, "mr": 0.4}
            
        final_p_big = (p_ngram * weights["ngram"]) + (p_markov * weights["markov"]) + (p_sma * weights["sma"]) + (p_mr * weights["mr"])
        
        pred_size = 1 if final_p_big >= 0.5 else 0
        confidence = int(abs(final_p_big - 0.5) * 2 * 100) # Convert to 0-100 scale
        confidence = max(52, min(92, confidence))
        
        logic_tags = [l_ngram, l_markov, l_sma, l_mr]

        # ==========================================
        # DEEP NUMBER SELECTION (Target No)
        # ==========================================
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        # 1. Digit Transition Matrix
        last_num = nums[-1]
        trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num: trans[nums[i+1]] += 1
            
        # 2. Gap Analysis (Overdue numbers)
        gaps = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if gaps[nums[i]] == 1000: gaps[nums[i]] = len(nums) - i

        # 3. Frequency Analysis (Hot numbers in last 50)
        recent_freq = Counter(nums[-50:])

        best_num = pool[0]
        best_score = -1
        
        for n in pool:
            # Combine Transition (high weight) + Gap (medium) + Freq (low)
            t_score = trans.get(n, 0) * 0.5
            g_score = (gaps[n] / 50.0) * 0.3  # Normalize gap
            f_score = recent_freq.get(n, 0) * 0.2
            
            total_score = t_score + g_score + f_score
            if total_score > best_score:
                best_score = total_score
                best_num = n

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags)
        }

PREDICTOR = DeepMatrixPredictor()

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

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Deep Math</b>: {h(p['logic'])}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V105 TRUE DEEP MATRIX</i>
"""

async def send_msg(app, text):
    try:
        await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
    except Exception as e:
        log.error(f"Send error: {e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 V105 TRUE DEEP MATRIX Active!\nMulti-Order Markov & N-Gram (4-15) is running.")

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info("V105 Worker loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if not STORE.prediction_exists(next_issue):
                    p = PREDICTOR.generate_prediction()
                    if p:
                        STORE.save_prediction(p['issue'], p['size'], p['num'])
                        recent = STORE.recent_outcomes(12)
                        await send_msg(app, format_prediction(p, recent))
                        log.info(f"Pred {p['issue']} | Logic: {p['logic']}")
            
            gc.collect() 
            await asyncio.sleep(CFG.fetch_every)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            await asyncio.sleep(2)

# =========================
# WEB SERVER & MAIN
# =========================
async def handle_ping(request):
    return web.Response(text="V105 is running!")

async def main():
    CFG.validate()
    
    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server on port {port}")

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








