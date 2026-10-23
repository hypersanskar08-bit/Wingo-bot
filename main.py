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
    bot_name: str = "V152 HYPER-RECOVERY TITAN"

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
log = logging.getLogger("v152")
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
            # Auto-fix for old databases missing 'sigs' column
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
# V152 HYPER-RECOVERY TITAN
# =========================
class HyperRecoveryTitan:
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

    # --- EXACT CSV LOGIC (JACK MAKER) ---
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

    # --- HTML LOGIC: RUN PATTERNS ---
    def extract_runs(self, seq):
        runs = []
        if not seq: return runs
        curr, length = seq[0], 1
        for x in seq[1:]:
            if x == curr: length += 1
            else:
                runs.append({"side": curr, "len": length})
                curr, length = x, 1
        runs.append({"side": curr, "len": length})
        return runs

    def analyze_run_pattern(self, runs):
        if len(runs) < 3: return None
        ongoing = runs[-1]
        best = None
        
        for k in range(min(13, len(runs)), 2, -1):
            window = runs[-k:]
            prefix = window[:-1]
            cont, brk = 0, 0
            
            for j in range(k, len(runs) - 1):
                cand = runs[j]
                if cand['len'] < ongoing['len']: continue
                ok = True
                for t in range(k - 1):
                    if runs[j - (k - 1) + t]['len'] != prefix[t]['len']:
                        ok = False
                        break
                if ok:
                    if cand['len'] > ongoing['len']: cont += 1
                    else: brk += 1
                    
            total = cont + brk
            if total >= 3:
                p_cont = (cont + 1) / (total + 2)
                dir = ongoing['side'] if p_cont >= 0.5 else 1 - ongoing['side']
                best = (dir, max(p_cont, 1 - p_cont))
                break
        return best

    # --- DEEP ENGINES ---
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
            return (1 if p >= 0.5 else 0, max(p, 1-p))
        return None

    def analyze_zscore(self, seq):
        if len(seq) < 30: return None
        w = seq[-30:]
        big_c = sum(w)
        z = (big_c - 15) / math.sqrt(30 * 0.25)
        if z > 1.8: return (0, 0.8)
        if z < -1.8: return (1, 0.8)
        return None

    def analyze_exhaustion(self, seq):
        streak = 1
        for i in range(len(seq)-2, -1, -1):
            if seq[i] == seq[-1]: streak += 1
            else: break
            
        if streak >= 4:
            breaks, conts = 0, 0
            for i in range(len(seq)-streak):
                if all(seq[i+j] == seq[-1] for j in range(streak)):
                    if i+streak < len(seq):
                        if seq[i+streak] == seq[-1]: conts += 1
                        else: breaks += 1
            if breaks + conts >= 2:
                if breaks > conts: return (1-seq[-1], 0.75)
                else: return (seq[-1], 0.75)
        return None

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        seq = [x["size"] for x in self.history]
        runs = self.extract_runs(seq)
        last_size = seq[-1]
        
        # 🔥 SUPER RECOVERY MODE (Triggered on 1 Loss)
        if self.loss_streak >= 1:
            logic_tags = ["RECOVERY_MODE"]
            
            # 1. Deep Markov (Order 5 down to 3)
            mk = None
            for o in [5, 4, 3]:
                mk = self.analyze_markov(seq, o)
                if mk: 
                    logic_tags.append(f"M{o}")
                    break
                    
            # 2. Exhaustion
            ex = self.analyze_exhaustion(seq)
            if ex:
                logic_tags.append("Exhaustion")
                
            # 3. Z-Score
            zs = self.analyze_zscore(seq)
            if zs:
                logic_tags.append("ZScore")
                
            # Decision: If Markov and Exhaustion agree, go with them
            if mk and ex and mk[0] == ex[0]:
                pred_size, conf = mk[0], max(mk[1], ex[1]) + 0.1
            elif mk:
                pred_size, conf = mk
            elif ex:
                pred_size, conf = ex
            elif zs:
                pred_size, conf = zs
            else:
                # Absolute fallback: follow last 10 majority
                pred_size = 1 if sum(seq[-10:]) >= 5 else 0
                conf = 0.6
                logic_tags = ["SafeMajority"]
                
            confidence = int(min(88, max(65, conf * 100)))
            mode = "SUPER_RECOVERY"

        # 🔥 NORMAL MODE
        else:
            logic_tags = []
            
            # 1. Run Pattern
            rp = self.analyze_run_pattern(runs)
            if rp:
                logic_tags.append("RunPattern")
                
            # 2. Markov 4
            mk = self.analyze_markov(seq, 4)
            if mk:
                logic_tags.append("Markov4")
                
            # 3. Z-Score
            zs = self.analyze_zscore(seq)
            if zs:
                logic_tags.append("ZScore")
                
            # Combine (Voting)
            votes = [rp, mk, zs]
            big_v = sum(v[1] for v in votes if v and v[0] == 1)
            small_v = sum(v[1] for v in votes if v and v[0] == 0)
            
            if big_v > small_v:
                pred_size = 1
                conf = big_v / (big_v + small_v)
            elif small_v > big_v:
                pred_size = 0
                conf = small_v / (big_v + small_v)
            else:
                pred_size = last_size
                conf = 0.6
                logic_tags = ["Momentum"]
                
            confidence = int(min(85, max(60, conf * 100)))
            mode = "NORMAL"

        # Target Number (Always CSV Logic)
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

PREDICTOR = HyperRecoveryTitan()

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
⚡ <i>V152 HYPER-RECOVERY TITAN</i>
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
    log.info("V152 Titan started...")
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
    return web.Response(text="V152 Titan is running!")

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
