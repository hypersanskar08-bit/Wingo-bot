"""
FTW V23 FULL CODE (DEEP PATTERN + ADVANCED DRAGON)
=================
- Multi-Window Sequence Voting (No single pattern bias)
- Pattern Aging (Ignores old patterns)
- Advanced Dragon Riding (Momentum Slope + Fake-Out Protection)
- Invert Rule: WR <= 56%
- Smart Recovery (No blind rollback)
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
    db_path: str = os.environ.get("DB_PATH", "ftw_v23_full.db")
    cache_file: str = "ftw_v23_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V23 DEEP ENGINE"
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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_v23_full.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v23")

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

def alternating_score(seq: List[int], window: int = 12) -> float:
    if len(seq) < 2: return 0.0
    arr = seq[-window:]
    changes = sum(1 for i in range(1, len(arr)) if arr[i] != arr[i - 1])
    return changes / max(1, len(arr) - 1)

def volatility(seq: List[int], window: int = 20) -> float:
    if len(seq) < 2: return 0.0
    arr = seq[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

def ratio_big(seq: List[int], window: int = 20) -> float:
    if not seq: return 0.5
    return sum(seq[-window:]) / len(seq[-window:])

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8: return "COLD"
    run = trailing_run(seq)
    alt = alternating_score(seq, 10)
    ratio = ratio_big(seq, 10)

    if alt >= 0.65: return "ALTERNATING"
    if run >= 5: return "TREND"
    if run >= 3: return "TRAP"
    if abs(ratio - 0.5) <= 0.12: return "CHOPPY"
    return "CYCLE"

def signal_strength(history: List[Dict[str, Any]]) -> float:
    if len(history) < 8: return 0.2
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    run = trailing_run(seq)
    alt = alternating_score(seq, 12)
    ratio = ratio_big(seq, 20)

    s = 0.0
    if regime == "TRAP": s += 0.35
    if regime == "TREND": s += 0.25
    if regime == "ALTERNATING": s += 0.30
    if run >= 3: s += 0.25
    if abs(ratio - 0.5) > 0.18: s += 0.15
    if alt >= 0.6: s += 0.15
    return clamp(min(1.0, s), 0.0, 1.0)

# ============================================================
# STATE & STORAGE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.loss_streak = 0
        self.win_streak = 0
        self.recovery_mode = False
        self.fake_breakouts = 0 # For Trend Tracking

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
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_stats (
                    pattern_key TEXT PRIMARY KEY, hit_count INTEGER DEFAULT 0, miss_count INTEGER DEFAULT 0,
                    last_seen REAL DEFAULT 0, support REAL DEFAULT 0, confidence REAL DEFAULT 0
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
# TREND ENGINE (ADVANCED DRAGON RIDING)
# ============================================================
def advanced_trend_analysis(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < 12: return {"size": "SMALL", "logic": "WARMUP", "confidence": 0.5, "stake": "0.5x"}
    
    seq = [x["size"] for x in history]
    current_side = seq[-1]
    run = trailing_run(seq)
    
    if run < 3: return None
    
    # Momentum Slope Calculation
    last3 = seq[-3:]
    prev3 = seq[-6:-3]
    
    last_same = sum(1 for v in last3 if v == current_side)
    prev_same = sum(1 for v in prev3 if v == current_side)
    
    momentum = last_same - prev_same # +2 means strong trend, -2 means fading
    
    # Fake-Out Detection: If trend breaks once but comes back
    if len(seq) > 8:
        if seq[-1] == seq[-3] and seq[-2] != seq[-1]:
            STATE.fake_breakouts += 1
        else:
            STATE.fake_breakouts = max(0, STATE.fake_breakouts - 1)
            
    # If Momentum is strong, ride it
    if momentum >= 1 or run >= 5:
        return {
            "size": "BIG" if current_side == 1 else "SMALL",
            "logic": f"ADV DRAGON RIDING (Run:{run} | Mom:{momentum})",
            "confidence": 0.88,
            "stake": "1x"
        }
    # If Momentum is decaying, prepare for reversal
    elif momentum <= -1:
        return {
            "size": "SMALL" if current_side == 1 else "BIG",
            "logic": f"ADV DRAGON DECAY (Mom:{momentum})",
            "confidence": 0.85,
            "stake": "1x"
        }
    
    return None

# ============================================================
# DEEP SEQUENCE PATTERN ENGINE (VOTING SYSTEM)
# ============================================================
def deep_pattern_analysis(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    total_history = len(seq)
    
    # Store votes from different window sizes
    big_votes = 0.0
    small_votes = 0.0
    
    best_edge = 0.0
    best_window = 0
    best_logic = ""
    is_trap = False
    
    # Analyze windows from 5 to 14
    for window in range(5, 15):
        if total_history < window + 5: continue
        
        suffix = tuple(seq[-window:])
        exact_big = 0.0
        exact_small = 0.0
        exact_weight_total = 0.0
        recent_matches = []
        
        # Lookback limit (Only check last 300 periods for speed, older patterns are dead)
        lookback_limit = min(total_history, 300) 
        
        for i in range(total_history - window - 1, total_history - lookback_limit, -1):
            past_pattern = tuple(seq[i:i + window])
            
            if past_pattern == suffix:
                next_res = seq[i + window]
                
                # Pattern Aging: Older patterns get 0 weight after 100 rounds
                distance_from_end = total_history - (i + window)
                if distance_from_end > 100: continue 
                
                recency_weight = max(0.1, 1.0 - (distance_from_end / 100.0))
                
                exact_weight_total += recency_weight
                if next_res == 1: exact_big += recency_weight
                else: exact_small += recency_weight
                
                if distance_from_end <= 30:
                    recent_matches.append(next_res)

        if exact_weight_total >= 2.0:
            # Laplace Smoothing
            big_prob = (exact_big + 1) / (exact_weight_total + 2)
            small_prob = (exact_small + 1) / (exact_weight_total + 2)
            
            pred = "BIG" if big_prob >= small_prob else "SMALL"
            win_rate = max(big_prob, small_prob)
            edge = abs(big_prob - small_prob) * 100.0
            
            # 🆕 USER RULE: Invert if WR <= 56%
            if win_rate <= 0.56:
                pred = "SMALL" if pred == "BIG" else "BIG"
                is_trap = True
                edge = 100.0 - edge # Invert the edge
                win_rate = 1.0 - win_rate
                
            # Add to Voting System
            if pred == "BIG":
                big_votes += edge
            else:
                small_votes += edge
                
            # Track the strongest signal
            if edge > best_edge and win_rate > 0.56:
                best_edge = edge
                best_window = window
                best_logic = f"DEEP {window}-SEQ (WR:{win_rate*100:.1f}% | Edge:{edge:.1f}%)"
                
    if big_votes == 0 and small_votes == 0:
        return None
        
    total_votes = big_votes + small_votes
    final_pred = "BIG" if big_votes > small_votes else "SMALL"
    vote_percentage = (max(big_votes, small_votes) / total_votes) * 100.0
    
    # If the voting is very one-sided
    if vote_percentage > 70.0:
        return {
            "size": final_pred,
            "logic": f"DEEP VOTING: {final_pred} ({vote_percentage:.1f}% Consensus)",
            "confidence": clamp(vote_percentage / 100.0, 0.65, 0.92),
            "stake": "2x" if vote_percentage > 85.0 else "1x"
        }
        
    # If top pattern edge is huge, override voting
    if best_edge > 30.0:
         return {
            "size": "BIG" if "BIG" in best_logic else "SMALL", # Simplified logic
            "logic": best_logic,
            "confidence": clamp(best_edge / 100.0, 0.70, 0.95),
            "stake": "2x"
        }

    return None

# ============================================================
# ENSEMBLE & RECOVERY
# ============================================================
def recovery_logic(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    if not seq: return None
    
    # If Loss streak is high, DO NOT fight the trend.
    if STATE.loss_streak >= 2:
        run = trailing_run(seq)
        if run >= 4:
            last = seq[-1]
            return {"size": "BIG" if last == 1 else "SMALL", "logic": "RECOVERY: RIDING TREND", "confidence": 0.80, "stake": "1x"}
            
        recent = seq[-6:]
        small_share = sum(1 for v in recent if v == 0) / len(recent)
        big_share = sum(1 for v in recent if v == 1) / len(recent)
        if small_share >= 0.80: return {"size": "BIG", "logic": "CONTROLLED RECOVERY", "confidence": 0.75, "stake": "1x"}
        if big_share >= 0.80: return {"size": "SMALL", "logic": "CONTROLLED RECOVERY", "confidence": 0.75, "stake": "1x"}
        
    return None

def hybrid_fallback(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    bias = sum(seq[-20:]) / 20.0 if len(seq) >= 20 else 0.5
    size = "BIG" if bias >= 0.5 else "SMALL"
    return {"size": size, "logic": "HYBRID FALLBACK", "confidence": 0.60, "stake": "0.5x"}

def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {"issue": "1", "type": "SIZE", "size": "BIG" if n >= 5 else "SMALL", "num": n, "raw_conf": 50, "logic": "WARMING UP", "stake": "0.5x"}

    history = STATE.history[-2000:]
    
    # 1. Check Recovery
    recovery = recovery_logic(history)
    if recovery:
        final_res = recovery
    else:
        # 2. Check Deep Pattern Analysis
        pattern = deep_pattern_analysis(history)
        
        # 3. Check Advanced Trend
        trend = advanced_trend_analysis(history)
        
        # 4. Decide Final (Pattern gets priority if Edge is strong)
        if pattern and pattern["confidence"] >= 0.75:
            final_res = pattern
        elif trend:
            final_res = trend
        elif pattern:
            final_res = pattern
        else:
            final_res = hybrid_fallback(history)

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
    
    # Exact Number Sequence
    for window in range(3, 8):
        if len(nums) < window + 2: continue
        suffix = tuple(nums[-window:])
        next_counts = defaultdict(int)
        for i in range(len(nums) - window):
            if tuple(nums[i:i + window]) == suffix:
                next_counts[nums[i + window]] += 1
                
        if next_counts:
            best_num, freq = Counter(next_counts).most_common(1)[0]
            if freq >= 2 and best_num in valid:
                return best_num

    # Fallback: Most common in family
    family_nums = [n for n in nums[-20:] if n in valid]
    if family_nums:
        return Counter(family_nums).most_common(1)[0][0]
        
    return random.choice(valid)

# ============================================================
# API FETCHING & FORMATTING
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

async def health_check(request): return web.Response(text="FTW V23 is Running")

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
