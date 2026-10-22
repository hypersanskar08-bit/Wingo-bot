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
from telegram.error import TimedOut, NetworkError

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
    request_timeout: float = 10.0
    bot_name: str = "V129 ULTIMATE VENGEANCE"

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
log = logging.getLogger("v129_ultimate")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, 
                    mode TEXT, logic TEXT, created_at REAL
                )
            """)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, issue, size, num, mode, logic):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, mode, logic, created_at) VALUES (?, ?, ?, ?, ?, ?)", 
                      (issue, size, num, mode, logic, time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, mode FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, mode in rows:
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
                    "status": status, "mode": mode
                })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# =========================
# V129 ULTIMATE MULTI-MODE PREDICTOR
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

    # --- ULTIMATE DEEP MARKOV ENGINE ---
    def engine_deep_markov(self, sizes):
        orders = [6, 5, 4, 3]
        
        for order in orders:
            if len(sizes) < order + 5: continue
            target = sizes[-order:]
            
            exact_big, exact_small = 0, 0
            fuzzy_big, fuzzy_small = 0, 0
            
            for i in range(len(sizes) - order):
                window = sizes[i:i+order]
                next_val = sizes[i+order]
                
                distance = sum(1 for a, b in zip(target, window) if a != b)
                recency_weight = 0.95 ** (len(sizes) - (i + order))
                
                if distance == 0:
                    if next_val == 1: exact_big += 1 * recency_weight
                    else: exact_small += 1 * recency_weight
                elif distance == 1:
                    if next_val == 1: fuzzy_big += 1 * (recency_weight * 0.5)
                    else: fuzzy_small += 1 * (recency_weight * 0.5)
            
            total_exact = exact_big + exact_small
            total_fuzzy = fuzzy_big + fuzzy_small
            
            if total_exact >= 0.5:
                p_big = (exact_big + 0.5) / (total_exact + 1.0)
                return p_big, f"M{order}_Exact"
            
            if total_fuzzy >= 1.0:
                p_big = (fuzzy_big + 0.5) / (total_fuzzy + 1.0)
                return p_big, f"M{order}_Fuzzy"
                
        return 0.5, "M_None"

    def detect_market_mode(self, sizes):
        if self.loss_streak >= 1:
            return "VENGEANCE"

        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
            
        if streak >= 3:
            return "TREND"

        if len(sizes) >= 30:
            w = sizes[-30:]
            big_count = sum(w)
            z = (big_count - 15) / math.sqrt(30 * 0.25)
            if z > 1.8 or z < -1.8:
                return "EXTREME"

        if len(sizes) >= 15:
            switches = sum(1 for i in range(1, 15) if sizes[-i] != sizes[-i-1])
            if switches >= 11:
                return "CHOP"

        return "NORMAL"

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        mode = self.detect_market_mode(sizes)
        
        # 🔥 FIX: Define streak here so it doesn't throw NameError
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break

        pred_size = 0
        logic = ""
        confidence = 60

        # ==========================================
        # 🚨 VENGEANCE (Ultra Deep Mode)
        # ==========================================
        if mode == "VENGEANCE":
            p_mark, l_mark = self.engine_deep_markov(sizes)
            
            p_z = 0.5
            if len(sizes) >= 30:
                w = sizes[-30:]
                big_count = sum(w)
                z = (big_count - 15) / math.sqrt(30 * 0.25)
                if z > 1.5: p_z = 0.1
                elif z < -1.5: p_z = 0.9

            final_p = (p_mark * 0.6) + (p_z * 0.4)
            pred_size = 1 if final_p >= 0.5 else 0
            
            logic = f"V_{l_mark} + V_ZCore"
            confidence = 75 + int(abs(final_p - 0.5) * 20)

        # ==========================================
        # 📈 TREND
        # ==========================================
        elif mode == "TREND":
            pred_size = sizes[-1]
            logic = f"TREND_LOCK({streak}x)"
            confidence = 75

        # ==========================================
        # ⚖️ EXTREME (Reversal)
        # ==========================================
        elif mode == "EXTREME":
            w = sizes[-30:]
            big_count = sum(w)
            if big_count >= 20: 
                pred_size = 0
                logic = f"EXTREME_REVERSION"
            else: 
                pred_size = 1
                logic = f"EXTREME_REVERSION"
            confidence = 70

        # ==========================================
        # 🌪️ CHOP (Zigzag)
        # ==========================================
        elif mode == "CHOP":
            pred_size = 1 - sizes[-1]
            logic = "CHOP_ZIGZAG"
            confidence = 65

        # ==========================================
        # 🧠 NORMAL (Pattern / Markov)
        # ==========================================
        else:
            p_pat, l_pat = self.engine_deep_markov(sizes)
            pred_size = 1 if p_pat >= 0.5 else 0
            logic = l_pat
            confidence = int(abs(p_pat - 0.5) * 2 * 100)
            confidence = min(75, max(55, confidence))

        confidence = min(85, max(52, confidence))

        # Target Number
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        last_num = nums[-1]
        
        trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num: trans[nums[i+1]] += 1
            
        gaps = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if gaps[nums[i]] == 1000: gaps[nums[i]] = len(nums) - i
            
        best_num = pool[0]
        best_score = -999
        
        for n in pool:
            t_score = (trans.get(n, 0) / max(1, sum(trans.values()))) * 100
            g_score = min(gaps[n], 30) / 30 * 100
            score = (t_score * 0.6) + (g_score * 0.4)
            if n == (last_num + 1) % 10 or n == (last_num - 1 + 10) % 10: score += 15
                
            if score > best_score:
                best_score = score
                best_num = n

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic,
            "mode": mode
        }

PREDICTOR = UltimatePredictor()

# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Logic</b>: {h(p['logic'])}
⚖️ <b>Mode</b>: <b>{h(p['mode'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V129 ULTIMATE VENGEANCE</i>
"""

async def send_msg(app, text, retries=3):
    for attempt in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Send error: {e}")
            break

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info("V129 Ultimate loop started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']: PREDICTOR.loss_streak += 1
                    else: PREDICTOR.loss_streak = 0
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = PREDICTOR.generate_prediction()
                        if p:
                            STORE.save_prediction(p['issue'], p['size'], p['num'], p['mode'], p['logic'])
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
# MAIN
# =========================
async def handle_ping(request):
    return web.Response(text="V129 Ultimate is running!")

async def main():
    CFG.validate()
    
    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    
    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        
        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
