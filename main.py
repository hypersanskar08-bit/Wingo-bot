"""
FTW V27 FULL CODE (ANTI-LOSS ENGINE)
=================
- REMOVED HYBRID FALLBACK (It was predicting blindly)
- Added SMART FALLBACK (Follows last 3 periods)
- Pattern gets Highest Priority
- Deep Voting Inverts if Consensus > 75% (Trap)
- Telegram VIP Channel Ready
"""

import asyncio
import gc
import html
import json
import logging
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web
from telegram.ext import Application

# ============================================================
# CONFIG
# ============================================================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TELEGRAM_TOKEN", "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo")
    chat_id_raw: str = os.environ.get("CHAT_ID", "-1004485961768")
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_v27_full.db")
    cache_file: str = "ftw_v27_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V27 ANTI-LOSS"
    min_history: int = 8
    recent_limit: int = 12

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls if urls else [
            "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
        ]

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# ============================================================
# LOGGING & UTILS
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_v27_full.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v27")

def h(x: Any) -> str: return html.escape(str(x))
def safe_int(x, default=0):
    try: return int(x)
    except: return default

def normalize_issue(value: Any) -> Optional[int]:
    if value is None: return None
    if isinstance(value, str):
        value = value.strip()
        if value == "": return None
        if value.isdigit(): return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits: return int(digits)
    try: return int(value)
    except: return None

def clamp(v: float, low: float, high: float) -> float: return max(low, min(high, v))

def trailing_run(seq: List[int]) -> int:
    if not seq: return 0
    last = seq[-1]
    c = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last: c += 1
        else: break
    return c

# ============================================================
# STATE & STORAGE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.loss_streak = 0
        self.win_streak = 0
        self.recovery_mode = False

    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-2000:], f)
        except: pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list): self.history = data
        except: pass

STATE = BotState()

class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, pred_type TEXT, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT,
                    logic TEXT, details TEXT, stake TEXT, created_at REAL
                )
            """)

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions (issue, pred_type, size, num, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["issue"], p["type"], p.get("size", ""), p.get("num", 0), p.get("logic", "N/A"), json.dumps(p.get("details", {})), p.get("stake", "1x"), time.time()))

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, pred_type, size, num, details, stake FROM predictions WHERE status='PENDING'").fetchall()

        for row in rows:
            issue, pred_type, p_size, p_num, details_json, stake = row
            act = history_by_issue.get(str(issue))
            if not act: continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == actual_size) if p_size else True

            if win and p_num == act["num"]: status = "JACK 🏆"
            elif win: status = "WIN ✅"
            else: status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", (status, act["num"], actual_size, issue))

            resolved.append({
                "issue": issue, "type": pred_type or "SIZE", "size": p_size, "num": p_num, "stake": stake,
                "actual_num": act["num"], "actual_size": actual_size, "status": status, "win": win,
                "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)

# ============================================================
# 1. ADVANCED TREND ENGINE (WITH TRAP PROTECTION)
# ============================================================
def advanced_trend_analysis(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 12: return None
    
    seq = [x["size"] for x in history]
    current_side = seq[-1]
    run = trailing_run(seq)
    
    # 🆕 TRAP PROTECTION: Don't predict on Run:3. Run:3 is often a trap.
    if run < 4: return None
    
    last3 = seq[-3:]
    prev3 = seq[-6:-3]
    last_same = sum(1 for v in last3 if v == current_side)
    prev_same = sum(1 for v in prev3 if v == current_side)
    momentum = last_same - prev_same
    
    # Only ride if momentum is positive or heavy run
    if momentum >= 1 or run >= 5:
        return {
            "size": "BIG" if current_side == 1 else "SMALL",
            "logic": f"ADV DRAGON (Run:{run} | Mom:{momentum})",
            "confidence": 0.88,
            "stake": "1x"
        }
    elif momentum <= -1:
        return {
            "size": "SMALL" if current_side == 1 else "BIG",
            "logic": f"ADV DRAGON DECAY (Mom:{momentum})",
            "confidence": 0.85,
            "stake": "1x"
        }
    return None

# ============================================================
# 2. ULTRA STRONG PATTERN ENGINE
# ============================================================
def get_best_pattern(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    total_history = len(seq)
    current_run = trailing_run(seq)
    current_live_side = "BIG" if seq[-1] == 1 else "SMALL"
    
    best_candidate = None
    highest_score = 0
    
    for window in range(5, 15):
        if total_history < window + 5: continue
        
        suffix = tuple(seq[-window:])
        exact_big = 0.0
        exact_small = 0.0
        exact_weight_total = 0.0
        
        lookback_limit = min(total_history, 300)
        
        for i in range(total_history - window - 1, total_history - lookback_limit, -1):
            past_pattern = tuple(seq[i:i + window])
            
            if past_pattern == suffix:
                next_res = seq[i + window]
                distance_from_end = total_history - (i + window)
                
                if distance_from_end > 100: continue 
                
                recency_weight = max(0.1, 1.0 - (distance_from_end / 100.0))
                exact_weight_total += recency_weight
                if next_res == 1: exact_big += recency_weight
                else: exact_small += recency_weight

        if exact_weight_total >= 2.0:
            big_prob = (exact_big + 1) / (exact_weight_total + 2)
            small_prob = (exact_small + 1) / (exact_weight_total + 2)
            
            pred = "BIG" if big_prob >= small_prob else "SMALL"
            win_rate = max(big_prob, small_prob)
            edge = abs(big_prob - small_prob) * 100.0
            
            is_inverted = False
            
            if win_rate <= 0.56:
                pred = "SMALL" if pred == "BIG" else "BIG"
                is_inverted = True
                edge = 100.0 - edge
                win_rate = 1.0 - win_rate
                
            # Regime Guard
            if current_run >= 2 and pred != current_live_side:
                if edge < 35.0: continue 

            score = edge + (exact_weight_total * 2.0) + (window * 2.0)
            
            if score > highest_score:
                highest_score = score
                tag = "INVERTED EXACT" if is_inverted else "STRONG EXACT"
                
                best_candidate = {
                    "size": pred, 
                    "logic": f"{tag} {window}-SEQ (WR:{win_rate*100:.1f}% | Edge:{edge:.1f}%)",
                    "confidence": clamp(edge / 100.0, 0.55, 0.95) if is_inverted else clamp(win_rate, 0.55, 0.95),
                    "stake": "2x" if edge > 25 else "1x" if edge > 15 else "0.5x"
                }
                
    return best_candidate

# ============================================================
# 3. CONTRARIAN DEEP VOTING (TRAP INVERSION)
# ============================================================
def deep_voting_analysis(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    total_history = len(seq)
    
    big_votes = 0.0
    small_votes = 0.0
    
    for window in range(5, 15):
        if total_history < window + 5: continue
        suffix = tuple(seq[-window:])
        exact_big, exact_small, exact_weight_total = 0.0, 0.0, 0.0
        
        lookback_limit = min(total_history, 300)
        for i in range(total_history - window - 1, total_history - lookback_limit, -1):
            if tuple(seq[i:i + window]) == suffix:
                next_res = seq[i + window]
                distance_from_end = total_history - (i + window)
                if distance_from_end > 100: continue 
                
                recency_weight = max(0.1, 1.0 - (distance_from_end / 100.0))
                exact_weight_total += recency_weight
                if next_res == 1: exact_big += recency_weight
                else: exact_small += recency_weight

        if exact_weight_total >= 2.0:
            big_prob = (exact_big + 1) / (exact_weight_total + 2)
            small_prob = (exact_small + 1) / (exact_weight_total + 2)
            
            pred = "BIG" if big_prob >= small_prob else "SMALL"
            edge = abs(big_prob - small_prob) * 100.0
            
            if max(big_prob, small_prob) <= 0.56:
                pred = "SMALL" if pred == "BIG" else "BIG"
                
            if pred == "BIG": big_votes += edge
            else: small_votes += edge
                
    total_votes = big_votes + small_votes
    if total_votes == 0: return None
    
    final_pred = "BIG" if big_votes > small_votes else "SMALL"
    vote_percentage = (max(big_votes, small_votes) / total_votes) * 100.0
    
    # Contrarian Logic (Trap Alert)
    if vote_percentage >= 75.0:
        inverted_pred = "SMALL" if final_pred == "BIG" else "BIG"
        return {
            "size": inverted_pred,
            "logic": f"TRAP VOTING INVERTED ({vote_percentage:.1f}% Consensus)",
            "confidence": 0.78,
            "stake": "1x"
        }
    elif vote_percentage >= 60.0:
        return {
            "size": final_pred,
            "logic": f"SAFE VOTING ({vote_percentage:.1f}% Consensus)",
            "confidence": 0.70,
            "stake": "1x"
        }
    return None

# ============================================================
# 4. SMART FALLBACK (REPLACED HYBRID)
# ============================================================
def smart_fallback(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    last_3 = seq[-3:] if len(seq) >= 3 else seq
    
    if len(last_3) < 3:
        return {"size": "BIG", "logic": "SMART FALLBACK", "confidence": 0.55, "stake": "0.5x"}
        
    ones = sum(last_3)
    
    if ones == 3: return {"size": "BIG", "logic": "SMART FALLBACK (3 BIGs)", "confidence": 0.65, "stake": "0.5x"}
    if ones == 0: return {"size": "SMALL", "logic": "SMART FALLBACK (3 SMALLs)", "confidence": 0.65, "stake": "0.5x"}
    if ones == 2: return {"size": "BIG", "logic": "SMART FALLBACK (2 BIGs)", "confidence": 0.60, "stake": "0.5x"}
    if ones == 1: return {"size": "SMALL", "logic": "SMART FALLBACK (2 SMALLs)", "confidence": 0.60, "stake": "0.5x"}
    
    return {"size": "BIG", "logic": "SMART FALLBACK", "confidence": 0.55, "stake": "0.5x"}

# ============================================================
# ENSEMBLE & MAIN GENERATION
# ============================================================
def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {"issue": "1", "type": "SIZE", "size": "BIG" if n >= 5 else "SMALL", "num": n, "raw_conf": 50, "logic": "WARMING UP", "stake": "0.5x"}

    history = STATE.history[-2000:]
    
    # Get all engines
    pattern = get_best_pattern(history)
    trend = advanced_trend_analysis(history)
    voting = deep_voting_analysis(history)
    
    final_res = None
    
    # 🆕 PRIORITY LOGIC (Pattern is King now)
    # 1. If strong pattern exists, use it.
    if pattern and pattern["confidence"] >= 0.60:
        final_res = pattern
    # 2. If strong trend exists, use it.
    elif trend:
        final_res = trend
    # 3. If voting has a strong signal, use it.
    elif voting:
        final_res = voting
    # 4. Safe fallback
    else:
        final_res = smart_fallback(history)

    final_size = final_res["size"]
    final_num = choose_number_for_size(final_size, history)
    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue, "type": "SIZE", "size": final_size, "num": final_num, 
        "raw_conf": int(final_res["confidence"] * 100), "logic": final_res["logic"], "stake": final_res["stake"]
    }

def learn_from_resolution(res: Dict[str, Any]):
    if res["win"]: STATE.win_streak += 1; STATE.loss_streak = 0; STATE.recovery_mode = False
    else: STATE.loss_streak += 1; STATE.win_streak = 0; STATE.recovery_mode = STATE.loss_streak >= 2
    STATE.save_cache()

# ============================================================
# NUMBER PREDICTION
# ============================================================
def family_for_size(size: str) -> List[int]: return [5, 6, 7, 8, 9] if size == "BIG" else [0, 1, 2, 3, 4]

def choose_number_for_size(size: str, history: List[Dict[str, Any]]) -> int:
    valid = family_for_size(size)
    nums = [x["num"] for x in history]
    
    for window in range(3, 8):
        if len(nums) < window + 2: continue
        suffix = tuple(nums[-window:])
        next_counts = defaultdict(int)
        for i in range(len(nums) - window):
            if tuple(nums[i:i + window]) == suffix:
                next_counts[nums[i + window]] += 1
                
        if next_counts:
            best_num, freq = Counter(next_counts).most_common(1)[0]
            if freq >= 2 and best_num in valid: return best_num

    family_nums = [n for n in nums[-20:] if n in valid]
    if family_nums: return Counter(family_nums).most_common(1)[0][0]
    return random.choice(valid)

# ============================================================
# API & FORMATTING
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None: return []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        d = payload.get("data", {})
        items = d.get("list", []) if isinstance(d, dict) else d if isinstance(d, list) else payload.get("result", [])
    else: items = []

    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None or str(issue) in seen: continue
        seen.add(str(issue))
        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if 0 <= num <= 9: out.append({"issue": str(issue), "num": num, "size": 1 if num >= 5 else 0})

    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data(session):
    for url in CFG.api_urls():
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200: continue
                parsed = parse_history_payload(await response.json(content_type=None))
                if parsed:
                    STATE.history = parsed[-1000:]
                    STATE.save_cache()
                    return True
        except: pass
    return False

def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_lines = []
    for r in recent_rows[-CFG.recent_limit:]:
        issue, size, status = str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else ""
        if not issue: continue
        recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> {h(size)} → <b>{h(status)}</b>" if status else f"➜ <code>{h(issue[-4:])}</code> {h(size)}")

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Engine</b>: {h(pred.get('logic', 'N/A'))}
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent</b>:
{chr(10).join(recent_lines)}
━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res: Dict[str, Any]):
    return (f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'])}</code>\nPred: <b>{h(res['size'])} {h(res['num'])}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>")

async def send_msg(bot, text: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await asyncio.wait_for(bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"), timeout=CFG.request_timeout)
            return True
        except: await asyncio.sleep(1)
    return False

# ============================================================
# WORKER & MAIN
# ============================================================
async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_data(session):
                history_by_issue = {str(x["issue"]): x for x in STATE.history}
                for res in STORE.resolve_pending(history_by_issue):
                    learn_from_resolution(res)
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = generate_prediction()
                    if pred:
                        STORE.save_prediction(pred)
                        await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop exception: %s", e)
            await asyncio.sleep(2)

async def health_check(request): return web.Response(text="FTW V27 is Running")

async def main():
    CFG.validate()
    STATE.load_cache()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    await app.initialize()
    await app.start()
    log.info("Bot started: %s", (await app.bot.get_me()).username)
    
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(worker_loop(app, session))
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
