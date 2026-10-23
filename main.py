"""
V400 DEEP-PATTERN ENGINE
========================
Fixes:
  * REMOVED SKIP completely. Always predicts.
  * Log-Odds (Logit) Ensemble for mathematically superior confidence.
  * N-Gram Pattern Matcher (exact sequence matching).
  * Multi-Timeframe Moving Average Crossover.
  * Even/Odd Parity Pattern Tracker.
  * Contrarian Loss Streak logic (flips prediction after 2 losses).
"""

import asyncio
import gc
import html
import json
import logging
import math
import os
import random
import sqlite3
import time
from collections import Counter, deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.error import TimedOut, NetworkError

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA"
    chat_id_raw: str = "1264164655"
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "predictor_v400.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V400 DEEP-PATTERN"
    min_history: int = 50
    warm_min: int = 20
    admin_ids: tuple = ()

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# =========================
# LOGGING
# =========================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v400.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v400")

def h(x: Any) -> str:
    return html.escape(str(x))

# =========================
# MATH & PROBABILITY
# =========================
def logit(p):
    p = max(0.01, min(0.99, p))
    return math.log(p / (1 - p))

def inv_logit(z):
    return 1 / (1 + math.exp(-z))

class Beta:
    __slots__ = ("a", "b")
    def __init__(self, a: float = 1.0, b: float = 1.0):
        self.a = a
        self.b = b

    def update(self, correct: bool):
        if correct: self.a += 1.0
        else: self.b += 1.0

    def n(self) -> float:
        return max(0.0, self.a + self.b - 2.0)

    def mean(self) -> float:
        return self.a / (self.a + self.b)

    def sample_thompson(self) -> float:
        return random.betavariate(self.a, self.b)

    def to_dict(self):
        return {"a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d):
        return cls(d["a"], d["b"])

class Calibration:
    def __init__(self, maxlen: int = 400):
        self.samples = deque(maxlen=maxlen)
    def add(self, conf: int, correct: bool):
        self.samples.append((conf, 1 if correct else 0))
    def calibrate(self, raw_conf: int) -> int:
        if len(self.samples) < 20: return 50
        n = w = W = 0
        for c, win in self.samples:
            W += win
            if abs(c - raw_conf) <= 6:
                n += 1; w += win
        base = (W + 5.0) / (len(self.samples) + 10)
        p = (w + 12 * base) / (n + 12)
        return int(max(0.5, min(0.85, p)) * 100)

# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT, p_big REAL, calibrated_conf INTEGER,
                    consensus REAL, stake TEXT, created_at REAL
                )
            """)
            c.execute("CREATE TABLE IF NOT EXISTS state_kv (key TEXT PRIMARY KEY, value TEXT)")

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, mode, logic, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["mode"], p["logic"], json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "BALANCED"), json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50), p.get("consensus", 0.5), p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()
        for row in rows:
            (issue, p_size, p_num, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake) = row
            if issue not in history_dict: continue
            act = history_dict[issue]
            act_size_str = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size_str)
            status = ("JACK 🏆" if p_num == act["num"] else "WIN ✅") if win else "LOSS ❌"
            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", (status, act["num"], act_size_str, issue))
            resolved.append({
                "issue": issue, "size": p_size, "num": p_num, "actual_num": act["num"], "actual_size": act_size_str,
                "status": status, "mode": mode, "win": win, "raw_conf": raw_conf, "regime": regime,
                "details": json.loads(details_json) if details_json else {}, "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5, "calibrated_conf": cal_conf or 50, "consensus": consensus or 0.5, "stake": stake or "1x"
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def overall_stats(self):
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*), SUM(CASE WHEN status LIKE 'WIN%' OR status LIKE 'JACK%' THEN 1 ELSE 0 END) FROM predictions WHERE status!='PENDING'").fetchone()
        return (row[0] or 0), (row[1] or 0)

    def load_kv(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM state_kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_kv(self, key: str, value: str):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO state_kv (key, value) VALUES (?, ?)", (key, value))

STORE = Storage(CFG.db_path)

# =========================
# DEEP DETECTORS
# =========================
@dataclass
class Detector:
    key: str
    name: str
    dir: int
    conf: int
    weight: float
    family: str = "generic"

def run_detectors(sizes, nums):
    out = []
    n = len(sizes)
    if n < CFG.warm_min: return out

    # 1. N-Gram Pattern Matcher (Exact sequence)
    if n >= 10:
        seq = tuple(sizes[-3:])
        matches = 0
        next_big = 0
        for i in range(len(sizes) - 3):
            if tuple(sizes[i:i+3]) == seq:
                matches += 1
                if sizes[i+3] == 1: next_big += 1
        if matches >= 4:
            p = next_big / matches
            strength = abs(p - 0.5) * 2
            if strength > 0.1:
                out.append(Detector("NGram3", f"N-Gram({p:.2f})", 1 if p > 0.5 else 0, int(50 + strength * 40), 1.0 + strength * 1.5, "ngram"))

    # 2. Multi-Timeframe MA Crossover
    if n >= 20:
        ma5 = sum(sizes[-5:]) / 5
        ma15 = sum(sizes[-15:]) / 15
        if abs(ma5 - ma15) > 0.15:
            out.append(Detector("MA_Cross", f"MA-Cross({ma5:.1f}v{ma15:.1f})", 1 if ma5 > ma15 else 0, 60, 1.2, "ma"))

    # 3. Parity (Even/Odd) Tracker
    if n >= 10:
        parities = [1 if x % 2 == 0 else 0 for x in nums[-10:]]
        last_par = parities[-1]
        run = 1
        for x in reversed(parities[:-1]):
            if x == last_par: run += 1
            else: break
        if run >= 3:
            # Parity breaks after 3
            out.append(Detector("ParityBreak", "Parity-Break", 1 - last_par, 60, 1.0, "parity"))
        elif run == 1:
            out.append(Detector("ParityFollow", "Parity-Follow", last_par, 55, 0.8, "parity"))

    # 4. Standard Markov (L4)
    if n >= 20:
        ctx = (sizes[-4] << 3) | (sizes[-3] << 2) | (sizes[-2] << 1) | sizes[-1]
        bN = tN = 0
        for i in range(4, n):
            c = (sizes[i-4] << 3) | (sizes[i-3] << 2) | (sizes[i-2] << 1) | sizes[i-1]
            if c == ctx:
                tN += 1
                if sizes[i] == 1: bN += 1
        if tN >= 10:
            p = (bN + 0.5) / (tN + 1)
            out.append(Detector("Markov4", f"Markov-L4({p:.2f})", 1 if p > 0.5 else 0, int(50 + abs(p-0.5)*50), 0.8 + abs(p-0.5)*2, "markov"))

    # 5. Streak / Dragon
    last = sizes[-1]
    run = 1
    for i in range(n - 2, -1, -1):
        if sizes[i] == last: run += 1
        else: break
    if run >= 4:
        out.append(Detector("Dragon", f"Dragon×{run}", last, min(90, 60 + run * 5), 1.5, "streak"))
    elif run == 2:
        out.append(Detector("Streak2", "Streak×2", last, 56, 0.9, "streak"))

    # 6. Bollinger Reversion (Sizes)
    if n >= 20:
        w20 = sizes[-20:]
        ma = sum(w20) / 20
        std = math.sqrt(sum((x - ma) ** 2 for x in w20) / 20)
        if std > 0.1:
            if last >= ma + 2 * std: out.append(Detector("BollingerRev", "Bollinger-Rev", 0, 70, 1.3, "bollinger"))
            elif last <= ma - 2 * std: out.append(Detector("BollingerRev", "Bollinger-Rev", 1, 70, 1.3, "bollinger"))

    return out

# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.beta_global = {}
        self.calibration = {}
        self.global_calibration = Calibration()
        self.loss_streak = 0
        self._load_persisted()

    def _load_persisted(self):
        raw = STORE.load_kv("state_v400")
        if not raw: return
        try:
            d = json.loads(raw)
            for k, v in d.get("beta_global", {}).items(): self.beta_global[k] = Beta.from_dict(v)
            self.loss_streak = d.get("loss_streak", 0)
        except Exception: pass

    def save_persisted(self):
        try:
            d = {
                "beta_global": {k: b.to_dict() for k, b in self.beta_global.items()},
                "loss_streak": self.loss_streak,
            }
            STORE.save_kv("state_v400", json.dumps(d))
        except Exception: pass

STATE = BotState()

def get_beta(det: str) -> Beta:
    if det not in STATE.beta_global: STATE.beta_global[det] = Beta()
    return STATE.beta_global[det]

def get_calibration(regime: str) -> Calibration:
    if regime not in STATE.calibration: STATE.calibration[regime] = Calibration()
    return STATE.calibration[regime]

# =========================
# DEEP ENSEMBLE (Log-Odds)
# =========================
def merge_detectors(detectors):
    if not detectors: return None
    
    total_log_odds = 0.0
    used = []

    for d in detectors:
        beta = get_beta(d.key)
        
        # Confidence from detector (0.0 to 1.0)
        p_det = d.conf / 100.0
        if d.dir == 0: p_det = 1.0 - p_det # Map to P(BIG)

        # Log-odds conversion
        z = logit(p_det)

        # Weight from Thompson Sampling (Meta-learning)
        w_beta = beta.sample_thompson() if beta.n() >= 10 else 0.5
        
        # Final weight
        w = d.weight * (w_beta * 2) 
        
        # Direction applied
        if d.dir == 0: w = -w # Negative for SMALL

        total_log_odds += z * w

        used.append({
            "name": d.name, "key": d.key, "dir": d.dir, "weight": round(w, 2)
        })

    # Convert back to probability
    p_big = inv_logit(total_log_odds)
    
    # Consensus
    consensus = max(p_big, 1 - p_big)
    raw_conf = int(50 + abs(p_big - 0.5) * 100)
    pred_dir = 1 if p_big >= 0.5 else 0

    return {"p_big": p_big, "dir": pred_dir, "raw_conf": raw_conf, "consensus": consensus, "used": used}

def generate_prediction():
    if len(STATE.history) < CFG.min_history: return None
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    
    detectors = run_detectors(sizes, nums)
    merged = merge_detectors(detectors)
    
    if not merged:
        # Fallback if no detectors fire
        merged = {"p_big": 0.5, "dir": sizes[-1], "raw_conf": 50, "consensus": 0.5, "used": []}

    calibrated_conf = get_calibration("GLOBAL").calibrate(merged["raw_conf"])
    
    # 🔥 NO SKIP LOGIC (Always Predict)
    stake = "1x"
    if merged["raw_conf"] >= 75:
        stake = "2x"
    elif merged["raw_conf"] >= 65:
        stake = "1.5x"

    # 🔥 CONTRARIAN LOSS STREAK
    # If bot loses 2 times, assume market is tricking it. Flip the prediction.
    warning = None
    if STATE.loss_streak >= 2:
        merged["dir"] = 1 - merged["dir"]
        merged["p_big"] = 1.0 - merged["p_big"]
        stake = "0.5x" # Reduce stake to be safe
        warning = "CONTRARIAN (Flipped)"
    
    issue = str(int(STATE.history[-1]["issue"]) + 1)
    top_sigs = merged["used"][:6]
    
    # Number Picker (Dirichlet)
    pool = range(5, 10) if merged["dir"] == 1 else range(0, 5)
    # Simple frequency pick for numbers
    freq = Counter(nums[-30:])
    num = max(pool, key=lambda x: freq[x])

    return {
        "issue": issue, "size": "BIG" if merged["dir"] == 1 else "SMALL", "num": num,
        "p_big": merged["p_big"], "confidence": calibrated_conf, "raw_conf": merged["raw_conf"],
        "consensus": merged["consensus"], "mode": "DEEP", "regime": "DYNAMIC",
        "logic": ", ".join(s["name"] for s in top_sigs) or "Fallback",
        "sigs": top_sigs, "deferral": warning, "stake": stake,
        "details": {
            "detectors": [{"key": s["key"], "dir": s["dir"], "name": s["name"]} for s in merged["used"]],
            "regime": "DYNAMIC", "p_big": merged["p_big"], "consensus": merged["consensus"]
        },
    }

def learn_from_resolution(res: dict):
    actual_size = 1 if res["actual_size"] == "BIG" else 0
    details = res.get("details") or {}
    
    for d in details.get("detectors", []):
        correct = (d["dir"] == actual_size)
        get_beta(d["key"]).update(correct)

    raw = res.get("raw_conf")
    if isinstance(raw, int):
        get_calibration("GLOBAL").add(raw, res["win"])

    STATE.save_persisted()

async def fetch_data(session):
    try:
        async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
            if r.status != 200: return False
            data = await r.json()
            items = data.get("data", {}).get("list", [])
            if not items: return False
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
                except Exception: continue
            STATE.history = out
            return True
    except Exception: return False

# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n" for r in recent_rows[-12:])
    sig_text = "\n".join(f"  {h(s['name'])} → {'▲ BIG' if s['dir']==1 else '▼ SMALL'}" for s in p["sigs"][:6]) or "  <i>no active signals</i>"
    
    p_big_pct = p["p_big"] * 100
    consensus_pct = p["consensus"] * 100

    warning = ""
    if p.get("deferral") == "CONTRARIAN (Flipped)":
        warning = "\n🔄 <b>CONTRARIAN MODE</b> — Flipped to break loss streak.\n"

    stake_emoji = {"2x": "🔥 2x", "1.5x": "🟢 1.5x", "1x": "🟡 1x", "0.5x": "🔴 0.5x"}.get(p['stake'], "1x")

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

🎲 <b>P(BIG)</b>: <b>{p_big_pct:.1f}%</b>
📈 <b>Confidence</b>: <b>{p['confidence']}%</b> <i>(raw {p['raw_conf']}%)</i>
💰 <b>Stake</b>: <b>{stake_emoji}</b>
{warning}
🔎 <b>Active Signals</b>:
{sig_text}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])} <i>(Stake: {res['stake']})</i>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except (TimedOut, NetworkError): await asyncio.sleep(2)
        except Exception as e: log.error(f"Send error: {e}"); break
    return False

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info(f"{CFG.bot_name} started...")
    last_processed_issue = None

    while True:
        try:
            if await fetch_data(session):
                hist_dict = {x["issue"]: x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)
                
                for res in resolved:
                    learn_from_resolution(res)
                    if res["win"]: 
                        STATE.loss_streak = 0
                    else: 
                        STATE.loss_streak += 1
                    
                    await send_msg(app, format_result(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)

                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            await send_msg(app, format_prediction(p, STORE.recent_outcomes(12)))
                            last_processed_issue = latest_issue
            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception(f"Loop error: {e}"); await asyncio.sleep(2)

# =========================
# MAIN
# =========================
async def main():
    CFG.validate()
    if not CFG.telegram_token: raise RuntimeError("TG_TOKEN env variable is empty")

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    
    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
