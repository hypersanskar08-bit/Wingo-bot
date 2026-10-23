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
    bot_name: str = "V168 OMNIPOTENT AI"

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
log = logging.getLogger("v168")
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
            # Expert Weights for Meta-Learner
            c.execute("""
                CREATE TABLE IF NOT EXISTS expert_stats (
                    name TEXT PRIMARY KEY, wins INTEGER, losses INTEGER
                )
            """)
            # Init experts
            for eng in ['Trend', 'Chop', 'Pattern', 'Math']:
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
                if total < 5:
                    weights[name] = 1.0 # Default weight
                else:
                    acc = w / total
                    # Weight ranges from 0.2 to 2.0 based on accuracy
                    weights[name] = max(0.2, min(2.0, acc * 2.0))
        return weights

STORE = Storage(CFG.db_path)

# =========================
# V168 OMNIPOTENT ENGINE
# =========================
class OmnipotentAI:
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

    def get_regime(self, seq):
        if len(seq) < 15: return "NORMAL"
        w = seq[-15:]
        switches = sum(1 for i in range(1, 15) if w[i] != w[i-1])
        if switches >= 10: return "CHOP"
        if switches <= 4: return "TREND"
        return "NORMAL"

    def get_best_number(self, data, side):
        # CSV logic
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

    # 🔥 EXPERTS
    def expert_trend(self, seq):
        streak = 1
        for i in range(len(seq)-2, -1, -1):
            if seq[i] == seq[-1]: streak += 1
            else: break
        if streak >= 3:
            return seq[-1], min(0.9, 0.65 + streak * 0.05), f"Trend({streak}x)"
        return None

    def expert_chop(self, seq):
        if self.get_regime(seq) == "CHOP":
            return 1 - seq[-1], 0.70, "ChopFlip"
        return None

    def expert_pattern(self, seq, nums):
        # Check Size Pattern
        for L in range(12, 2, -1):
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
                p = (big+1)/(total+2)
                if p >= 0.6 or p <= 0.4:
                    return (1 if p >= 0.5 else 0), max(p, 1-p), f"Pat{L}"
        return None

    def expert_math(self, seq):
        # Z-Score
        w30 = seq[-30:]
        z = (sum(w30) - 15.0) / math.sqrt(30 * 0.25)
        if z > 1.6: return 0, 0.70, "ZScore(S)"
        if z < -1.6: return 1, 0.70, "ZScore(B)"
        return None

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        regime = self.get_regime(seq)
        weights = STORE.get_weights()
        
        signals = []
        
        # ==========================================
        # 🔥 1. COLLECT SIGNALS FROM EXPERTS
        # ==========================================
        t_res = self.expert_trend(seq)
        if t_res: signals.append(('Trend', t_res))
        
        c_res = self.expert_chop(seq)
        if c_res: signals.append(('Chop', c_res))
        
        p_res = self.expert_pattern(seq, nums)
        if p_res: signals.append(('Pattern', p_res))
        
        m_res = self.expert_math(seq)
        if m_res: signals.append(('Math', m_res))

        # ==========================================
        # 🔥 2. RECOVERY OVERRIDE
        # ==========================================
        if self.loss_streak >= 2:
            # Force pure math (Z-Score) or most confident signal
            best_sig = None
            if m_res:
                best_sig = m_res
            else:
                best_sig = (1 - seq[-1], 0.65, "RecoveryFlip")
            
            return self._build_response(best_sig[0], best_sig[1], best_sig[2], "ULTRA_RECOVERY")

        if self.loss_streak == 1:
            # Use Z-Score if available, else best signal
            if m_res:
                return self._build_response(m_res[0], m_res[1], m_res[2], "RECOVERY")
            elif signals:
                best = max(signals, key=lambda x: x[1][1])
                return self._build_response(best[1][0], best[1][1], best[1][2], "RECOVERY")

        # ==========================================
        # 🔥 3. META-LEARNER MIXER (Normal Mode)
        # ==========================================
        if not signals:
            pred = seq[-1] # Fallback
            return self._build_response(pred, 0.55, "Fallback", "NORMAL")

        log_odds = 0
        logic_tags = []
        
        for name, (direction, conf, tag) in signals:
            weight = weights.get(name, 1.0)
            log_odds += (1 if direction == 1 else -1) * math.log(conf / (1 - conf)) * weight
            logic_tags.append(tag)

        pred_size = 1 if log_odds > 0 else 0
        confidence = int(min(88, max(60, 50 + abs(log_odds) * 10)))
        
        # If confidence is too low, trust the trend/regime
        if confidence < 60:
            if regime == "TREND":
                pred_size = seq[-1]
                confidence = 65
                logic_tags = ["RegimeTrend"]
            else:
                pred_size = seq[-1]
                confidence = 55
                logic_tags = ["RegimeFollow"]

        return self._build_response(pred_size, confidence/100, " + ".join(logic_tags[:2]), "NORMAL")

    def _build_response(self, pred_size, conf, tag, mode):
        target_num = self.get_best_number(self.history, pred_size)
        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": int(min(88, max(52, conf * 100))),
            "logic": tag,
            "mode": mode,
            "sigs": [tag] # Save for meta-learner update
        }

PREDICTOR = OmnipotentAI()

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
⚡ <i>V168 OMNIPOTENT AI</i>
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
    log.info("V168 Omnipotent AI started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if res['win']: PREDICTOR.loss_streak = 0
                    else: PREDICTOR.loss_streak += 1
                    
                    # Update Meta-Learner
                    try:
                        sigs = json.loads(res.get('sigs') or '[]')
                        for s in sigs:
                            # Extract expert name from tag (e.g., 'Trend' from 'Trend(3x)')
                            for eng in ['Trend', 'Chop', 'Pattern', 'Math']:
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
    return web.Response(text="V168 Omnipotent is running!")

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
