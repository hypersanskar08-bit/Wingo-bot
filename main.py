import asyncio
import contextlib
import gc
import html
import itertools
import json
import logging
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict
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
    bot_name: str = "V137 HYPER-QUANTUM"

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
log = logging.getLogger("v137")
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS vault (
                    pat_key TEXT PRIMARY KEY, wins INTEGER, total INTEGER
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

    def update_vault(self, key, hit):
        with self._conn() as c:
            row = c.execute("SELECT wins, total FROM vault WHERE pat_key=?", (key,)).fetchone()
            if row:
                c.execute("UPDATE vault SET wins=?, total=? WHERE pat_key=?", (row[0]+hit, row[1]+1, key))
            else:
                c.execute("INSERT INTO vault (pat_key, wins, total) VALUES (?, ?, ?)", (key, hit, 1))
                
    def get_vault(self):
        with self._conn() as c:
            rows = c.execute("SELECT pat_key, wins, total FROM vault").fetchall()
            return {k: (w, t) for k, w, t in rows}

STORE = Storage(CFG.db_path)

# =========================
# UTILS & MATH
# =========================
def binom_tail(n, c, p):
    if c <= 0: return 1
    if c > n: return 0
    tot = 0
    coef = 1
    for k in range(n + 1):
        if k > 0: coef = coef * (n - k + 1) / k
        if k >= c: tot += coef * (p ** k) * ((1 - p) ** (n - k))
    return min(1, tot)

def bayes_conf(wins, total):
    if total == 0: return 0.5
    return (wins + 1) / (total + 2)

def sample_weight(total):
    return total / (total + 5)

def logit(p):
    p = max(0.03, min(0.97, p))
    return math.log(p / (1 - p))

# =========================
# V137 HYPER-QUANTUM (META-LEARNER)
# =========================
class HyperQuantum:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.vault = STORE.get_vault()
        
        # Meta-Learner Stats
        self.engine_stats = {
            'Pattern': {'wins': 0, 'total': 0},
            'Markov': {'wins': 0, 'total': 0},
            'ZScore': {'wins': 0, 'total': 0},
            'Trend': {'wins': 0, 'total': 0}
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

    def get_engine_weight(self, name):
        stats = self.engine_stats[name]
        if stats['total'] < 5: return 1.0
        acc = stats['wins'] / stats['total']
        return max(0.2, acc * 2) # If 50% acc, weight is 1.0. If 30%, weight is 0.6.

    def vault_mult(self, key):
        if key not in self.vault: return 1.0
        wins, total = self.vault[key]
        acc = (wins + 1) / (total + 2)
        return max(0.6, min(1.5, 1 + (acc - 0.5) * 4))

    # --- PATTERN MINING (HTML PORT) ---
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

    def analyze_run(self, runs):
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
                p_cont = bayes_conf(cont, total)
                adj_p = 0.5 + (p_cont - 0.5) * sample_weight(total)
                dir = ongoing['side'] if adj_p >= 0.5 else 1 - ongoing['side']
                best = {
                    'dir': dir,
                    'conf': max(adj_p, 1 - adj_p),
                    'key': f"Run{k}:{dir}",
                    'weight': 1.5
                }
                break
        return best

    def analyze_num_gaps(self, nums):
        N = len(nums)
        if N < 10: return None
        cur = N - 1
        best = None
        for i in range(max(0, N - 15), N - 1):
            if nums[i] != nums[cur]: continue
            common = [0]
            for d in range(1, 8):
                if i - d >= 0 and nums[i-d] == nums[cur-d]:
                    common.append(d)
            if len(common) >= 3:
                dist = Counter()
                for j in range(max(common), N - 1):
                    if all(nums[j - off] == nums[cur - off] for off in common):
                        dist[nums[j+1]] += 1
                if sum(dist.values()) >= 2:
                    b_cnt = sum(v for k, v in dist.items() if k >= 5)
                    s_cnt = sum(v for k, v in dist.items() if k < 5)
                    p_big = bayes_conf(b_cnt, b_cnt + s_cnt)
                    vote_side = 1 if p_big >= 0.5 else 0
                    
                    pool = [5,6,7,8,9] if vote_side == 1 else [0,1,2,3,4]
                    best_num, max_c = pool[0], 0
                    for k, v in dist.items():
                        if k in pool and v > max_c:
                            max_c = v
                            best_num = k
                    best = {
                        'dir': vote_side,
                        'conf': max(p_big, 1 - p_big),
                        'num': best_num,
                        'key': f"NumGap:{len(common)}",
                        'weight': 1.2
                    }
                    break
        return best

    def analyze_markov(self, sizes):
        if len(sizes) < 5: return None
        state = tuple(sizes[-4:])
        big, small = 0, 0
        for i in range(len(sizes) - 4):
            if tuple(sizes[i:i+4]) == state:
                if sizes[i+4] == 1: big += 1
                else: small += 1
        total = big + small
        if total >= 2:
            p = bayes_conf(big, total)
            return {
                'dir': 1 if p >= 0.5 else 0,
                'conf': max(p, 1 - p),
                'key': "Markov4",
                'weight': 1.0
            }
        return None

    def analyze_zscore(self, sizes):
        if len(sizes) < 30: return None
        w = sizes[-30:]
        big_c = sum(w)
        z = (big_c - 15) / math.sqrt(30 * 0.25)
        if z > 1.5:
            return {'dir': 0, 'conf': 0.7, 'key': "ZScore:S", 'weight': 1.2}
        if z < -1.5:
            return {'dir': 1, 'conf': 0.7, 'key': "ZScore:B", 'weight': 1.2}
        return None

    def detect_mode(self, sizes):
        if len(sizes) < 20: return "MIXED"
        w = sizes[-20:]
        switches = sum(1 for i in range(1, 20) if w[i] != w[i-1])
        if switches >= 12: return "ZIGZAG"
        if switches <= 8: return "TREND"
        return "MIXED"

    def generate_prediction(self):
        if len(self.history) < 50: return None
        
        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        mode = self.detect_mode(sizes)
        runs = self.extract_runs(sizes)
        
        signals = []
        
        def add_sig(engine, res):
            if not res: return
            w = res['weight'] * self.get_engine_weight(engine) * self.vault_mult(res['key'])
            signals.append({
                'engine': engine,
                'dir': res['dir'],
                'conf': res['conf'],
                'key': res['key'],
                'weight': w
            })

        add_sig('Pattern', self.analyze_run(runs))
        add_sig('Pattern', self.analyze_num_gaps(nums))
        add_sig('Markov', self.analyze_markov(sizes))
        add_sig('ZScore', self.analyze_zscore(sizes))
        
        # Mode Override (Trend / Zigzag)
        if mode == "TREND" and runs[-1]['len'] >= 3:
            add_sig('Trend', {'dir': runs[-1]['side'], 'conf': 0.75, 'key': "TREND", 'weight': 2.0})
        elif mode == "ZIGZAG":
            add_sig('Trend', {'dir': 1-sizes[-1], 'conf': 0.65, 'key': "ZIGZAG", 'weight': 1.5})

        # Mixer (Log Odds)
        log_odds = 0.0
        for s in signals:
            log_odds += (1 if s['dir'] == 1 else -1) * logit(s['conf']) * s['weight']

        pred_size = 1 if log_odds > 0 else 0
        confidence = int(min(85, max(52, 50 + abs(log_odds) * 10)))
        
        # Loss Streak Protection
        if self.loss_streak >= 2:
            # In high loss streak, only trust ZScore and Strong Trend
            safe_sigs = [s for s in signals if s['engine'] in ['ZScore', 'Trend']]
            if safe_sigs:
                safe_log_odds = sum((1 if s['dir'] == 1 else -1) * logit(s['conf']) * s['weight'] for s in safe_sigs)
                pred_size = 1 if safe_log_odds > 0 else 0
                confidence = 75
                logic_tags = [s['key'] for s in safe_sigs]
            else:
                logic_tags = ["SAFE_MODE_NEUTRAL"]
        else:
            logic_tags = [s['key'] for s in signals]

        # Target Number
        target_num = 0
        # Try to find from pattern
        for s in signals:
            if 'num' in s: target_num = s['num']; break
            
        if target_num == 0 or (pred_size == 1 and target_num < 5) or (pred_size == 0 and target_num >= 5):
            pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
            target_num = max(pool, key=lambda n: nums[-50:].count(n))

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode,
            "sigs": [{"k": s['key'], "engine": s['engine'], "hit": 0} for s in signals]
        }

PREDICTOR = HyperQuantum()

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
⚡ <i>V137 HYPER-QUANTUM</i>
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
    log.info("V137 Hyper-Quantum started...")
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
                            STORE.update_vault(s['k'], 1 if res['win'] else 0)
                            if s['engine'] in PREDICTOR.engine_stats:
                                PREDICTOR.engine_stats[s['engine']]['total'] += 1
                                if res['win']: PREDICTOR.engine_stats[s['engine']]['wins'] += 1
                    except: pass
                    PREDICTOR.vault = STORE.get_vault()
                    
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
    return web.Response(text="V137 Hyper-Quantum is running!")

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
