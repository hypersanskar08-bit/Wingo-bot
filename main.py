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
    request_timeout: float = 15.0
    bot_name: str = "V164 DEEP-TRACKER"

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
log = logging.getLogger("v164")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, 
                    mode TEXT, logic TEXT, sigs TEXT, created_at REAL
                )
            """)
            try:
                c.execute("ALTER TABLE predictions ADD COLUMN sigs TEXT")
            except sqlite3.OperationalError:
                pass
            c.execute("""
                CREATE TABLE IF NOT EXISTS model_weights (
                    id INTEGER PRIMARY KEY, value REAL
                )
            """)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, issue, size, num, mode, logic, sigs):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, mode, logic, sigs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                      (issue, size, num, mode, logic, json.dumps(sigs), time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, mode, sigs FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, mode, sigs_json in rows:
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
                    "status": status, "mode": mode, "sigs": sigs_json, "win": win
                })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]
        
    def save_weights(self, weights):
        with self._conn() as c:
            c.execute("DELETE FROM model_weights")
            for i, w in enumerate(weights):
                c.execute("INSERT INTO model_weights (id, value) VALUES (?, ?)", (i, w))
                
    def load_weights(self):
        try:
            with self._conn() as c:
                rows = c.execute("SELECT value FROM model_weights ORDER BY id").fetchall()
                return [r[0] for r in rows]
        except Exception:
            return None

STORE = Storage(CFG.db_path)

# =========================
# MATH HELPERS
# =========================
def skewness(seq):
    n = len(seq)
    mean = sum(seq)/n
    var = sum((x-mean)**2 for x in seq) / n
    if var == 0: return 0
    m3 = sum((x-mean)**3 for x in seq) / n
    return m3 / (var**1.5)

def autocorr(seq, lag):
    n = len(seq)
    if n <= lag: return 0
    mean = sum(seq)/n
    var = sum((x-mean)**2 for x in seq)
    if var == 0: return 0
    num = sum((seq[i]-mean)*(seq[i+lag]-mean) for i in range(n-lag))
    return num / var

# =========================
# V164 DEEP-TRACKER ENGINE
# =========================
class DeepTracker:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.lr = 0.05
        
        saved_weights = STORE.load_weights()
        if saved_weights and len(saved_weights) == 23:
            self.weights = saved_weights
            self.warmed_up = True
        else:
            self.weights = [0.0] * 23
            self.warmed_up = False
            
        self.save_counter = 0

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

    # --- 🔥 SPLIT PATTERN SCANNER ---
    def scan_patterns(self, seq, nums):
        sig_num_short = 0.0
        sig_size_long = 0.0
        sig_single = 0.0
        
        # 1. Short Combined Pattern (Length 3 to 5) - Numbers & Size
        for L in range(5, 2, -1):
            if len(seq) < L + 5: continue
            target_seq = tuple(seq[-L:])
            target_num = tuple(nums[-L:])
            
            big, small = 0, 0
            for i in range(len(seq) - L):
                if tuple(seq[i:i+L]) == target_seq and tuple(nums[i:i+L]) == target_num:
                    if seq[i+L] == 1: big += 1
                    else: small += 1
            total = big + small
            if total >= 2:
                p = (big + 1) / (total + 2)
                sig_num_short = (p - 0.5) * 2.0
                break

        # 2. Long Size Pattern (Length 6 to 12) - Only Size
        for L in range(12, 5, -1):
            if len(seq) < L + 5: continue
            target_seq = tuple(seq[-L:])
            
            big, small = 0, 0
            for i in range(len(seq) - L):
                if tuple(seq[i:i+L]) == target_seq:
                    if seq[i+L] == 1: big += 1
                    else: small += 1
            total = big + small
            if total >= 3:
                p = (big + 1) / (total + 2)
                sig_size_long = (p - 0.5) * 2.0
                break

        # 3. Single Number Repetition (Length 7 to 12)
        if len(nums) >= 12:
            for L in range(12, 6, -1):
                if len(nums) < L + 5: continue
                window = nums[-L:]
                freq = Counter(window)
                dominant, count = freq.most_common(1)[0]
                
                if count >= 4:
                    follow_big, follow_small = 0, 0
                    for i in range(len(nums) - L):
                        hist_window = nums[i:i+L]
                        if hist_window.count(dominant) >= count:
                            if i+L < len(nums):
                                if nums[i+L] >= 5: follow_big += 1
                                else: follow_small += 1
                    total = follow_big + follow_small
                    if total >= 3:
                        p = (follow_big + 1) / (total + 2)
                        sig_single = (p - 0.5) * 2.0
                        break
                        
        return sig_num_short, sig_size_long, sig_single

    def extract_features(self, seq, nums):
        if len(seq) < 50: return None
        feat = [1.0] # 0. Bias
        
        # 1-5: Recent 5 sizes
        for i in range(1, 6): feat.append(1.0 if seq[-1-i] == 1 else -1.0)
            
        # 6-10: Markov 2 to 6
        for order in [2, 3, 4, 5, 6]:
            target = tuple(seq[-order:])
            big, small = 0, 0
            for i in range(len(seq) - order):
                if tuple(seq[i:i+order]) == target:
                    if seq[i+order] == 1: big += 1
                    else: small += 1
            total = big + small
            p = (big + 1) / (total + 2) if total > 0 else 0.5
            feat.append((p - 0.5) * 2.0)
            
        # 11: Z-Score (30)
        w30 = seq[-30:]
        z = (sum(w30) - 15.0) / math.sqrt(30 * 0.25)
        feat.append(max(-2.0, min(2.0, z)) / 2.0)
        
        # 12: CUSUM
        pos, neg = 0.0, 0.0
        for s in seq[-30:]:
            pos = max(0, pos + s - 0.6)
            neg = max(0, neg - s + 0.4)
        feat.append(max(-1.0, min(1.0, (pos - neg) / 3.0)))
        
        # 13: Entropy
        w20 = seq[-20:]
        p_big = sum(w20) / 20.0
        p_small = 1.0 - p_big
        ent = 0.0
        if p_big > 0: ent -= p_big * math.log2(p_big)
        if p_small > 0: ent -= p_small * math.log2(p_small)
        feat.append((ent - 0.5) * 2.0)
        
        # 14: Streak
        streak = 1
        for i in range(len(seq)-2, -1, -1):
            if seq[i] == seq[-1]: streak += 1
            else: break
        direction = 1.0 if seq[-1] == 1 else -1.0
        feat.append((min(10, streak) / 10.0) * direction)
        
        # 15: Momentum
        feat.append((sum(seq[-10:]) / 10.0 - 0.5) * 2.0)
        
        # 🔥 16-18: Split Pattern Results
        sig_num_short, sig_size_long, sig_single = self.scan_patterns(seq, nums)
        feat.append(sig_num_short)
        feat.append(sig_size_long)
        feat.append(sig_single)
        
        # 19: Trend Shift (After Size Pattern Break)
        sig_trend = 0.0
        if len(seq) >= 15:
            target = tuple(seq[-6:])
            last_side = seq[-1]
            follow_same, follow_opp = 0, 0
            for i in range(len(seq) - 6):
                if tuple(seq[i:i+6]) == target:
                    if seq[i+6] == last_side: follow_same += 1
                    else: follow_opp += 1
            total = follow_same + follow_opp
            if total >= 3:
                p_same = (follow_same + 1) / (total + 2)
                p_big = p_same if last_side == 1 else (1 - p_same)
                sig_trend = (p_big - 0.5) * 2.0
        feat.append(sig_trend)
        
        # 20-22: Autocorrelation
        feat.append(max(-1, min(1, autocorr(seq[-30:], 1))))
        feat.append(max(-1, min(1, autocorr(seq[-30:], 2))))
        feat.append(max(-1, min(1, autocorr(seq[-30:], 3))))
        
        # 23: Skewness
        sk = max(-2, min(2, skewness(seq[-30:])))
        feat.append(sk / 2.0)
        
        return feat[:23]

    def sigmoid(self, x):
        try: return 1 / (1 + math.exp(-max(-20, min(20, x))))
        except OverflowError: return 0.0 if x < 0 else 1.0

    def train_model(self, feat, actual_size):
        if not feat or len(feat) != 23: return
        z = sum(w * f for w, f in zip(self.weights, feat))
        pred = self.sigmoid(z)
        
        error = actual_size - pred
        for i in range(23):
            reg = 0.001 * self.weights[i]
            self.weights[i] += self.lr * (error * feat[i] - reg)
            
        self.save_counter += 1
        if self.save_counter % 10 == 0:
            STORE.save_weights(self.weights)

    def warmup_training(self, seq, nums):
        print("🔥 Warmup Training on 1000 results started (5 Epochs)...")
        for epoch in range(5):
            for i in range(50, len(seq)):
                p_seq = seq[:i]
                p_nums = nums[:i]
                p_feat = self.extract_features(p_seq, p_nums)
                if p_feat:
                    z = sum(w * f for w, f in zip(self.weights, p_feat))
                    pred = self.sigmoid(z)
                    err = seq[i] - pred
                    for j in range(23):
                        reg = 0.001 * self.weights[j]
                        self.weights[j] += self.lr * (err * p_feat[j] - reg)
        STORE.save_weights(self.weights)
        self.warmed_up = True
        print("🔥 Warmup Training complete!")

    def get_best_number(self, data, side):
        nums = [x['num'] for x in data]
        
        # 1. Long Pattern (Single Number)
        recent_10 = nums[-10:]
        freq = Counter(recent_10)
        dominant, count = freq.most_common(1)[0]
        if count >= 4:
            next_nums = []
            for i in range(len(nums) - 10):
                w = nums[i:i+10]
                if w.count(dominant) >= count:
                    if i+10 < len(nums):
                        next_nums.append(nums[i+10])
            if next_nums:
                pool_next = [n for n in next_nums if (n >= 5) == (side == 1)]
                if pool_next:
                    return Counter(pool_next).most_common(1)[0][0]
        
        # 2. Short Pattern
        for L in [5, 4, 3]:
            if len(nums) < L + 5: continue
            target = tuple(nums[-L:])
            follows = []
            for i in range(len(nums) - L):
                if tuple(nums[i:i+L]) == target:
                    if i+L < len(nums):
                        follows.append(nums[i+L])
            if follows:
                pool_follows = [n for n in follows if (n >= 5) == (side == 1)]
                if pool_follows:
                    return Counter(pool_follows).most_common(1)[0][0]
                    
        # 3. CSV Fallback
        d = nums[::-1]
        last_n = d[0]
        freq30 = Counter(d[:30])
        tm = Counter()
        for i in range(1, len(d)): tm[f"{d[i]}>{d[i-1]}"] += 1
        pool = [5, 6, 7, 8, 9] if side == 1 else [0, 1, 2, 3, 4]
        
        scored = []
        for num in pool:
            gp = d.index(num) if num in pool else 60
            gg = min(gp, 30)
            tr = tm.get(f"{last_n}>{num}", 0)
            nh = 1.5 if (num == (last_n + 1) % 10 or num == (last_n + 9) % 10) else 0
            s = gg * 1.2 + freq30.get(num, 0) * 1.5 + tr * 3 + nh
            scored.append((s, num))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][1]

    def generate_prediction(self):
        if len(self.history) < 100: return None
        
        seq = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        if not self.warmed_up:
            self.warmup_training(seq, nums)
        
        feat = self.extract_features(seq, nums)
        if not feat: return None
        
        z = sum(w * f for w, f in zip(self.weights, feat))
        p_big = self.sigmoid(z)
        
        # Advanced Recovery Logic
        if self.loss_streak >= 1:
            pattern_signal = (feat[16] + feat[17] + feat[18] + feat[19]) / 4.0
            p_big = p_big * 0.4 + (0.5 + pattern_signal / 2.0) * 0.6
            
        pred_size = 1 if p_big >= 0.5 else 0
        conf_val = p_big if pred_size == 1 else (1 - p_big)
        confidence = int(min(88, max(52, conf_val * 100)))
        
        logic_tags = []
        if abs(feat[16]) > 0.2: logic_tags.append("NumShort(3-5)")
        if abs(feat[17]) > 0.2: logic_tags.append("SizeLong(6-12)")
        if abs(feat[18]) > 0.2: logic_tags.append("SingleRep(7-12)")
        if abs(feat[19]) > 0.2: logic_tags.append("TrendShift")
        if not logic_tags: logic_tags.append("PureMath")
        
        mode = "RECOVERY" if self.loss_streak >= 1 else "NEURAL_DEEP"
        target_num = self.get_best_number(self.history, pred_size)

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode,
            "sigs": logic_tags
        }

PREDICTOR = DeepTracker()

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
⚡ <i>V164 DEEP-TRACKER</i>
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
    log.info("V164 Deep Tracker started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if res['win']: PREDICTOR.loss_streak = 0
                    else: PREDICTOR.loss_streak += 1
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        if len(PREDICTOR.history) > 1:
                            past_seq = [x["size"] for x in PREDICTOR.history[:-1]]
                            past_nums = [x["num"] for x in PREDICTOR.history[:-1]]
                            past_feat = PREDICTOR.extract_features(past_seq, past_nums)
                            if past_feat:
                                PREDICTOR.train_model(past_feat, PREDICTOR.history[-1]["size"])

                        p = PREDICTOR.generate_prediction()
                        if p:
                            STORE.save_prediction(p['issue'], p['size'], p['num'], p['mode'], p['logic'], p['sigs'])
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
    return web.Response(text="V164 Deep Tracker is running!")

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
