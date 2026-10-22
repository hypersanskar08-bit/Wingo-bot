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
    bot_name: str = "V135 HYPER-MINER"

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
log = logging.getLogger("v135_miner")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT, 
                    mode TEXT, logic TEXT, created_at REAL
                )
            """)
            # For Mistake Book
            c.execute("""
                CREATE TABLE IF NOT EXISTS mistake_book (
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

    def save_prediction(self, issue, size, num, mode, logic):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue, size, num, mode, logic, created_at) VALUES (?, ?, ?, ?, ?, ?)", 
                      (issue, size, num, mode, logic, time.time()))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, mode FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, mode in rows:
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
                    "status": status, "mode": mode
                })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]
        
    def update_book(self, key, hit):
        with self._conn() as c:
            row = c.execute("SELECT wins, total FROM mistake_book WHERE pat_key=?", (key,)).fetchone()
            if row:
                wins, total = row
                c.execute("UPDATE mistake_book SET wins=?, total=? WHERE pat_key=?", (wins+hit, total+1, key))
            else:
                c.execute("INSERT INTO mistake_book (pat_key, wins, total) VALUES (?, ?, ?)", (key, hit, 1))
                
    def get_book(self):
        with self._conn() as c:
            rows = c.execute("SELECT pat_key, wins, total FROM mistake_book").fetchall()
            return {k: (w, t) for k, w, t in rows}

STORE = Storage(CFG.db_path)

# =========================
# UTILS & STATS (Ported from HTML)
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

def wilson_lower(w, n, z=1.64):
    if n == 0: return 0
    p = w / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d

def logit(p):
    p = max(0.03, min(0.97, p))
    return math.log(p / (1 - p))

# =========================
# V135 FULL PATTERN MINER
# =========================
class HyperMinerPredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.book = STORE.get_book()

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

    def pat_mult(self, key):
        if key not in self.book: return 1.0
        wins, total = self.book[key]
        acc = (wins + 1) / (total + 2)
        return max(0.2, min(1.6, 1 + (acc - 0.5) * 3))

    # --- HTML RUN PATTERNS ---
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
        best_match = None
        
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
                best_match = {
                    'dir': ongoing['side'] if p_cont >= 0.5 else 1 - ongoing['side'],
                    'conf': max(p_cont, 1 - p_cont),
                    'key': f"Run:{k}:{ongoing['side']}",
                    'weight': total
                }
                break
        return best_match

    # --- HTML NUMBER GAP PATTERNS ---
    def analyze_num_gaps(self, nums):
        N = len(nums)
        if N < 10: return None
        cur = N - 1
        best_match = None
        
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
                    p_big = (b_cnt + 1) / (b_cnt + s_cnt + 2)
                    
                    vote_side = 1 if p_big >= 0.5 else 0
                    pool = [5,6,7,8,9] if vote_side == 1 else [0,1,2,3,4]
                    
                    best_num, max_c = pool[0], 0
                    for k, v in dist.items():
                        if k in pool and v > max_c:
                            max_c = v
                            best_num = k
                            
                    best_match = {
                        'dir': vote_side,
                        'conf': max(p_big, 1 - p_big),
                        'num': best_num,
                        'key': f"NumGap:{len(common)}",
                        'weight': sum(dist.values())
                    }
                    break
        return best_match

    # --- HTML RELATION PATTERNS (Same/Opposite) ---
    def analyze_rel(self, nums, num_matches_hits):
        # Simplified version for speed: check last 3 occurrences of last digit
        N = len(nums)
        cur = N - 1
        if N < 10: return None
        
        for L in range(1, 8):
            n, same, opp = 0, 0, 0
            for j in range(N - 10, N - 1):
                if nums[j] == nums[cur]:
                    src = nums[j + 1 - L]
                    if src >= 0:
                        n += 1
                        if nums[j+1] == src: same += 1
                        elif nums[j+1] == (src + 5) % 10: opp += 1
            
            if n >= 3:
                if same / n >= 0.8:
                    d = nums[cur + 1 - L]
                    return {'dir': 1 if d >= 5 else 0, 'conf': same/n, 'num': d, 'key': f"Rel:Same:{L}"}
                if opp / n >= 0.8:
                    d = (nums[cur + 1 - L] + 5) % 10
                    return {'dir': 1 if d >= 5 else 0, 'conf': opp/n, 'num': d, 'key': f"Rel:Opp:{L}"}
        return None

    # --- HTML HYPER RULES ---
    def analyze_hyper(self, nums, sizes):
        n = min(10, len(nums))
        if n < 3: return 0
        use = nums[::-1][:n]
        last = use[0]
        avg = sum(use) / n
        range_val = max(use) - min(use)
        even = sum(1 for x in use if x % 2 == 0)
        special = sum(1 for x in use if x == 0 or x == 5)
        
        vote = 0
        # Avg
        if avg >= 5.2: vote -= 2
        elif avg <= 3.8: vote += 2
        else: vote += -1 if last >= 5 else 1
        
        # Pattern ASC/DESC
        if use[0] < use[1] < use[2]: vote += 1
        elif use[0] > use[1] > use[2]: vote -= 1
        
        # Special 0/5
        if special >= 2: vote += -1 if last >= 5 else 1
        
        # Streak
        stk = 1
        for i in range(1, min(20, len(sizes))):
            if sizes[-1-i] == sizes[-1]: stk += 1
            else: break
        if stk >= 4: vote += 4 if sizes[-1] == 1 else -4
        
        return vote

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
        
        log_odds = 0.0
        big_votes, small_votes = 0.0, 0.0
        logic_tags = []
        target_num = nums[-1] # default
        
        def add_signal(dir, conf, weight, key):
            nonlocal log_odds, big_votes, small_votes
            mult = self.pat_mult(key)
            w = weight * mult
            if dir == 1: big_votes += w
            else: small_votes += w
            log_odds += (1 if dir == 1 else -1) * logit(conf) * mult
            logic_tags.append(key)

        # 1. Run Pattern
        rm = self.analyze_run(runs)
        if rm:
            add_signal(rm['dir'], rm['conf'], 2.0, rm['key'])
            
        # 2. Number Gap Pattern
        nm = self.analyze_num_gaps(nums)
        if nm:
            add_signal(nm['dir'], nm['conf'], 1.5, nm['key'])
            target_num = nm['num']
            
        # 3. Relation Pattern
        rel = self.analyze_rel(nums, [])
        if rel:
            add_signal(rel['dir'], rel['conf'], 2.5, rel['key'])
            target_num = rel['num']
            
        # 4. HYPER Rules
        hx_vote = self.analyze_hyper(nums, sizes)
        if hx_vote != 0:
            hx_dir = 1 if hx_vote > 0 else 0
            add_signal(hx_dir, 0.6, abs(hx_vote) * 0.5, "HYPER_RULE")
            
        # 5. Mode Override
        if mode == "TREND" and runs[-1]['len'] >= 3:
            add_signal(runs[-1]['side'], 0.7, 2.0, "TrendMode")
        elif mode == "ZIGZAG":
            add_signal(1 - sizes[-1], 0.65, 2.0, "ZigzagMode")

        # Final Decision
        pred_size = 1 if log_odds > 0 else 0
        confidence = int(max(big_votes, small_votes) / (big_votes + small_votes + 1e-9) * 100)
        confidence = min(85, max(52, confidence))
        
        # Ensure target number matches predicted size
        if pred_size == 1 and target_num < 5: target_num = 7
        if pred_size == 0 and target_num >= 5: target_num = 3

        logic_str = " + ".join(logic_tags[:3])
        
        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": target_num,
            "confidence": confidence,
            "logic": logic_str,
            "mode": mode
        }

PREDICTOR = HyperMinerPredictor()

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
⚡ <i>V135 FULL HYPER-MINER</i>
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
    log.info("V135 Hyper-Miner started...")
    last_processed_issue = None

    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}
                
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']: PREDICTOR.loss_streak += 1
                    else: PREDICTOR.loss_streak = 0
                    
                    # Update Mistake Book
                    if "Logic" in res:
                        for k in res['mode'].split(" + "):
                            STORE.update_book(k, 1 if "WIN" in res['status'] or "JACK" in res['status'] else 0)
                    
                    text = f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)
                
                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = PREDICTOR.generate_prediction()
                        if p:
                            STORE.save_prediction(p['issue'], p['size'], p['num'], p['mode'], p['logic'])
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
    return web.Response(text="V135 Hyper-Miner is running!")

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
