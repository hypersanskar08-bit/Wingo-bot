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
    bot_name: str = "V150 TITAN BREAKER (FIXED)"

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
log = logging.getLogger("v150")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, 
                    mode TEXT, logic TEXT, sigs TEXT, created_at REAL
                )
            """)
            # 🔥 FIX: Add sigs column if it doesn't exist in old DB
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
# V150 TITAN BREAKER
# =========================
class TitanBreaker:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        
        self.engine_stats = {
            'Markov': {'w': 2.0, 'l': 0},
            'ZScore': {'w': 1.5, 'l': 0},
            'CUSUM': {'w': 1.5, 'l': 0},
            'Exhaustion': {'w': 2.0, 'l': 0}
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
        d = [x['num'] for x in reversed(data[-80:])]
        if not d: return 0
        last_n = d[0]
        trans = Counter()
        for i in range(1, len(d)):
            if d[i] == last_n: trans[d[i-1]] += 1
        gaps = {i: 80 for i in range(10)}
        for i in range(len(d)):
            if gaps[d[i]] == 80: gaps[d[i]] = i
        freq = Counter(d[:30])
        pool = [5, 6, 7, 8, 9] if side == 1 else [0, 1, 2, 3, 4]
        
        best_num, best_score = pool[0], -999
        for n in pool:
            trans_score = trans.get(n, 0) * 4.0
            gap_score = min(gaps[n], 30) * 1.5
            freq_score = freq.get(n, 0) * 1.0
            neigh_score = 3.0 if (n == (last_n + 1) % 10 or n == (last_n - 1 + 10) % 10) else 0
            total = trans_score + gap_score + freq_score + neigh_score + (n * 0.01)
            if total > best_score:
                best_score = total
                best_num = n
        return best_num

    def get_market_regime(self, seq):
        if len(seq) < 15: return "NORMAL"
        w = seq[-15:]
        switches = sum(1 for i in range(1, 15) if w[i] != w[i-1])
        rate = switches / 14.0
        if rate >= 0.73: return "ZIGZAG"
        if rate <= 0.40: return "TREND"
        return "NORMAL"

    def signal_cusum(self, seq):
        if len(seq) < 30: return 0
        pos_sum, neg_sum = 0.0, 0.0
        for s in seq[-30:]:
            pos_sum = max(0, pos_sum + s - 0.6)
            neg_sum = max(0, neg_sum - s + 0.4)
        if pos_sum > 2.5: return 1
        if neg_sum > 2.5: return 1
        return 0
    
    def signal_exhaustion(self, seq):
        streak = 1
        for i in range(len(seq)-2, -1, -1):
            if seq[i] == seq[-1]: streak += 1
            else: break
        if streak < 4: return 0
        breaks, conts = 0, 0
        for i in range(len(seq) - streak):
            if all(seq[i+j] == seq[-1] for j in range(streak)):
                if i + streak < len(seq):
                    if seq[i+streak] == seq[-1]: conts += 1
                    else: breaks += 1
        total = breaks + conts
        if total >= 2 and breaks / total >= 0.60: return 1
        return 0
    
    def signal_volatility(self, seq):
        if len(seq) < 10: return 0
        w = seq[-10:]
        last_side = seq[-1]
        opposite_count = sum(1 for x in w[-5:] if x != last_side)
        if opposite_count >= 1: return 1
        return 0

    def signal_ewma(self, seq):
        if len(seq) < 20: return 0
        alpha_s = 0.4
        alpha_l = 0.1
        ema_s = seq[-20]
        ema_l = seq[-20]
        for x in seq[-19:]:
            ema_s = alpha_s * x + (1 - alpha_s) * ema_s
            ema_l = alpha_l * x + (1 - alpha_l) * ema_l
        diff = ema_s - ema_l
        last_side = seq[-1]
        if last_side == 1 and diff < -0.1: return 1
        if last_side == 0 and diff > 0.1: return 1
        return 0

    def signal_antimarkov(self, seq):
        order = 3
        if len(seq) < order + 5: return 0
        target = tuple(seq[-order:])
        big, small = 0, 0
        for i in range(len(seq) - order):
            if tuple(seq[i:i+order]) == target:
                if seq[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 3:
            if seq[-1] == 1 and small / total >= 0.70: return 1
            if seq[-1] == 0 and big / total >= 0.70: return 1
        return 0
    
    def calculate_break_score(self, seq):
        signals = {
            'CUSUM': self.signal_cusum(seq),
            'Exhaustion': self.signal_exhaustion(seq),
            'Volatility': self.signal_volatility(seq),
            'EWMA': self.signal_ewma(seq),
            'AntiMarkov': self.signal_antimarkov(seq)
        }
        total = sum(signals.values())
        return total, signals

    def analyze_markov(self, seq):
        for order in [5, 4, 3, 2]:
            if len(seq) < order + 5: continue
            target = tuple(seq[-order:])
            big, small = 0, 0
            for i in range(len(seq) - order):
                if tuple(seq[i:i+order]) == target:
                    if seq[i+order] == 1: big += 1
                    else: small += 1
            total = big + small
            if total >= 3:
                p = (big + 1) / (total + 2)
                return (1 if p >= 0.5 else 0, max(p, 1-p)), f"M{order}"
        return None, ""

    def analyze_zscore(self, seq):
        if len(seq) < 30: return None, ""
        w = seq[-30:]
        z = (sum(w) - 15) / math.sqrt(30 * 0.25)
        if z > 1.6: return (0, 0.8), "ZScore(S)"
        if z < -1.6: return (1, 0.8), "ZScore(B)"
        return None, ""

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        last_size = seq[-1]
        regime = self.get_market_regime(seq)
        
        pred_size = last_size
        confidence = 60
        logic_tags = []

        if regime == "ZIGZAG":
            pred_size = 1 - last_size
            confidence = 75
            logic_tags = ["ZIGZAG_FLIP"]

        elif regime == "TREND":
            streak = 1
            for i in range(len(seq)-2, -1, -1):
                if seq[i] == last_size: streak += 1
                else: break

            break_score, _ = self.calculate_break_score(seq)

            if break_score >= 3:
                pred_size = 1 - last_size
                confidence = min(88, 70 + break_score * 3)
                logic_tags = [f"BREAK({break_score}/5)"]
                if break_score == 5:
                    logic_tags = ["MEGA_BREAK(5/5)"]
            elif break_score == 2:
                pred_size = last_size
                confidence = 60
                logic_tags = [f"TREND_RIDE({streak}x)", f"Warn_Break({break_score}/5)"]
            else:
                pred_size = last_size
                confidence = min(88, 70 + streak * 2)
                logic_tags = [f"TREND_RIDE({streak}x)"]

        else:
            signals = {}
            zs, zs_tag = self.analyze_zscore(seq)
            if zs: signals['ZScore'] = (zs, zs_tag)
            mk, mk_tag = self.analyze_markov(seq)
            if mk: signals['Markov'] = (mk, mk_tag)
            
            break_score, _ = self.calculate_break_score(seq)
            if break_score >= 3:
                pred_size = 1 - last_size
                confidence = 75
                logic_tags = ["NORMAL_BREAK"]
            else:
                log_odds = 0
                for name, ((direction, conf), tag) in signals.items():
                    stats = self.engine_stats.get(name, {'w': 1, 'l': 0})
                    acc = (stats['w'] + 1) / (stats['w'] + stats['l'] + 2)
                    weight = max(0.2, acc * 2.5)
                    log_odds += (1 if direction == 1 else -1) * math.log(conf / (1 - conf)) * weight
                    logic_tags.append(tag)

                if not signals:
                    pred_size = 1 if sum(seq[-20:]) >= 10 else 0
                    confidence = 55
                    logic_tags = ["Fallback"]
                else:
                    pred_size = 1 if log_odds > 0 else 0
                    confidence = int(min(88, max(60, 50 + abs(log_odds) * 10)))

        target_num = self.get_best_number(self.history, pred_size)
        mode = "VENGEANCE" if self.loss_streak >= 1 else "NORMAL"

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode,
            "regime": regime
        }

PREDICTOR = TitanBreaker()

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
⚖️ <b>Regime</b>: <b>{h(p['regime'])}</b>
🔮 <b>Mode</b>: <b>{h(p['mode'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V150 TITAN BREAKER</i>
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
    log.info("V150 Titan Breaker started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if res['win']: PREDICTOR.loss_streak = 0
                    else: PREDICTOR.loss_streak += 1
                    
                    # 🔥 FIX: Safe parsing for old DB records
                    sigs_json = res.get('sigs')
                    if sigs_json:
                        try:
                            sigs = json.loads(sigs_json)
                            for s in sigs:
                                name = s.split('_')[-1]
                                if name in PREDICTOR.engine_stats:
                                    if res['win']: PREDICTOR.engine_stats[name]['w'] += 1
                                    else: PREDICTOR.engine_stats[name]['l'] += 1
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
    return web.Response(text="V150 Titan Breaker is running!")

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
        # 🔥 FIX: Clear webhook to avoid Conflict error
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        
        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

