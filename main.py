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
    bot_name: str = "V127 REGIME-GATED EXPERTS"

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
log = logging.getLogger("v127_experts")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            # Predictions Table
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, 
                    regime INTEGER, expert_probs TEXT, logic TEXT, created_at REAL
                )
            """)
            # Expert Stats Table (The Brain)
            c.execute("""
                CREATE TABLE IF NOT EXISTS expert_stats (
                    regime INTEGER, expert TEXT, wins INTEGER, total INTEGER,
                    PRIMARY KEY (regime, expert)
                )
            """)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, issue, size, num, regime, expert_probs, logic):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, regime, expert_probs, logic, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                      (issue, size, num, regime, json.dumps(expert_probs), logic, time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, regime, expert_probs FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, regime, probs_json in rows:
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
                    "status": status, "regime": regime, "expert_probs": probs_json
                })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def get_expert_stats(self):
        with self._conn() as c:
            rows = c.execute("SELECT regime, expert, wins, total FROM expert_stats").fetchall()
            return {(r, e): (w, t) for r, e, w, t in rows}

    def save_expert_stats(self, regime, expert, wins, total):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO expert_stats (regime, expert, wins, total) VALUES (?, ?, ?, ?)", (regime, expert, wins, total))

STORE = Storage(CFG.db_path)

# =========================
# V127 MIXTURE OF EXPERTS (MOE)
# =========================
class RegimeGatedPredictor:
    def __init__(self):
        self.history = []
        self.stats = STORE.get_expert_stats()
        
        self.experts = ['momentum', 'trend', 'reversion', 'pattern', 'markov']
        
        # Initialize stats if empty
        for r in range(6):
            for e in self.experts:
                if (r, e) not in self.stats:
                    self.stats[(r, e)] = (0, 0)

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

    def get_regime(self, sizes):
        """0:MIXED, 1:TREND_BIG, 2:TREND_SMALL, 3:CHOP, 4:EXTREME_BIG, 5:EXTREME_SMALL"""
        if len(sizes) < 30: return 0
        w = sizes[-30:]
        big_count = sum(w)
        
        # Z-Score Extreme
        z = (big_count - 15) / math.sqrt(30 * 0.25)
        if z > 1.5: return 4
        if z < -1.5: return 5
        
        # Choppiness
        switches = sum(1 for i in range(1, 15) if sizes[-i] != sizes[-i-1])
        if switches >= 10: return 3
        
        # Trend
        sma10 = sum(sizes[-10:]) / 10.0
        sma30 = sum(sizes[-30:]) / 30.0
        if sma10 > 0.65 and sma30 > 0.55: return 1
        if sma10 < 0.35 and sma30 < 0.45: return 2
        
        return 0

    # --- EXPERT 1: MOMENTUM ---
    def expert_momentum(self, sizes):
        return 1.0 if sizes[-1] == 1 else 0.0

    # --- EXPERT 2: TREND (Streak Follower) ---
    def expert_trend(self, sizes):
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
        if streak >= 3: return 1.0 if sizes[-1] == 1 else 0.0
        return 0.5

    # --- EXPERT 3: REVERSION (Contrarian) ---
    def expert_reversion(self, sizes):
        big_count = sum(sizes[-20:])
        if big_count >= 14: return 0.1 # Expect Small
        if big_count <= 6: return 0.9  # Expect Big
        return 0.5

    # --- EXPERT 4: DEEP PATTERN ---
    def expert_pattern(self, sizes):
        for k in range(10, 3, -1):
            if len(sizes) < k+1: continue
            pat = tuple(sizes[-k:])
            big, small = 0, 0
            for i in range(len(sizes)-k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k] == 1: big += 1
                    else: small += 1
            if big+small >= 2: return (big+1)/(big+small+2)
        return 0.5

    # --- EXPERT 5: MARKOV-4 ---
    def expert_markov(self, sizes):
        order = 4
        if len(sizes) < order+1: return 0.5
        state = tuple(sizes[-order:])
        big, small = 0, 0
        for i in range(len(sizes)-order):
            if tuple(sizes[i:i+order]) == state:
                if sizes[i+order] == 1: big += 1
                else: small += 1
        if big+small >= 2: return (big+1)/(big+small+2)
        return 0.5

    def update_learning(self, regime, expert_probs_json, actual_size):
        """Core RL Brain: Update Expert x Regime accuracy"""
        if not expert_probs_json: return
        try:
            probs = json.loads(expert_probs_json)
            for name, p_big in probs.items():
                pred_size = 1 if p_big >= 0.5 else 0
                wins, total = self.stats.get((regime, name), (0, 0))
                
                if pred_size == actual_size:
                    wins += 1
                total += 1
                
                self.stats[(regime, name)] = (wins, total)
                STORE.save_expert_stats(regime, name, wins, total)
        except Exception as e:
            log.error(f"Learn error: {e}")

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        regime = self.get_regime(sizes)
        
        expert_probs = {
            'momentum': self.expert_momentum(sizes),
            'trend': self.expert_trend(sizes),
            'reversion': self.expert_reversion(sizes),
            'pattern': self.expert_pattern(sizes),
            'markov': self.expert_markov(sizes)
        }

        weighted_sum = 0
        total_weight = 0
        logic_tags = []

        # 🔥 CONTEXTUAL BANDIT WEIGHTING (Data-Driven)
        for name, p_big in expert_probs.items():
            wins, total = self.stats.get((regime, name), (0, 0))
            
            if total < 3:
                # Need data: Explore this expert
                weight = 1.0 
            else:
                acc = wins / total
                # Exploration bonus (UCB1)
                exploration = math.sqrt(2 * math.log(total + 10) / total)
                weight = max(0.05, acc + exploration * 0.5)
            
            # Only show experts that have good weight
            if weight > 0.55:
                direction = "B" if p_big >= 0.5 else "S"
                logic_tags.append(f"{name.capitalize()}({direction})")

            weighted_sum += p_big * weight
            total_weight += weight

        final_p_big = weighted_sum / total_weight if total_weight > 0 else 0.5
        pred_size = 1 if final_p_big >= 0.5 else 0
        
        confidence = int(abs(final_p_big - 0.5) * 2 * 100)
        
        # If confidence is too low, it means experts are disagreeing. 
        # Reduce confidence to avoid false hope.
        confidence = min(88, max(51, confidence))
        
        if not logic_tags:
            logic_tags = ["MixtureOfExperts"]

        # NUMBER MATRIX
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
            "logic": " + ".join(logic_tags[:3]),
            "regime": regime,
            "expert_probs": expert_probs
        }

PREDICTOR = RegimeGatedPredictor()

# =========================
# FORMATTING
# =========================
def get_regime_name(regime):
    names = {0: "MIXED", 1: "TREND_BIG", 2: "TREND_SMALL", 3: "CHOP", 4: "EXTREME_BIG", 5: "EXTREME_SMALL"}
    return names.get(regime, "UNKNOWN")

def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    reg_name = get_regime_name(p['regime'])

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Best Experts</b>: {h(p['logic'])}
🌐 <b>Market Regime</b>: {reg_name}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V127 REGIME-GATED EXPERTS (AI)</i>
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
    log.info("V127 Regime-Gated Experts started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    act_int = 1 if res['actual_size'] == 'BIG' else 0
                    
                    # 🔥 FIRE THE LEARNING ENGINE
                    PREDICTOR.update_learning(
                        res.get('regime'), 
                        res.get('expert_probs'), 
                        act_int
                    )
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = PREDICTOR.generate_prediction()
                        if p:
                            STORE.save_prediction(p['issue'], p['size'], p['num'], p['regime'], p['expert_probs'], p['logic'])
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
    return web.Response(text="V127 MoE is running!")

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
