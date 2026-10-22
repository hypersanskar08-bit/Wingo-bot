import asyncio
import contextlib
import gc
import html
import json
import logging
import math
import os
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
    source_api_url: str = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    db_path: str = "predictor.db"
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 6
    bot_name: str = "V119 ULTIMATE OVERRIDE"

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
log = logging.getLogger("v119_override")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, logic TEXT, engine_probs TEXT, created_at REAL
                )
            """)
            try: c.execute("ALTER TABLE predictions ADD COLUMN engine_probs TEXT")
            except sqlite3.OperationalError: pass

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, issue, size, num, logic, engine_probs):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, logic, engine_probs, created_at) VALUES (?, ?, ?, ?, ?, ?)", 
                      (issue, size, num, logic, json.dumps(engine_probs), time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, engine_probs FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, probs_json in rows:
            if issue in history_dict:
                act = history_dict[issue]
                act_size_str = "BIG" if act['size'] == 1 else "SMALL"
                
                win = (p_size == act_size_str)
                status = ("JACK 🏆" if p_num == act['num'] else "WIN ✅") if win else "LOSS ❌"
                    
                with self._conn() as c:
                    c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", 
                              (status, act['num'], act_size_str, issue))
                resolved.append({
                    "issue": issue, "size": p_size, "num": p_num, 
                    "actual_num": act['num'], "actual_size": act_size_str, 
                    "status": status, "engine_probs": probs_json
                })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# =========================
# V119 ULTIMATE PREDICTOR
# =========================
class UltimateOverridePredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.recent_results = [] # Track recent wins/losses

    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200: return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])
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
                    except: continue
                self.history = out
                return True
        except Exception:
            return False

    def engine_pattern(self, sizes):
        for k in range(12, 3, -1):
            if len(sizes) < k + 1: continue
            pat = tuple(sizes[-k:])
            big, small = 0, 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k] == 1: big += 1
                    else: small += 1
            total = big + small
            if total >= 2:
                return (big + 1) / (total + 2), f"P{k}"
        return 0.5, "P_None"

    def engine_markov(self, sizes, order=3):
        if len(sizes) < order + 1: return 0.5, "M_None"
        state = tuple(sizes[-order:])
        big, small = 0, 0
        for i in range(len(sizes) - order):
            if tuple(sizes[i:i+order]) == state:
                if sizes[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            return (big + 1) / (total + 2), f"M{order}"
        return 0.5, "M_None"

    def engine_trend(self, sizes):
        if len(sizes) < 30: return 0.5, "T_None"
        # SMA Crossover 5 vs 15
        sma5 = sum(sizes[-5:]) / 5.0
        sma15 = sum(sizes[-15:]) / 15.0
        diff = sma5 - sma15
        
        if diff > 0.15: return 0.7, "Trend_Bull"
        if diff < -0.15: return 0.3, "Trend_Bear"
        return 0.5, "Trend_Neutral"

    def engine_balance(self, sizes):
        if len(sizes) < 30: return 0.5, "B_None"
        big_count = sizes[-30:].count(1)
        ratio = big_count / 30.0
        # Mean Reversion
        if ratio > 0.7: return 0.2, "Bal_Small" # Too many BIG, expect SMALL
        if ratio < 0.3: return 0.8, "Bal_Big"   # Too many SMALL, expect BIG
        return 0.5, "Bal_Neutral"

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        # 1. Get Base Engine Votes
        p_pat, l_pat = self.engine_pattern(sizes)
        p_mark, l_mark = self.engine_markov(sizes, order=3)
        p_trend, l_trend = self.engine_trend(sizes)
        p_bal, l_bal = self.engine_balance(sizes)

        # Combine (Equal Weights)
        final_p_big = (p_pat * 0.35) + (p_mark * 0.25) + (p_trend * 0.20) + (p_bal * 0.20)
        
        # 2. Trend Lock Override
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break

        logic = f"{l_pat}+{l_mark}+{l_trend}"
        if streak >= 4:
            final_p_big = 1.0 if sizes[-1] == 1 else 0.0
            logic = f"STRONG_TREND({streak}x)"

        pred_size = 1 if final_p_big >= 0.5 else 0

        # 3. THE MAGIC: DYNAMIC INVERSION
        if len(self.recent_results) >= 10:
            recent_wins = sum(self.recent_results[-10:])
            if recent_wins <= 4:
                # If we are losing badly, INVERT the prediction
                pred_size = 1 - pred_size
                logic = "INVERTED_" + logic
                final_p_big = 1.0 - final_p_big

        confidence = int(abs(final_p_big - 0.5) * 2 * 100)
        confidence = min(85, max(52, confidence)) # Anti-Fake Confidence

        # --- NUMBER MATRIX ---
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        last_num = nums[-1]
        
        trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num: trans[nums[i+1]] += 1
            
        gaps = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if gaps[nums[i]] == 1000: gaps[nums[i]] = len(nums) - i
            
        freq = Counter(nums[-50:])
        
        best_num = pool[0]
        best_score = -999
        
        for n in pool:
            t_score = (trans.get(n, 0) / max(1, sum(trans.values()))) * 100
            g_score = min(gaps[n], 30) / 30 * 100
            f_score = (freq.get(n, 0) / 50) * 100
            
            score = (t_score * 0.5) + (g_score * 0.3) + (f_score * 0.2)
            if n == (last_num + 1) % 10 or n == (last_num - 1 + 10) % 10: score += 12
                
            if score > best_score:
                best_score = score
                best_num = n

        engine_probs = {'pattern': p_pat, 'markov': p_mark, 'trend': p_trend, 'balance': p_bal}
        
        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic,
            "engine_probs": engine_probs
        }

PREDICTOR = UltimateOverridePredictor()

# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    mode = "🚨 RECOVERY" if PREDICTOR.loss_streak > 0 else "🧠 NORMAL"

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Logic</b>: {h(p['logic'])}
⚖️ <b>Mode</b>: {mode}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V119 ULTIMATE OVERRIDE</i>
"""

async def send_msg(app, text):
    try:
        await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
    except Exception as e:
        log.error(f"Send error: {e}")

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info("V119 Ultimate loop started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    is_win = ("WIN" in res['status'] or "JACK" in res['status'])
                    
                    if is_win:
                        PREDICTOR.loss_streak = 0
                        PREDICTOR.recent_results.append(1)
                    else:
                        PREDICTOR.loss_streak += 1
                        PREDICTOR.recent_results.append(0)
                        
                    if len(PREDICTOR.recent_results) > 20:
                        PREDICTOR.recent_results.pop(0)
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                # 🔥 FIX: Prediction Miss Issue
                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = PREDICTOR.generate_prediction()
                        if p:
                            STORE.save_prediction(p['issue'], p['size'], p['num'], p['logic'], p['engine_probs'])
                            recent = STORE.recent_outcomes(12)
                            await send_msg(app, format_prediction(p, recent))
                            last_processed_issue = latest_issue
            
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
    return web.Response(text="V119 Ultimate Override is running perfectly!")

async def main():
    CFG.validate()
    
    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server running on port {port}")

    app = Application.builder().token(CFG.telegram_token).build()
    
    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        
        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
