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
    bot_name: str = "V122 HYPER-ADAPTIVE"

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
log = logging.getLogger("v122_adaptive")
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS engine_weights (
                    name TEXT PRIMARY KEY, weight REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS number_penalties (
                    num INTEGER PRIMARY KEY, penalty REAL
                )
            """)

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

    def load_weights(self):
        with self._conn() as c:
            rows = c.execute("SELECT name, weight FROM engine_weights").fetchall()
            return {name: w for name, w in rows}

    def save_weights(self, weights):
        with self._conn() as c:
            for name, w in weights.items():
                c.execute("INSERT OR REPLACE INTO engine_weights (name, weight) VALUES (?, ?)", (name, w))

    def load_number_penalties(self):
        with self._conn() as c:
            rows = c.execute("SELECT num, penalty FROM number_penalties").fetchall()
            return {num: p for num, p in rows}

    def save_number_penalties(self, penalties):
        with self._conn() as c:
            for num, p in penalties.items():
                c.execute("INSERT OR REPLACE INTO number_penalties (num, penalty) VALUES (?, ?)", (num, p))

STORE = Storage(CFG.db_path)

# =========================
# V122 HYPER-ADAPTIVE ENGINE
# =========================
class HyperAdaptivePredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        
        self.weights = STORE.load_weights()
        self.number_penalties = STORE.load_number_penalties()
        
        for eng in ['pattern', 'markov', 'zscore', 'trendseq', 'fuzzy']:
            if eng not in self.weights:
                self.weights[eng] = 1.0
        for n in range(10):
            if n not in self.number_penalties:
                self.number_penalties[n] = 1.0

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
        for k in range(10, 3, -1):
            if len(sizes) < k + 1: continue
            pat = tuple(sizes[-k:])
            big, small = 0, 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k] == 1: big += 1
                    else: small += 1
            total = big + small
            if total >= 2: return (big + 1) / (total + 2), f"Pat{k}"
        return 0.5, "Pat0"

    def engine_markov(self, sizes):
        order = 4
        if len(sizes) < order + 1: return 0.5, "Mrk0"
        state = tuple(sizes[-order:])
        big, small = 0, 0
        for i in range(len(sizes) - order):
            if tuple(sizes[i:i+order]) == state:
                if sizes[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2: return (big + 1) / (total + 2), "Mrk4"
        return 0.5, "Mrk0"

    def engine_zscore(self, sizes):
        if len(sizes) < 40: return 0.5, "Z0"
        w = sizes[-40:]
        big_count = sum(w)
        z = (big_count - 20) / math.sqrt(40 * 0.25)
        if z > 1.5: return 0.15, f"Z-Ext"
        if z < -1.5: return 0.85, f"Z-Ext"
        return 0.5, "Z0"

    def engine_trendseq(self, sizes):
        if len(sizes) < 20: return 0.5, "Trd0"
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break

        breaks, conts = 0, 0
        for i in range(len(sizes) - streak - 1):
            if all(sizes[i+j] == sizes[-1] for j in range(streak)):
                if i + streak < len(sizes):
                    if sizes[i+streak] == sizes[-1]: conts += 1
                    else: breaks += 1
        
        total = breaks + conts
        if total >= 3:
            if breaks > conts: return (0.0 if sizes[-1] == 1 else 1.0), "TrdBrk"
            else: return (1.0 if sizes[-1] == 1 else 0.0), "TrdCnt"
        return (1.0 if sizes[-1] == 1 else 0.0), "TrdMom"

    def engine_fuzzy(self, sizes):
        if len(sizes) < 12: return 0.5, "Fz0"
        target = sizes[-8:]
        best_score, next_val = 0, 0.5
        for i in range(len(sizes) - 12):
            window = sizes[i:i+8]
            score = sum(1 for a, b in zip(target, window) if a == b)
            if score >= 7:
                best_score, next_val = score, sizes[i+8]
        if best_score >= 7: return (0.8 if next_val == 1 else 0.2), "Fz80"
        return 0.5, "Fz0"

    def update_learning(self, engine_probs_json, actual_size, pred_num, actual_num):
        if not engine_probs_json: return
        try:
            probs = json.loads(engine_probs_json)
            for name, p_big in probs.items():
                if name in self.weights:
                    pred_size = 1 if p_big >= 0.5 else 0
                    if pred_size != actual_size:
                        self.weights[name] = max(0.1, self.weights[name] - 0.1)
                    else:
                        self.weights[name] = min(3.0, self.weights[name] + 0.05)
            
            if pred_num != actual_num:
                self.number_penalties[pred_num] = max(0.2, self.number_penalties[pred_num] - 0.2)
            else:
                self.number_penalties[pred_num] = 1.0

            for n in self.number_penalties:
                if n != pred_num:
                    self.number_penalties[n] = min(1.0, self.number_penalties[n] + 0.05)

            STORE.save_weights(self.weights)
            STORE.save_number_penalties(self.number_penalties)
        except Exception:
            pass

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        p_pat, l_pat = self.engine_pattern(sizes)
        p_mark, l_mark = self.engine_markov(sizes)
        p_z, l_z = self.engine_zscore(sizes)
        p_trend, l_trend = self.engine_trendseq(sizes)
        p_fz, l_fz = self.engine_fuzzy(sizes)

        raw_probs = {'pattern': p_pat, 'markov': p_mark, 'zscore': p_z, 'trendseq': p_trend, 'fuzzy': p_fz}
        final_probs = {}
        logic_tags = []
        
        weighted_sum = 0
        total_w = 0

        for name, p_big in raw_probs.items():
            w = self.weights[name]
            if w < 0.4:
                final_probs[name] = 1.0 - p_big
                logic_tags.append(f"INV_{name}")
            else:
                final_probs[name] = p_big
                # 🔥 FIX: Display logic for BOTH Big and Small
                direction = "B" if p_big >= 0.5 else "S"
                logic_tags.append(f"{name}({direction})")

            weighted_sum += final_probs[name] * w
            total_w += w

        final_p_big = weighted_sum / total_w if total_w > 0 else 0.5
        pred_size = 1 if final_p_big >= 0.5 else 0
        confidence = int(abs(final_p_big - 0.5) * 2 * 100)

        if 48 <= confidence <= 55:
            pred_size = 1 - pred_size
            logic_tags = ["CONTRARIAN_OVERRIDE"]
            confidence = 65
        
        confidence = min(88, max(55, confidence))

        # Number Matrix
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
            if n == (last_num + 1) % 10 or n == (last_num - 1 + 10) % 10: score += 15
            score *= self.number_penalties[n]
                
            if score > best_score:
                best_score = score
                best_num = n

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags),
            "engine_probs": final_probs
        }

PREDICTOR = HyperAdaptivePredictor()

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

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V122 HYPER-ADAPTIVE MATRIX</i>
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
    log.info("V122 Hyper-Adaptive loop started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']: PREDICTOR.loss_streak += 1
                    else: PREDICTOR.loss_streak = 0
                    
                    act_int = 1 if res['actual_size'] == 'BIG' else 0
                    PREDICTOR.update_learning(res.get('engine_probs'), act_int, res.get('num'), res.get('actual_num'))
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
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
    return web.Response(text="V122 Hyper-Adaptive is running!")

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
