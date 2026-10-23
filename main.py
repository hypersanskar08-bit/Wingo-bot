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
    bot_name: str = "V155 APEX PREDATOR (FIXED)"

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
log = logging.getLogger("v155")
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

STORE = Storage(CFG.db_path)

# =========================
# V155 APEX PREDATOR
# =========================
class ApexPredator:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.last_pred_size = None
        
        self.engine_stats = {
            'Markov': {'w': 2.0, 'l': 0},
            'ZScore': {'w': 1.5, 'l': 0},
            'CUSUM': {'w': 1.5, 'l': 0},
            'EWMA': {'w': 1.0, 'l': 0},
            'Trend': {'w': 1.0, 'l': 0}
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

    def get_best_number(self, data, side):
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

    def analyze_markov(self, seq, order=4):
        if len(seq) < order + 5: return None, ""
        target = tuple(seq[-order:])
        big, small = 0, 0
        for i in range(len(seq) - order):
            if tuple(seq[i:i+order]) == target:
                if seq[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            p = (big + 1) / (total + 2)
            return ((1 if p >= 0.5 else 0), max(p, 1-p)), f"M{order}"
        return None, ""

    def analyze_zscore(self, seq):
        if len(seq) < 30: return None, ""
        w = seq[-30:]
        z = (sum(w) - 15) / math.sqrt(30 * 0.25)
        if z > 1.6: return (0, 0.8), "ZScore(S)"
        if z < -1.6: return (1, 0.8), "ZScore(B)"
        return None, ""

    def analyze_cusum(self, seq):
        if len(seq) < 30: return None, ""
        pos_sum, neg_sum = 0.0, 0.0
        for s in seq[-30:]:
            pos_sum = max(0, pos_sum + s - 0.6)
            neg_sum = max(0, neg_sum - s + 0.4)
        if pos_sum > 2.5: return (0, 0.75), "CUSUM(S)"
        if neg_sum > 2.5: return (1, 0.75), "CUSUM(B)"
        return None, ""

    def analyze_ewma(self, seq):
        if len(seq) < 20: return None, ""
        alpha_s, alpha_l = 0.3, 0.05
        ema_s, ema_l = seq[-20], seq[-20]
        for x in seq[-19:]:
            ema_s = alpha_s * x + (1 - alpha_s) * ema_s
            ema_l = alpha_l * x + (1 - alpha_l) * ema_l
        diff = ema_s - ema_l
        if diff > 0.15: return (1, 0.65), "EWMA(B)"
        if diff < -0.15: return (0, 0.65), "EWMA(S)"
        return None, ""

    def get_meta_weight(self, engine_name):
        stats = self.engine_stats.get(engine_name, {'w': 1, 'l': 0})
        acc = (stats['w'] + 1) / (stats['w'] + stats['l'] + 2)
        return max(0.2, acc * 2.5)

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        last_size = seq[-1]
        signals = {}
        
        streak = 1
        for i in range(len(seq)-2, -1, -1):
            if seq[i] == last_size: streak += 1
            else: break

        # 🔥 1. ADVANCED RECOVERY MODE (1 Loss)
        if self.loss_streak >= 1:
            logic_tags = ["RECOVERY_MODE"]
            mode = "ADVANCED_RECOVERY"
            
            # FIX: Extract actual data (direction, conf) from tuple, not string
            zs, zs_tag = self.analyze_zscore(seq)
            if zs: signals['ZScore'] = (zs, 3.0)
            
            cu, cu_tag = self.analyze_cusum(seq)
            if cu: signals['CUSUM'] = (cu, 3.0)
            
            for o in [5, 4, 3]:
                mk, mk_tag = self.analyze_markov(seq, o)
                if mk:
                    signals['Markov'] = (mk, 2.0)
                    break
                    
            if self.last_pred_size != last_size and streak >= 3:
                signals['Trend'] = ((last_size, 0.8), 1.0)
                logic_tags.append("TREND_RIDE_FORCED")

        # 🧠 2. NORMAL MODE
        else:
            mode = "NORMAL"
            logic_tags = []
            
            mk, mk_tag = self.analyze_markov(seq, 4)
            if mk: signals['Markov'] = (mk, 1.0)
            
            zs, zs_tag = self.analyze_zscore(seq)
            if zs: signals['ZScore'] = (zs, 1.0)
            
            cu, cu_tag = self.analyze_cusum(seq)
            if cu: signals['CUSUM'] = (cu, 1.0)
            
            ew, ew_tag = self.analyze_ewma(seq)
            if ew: signals['EWMA'] = (ew, 1.0)
            
            if streak >= 4:
                signals['Trend'] = ((last_size, 0.85), 1.0)
                logic_tags.append(f"STRONG_TREND({streak}x)")

        # 3. Meta-Learner Mixer
        log_odds = 0
        for name, (data, weight_mult) in signals.items():
            direction, conf = data
            weight = self.get_meta_weight(name) * weight_mult
            log_odds += (1 if direction == 1 else -1) * math.log(conf / (1 - conf)) * weight
            logic_tags.append(name)

        # 4. Final Decision
        if not signals:
            pred_size = 1 if sum(seq[-20:]) >= 10 else 0
            confidence = 55
            logic_tags = ["SafeMajority"]
        else:
            pred_size = 1 if log_odds > 0 else 0
            confidence = int(min(88, max(60, 50 + abs(log_odds) * 10)))

        target_num = self.get_best_number(self.history, pred_size)
        self.last_pred_size = pred_size

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode,
            "sigs": list(signals.keys())
        }

PREDICTOR = ApexPredator()

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
⚡ <i>V155 APEX PREDATOR (FIXED)</i>
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
    log.info("V155 Apex Predator started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if res['win']: PREDICTOR.loss_streak = 0
                    else: PREDICTOR.loss_streak += 1
                    
                    sigs_json = res.get('sigs')
                    if sigs_json:
                        try:
                            sigs = json.loads(sigs_json)
                            for s in sigs:
                                if s in PREDICTOR.engine_stats:
                                    if res['win']: PREDICTOR.engine_stats[s]['w'] += 1
                                    else: PREDICTOR.engine_stats[s]['l'] += 1
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
    return web.Response(text="V155 Apex Predator is running!")

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
