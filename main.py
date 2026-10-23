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
    bot_name: str = "V170 ULTIMATE ADVANCED AI"

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
log = logging.getLogger("v170")
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
            # Expert Weights
            c.execute("""
                CREATE TABLE IF NOT EXISTS expert_stats (
                    name TEXT PRIMARY KEY, wins INTEGER, losses INTEGER
                )
            """)
            for eng in ['Trend', 'CUSUM', 'Pattern', 'Math', 'Markov']:
                c.execute("INSERT OR IGNORE INTO expert_stats (name, wins, losses) VALUES (?, 0, 0)", (eng,))

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

    def update_expert(self, name, is_win):
        with self._conn() as c:
            if is_win:
                c.execute("UPDATE expert_stats SET wins = wins + 1 WHERE name = ?", (name,))
            else:
                c.execute("UPDATE expert_stats SET losses = losses + 1 WHERE name = ?", (name,))
                
    def get_weights(self):
        weights = {}
        with self._conn() as c:
            rows = c.execute("SELECT name, wins, losses FROM expert_stats").fetchall()
            for name, w, l in rows:
                total = w + l
                if total < 3:
                    weights[name] = 1.0
                else:
                    acc = w / total
                    weights[name] = max(0.2, min(2.5, acc * 2.5))
        return weights

STORE = Storage(CFG.db_path)

# =========================
# MATH HELPERS
# =========================
def wilson_lb(wins, total, z=1.64):
    if total == 0: return 0
    p = wins / total
    d = 1 + z*z / total
    return (p + z*z/(2*total) - z * math.sqrt(p*(1-p)/total + z*z/(4*total*total))) / d

# =========================
# V170 ULTIMATE ADVANCED ENGINE
# =========================
class UltimateAdvancedAI:
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

    def get_best_number(self, data, side):
        # CSV fallback logic
        d = [x['num'] for x in reversed(data[-60:])]
        if not d: return 0
        last_n = d[0]
        freq = Counter(d[:30])
        tm = Counter()
        for i in range(1, len(d)):
            tm[f"{d[i]}>{d[i-1]}"] += 1
        pool = [5, 6, 7, 8, 9] if side == 1 else [0, 1, 2, 3, 4]
        
        scored = []
        for num in pool:
            gp = d.index(num) if num in pool else 60
            gg = min(gp, 30)
            tr = tm.get(f"{last_n}>{num}", 0)
            nh = 1.5 if (num == (last_n + 1) % 10 or num == (last_n + 9) % 10) else 0
            s = gg * 1.2 + freq.get(num, 0) * 1.5 + tr * 3 + nh
            scored.append((s, num))
            
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][1] if scored else pool[0]

    # 🔥 EXPERT 1: CUSUM (Trend Shift Detector)
    def expert_cusum(self, seq):
        if len(seq) < 20: return None
        pos, neg = 0.0, 0.0
        target, k, h_thresh = 0.5, 0.1, 2.0
        
        for s in seq[-30:]:
            pos = max(0, pos + s - target - k)
            neg = max(0, neg - s + target - k)
            
        if pos > h_thresh:
            return 1, min(0.90, 0.60 + pos * 0.05), "CUSUM_Shift(B)" # Shifted to BIG
        if neg > h_thresh:
            return 0, min(0.90, 0.60 + neg * 0.05), "CUSUM_Shift(S)" # Shifted to SMALL
        return None

    # 🔥 EXPERT 2: PATTERN MATCHER (Length 3-12)
    def expert_pattern(self, seq, nums):
        best_sig = None
        best_score = 0
        
        # Size Patterns (Length 6 to 12)
        for L in range(12, 5, -1):
            if len(seq) < L + 5: continue
            target = tuple(seq[-L:])
            big, small = 0, 0
            for i in range(len(seq) - L):
                if tuple(seq[i:i+L]) == target:
                    if i+L < len(seq):
                        if seq[i+L] == 1: big += 1
                        else: small += 1
            total = big + small
            if total >= 3:
                lb = wilson_lb(max(big, small), total)
                if lb > 0.60 and lb > best_score:
                    best_score = lb
                    best_sig = (1 if big > small else 0, lb, f"SzPat{L}")
                    
        # Number Patterns (Length 3 to 5)
        for L in range(5, 2, -1):
            if len(nums) < L + 5: continue
            target = tuple(nums[-L:])
            big, small = 0, 0
            for i in range(len(nums) - L):
                if tuple(nums[i:i+L]) == target:
                    if i+L < len(nums):
                        if nums[i+L] >= 5: big += 1
                        else: small += 1
            total = big + small
            if total >= 2:
                lb = wilson_lb(max(big, small), total)
                if lb > 0.60 and lb > best_score:
                    best_score = lb
                    best_sig = (1 if big > small else 0, lb, f"NumPat{L}")
                    
        return best_sig

    # 🔥 EXPERT 3: MATH (Z-Score / Autocorr)
    def expert_math(self, seq):
        if len(seq) < 30: return None
        w30 = seq[-30:]
        z = (sum(w30) - 15.0) / math.sqrt(30 * 0.25)
        if z > 1.8: return 0, 0.75, "ZScore(S)"
        if z < -1.8: return 1, 0.75, "ZScore(B)"
        return None

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        weights = STORE.get_weights()
        
        signals = []
        
        c_res = self.expert_cusum(seq)
        if c_res: signals.append(('CUSUM', c_res))
        
        p_res = self.expert_pattern(seq, nums)
        if p_res: signals.append(('Pattern', p_res))
        
        m_res = self.expert_math(seq)
        if m_res: signals.append(('Math', m_res))

        # ==========================================
        # 🔥 RECOVERY MODE (High Priority)
        # ==========================================
        if self.loss_streak >= 1:
            # Never use Z-Score in recovery. Only trust CUSUM Shift or Pattern.
            valid_sigs = [s for s in signals if s[0] in ['CUSUM', 'Pattern']]
            if valid_sigs:
                best = max(valid_sigs, key=lambda x: x[1][1] * weights.get(x[0], 1.0))
                tag = "REC_" + best[1][2]
                return self._build_response(best[1][0], best[1][1], tag, "RECOVERY")
            else:
                # If no signal, FORCE FLIP from last result (since 1 loss means last was wrong)
                pred = 1 - seq[-1]
                return self._build_response(pred, 0.75, "REC_ForceFlip", "RECOVERY")

        # ==========================================
        # 🔥 NORMAL MODE (Meta-Learner Mixer)
        # ==========================================
        if not signals:
            # Absolute Fallback: Majority of last 10
            pred = 1 if sum(seq[-10:]) >= 5 else 0
            return self._build_response(pred, 0.55, "MajorityFallback", "NORMAL")

        log_odds = 0
        logic_tags = []
        
        for name, (direction, conf, tag) in signals:
            weight = weights.get(name, 1.0)
            log_odds += (1 if direction == 1 else -1) * math.log(conf / (1 - conf)) * weight
            logic_tags.append(tag)

        pred_size = 1 if log_odds > 0 else 0
        confidence = int(min(88, max(60, 50 + abs(log_odds) * 10)))

        return self._build_response(pred_size, confidence/100, " + ".join(logic_tags[:2]), "NORMAL")

    def _build_response(self, pred_size, conf, tag, mode):
        target_num = self.get_best_number(self.history, pred_size)
        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": int(min(90, max(55, conf * 100))),
            "logic": tag,
            "mode": mode,
            "sigs": [tag]
        }

PREDICTOR = UltimateAdvancedAI()

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
⚡ <i>V170 ULTIMATE ADVANCED AI</i>
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
    log.info("V170 Ultimate Advanced AI started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if res['win']: PREDICTOR.loss_streak = 0
                    else: PREDICTOR.loss_streak += 1
                    
                    try:
                        sigs = json.loads(res.get('sigs') or '[]')
                        for s in sigs:
                            for eng in ['Trend', 'CUSUM', 'Pattern', 'Math', 'Markov']:
                                if eng in s:
                                    STORE.update_expert(eng, res['win'])
                                    break
                    except Exception:
                        pass
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
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
    return web.Response(text="V170 Ultimate is running!")

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
