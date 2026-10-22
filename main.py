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
    bot_name: str = "V118 REVERSE APEX"

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
log = logging.getLogger("v118_reverse")
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
# V118 REVERSE APEX ENGINE
# =========================
class ReverseApexPredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        
        self.engines = ['pattern', 'markov', 'hash', 'chaos', 'quantum', 'cusum', 'entropy']
        self.engine_history = {name: [] for name in self.engines}

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

    def update_engine_accuracy(self, engine_probs_json, actual_size):
        if not engine_probs_json: return
        try:
            probs = json.loads(engine_probs_json)
            for name, p_big in probs.items():
                if name in self.engine_history:
                    pred_size = 1 if p_big >= 0.5 else 0
                    is_correct = (pred_size == actual_size)
                    self.engine_history[name].append(1 if is_correct else 0)
                    if len(self.engine_history[name]) > 10: 
                        self.engine_history[name].pop(0)
        except Exception:
            pass

    def engine_hash(self, sizes):
        if len(sizes) < 6: return 0.5, "H_None"
        target_hash = tuple(sizes[-5:])
        big, small = 0, 0
        for i in range(len(sizes) - 5):
            if tuple(sizes[i:i+5]) == target_hash:
                if sizes[i+5] == 1: big += 1
                else: small += 1
        total = big + small
        if total > 0: return (big + 1) / (total + 2), f"Hash({total}x)"
        return 0.5, "H_None"

    def engine_chaos(self, sizes):
        if len(sizes) < 10: return 0.5, "Ch_None"
        x, y, z = sizes[-3], sizes[-2], sizes[-1]
        best_dist, next_val = 999, 0.5
        for i in range(len(sizes) - 4):
            dist = math.sqrt((x-sizes[i])**2 + (y-sizes[i+1])**2 + (z-sizes[i+2])**2)
            if dist < best_dist:
                best_dist, next_val = dist, sizes[i+3]
        if best_dist <= 1.0: return (0.8 if next_val == 1 else 0.2), f"Chaos({best_dist:.1f})"
        return 0.5, "Ch_None"

    def engine_quantum(self, sizes):
        if len(sizes) < 20: return 0.5, "Q_None"
        wave_big, wave_small = 0, 0
        for i in range(10, 30):
            if len(sizes) < i: break
            freq = 3.14 / (1 + (i - 10)) 
            if sizes[-i] == 1: wave_big += math.sin(freq)
            else: wave_small += math.sin(freq)
        total = abs(wave_big) + abs(wave_small)
        if total == 0: return 0.5, "Q_None"
        return abs(wave_big) / total, "Quantum"

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
            if total >= 1: return (big + 1) / (total + 2), f"P{k}"
        return 0.5, "P_None"

    def engine_markov(self, sizes, order=4):
        if len(sizes) < order + 1: return 0.5, "M_None"
        state = tuple(sizes[-order:])
        big, small = 0, 0
        for i in range(len(sizes) - order):
            if tuple(sizes[i:i+order]) == state:
                if sizes[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 1: return (big + 1) / (total + 2), f"M{order}"
        return 0.5, "M_None"

    def engine_cusum(self, sizes):
        if len(sizes) < 30: return 0.5, "CUSUM_None"
        pos_sum, neg_sum = 0.0, 0.0
        for s in sizes[-30:]:
            pos_sum = max(0, pos_sum + s - 0.6)
            neg_sum = max(0, neg_sum - s + 0.4)
        if pos_sum > 2.0: return 0.75, "Shift_Big"
        if neg_sum > 2.0: return 0.25, "Shift_Small"
        return 0.5, "CUSUM_Neutral"

    def engine_entropy(self, sizes):
        if len(sizes) < 20: return 0.5, "E_None"
        w = sizes[-20:]
        p1 = sum(w) / 20.0
        p0 = 1 - p1
        ent = 0
        if p1 > 0: ent -= p1 * math.log2(p1)
        if p0 > 0: ent -= p0 * math.log2(p0)
        if ent < 0.7: return (0.7 if p1 > 0.5 else 0.3), "Ent"
        return 0.5, "Ent_Rand"

    # ==========================================
    # NORMAL MODE (INVERTED)
    # ==========================================
    def generate_normal_prediction(self, sizes, nums):
        raw_engines = {
            'hash': self.engine_hash(sizes),
            'chaos': self.engine_chaos(sizes),
            'quantum': self.engine_quantum(sizes),
            'pattern': self.engine_pattern(sizes),
            'markov': self.engine_markov(sizes, order=4),
            'cusum': self.engine_cusum(sizes),
            'entropy': self.engine_entropy(sizes)
        }

        final_engine_probs = {}
        logic_tags = []
        weighted_sum = 0
        total_w = 0

        for name, (p_big, label) in raw_engines.items():
            hist = self.engine_history[name]
            
            if len(hist) < 5:
                weight = 1.0
                final_engine_probs[name] = p_big
                logic_tags.append(label)
            else:
                acc = sum(hist) / len(hist)
                if acc >= 0.6:
                    weight = 2.0
                    final_engine_probs[name] = p_big
                    logic_tags.append(label)
                elif acc <= 0.3: 
                    weight = 1.5
                    final_engine_probs[name] = 1.0 - p_big
                    logic_tags.append(f"INV_{label}")
                else:
                    weight = 0.0
                    final_engine_probs[name] = p_big

            weighted_sum += final_engine_probs[name] * weight
            total_w += weight

        matrix_p_big = weighted_sum / total_w if total_w > 0 else 0.5
        matrix_pred = 1 if matrix_p_big >= 0.5 else 0

        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break

        pred_size = matrix_pred
        logic = " + ".join(logic_tags[:3])

        if streak >= 4:
            pred_size = sizes[-1]
            matrix_p_big = 1.0 if pred_size == 1 else 0.0
            logic = f"TREND_LOCK({streak}x)"
        elif abs(matrix_p_big - 0.5) < 0.02:
            pred_size = sizes[-1]
            matrix_p_big = 1.0 if pred_size == 1 else 0.0
            logic = "Momentum"

        # 🔥🔥🔥 MAIN CHANGE: INVERT NORMAL PREDICTION 🔥🔥🔥
        pred_size = 1 - pred_size
        logic = "REVERSE_" + logic

        confidence = int(abs(matrix_p_big - 0.5) * 2 * 100)
        confidence = min(92, max(51, confidence))
        if streak >= 4: confidence = min(90, 65 + streak * 3)

        return pred_size, confidence, logic, final_engine_probs

    # ==========================================
    # VENGEANCE MODE (ULTRA DEEP)
    # ==========================================
    def generate_vengeance_prediction(self, sizes, nums):
        logic_tags = []
        v_big, v_small = 0.0, 0.0
        
        # 1. Streak & Trend
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
            
        if streak >= 5:
            if sizes[-1] == 1: v_big += 5.0
            else: v_small += 5.0
            logic_tags.append(f"V_MegaTrend({streak}x)")
        elif streak == 4:
            if sizes[-1] == 1: v_big += 3.0
            else: v_small += 3.0
            logic_tags.append(f"V_StrongTrend({streak}x)")
        elif streak == 3:
            # Deep check: Did 3-streaks usually break or continue?
            breaks, conts = 0, 0
            for i in range(len(sizes) - 3):
                if sizes[i] == sizes[i+1] == sizes[i+2] == sizes[-1]:
                    if sizes[i+3] == sizes[-1]: conts += 1
                    else: breaks += 1
            if breaks > conts:
                if sizes[-1] == 1: v_small += 3.0
                else: v_big += 3.0
                logic_tags.append("V_StreakBreak")
            else:
                if sizes[-1] == 1: v_big += 2.0
                else: v_small += 2.0
                logic_tags.append("V_StreakCont")

        # 2. Deep Mean Reversion (Over 50 periods)
        big_count = sizes[-50:].count(1)
        big_ratio = big_count / 50.0
        if big_ratio > 0.70: 
            v_small += 4.0
            logic_tags.append("V_MassBalance(S)")
        elif big_ratio < 0.30: 
            v_big += 4.0
            logic_tags.append("V_MassBalance(B)")
            
        # 3. Order-6 Markov (Ultra Deep History)
        order = 6
        if len(sizes) > order:
            state = tuple(sizes[-order:])
            m_big, m_small = 0, 0
            for i in range(len(sizes) - order):
                if tuple(sizes[i:i+order]) == state:
                    if sizes[i+order] == 1: m_big += 1
                    else: m_small += 1
            if m_big + m_small > 0:
                if m_big > m_small:
                    v_big += 3.0
                    logic_tags.append(f"V_Mark6({m_big}B/{m_small}S)")
                else:
                    v_small += 3.0
                    logic_tags.append(f"V_Mark6({m_big}B/{m_small}S)")

        # 4. Number Sequence Pattern
        last_nums = tuple(nums[-3:])
        n_big, n_small = 0, 0
        for i in range(len(nums) - 3):
            if tuple(nums[i:i+3]) == last_nums:
                if nums[i+3] >= 5: n_big += 1
                else: n_small += 1
        if n_big > n_small and n_big > 2:
            v_big += 2.5
            logic_tags.append("V_NumPat(B)")
        elif n_small > n_big and n_small > 2:
            v_small += 2.5
            logic_tags.append("V_NumPat(S)")

        # Final Vengeance Decision
        if v_big > v_small:
            pred_size = 1
            prob = v_big / (v_big + v_small)
        elif v_small > v_big:
            pred_size = 0
            prob = v_small / (v_big + v_small)
        else:
            pred_size = 0 if sizes[-1] == 1 else 1
            prob = 0.75
            logic_tags.append("V_ChaosFlip")
            
        confidence = int(75 + (prob * 20)) # 75% to 95%
        confidence = min(95, confidence)
        
        final_engine_probs = {'vengeance': (1.0 if pred_size == 1 else 0.0)}
        return pred_size, confidence, " + ".join(logic_tags), final_engine_probs

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        # CHOOSE MODE
        if self.loss_streak >= 1:
            pred_size, confidence, logic, engine_probs = self.generate_vengeance_prediction(sizes, nums)
        else:
            pred_size, confidence, logic, engine_probs = self.generate_normal_prediction(sizes, nums)

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

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic,
            "engine_probs": engine_probs
        }

PREDICTOR = ReverseApexPredictor()

# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    mode = "🚨 VENGEANCE OVERDRIVE" if PREDICTOR.loss_streak > 0 else "🔄 NORMAL (REVERSED)"

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
⚡ <i>V118 REVERSE APEX</i>
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
    log.info("V118 Reverse Apex loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']: PREDICTOR.loss_streak += 1
                    else: PREDICTOR.loss_streak = 0
                    
                    act_int = 1 if res['actual_size'] == 'BIG' else 0
                    PREDICTOR.update_engine_accuracy(res.get('engine_probs'), act_int)
                    
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
    return web.Response(text="V118 Reverse Apex is running perfectly!")

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
