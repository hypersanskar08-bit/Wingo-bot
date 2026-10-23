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
    bot_name: str = "V146 ULTIMATE TITAN"

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
log = logging.getLogger("v146")
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
# V146 ULTIMATE TITAN
# =========================
class UltimateTitan:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        
        # 6 Engines for Meta-Learner
        self.engine_stats = {
            'Run': {'w': 1, 'l': 0},
            'Markov': {'w': 1, 'l': 0},
            'ZScore': {'w': 1, 'l': 0},
            'Entropy': {'w': 1, 'l': 0},
            'CUSUM': {'w': 1, 'l': 0},
            'Fuzzy': {'w': 1, 'l': 0}
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

    # --- EXACT CSV LOGIC: hxDigitScores (JACK MAKER) ---
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

    # --- ENGINE 1: RUN PATTERN ---
    def analyze_run(self, seq):
        runs = []
        curr, count = seq[0], 1
        for x in seq[1:]:
            if x == curr: count += 1
            else:
                runs.append((curr, count))
                curr, count = x, 1
        runs.append((curr, count))
        
        n = len(runs)
        if n < 3: return None
        
        ongoing_side, ongoing_len = runs[-1]
        
        for k in [4, 3, 2]:
            if n < k + 1: continue
            target = [r[1] for r in runs[-k:]]
            cont, brk = 0, 0
            
            for j in range(k, n - 1):
                cand = runs[j]
                if cand[1] < ongoing_len: continue
                ok = True
                for t in range(k):
                    if runs[j - k + t][1] != target[t]:
                        ok = False
                        break
                if ok:
                    if cand[1] > ongoing_len: cont += 1
                    else: brk += 1
                    
            if cont + brk >= 2:
                p_cont = (cont + 1) / (cont + brk + 2)
                return (ongoing_side, p_cont) if p_cont >= 0.5 else (1 - ongoing_side, 1 - p_cont)
        return None

    # --- ENGINE 2: DEEP MARKOV (Multi-Order) ---
    def analyze_markov(self, seq, order):
        if len(seq) < order + 5: return None
        target = tuple(seq[-order:])
        big, small = 0, 0
        for i in range(len(seq) - order):
            if tuple(seq[i:i+order]) == target:
                if seq[i+order] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            p = (big + 1) / (total + 2)
            return (1, p) if p >= 0.5 else (0, 1-p)
        return None

    # --- ENGINE 3: Z-SCORE (Mean Reversion) ---
    def analyze_zscore(self, seq):
        if len(seq) < 30: return None
        w = seq[-30:]
        big_c = sum(w)
        z = (big_c - 15) / math.sqrt(30 * 0.25)
        if z > 1.8: return (0, 0.8)
        if z < -1.8: return (1, 0.8)
        return None

    # --- ENGINE 4: SHANNON ENTROPY (Chaos Detector) ---
    def analyze_entropy(self, seq):
        if len(seq) < 20: return None
        w = seq[-20:]
        p1 = sum(w) / 20.0
        p0 = 1 - p1
        ent = 0
        if p1 > 0: ent -= p1 * math.log2(p1)
        if p0 > 0: ent -= p0 * math.log2(p0)
        
        # High entropy = chaotic, expect flip. Low entropy = trending.
        if ent > 0.95: # Chaos
            return (1 - seq[-1], 0.65)
        elif ent < 0.5 and p1 > 0.5: # Strong Big Trend
            return (1, 0.70)
        elif ent < 0.5 and p1 < 0.5: # Strong Small Trend
            return (0, 0.70)
        return None

    # --- ENGINE 5: CUSUM (Trend Shift Detector) ---
    def analyze_cusum(self, seq):
        if len(seq) < 30: return None
        target = 0.5
        pos_sum, neg_sum = 0.0, 0.0
        for s in seq[-30:]:
            pos_sum = max(0, pos_sum + s - target - 0.1)
            neg_sum = max(0, neg_sum - s + target - 0.1)
            
        if pos_sum > 2.5: return (0, 0.75) # Shifted to BIG too much, expect SMALL
        if neg_sum > 2.5: return (1, 0.75)
        return None

    # --- ENGINE 6: FUZZY N-GRAM (Hamming Distance) ---
    def analyze_fuzzy(self, seq):
        if len(seq) < 10: return None
        target = seq[-6:]
        best_match = None
        max_matches = 0
        
        for i in range(len(seq) - 7):
            window = seq[i:i+6]
            matches = sum(1 for a, b in zip(target, window) if a == b)
            if matches >= 5 and matches > max_matches:
                max_matches = matches
                best_match = seq[i+6]
                
        if best_match is not None:
            return (best_match, 0.65 + (max_matches - 5) * 0.1)
        return None

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        signals = {}
        
        # 🔥 1 LOSS = ULTRA VENGEANCE
        if self.loss_streak >= 1:
            zs = self.analyze_zscore(seq)
            if zs: signals['V_ZScore'] = zs
            
            cu = self.analyze_cusum(seq)
            if cu: signals['V_CUSUM'] = cu
            
            for order in [6, 5, 4]:
                mk = self.analyze_markov(seq, order)
                if mk:
                    signals[f'V_Markov{order}'] = mk
                    break
                    
            en = self.analyze_entropy(seq)
            if en: signals['V_Entropy'] = en
            
            # If still no signals, Force Check Majority
            if not signals:
                big_c = sum(seq[-20:])
                signals['SafeMajority'] = (1 if big_c >= 10 else 0, 0.55)

        # NORMAL MODE
        else:
            rn = self.analyze_run(seq)
            if rn: signals['Run'] = rn
            
            fz = self.analyze_fuzzy(seq)
            if fz: signals['Fuzzy'] = fz
            
            mk = self.analyze_markov(seq, 4)
            if mk: signals['Markov4'] = mk
            
            zs = self.analyze_zscore(seq)
            if zs: signals['ZScore'] = zs

        # Meta-Learner Mixer
        total_w = 0
        log_odds = 0
        logic_tags = []
        
        for name, (direction, conf) in signals.items():
            stats = self.engine_stats.get(name.split('_')[-1], {'w': 1, 'l': 0})
            acc = (stats['w'] + 1) / (stats['w'] + stats['l'] + 2)
            weight = max(0.5, acc * 2.5)
            weight *= (conf / 0.5)
            
            log_odds += (1 if direction == 1 else -1) * math.log(conf / (1 - conf)) * weight
            total_w += weight
            logic_tags.append(name)

        pred_size = 1 if log_odds > 0 else 0
        confidence = int(min(88, max(60, 50 + abs(log_odds) * 10)))

        # Target Number
        target_num = self.get_best_number(self.history, pred_size)

        mode = "ULTRA_VENGEANCE" if self.loss_streak >= 1 else "NORMAL"

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode,
            "sigs": list(signals.keys())
        }

PREDICTOR = UltimateTitan()

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
⚡ <i>V146 ULTIMATE TITAN</i>
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
    log.info("V146 Ultimate Titan started...")
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
                        sigs = json.loads(res['sigs'])
                        for s in sigs:
                            name = s.split('_')[-1]
                            if name in PREDICTOR.engine_stats:
                                if res['win']: PREDICTOR.engine_stats[name]['w'] += 1
                                else: PREDICTOR.engine_stats[name]['l'] += 1
                    except: pass
                    
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
    return web.Response(text="V146 Ultimate Titan is running!")

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

