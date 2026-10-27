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
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from telegram.ext import Application

# ============================================================
# CONFIG (Token & Chat ID Added)
# ============================================================
@dataclass
class Config:
    # Tumhara Token yahan add kar diya gaya hai
    telegram_token: str = os.environ.get("TELEGRAM_TOKEN", "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo")
    # Tumhari Chat ID yahan add kar di gayi hai
    chat_id_raw: str = os.environ.get("CHAT_ID", "-1004485961768")
    
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_final_pattern.db")
    cache_file: str = "ftw_final_pattern_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW FINAL PATTERN"
    min_history: int = 8
    recent_limit: int = 12
    max_history: int = 1000
    min_repeat: int = 3

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls or [
            "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
        ]

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# ============================================================
# LOGGING
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

        fh = RotatingFileHandler(
            Path(CFG.log_dir) / "ftw_final_pattern.log",
            maxBytes=1_000_000,
            backupCount=3
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_final_pattern")

# ============================================================
# HELPERS
# ============================================================
def h(x: Any) -> str:
    return html.escape(str(x))

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def normalize_issue(value: Any) -> Optional[int]:
    if value is None: return None
    if isinstance(value, str):
        value = value.strip()
        if value == "": return None
        if value.isdigit(): return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits: return int(digits)
    try: return int(value)
    except Exception: return None

def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))

def trailing_run(seq: List[int]) -> int:
    if not seq: return 0
    last = seq[-1]
    count = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last: count += 1
        else: break
    return count

def volatility(seq: List[int], window: int = 20) -> float:
    if len(seq) < 2: return 0.0
    arr = seq[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

def ratio_big(seq: List[int], window: int = 20) -> float:
    if not seq: return 0.5
    arr = seq[-window:]
    return sum(arr) / max(1, len(arr))

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8: return "COLD"
    run = trailing_run(seq)
    alt = sum(1 for i in range(1, len(seq[-10:])) if seq[-10:][i] != seq[-10:][i - 1]) / max(1, len(seq[-10:]) - 1)
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
    alt = sum(1 for i in range(1, len(seq[-12:])) if seq[-12:][i] != seq[-12:][i - 1]) / max(1, len(seq[-12:]) - 1)
    s = 0.0
    if regime == "TRAP": s += 0.35
    if regime == "TREND": s += 0.25
    if regime == "ALTERNATING": s += 0.30
    if run >= 3: s += 0.25
    if alt >= 0.6: s += 0.15
    return clamp(min(1.0, s), 0.0, 1.0)

# ============================================================
# STATE
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
                json.dump(self.history[-CFG.max_history:], f)
        except Exception: pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data[-CFG.max_history:]
        except Exception: pass

STATE = BotState()

# ============================================================
# STORAGE
# ============================================================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
                CREATE TABLE IF NOT EXISTS pattern_penalties (
                    pattern_key TEXT PRIMARY KEY, fail_streak INTEGER DEFAULT 0,
                    penalty REAL DEFAULT 1.0, last_seen REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_memory (
                    pattern_key TEXT PRIMARY KEY, consec_losses INTEGER DEFAULT 0,
                    is_flipped INTEGER DEFAULT 0, total_preds INTEGER DEFAULT 0, total_wins INTEGER DEFAULT 0
                )
            """)

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["issue"], p["type"], p.get("size", ""), p.get("num", 0), p.get("logic", "N/A"), json.dumps(p.get("details", {})), p.get("stake", "1x"), time.time()))

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, pred_type, size, num, details, stake FROM predictions WHERE status='PENDING'").fetchall()

        for issue, pred_type, p_size, p_num, details_json, stake in rows:
            act = history_by_issue.get(str(issue))
            if not act: continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == actual_size) if p_size else False
            status = "JACK 🏆" if (win and p_num == act["num"]) else "WIN ✅" if win else "LOSS ❌"

            with self._conn() as c2:
                c2.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", (status, act["num"], actual_size, issue))

            resolved.append({
                "issue": issue, "type": pred_type or "SIZE", "size": p_size, "num": p_num, "stake": stake,
                "actual_num": act["num"], "actual_size": actual_size, "status": status, "win": win,
                "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, status FROM predictions WHERE status != 'PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def get_penalty(self, pattern_key: str) -> float:
        with self._conn() as c:
            row = c.execute("SELECT penalty FROM pattern_penalties WHERE pattern_key=?", (pattern_key,)).fetchone()
        return float(row[0]) if row else 1.0

    def penalize_pattern(self, pattern_key: str, success: bool):
        with self._conn() as c:
            row = c.execute("SELECT fail_streak, penalty FROM pattern_penalties WHERE pattern_key=?", (pattern_key,)).fetchone()
            fail_streak, penalty = (0, 1.0) if row is None else row
            if success: fail_streak = max(0, fail_streak - 1); penalty = min(1.0, penalty + 0.08)
            else: fail_streak += 1; penalty = max(0.10, penalty * 0.75)
            c.execute("""
                INSERT INTO pattern_penalties (pattern_key, fail_streak, penalty, last_seen) VALUES (?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET fail_streak=excluded.fail_streak, penalty=excluded.penalty, last_seen=excluded.last_seen
            """, (pattern_key, fail_streak, penalty, time.time()))

    def get_pattern_memory(self, key: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT consec_losses, is_flipped, total_preds, total_wins FROM pattern_memory WHERE pattern_key=?", (key,)).fetchone()
        return {"consec_losses": 0, "is_flipped": False, "total_preds": 0, "total_wins": 0} if not row else {"consec_losses": row[0], "is_flipped": bool(row[1]), "total_preds": row[2], "total_wins": row[3]}

    def record_pattern_outcome(self, key: str, won: bool):
        mem = self.get_pattern_memory(key)
        consec, is_flipped = mem["consec_losses"], mem["is_flipped"]
        preds, wins = mem["total_preds"] + 1, mem["total_wins"] + (1 if won else 0)
        
        if won: consec = 0; is_flipped = False
        else:
            consec += 1
            if consec >= 2: is_flipped = True
            
        with self._conn() as c:
            c.execute("""
                INSERT INTO pattern_memory (pattern_key, consec_losses, is_flipped, total_preds, total_wins) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET consec_losses=excluded.consec_losses, is_flipped=excluded.is_flipped, total_preds=excluded.total_preds, total_wins=excluded.total_wins
            """, (key, consec, int(is_flipped), preds, wins))

STORE = Storage(CFG.db_path)

# ============================================================
# ADVANCED PATTERN MATCHING
# ============================================================
def get_block_candidates(seq_str: str, min_len: int = 5, max_len: int = 16) -> List[str]:
    if not seq_str: return []
    blocks = []
    current_char, current_len = seq_str[0], 0
    for c in seq_str:
        if c == current_char: current_len += 1
        else:
            blocks.append(current_char * current_len)
            current_char, current_len = c, 1
    blocks.append(current_char * current_len)
    
    candidates = []
    for i in range(len(blocks)):
        candidate = "".join(blocks[i:])
        if min_len <= len(candidate) <= max_len: candidates.append(candidate)
    return candidates

def exact_size_sequence(history: List[Dict[str, Any]]):
    seq = [1 if x["size"] == 1 else 0 for x in history[-CFG.max_history:]]
    if len(seq) < 5: return None

    tail_str = "".join("B" if v == 1 else "S" for v in seq[-16:])
    candidates = get_block_candidates(tail_str, min_len=5, max_len=16)
    
    best_signal = None
    best_score = 0.0

    for cand in candidates:
        cand_seq = [1 if c == 'B' else 0 for c in cand]
        cand_len = len(cand_seq)

        match_indices = [i for i in range(len(seq) - cand_len) if seq[i:i+cand_len] == cand_seq]
        if len(match_indices) >= CFG.min_repeat:
            next_bits = [seq[i + cand_len] for i in match_indices if i + cand_len < len(seq)]
            if not next_bits: continue

            total = len(next_bits)
            decay = 0.85
            weights = [decay ** (total - 1 - k) for k in range(total)]
            w_big = sum(w for w, v in zip(weights, next_bits) if v == 1)
            w_small = sum(w for w, v in zip(weights, next_bits) if v == 0)
            w_total = w_big + w_small or 1.0

            big_prob = w_big / w_total
            small_prob = w_small / w_total
            predicted_bit_normal = 1 if big_prob >= small_prob else 0
            win_rate = max(big_prob, small_prob)
            
            sample_bonus = min(total / 10.0, 1.0) * 0.1
            length_bonus = (cand_len / 16.0) * 0.05
            score = win_rate + sample_bonus + length_bonus

            if win_rate >= 0.60 and score > best_score:
                best_score = score
                info = {
                    "length": cand_len, "pattern_text": cand, "predicted_bit_normal": predicted_bit_normal,
                    "total_count": total, "big_prob": big_prob, "small_prob": small_prob,
                    "confidence": abs(big_prob - small_prob), "win_rate": win_rate, "pattern_key": cand,
                }
                mem = STORE.get_pattern_memory(cand)
                if mem["is_flipped"]:
                    info["predicted_bit"] = 1 - predicted_bit_normal
                    info["is_flipped"] = True
                else:
                    info["predicted_bit"] = predicted_bit_normal
                    info["is_flipped"] = False
                    
                info["predicted_side"] = "BIG" if info["predicted_bit"] == 1 else "SMALL"
                best_signal = info

    return best_signal

def choose_valid_pattern(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    exact = exact_size_sequence(history)
    if exact:
        penalty = STORE.get_penalty(exact["pattern_key"])
        if penalty > 0.25:
            flip_text = " [FLIPPED]" if exact.get("is_flipped") else ""
            return {
                "size": exact["predicted_side"],
                "logic": f"SEQ {exact['length']} | {exact['pattern_text']} | n={exact['total_count']} | WR={exact['win_rate']*100:.1f}%{flip_text}",
                "confidence": clamp((exact["win_rate"] * 0.6) + (exact["confidence"] * 0.8), 0.55, 0.92),
                "stake": "1x" if exact["win_rate"] >= 0.72 else "0.5x",
                "pattern_key": exact["pattern_key"],
            }
    return None

# ============================================================
# ADVANCED TREND & FALLBACK
# ============================================================
def advanced_markov(seq: List[int], max_n: int = 4) -> Tuple[float, float]:
    if len(seq) < 5: return 0.5, 0.0
    total_score, total_weight = 0.0, 0.0
    for n in range(2, max_n + 1):
        if len(seq) <= n: continue
        pattern = tuple(seq[-n:])
        match_count, big_count = 0, 0
        for i in range(len(seq) - n):
            if tuple(seq[i:i+n]) == pattern:
                match_count += 1
                if seq[i+n] == 1: big_count += 1
        if match_count >= 2:
            prob = big_count / match_count
            weight = match_count * (n ** 1.5)
            total_score += prob * weight
            total_weight += weight
    return (total_score / total_weight, total_weight) if total_weight > 0 else (0.5, 0.0)

def hybrid_prediction(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    if not seq: return {"size": "BIG", "logic": "EMPTY", "confidence": 0.55, "stake": "0.5x"}

    run = trailing_run(seq)
    if run >= 3:
        return {"size": "BIG" if seq[-1] == 1 else "SMALL", "logic": f"TREND FOLLOW ({run}x)", "confidence": 0.60, "stake": "0.5x"}
        
    if len(seq) >= 20:
        sma5, sma20 = sum(seq[-5:]) / 5, sum(seq[-20:]) / 20
        if sma5 > sma20 + 0.15: return {"size": "BIG", "logic": f"SMA CROSS (5>20)", "confidence": 0.58, "stake": "0.5x"}
        elif sma5 < sma20 - 0.15: return {"size": "SMALL", "logic": f"SMA CROSS (5<20)", "confidence": 0.58, "stake": "0.5x"}

    markov_prob, _ = advanced_markov(seq)
    bias = sum(seq) / len(seq)
    score = (markov_prob * 0.5) + (bias * 0.3) + ((1.0 - min(volatility(seq, 20), 1.0)) * 0.2)
    size = "BIG" if score >= 0.5 else "SMALL"
    return {"size": size, "logic": f"MARKOV (P={markov_prob:.1%})", "confidence": clamp(max(score, 1.0 - score), 0.55, 0.78), "stake": "0.5x"}

def weighted_ensemble(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    seq_signal = choose_valid_pattern(history)
    hybrid = hybrid_prediction(history)
    
    sma_trend = "NEUTRAL"
    if len(seq) >= 20:
        sma5, sma20 = sum(seq[-5:]) / 5, sum(seq[-20:]) / 20
        if sma5 > sma20 + 0.15: sma_trend = "BIG"
        elif sma5 < sma20 - 0.15: sma_trend = "SMALL"

    final_size = seq_signal["size"] if seq_signal else hybrid["size"]
    final_score = seq_signal["confidence"] if seq_signal else hybrid["confidence"]
    logic = seq_signal["logic"] if seq_signal else hybrid["logic"]
    pattern_key = seq_signal["pattern_key"] if seq_signal else None

    if seq_signal and sma_trend != "NEUTRAL":
        if sma_trend == final_size:
            final_score = min(0.95, final_score + 0.10)
            logic += " + TREND MATCH"
        else:
            final_score = max(0.55, final_score - 0.15)
            logic += " (AGAINST TREND)"

    recent = STORE.recent_outcomes(8)
    recent_fail_count = sum(1 for _, _, s in recent if "LOSS" in str(s))

    if recent_fail_count >= 2:
        final_conf = max(0.55, final_score * 0.75)
        final_stake = "0.5x"
    else:
        final_conf = final_score
        final_stake = "2x" if final_conf >= 0.82 else "1x" if final_conf >= 0.72 else "0.5x"

    return {
        "size": final_size, "logic": logic, "confidence": clamp(final_conf, 0.55, 0.95),
        "stake": final_stake, "pattern_key": pattern_key,
    }

# ============================================================
# PREDICTION BUILD
# ============================================================
def build_prediction(history: List[Dict[str, Any]], issue: Any) -> Dict[str, Any]:
    if len(history) < CFG.min_history:
        n = random.randint(0, 9)
        return {"issue": str(issue), "type": "SIZE", "size": "BIG" if n >= 5 else "SMALL", "num": n, "raw_conf": 50, "logic": "WARMING UP", "stake": "0.5x", "details": {}}

    if STATE.loss_streak >= 2:
        return {"issue": str(issue), "type": "SIZE", "size": "SKIP", "num": 0, "raw_conf": 0, "logic": f"COOLDOWN (Loss Streak: {STATE.loss_streak})", "stake": "0x", "details": {}}

    ensemble = weighted_ensemble(history)
    pred_size = ensemble["size"]
    final_num = random.randint(5, 9) if pred_size == "BIG" else random.randint(0, 4)
    
    return {
        "issue": str(issue), "type": "SIZE", "size": pred_size, "num": final_num,
        "raw_conf": int(ensemble["confidence"] * 100), "logic": ensemble["logic"], "stake": ensemble["stake"],
        "details": {
            "signal_strength": signal_strength(history),
            "regime": detect_regime([x["size"] for x in history]),
            "pattern_key": ensemble.get("pattern_key")
        }
    }

# ============================================================
# PARSE & FETCH
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None: return []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list): items = payload["data"]
        elif isinstance(payload.get("data"), dict): items = payload["data"].get("list", []) if isinstance(payload["data"].get("list"), list) else []
        else: items = payload.get("result", payload.get("items", payload.get("history", [])))
    else: items = []

    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None: continue
        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if not (0 <= num <= 9): continue
        key = str(issue)
        if key in seen: continue
        seen.add(key)
        out.append({"issue": key, "num": num, "size": 1 if num >= 5 else 0})
    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_history(session):
    for url in CFG.api_urls():
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200: continue
                parsed = parse_history_payload(await response.json(content_type=None))
                if parsed:
                    STATE.history = parsed[-CFG.max_history:]
                    STATE.save_cache()
                    return True
        except Exception: pass
    return False

# ============================================================
# FORMAT / SEND
# ============================================================
def normalize_recent_row(row: Any):
    if not isinstance(row, (tuple, list)): return "", "", "", ""
    if len(row) >= 3: return str(row[0]), str(row[1]), str(row[2]), ""
    return "", "", "", ""

def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_lines = []
    for r in recent_rows[-CFG.recent_limit:]:
        issue, size, status, _ = normalize_recent_row(r)
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
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'])}</code>\nPred: <b>{h(res['size'])} {h(res['num'])}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(bot, text: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await asyncio.wait_for(bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"), timeout=CFG.request_timeout)
            return True
        except Exception: await asyncio.sleep(1)
    return False

# ============================================================
# WORKER LOOP
# ============================================================
async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_history(session):
                history_by_issue = {str(x["issue"]): x for x in STATE.history}
                for res in STORE.resolve_pending(history_by_issue):
                    if res["win"]:
                        STATE.win_streak += 1; STATE.loss_streak = 0; STATE.recovery_mode = False
                    else:
                        STATE.loss_streak += 1; STATE.win_streak = 0; STATE.recovery_mode = STATE.loss_streak >= 2

                    details = res.get("details", {})
                    pattern_key = details.get("pattern_key") if isinstance(details, dict) else None
                    
                    if pattern_key:
                        STORE.penalize_pattern(str(pattern_key), bool(res["win"]))
                        STORE.record_pattern_outcome(str(pattern_key), bool(res["win"]))

                    STATE.save_cache()
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = build_prediction(STATE.history, next_issue)
                    
                    if pred["size"] == "SKIP":
                        skip_msg = f"⚠️ <b>COOLDOWN ACTIVE</b>\nLagatar {STATE.loss_streak} loss hue hain. Bot 1 period observe kar raha hai.\nPeriod: <code>{next_issue}</code>"
                        await send_msg(app.bot, skip_msg)
                        STATE.loss_streak = 0
                        last_processed_issue = latest_issue
                        await asyncio.sleep(CFG.fetch_every)
                        continue
                        
                    STORE.save_prediction({**pred, "type": "SIZE"})
                    await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop exception: %s", e)
            await asyncio.sleep(2)

async def health_check(request): return web.Response(text="FTW FINAL PATTERN is Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

async def main():
    CFG.validate()
    STATE.load_cache()
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    await app.initialize()
    await app.start()
    log.info("Bot started: %s", (await app.bot.get_me()).username)
    await start_web_server()
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(worker_loop(app, session))
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
