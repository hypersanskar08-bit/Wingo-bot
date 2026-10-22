import asyncio
import contextlib
import gc
import html
import json
import logging
import math
import os
import sqlite3
import statistics
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
    # 1-Minute API
    source_api_url: str = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    db_path: str = "predictor.db"
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 6
    bot_name: str = "V112 QUANTUM CORE"

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
log = logging.getLogger("v112_core")
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
            try:
                c.execute("ALTER TABLE predictions ADD COLUMN engine_probs TEXT")
            except sqlite3.OperationalError:
                pass

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
# V112 QUANTUM PREDICTOR
# =========================
class QuantumPredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.engines = {
            'pattern': {'w': 1.0},
            'markov': {'w': 1.0},
            'cycle': {'w': 1.0},
            'cusum': {'w': 1.0},
            'entropy': {'w': 1.0}
        }

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

    # --- ENGINE 1: LONGEST PATTERN (N-Gram) ---
    def engine_pattern(self, sizes):
        for k in range(15, 3, -1):
            if len(sizes) < k + 1: continue
            pat = tuple(sizes[-k:])
            big, small = 0, 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k] == 1: big += 1
                    else: small += 1
            total = big + small
            if total >= 1:
                return (big + 1) / (total + 2), f"P{k}({big}B/{small}S)"
        return 0.5, "P_None"

    # --- ENGINE 2: MULTI-ORDER MARKOV ---
    def engine_markov(self, sizes):
        total_p, total_w = 0.0, 0.0
        for order in [2, 3, 4, 5]:
            if len(sizes) < order + 1: continue
            state = tuple(sizes[-order:])
            big, small = 0, 0
            for i in range(len(sizes) - order):
                if tuple(sizes[i:i+order]) == state:
                    if sizes[i+order] == 1: big += 1
                    else: small += 1
            t = big + small
            if t >= 1:
                p = (big + 1) / (t + 2)
                total_p += p * order
                total_w += order
        if total_w == 0: return 0.5, "M_None"
        return total_p / total_w, "M(2-5)"

    # --- ENGINE 3: AUTOCORRELATION (CYCLE) ---
    def engine_cycle(self, sizes):
        if len(sizes) < 50: return 0.5, "C_None"
        best_lag, best_corr = 1, 0
        for lag in range(3, 15): 
            matches = 0
            for i in range(len(sizes)-lag-20, len(sizes)-lag):
                if sizes[i] == sizes[i+lag]: matches += 1
            corr = matches / 20.0
            if corr > best_corr:
                best_corr = corr
                best_lag = lag
        if best_corr >= 0.65:
            pred = sizes[-best_lag]
            return (0.8 if pred == 1 else 0.2), f"Cyc{best_lag}"
        return 0.5, "C_None"

    # --- ENGINE 4: CUSUM (TREND SHIFT) ---
    def engine_cusum(self, sizes):
        if len(sizes) < 30: return 0.5, "CUSUM_None"
        target = 0.5
        pos_sum, neg_sum = 0.0, 0.0
        for s in sizes[-30:]:
            pos_sum = max(0, pos_sum + s - target - 0.1)
            neg_sum = max(0, neg_sum - s + target - 0.1)
        
        if pos_sum > 2.0: return 0.7, "Shift_Big"
        if neg_sum > 2.0: return 0.3, "Shift_Small"
        return 0.5, "CUSUM_Neutral"

    # --- ENGINE 5: ENTROPY (CHAOS DETECTOR) ---
    def engine_entropy(self, sizes):
        if len(sizes) < 20: return 0.5, "E_None"
        w = sizes[-20:]
        p1 = sum(w) / 20.0
        p0 = 1 - p1
        ent = 0
        if p1 > 0: ent -= p1 * math.log2(p1)
        if p0 > 0: ent -= p0 * math.log2(p0)
        
        if ent < 0.7:
            if p1 > 0.5: return 0.7, "Ent_Big"
            else: return 0.3, "Ent_Small"
        return 0.5, "Ent_Rand"

    def update_engine_weights(self, engine_probs_json, actual_size):
        if not engine_probs_json: return
        try:
            probs = json.loads(engine_probs_json)
            for name, p_big in probs.items():
                if name in self.engines:
                    pred_eng = 1 if p_big >= 0.5 else 0
                    if pred_eng == actual_size:
                        self.engines[name]['w'] = min(2.5, self.engines[name]['w'] + 0.05)
                    else:
                        self.engines[name]['w'] = max(0.1, self.engines[name]['w'] - 0.05)
        except Exception:
            pass

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        p_pat, l_pat = self.engine_pattern(sizes)
        p_mark, l_mark = self.engine_markov(sizes)
        p_cyc, l_cyc = self.engine_cycle(sizes)
        p_cusum, l_cusum = self.engine_cusum(sizes)
        p_ent, l_ent = self.engine_entropy(sizes)
        
        engine_probs = {
            'pattern': p_pat, 'markov': p_mark, 
            'cycle': p_cyc, 'cusum': p_cusum, 'entropy': p_ent
        }

        # Multipliers for Recovery Mode
        temp_mult = {'pattern': 1.0, 'markov': 1.0, 'cycle': 1.0, 'cusum': 1.0, 'entropy': 1.0}
        if self.loss_streak >= 1:
            temp_mult['pattern'] = 0.3  # Pattern fail ho raha hai
            temp_mult['markov'] = 1.5   # Markov zyada deep khelo
            temp_mult['cycle'] = 1.5    # Cycle dhoondo
            temp_mult['cusum'] = 2.0    # Trend shift pakdo

        weighted_sum = 0
        total_w = 0
        for name, p_big in engine_probs.items():
            w = self.engines[name]['w'] * temp_mult[name]
            weighted_sum += p_big * w
            total_w += w

        final_p_big = weighted_sum / total_w if total_w > 0 else 0.5
        pred_size = 1 if final_p_big >= 0.5 else 0
        
        # Confidence calculation (Max 88%, Min 52%)
        confidence = int(abs(final_p_big - 0.5) * 2 * 100)
        confidence = min(88, max(52, confidence))

        # ==========================================
        # DEEP NUMBER SELECTION MATRIX
        # ==========================================
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        last_num = nums[-1]
        
        # 1. Digit Transition Matrix
        trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num: trans[nums[i+1]] += 1
            
        # 2. Gap Analysis
        gaps = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if gaps[nums[i]] == 1000: gaps[nums[i]] = len(nums) - i
            
        # 3. Recent Frequency
        freq = Counter(nums[-50:])
        
        best_num = pool[0]
        best_score = -999
        
        for n in pool:
            # Base Scores
            t_score = (trans.get(n, 0) / max(1, sum(trans.values()))) * 100
            g_score = min(gaps[n], 30) / 30 * 100
            f_score = (freq.get(n, 0) / 50) * 100
            
            # Combined Weight
            score = (t_score * 0.6) + (g_score * 0.25) + (f_score * 0.15)
            
            # Neighbour Bonus (+-1 digit)
            if n == (last_num + 1) % 10 or n == (last_num - 1 + 10) % 10:
                score += 15
                
            if score > best_score:
                best_score = score
                best_num = n

        logic = f"{l_pat} + {l_mark} + {l_cyc} + {l_cusum}"
        
        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic,
            "engine_probs": engine_probs
        }

PREDICTOR = QuantumPredictor()

# =========================
# FORMATTING & SENDING
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
⚡ <i>V112 QUANTUM CORE</i>
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
    log.info("V112 Quantum loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']: PREDICTOR.loss_streak += 1
                    else: PREDICTOR.loss_streak = 0
                    
                    act_int = 1 if res['actual_size'] == 'BIG' else 0
                    PREDICTOR.update_engine_weights(res.get('engine_probs'), act_int)
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if not STORE.prediction_exists(next_issue):
                    p = PREDICTOR.generate_prediction()
                    if p:
                        STORE.save_prediction(p['issue'], p['size'], p['num'], p['logic'], p['engine_probs'])
                        recent = STORE.recent_outcomes(12)
                        await send_msg(app, format_prediction(p, recent))
            
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
    return web.Response(text="V112 Quantum Core is running perfectly!")

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
