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
    bot_name: str = "V172 HURST-ADAPTIVE"

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
log = logging.getLogger("v172")
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
# V172 HURST-ADAPTIVE ENGINE
# =========================
class HurstAdaptive:
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

    # 🔥 1. HURST EXPONENT (Fractal Analysis)
    def get_hurst(self, ts):
        lags = range(2, min(20, len(ts)//2))
        tau = []
        for lag in lags:
            pp = [ts[i+lag] - ts[i] for i in range(len(ts)-lag)]
            if not pp: continue
            tau.append(math.sqrt(sum(p*p for p in pp) / len(pp)))
            
        if len(tau) < 2 or all(t == 0 for t in tau): return 0.5
        
        log_lags = [math.log(l) for l in lags][:len(tau)]
        log_tau = [math.log(t) if t > 0 else -10 for t in tau]
        
        n = len(log_lags)
        m_x = sum(log_lags) / n
        m_y = sum(log_tau) / n
        num = sum((log_lags[i] - m_x) * (log_tau[i] - m_y) for i in range(n))
        den = sum((log_lags[i] - m_x)**2 for i in range(n))
        
        if den == 0: return 0.5
        h_val = num / den
        return max(0.0, min(1.0, h_val))

    # 🔥 2. BOLLINGER BANDS
    def get_bollinger(self, seq, window=20):
        if len(seq) < window: return 0.5, 0.5
        w = seq[-window:]
        ma = sum(w) / window
        std = math.sqrt(sum((x - ma)**2 for x in w) / window)
        return ma, std

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

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        last_size = seq[-1]
        
        # Calculate Indicators
        h_val = self.get_hurst(seq[-30:])
        ma, std = self.get_bollinger(seq)
        upper_band = ma + 2 * std
        lower_band = ma - 2 * std
        
        pred_size = last_size
        confidence = 60
        logic_tags = []
        mode = "NORMAL"

        # ==========================================
        # 🔥 1. RECOVERY MODE (Loss Hone Par)
        # ==========================================
        if self.loss_streak >= 1:
            mode = "RECOVERY"
            # In recovery, if Hurst is high, follow trend. If low, use mean reversion.
            if h_val > 0.55:
                pred_size = last_size
                confidence = 75
                logic_tags = ["REC_Trend_Cont"]
            else:
                if last_size >= upper_band:
                    pred_size = 0
                    confidence = 80
                    logic_tags = ["REC_Bollinger_Rev"]
                elif last_size <= lower_band:
                    pred_size = 1
                    confidence = 80
                    logic_tags = ["REC_Bollinger_Rev"]
                else:
                    # Extreme Z-score
                    w30 = seq[-30:]
                    z = (sum(w30) - 15.0) / math.sqrt(30 * 0.25)
                    if z > 1.5: pred_size, confidence, logic_tags = 0, 75, ["REC_ZScore_S"]
                    elif z < -1.5: pred_size, confidence, logic_tags = 1, 75, ["REC_ZScore_B"]
                    else: pred_size, confidence, logic_tags = 1 - last_size, 65, ["REC_Flip"]
                    
        # ==========================================
        # 🔥 2. TRENDING MARKET (Hurst > 0.55)
        # ==========================================
        elif h_val > 0.55:
            mode = "TRENDING"
            # Follow the trend
            pred_size = last_size
            confidence = min(85, 65 + (h_val - 0.55) * 100)
            logic_tags = [f"Trend_Follow(H={h_val:.2f})"]

        # ==========================================
        # 🔥 3. MEAN REVERTING MARKET (Hurst < 0.45)
        # ==========================================
        elif h_val < 0.45:
            mode = "MEAN_REVERSION"
            # Use Bollinger Bands
            if last_size >= upper_band:
                pred_size = 0
                confidence = 78
                logic_tags = [f"Bollinger_Rev(H={h_val:.2f})"]
            elif last_size <= lower_band:
                pred_size = 1
                confidence = 78
                logic_tags = [f"Bollinger_Rev(H={h_val:.2f})"]
            else:
                # Use Z-score
                w30 = seq[-30:]
                z = (sum(w30) - 15.0) / math.sqrt(30 * 0.25)
                if z > 1.0: pred_size, confidence, logic_tags = 0, 70, [f"ZScore_S(H={h_val:.2f})"]
                elif z < -1.0: pred_size, confidence, logic_tags = 1, 70, [f"ZScore_B(H={h_val:.2f})"]
                else: pred_size, confidence, logic_tags = last_size, 55, ["Random_Walk"]

        # ==========================================
        # 🔥 4. NEUTRAL MARKET (0.45 <= Hurst <= 0.55)
        # ==========================================
        else:
            mode = "NEUTRAL"
            # Wait for confirmation (MA Crossover)
            ma5 = sum(seq[-5:]) / 5.0
            ma20 = sum(seq[-20:]) / 20.0
            if ma5 > ma20: pred_size, confidence, logic_tags = 1, 60, ["MA_Crossover_B"]
            elif ma5 < ma20: pred_size, confidence, logic_tags = 0, 60, ["MA_Crossover_S"]
            else: pred_size, confidence, logic_tags = last_size, 55, ["Neutral_Momentum"]

        target_num = self.get_best_number(self.history, pred_size)
        
        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": int(min(88, max(55, confidence))),
            "logic": " + ".join(logic_tags),
            "mode": mode,
            "sigs": logic_tags
        }

PREDICTOR = HurstAdaptive()

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
⚡ <i>V172 HURST-ADAPTIVE ENGINE</i>
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
    log.info("V172 Hurst Adaptive started...")
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
    return web.Response(text="V172 Hurst Adaptive is running!")

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
