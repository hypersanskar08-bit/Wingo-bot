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
    db_path: str = os.environ.get("DB_PATH", "ftw_auto_adapt.db")
    cache_file: str = "ftw_auto_adapt_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW AUTO ADAPT"
    min_history: int = 8
    recent_limit: int = 12
    max_history: int = 2000

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
            Path(CFG.log_dir) / "ftw_auto_adapt.log",
            maxBytes=1_000_000,
            backupCount=3
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_auto_adapt")

# ============================================================
# GENERAL HELPERS
# ============================================================
def h(x: Any) -> str:
    return html.escape(str(x))

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def normalize_issue(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        if value.isdigit():
            return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            return int(digits)
    try:
        return int(value)
    except Exception:
        return None

def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))

def trailing_run(seq: List[int]) -> int:
    if not seq:
        return 0
    last = seq[-1]
    count = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return count

def alternating_score(seq: List[int], window: int = 12) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    changes = sum(1 for i in range(1, len(arr)) if arr[i] != arr[i - 1])
    return changes / max(1, len(arr) - 1)

def volatility(seq: List[int], window: int = 20) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

def ratio_big(seq: List[int], window: int = 20) -> float:
    if not seq:
        return 0.5
    arr = seq[-window:]
    return sum(arr) / max(1, len(arr))

def block_pattern(seq: List[int], window: int = 10) -> bool:
    if len(seq) < 4:
        return False
    arr = seq[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i + 4]
        if a == b and c == d and a != c:
            return True
    return False

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8:
        return "COLD"
    run = trailing_run(seq)
    alt = alternating_score(seq, 10)
    bp = block_pattern(seq, 10)
    ratio = ratio_big(seq, 10)

    if alt >= 0.65:
        return "ALTERNATING"
    if bp:
        return "BLOCK"
    if run >= 5:
        return "TREND"
    if run >= 3:
        return "TRAP"
    if abs(ratio - 0.5) <= 0.12:
        return "CHOPPY"
    return "CYCLE"

def signal_strength(history: List[Dict[str, Any]]) -> float:
    if len(history) < 8:
        return 0.2
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    run = trailing_run(seq)
    alt = alternating_score(seq, 12)
    bp = block_pattern(seq, 10)
    ratio = ratio_big(seq, 20)

    s = 0.0
    if regime == "TRAP":
        s += 0.35
    if regime == "TREND":
        s += 0.25
    if regime == "ALTERNATING":
        s += 0.30
    if bp:
        s += 0.20
    if run >= 3:
        s += 0.25
    if abs(ratio - 0.5) > 0.18:
        s += 0.15
    if alt >= 0.6:
        s += 0.15
    return clamp(min(1.0, s), 0.0, 1.0)

def side_balance(history: List[Dict[str, Any]], window: int = 20) -> float:
    if len(history) < 2:
        return 0.0
    seq = [x["size"] for x in history[-window:]]
    small_share = sum(1 for v in seq if v == 0) / len(seq)
    big_share = sum(1 for v in seq if v == 1) / len(seq)
    return small_share - big_share

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
        except Exception:
            pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data[-CFG.max_history:]
        except Exception:
            pass

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
                    issue TEXT PRIMARY KEY,
                    pred_type TEXT,
                    size TEXT,
                    num INTEGER,
                    status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER,
                    actual_size TEXT,
                    logic TEXT,
                    details TEXT,
                    stake TEXT,
                    created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_stats (
                    pattern_key TEXT PRIMARY KEY,
                    hit_count INTEGER DEFAULT 0,
                    miss_count INTEGER DEFAULT 0,
                    last_seen REAL DEFAULT 0,
                    support REAL DEFAULT 0,
                    confidence REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_penalties (
                    pattern_key TEXT PRIMARY KEY,
                    fail_streak INTEGER DEFAULT 0,
                    last_fail REAL DEFAULT 0,
                    penalty REAL DEFAULT 1.0
                )
            """)

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["type"], p.get("size", ""), p.get("num", 0),
                p.get("logic", "N/A"),
                json.dumps(p.get("details", {})),
                p.get("stake", "1x"),
                time.time()
            ))

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, pred_type, size, num, details, stake
                FROM predictions
                WHERE status='PENDING'
            """).fetchall()

        for issue, pred_type, p_size, p_num, details_json, stake in rows:
            act = history_by_issue.get(str(issue))
            if not act:
                continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == actual_size) if p_size else True
            if win and p_num == act["num"]:
                status = "JACK 🏆"
            elif win:
                status = "WIN ✅"
            else:
                status = "LOSS ❌"

            with self._conn() as c2:
                c2.execute("""
                    UPDATE predictions
                    SET status=?, actual_num=?, actual_size=?
                    WHERE issue=?
                """, (status, act["num"], actual_size, issue))

            resolved.append({
                "issue": issue,
                "type": pred_type or "SIZE",
                "size": p_size,
                "num": p_num,
                "stake": stake,
                "actual_num": act["num"],
                "actual_size": actual_size,
                "status": status,
                "win": win,
                "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, status
                FROM predictions
                WHERE status != 'PENDING'
                ORDER BY CAST(issue AS INTEGER) DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

    def update_pattern_stats(self, key: str, result: int):
        now = time.time()
        with self._conn() as c:
            row = c.execute("""
                SELECT hit_count, miss_count, last_seen, support, confidence
                FROM pattern_stats
                WHERE pattern_key=?
            """, (key,)).fetchone()

            if row is None:
                hit, miss, last_seen, support, confidence = 0, 0, 0.0, 0.0, 0.0
            else:
                hit, miss, last_seen, support, confidence = row

            if result == 1:
                hit += 1
            else:
                miss += 1

            total = hit + miss
            support = (hit / total) if total else 0.0
            confidence = abs(support - 0.5) * 2.0

            c.execute("""
                INSERT INTO pattern_stats
                (pattern_key, hit_count, miss_count, last_seen, support, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    hit_count=excluded.hit_count,
                    miss_count=excluded.miss_count,
                    last_seen=excluded.last_seen,
                    support=excluded.support,
                    confidence=excluded.confidence
            """, (key, hit, miss, now, support, confidence))

    def record_pattern_penalty(self, pattern_key: str, failed: bool):
        now = time.time()
        with self._conn() as c:
            row = c.execute("""
                SELECT fail_streak, last_fail, penalty
                FROM pattern_penalties
                WHERE pattern_key=?
            """, (pattern_key,)).fetchone()

            if row is None:
                fail_streak, last_fail, penalty = 0, 0.0, 1.0
            else:
                fail_streak, last_fail, penalty = row

            if failed:
                fail_streak += 1
                if fail_streak >= 1:
                    penalty = max(0.15, penalty * 0.75)
            else:
                fail_streak = max(0, fail_streak - 1)
                penalty = min(1.0, penalty + 0.08)

            c.execute("""
                INSERT INTO pattern_penalties
                (pattern_key, fail_streak, last_fail, penalty)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    fail_streak=excluded.fail_streak,
                    last_fail=excluded.last_fail,
                    penalty=excluded.penalty
            """, (pattern_key, fail_streak, now, penalty))

    def get_pattern_penalty(self, pattern_key: str) -> float:
        with self._conn() as c:
            row = c.execute("""
                SELECT penalty
                FROM pattern_penalties
                WHERE pattern_key=?
            """, (pattern_key,)).fetchone()
        if not row:
            return 1.0
        return float(row[0])

    def get_pattern_score(self, key: str) -> float:
        with self._conn() as c:
            row = c.execute("""
                SELECT support, confidence, last_seen
                FROM pattern_stats
                WHERE pattern_key=?
            """, (key,)).fetchone()
        if not row:
            return 0.0
        support, confidence, last_seen = row
        age = max(0.0, 1.0 - ((time.time() - float(last_seen)) / 600.0))
        base = (support * 0.7 + confidence * 0.3) * (0.5 + age * 0.5)
        penalty = self.get_pattern_penalty(key)
        return base * penalty

STORE = Storage(CFG.db_path)

# ============================================================
# AUTO ADAPTIVE WEIGHTS
# ============================================================
class AdaptiveWeights:
    def __init__(self):
        self.recent: List[float] = []

    def update(self, win: bool):
        self.recent.append(1.0 if win else 0.0)
        if len(self.recent) > 25:
            self.recent = self.recent[-25:]

    def current(self) -> Dict[str, float]:
        if not self.recent:
            return {"exact": 0.40, "markov": 0.22, "trend": 0.18, "parity": 0.10, "regime": 0.08, "recovery": 0.02}

        rate = sum(self.recent) / len(self.recent)

        if rate >= 0.65:
            return {"exact": 0.42, "markov": 0.20, "trend": 0.16, "parity": 0.10, "regime": 0.08, "recovery": 0.04}
        if rate >= 0.50:
            return {"exact": 0.36, "markov": 0.22, "trend": 0.18, "parity": 0.12, "regime": 0.09, "recovery": 0.03}
        return {"exact": 0.28, "markov": 0.24, "trend": 0.20, "parity": 0.14, "regime": 0.10, "recovery": 0.04}

ADAPTIVE = AdaptiveWeights()

# ============================================================
# PATTERN ANALYSIS
# ============================================================
def pattern_label_from_bits(bits: List[int]) -> str:
    return "".join("B" if b == 1 else "S" for b in bits)

def pattern_penalty_factor(pattern_key: str) -> float:
    return STORE.get_pattern_penalty(pattern_key)

def strong_pattern_signal(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    if len(seq) < 8:
        return None

    scans = []
    for window in range(5, 15):
        if len(seq) < window + 1:
            continue
        suffix = tuple(seq[-window:])
        counts = defaultdict(int)
        next_big = defaultdict(int)
        next_small = defaultdict(int)

        for i in range(len(seq) - window):
            key = tuple(seq[i:i + window])
            counts[key] += 1
            if seq[i + window] == 1:
                next_big[key] += 1
            else:
                next_small[key] += 1

        if suffix not in counts:
            continue

        total = counts[suffix]
        big_prob = next_big.get(suffix, 0) / total
        small_prob = next_small.get(suffix, 0) / total

        support = total / max(1, len(seq) - window)
        pred = "BIG" if big_prob >= small_prob else "SMALL"
        confidence = abs(big_prob - small_prob) * 100.0
        edge = abs(big_prob - small_prob) * 100.0

        if support < 0.02 and confidence < 15:
            continue

        key = f"{window}:{suffix}"
        penalty = pattern_penalty_factor(key)
        adjusted_score = (confidence + support * 100.0 + window * 2.5) * penalty

        scans.append({
            "window": window,
            "pattern_key": key,
            "suffix": suffix,
            "pred": pred,
            "confidence": confidence,
            "support": support,
            "edge": edge,
            "score": adjusted_score
        })

    if not scans:
        return None

    best = max(scans, key=lambda x: x["score"])
    if best["confidence"] < 15 and best["support"] < 0.05:
        return None

    penalty = pattern_penalty_factor(best["pattern_key"])
    final_conf = clamp(best["confidence"] / 100.0 * penalty, 0.35, 0.92)

    result = {
        "size": best["pred"],
        "logic": f"ADAPTIVE PATTERN {best['window']} | {pattern_label_from_bits(list(best['suffix']))} | conf={best['confidence']:.1f}% | edge={best['edge']:.1f}%",
        "confidence": final_conf,
        "stake": "1x" if final_conf >= 0.72 else "0.5x",
        "pattern_key": best["pattern_key"]
    }
    return result

# ============================================================
# TREND / CONTEXT
# ============================================================
def trend_phase(history: List[Dict[str, Any]]) -> str:
    if len(history) < 8:
        return "COLD"
    seq = [x["size"] for x in history]
    run = trailing_run(seq)
    current_side = seq[-1]
    recent = seq[-10:]
    same_share = sum(1 for v in recent if v == current_side) / len(recent)

    last3, prev3 = seq[-3:], seq[-6:-3]
    if len(last3) == 3 and len(prev3) == 3:
        last_same = sum(1 for v in last3 if v == current_side)
        prev_same = sum(1 for v in prev3 if v == current_side)
        momentum_decay = (last_same < prev_same)
    else:
        momentum_decay = False

    if run >= 5 and same_share >= 0.75 and momentum_decay:
        return "EXHAUSTED"
    if run >= 4 and same_share >= 0.68:
        return "DOMINANT"
    if run >= 2:
        return "BUILDING"
    return "COLD"

def momentum_decay_signal(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 8:
        return None
    seq = [x["size"] for x in history]
    phase = trend_phase(history)
    current_side = seq[-1]
    recent = seq[-12:]
    big_share = sum(1 for v in recent if v == 1) / len(recent)
    small_share = sum(1 for v in recent if v == 0) / len(recent)

    last3, prev3 = seq[-3:], seq[-6:-3]
    if len(last3) == 3 and len(prev3) == 3:
        last_same = sum(1 for v in last3 if v == current_side)
        prev_same = sum(1 for v in prev3 if v == current_side)
        is_decaying = (last_same < prev_same)
    else:
        is_decaying = False

    if phase == "EXHAUSTED" and is_decaying:
        if current_side == 1 and big_share >= 0.75:
            return {"size": "SMALL", "logic": "TREND EXHAUSTION: DECAY CONFIRMED", "confidence": 0.92, "stake": "1x"}
        if current_side == 0 and small_share >= 0.75:
            return {"size": "BIG", "logic": "TREND EXHAUSTION: DECAY CONFIRMED", "confidence": 0.92, "stake": "1x"}
    return None

def context_shift_signal(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 20:
        return None
    seq = [x["size"] for x in history]
    current_run = trailing_run(seq)
    current_side = seq[-1]

    if current_run < 3:
        return None

    for lookback in [5, 4, 3]:
        context_length = current_run + lookback
        if len(seq) < context_length + 1:
            continue

        target_context = tuple(seq[-context_length:])
        continuation_count = 0
        break_count = 0

        for i in range(len(seq) - context_length):
            if tuple(seq[i:i + context_length]) == target_context:
                next_idx = i + context_length
                if seq[next_idx] == current_side:
                    continuation_count += 1
                else:
                    break_count += 1

        total = continuation_count + break_count
        if total >= 3:
            cont_prob = continuation_count / total
            break_prob = break_count / total

            if break_prob >= 0.75:
                return {
                    "size": "SMALL" if current_side == 1 else "BIG",
                    "logic": f"CTX SHIFT: {break_prob * 100:.0f}% BREAK",
                    "confidence": 0.86,
                    "stake": "1x"
                }
            elif cont_prob >= 0.68:
                return {
                    "size": "BIG" if current_side == 1 else "SMALL",
                    "logic": f"CTX CONTINUE: {cont_prob * 100:.0f}% CONT",
                    "confidence": 0.86,
                    "stake": "1x"
                }
    return None

def trend_signal(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    if len(seq) < 3:
        return {"size": "BIG" if random.random() > 0.5 else "SMALL", "confidence": 0.5}

    hist_ctx = context_shift_signal(history)
    if hist_ctx:
        return hist_ctx

    run = trailing_run(seq)
    last = seq[-1]

    if run >= 3:
        last3, prev3 = seq[-3:], seq[-6:-3]
        if len(prev3) == 3:
            last_same = sum(1 for v in last3 if v == last)
            prev_same = sum(1 for v in prev3 if v == last)
            if last_same < prev_same:
                return {"size": "SMALL" if last == 1 else "BIG", "confidence": 0.85}
        return {"size": "BIG" if last == 1 else "SMALL", "confidence": 0.75}

    return {"size": "BIG" if result_bias(history) >= 0.5 else "SMALL", "confidence": 0.6}

# ============================================================
# EXACT PATTERN MATCH
# ============================================================
def exact_pattern_candidates(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    candidates = []
    for window in range(5, 15):
        if len(seq) < window + 1:
            continue
        suffix = tuple(seq[-window:])
        counts, next_big, next_small = defaultdict(int), defaultdict(int), defaultdict(int)

        for i in range(len(seq) - window):
            key = tuple(seq[i:i + window])
            counts[key] += 1
            if seq[i + window] == 1:
                next_big[key] += 1
            else:
                next_small[key] += 1

        if suffix not in counts:
            continue

        total = counts[suffix]
        big_prob = next_big.get(suffix, 0) / total
        small_prob = next_small.get(suffix, 0) / total

        support = total / max(1, len(seq) - window)
        pred = "BIG" if big_prob >= small_prob else "SMALL"
        confidence = abs(big_prob - small_prob) * 100.0

        if support < 0.02 and confidence < 15:
            continue

        key = f"{window}:{suffix}"
        score = confidence + (support * 100.0) + (window * 2.5) + STORE.get_pattern_score(key)
        candidates.append({
            "window": window,
            "suffix": suffix,
            "pred": pred,
            "confidence": confidence,
            "support": support,
            "score": score,
            "key": key
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:12]

def choose_exact_prediction(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = exact_pattern_candidates(history)
    if not candidates:
        return None
    best = candidates[0]
    if best["confidence"] < 15 and best["support"] < 0.05:
        return None

    pred = best["pred"]
    confidence = clamp(best["confidence"] / 100.0, 0.55, 0.92)
    STORE.update_pattern_stats(best["key"], 1 if pred == "BIG" else 0)

    return {
        "size": pred,
        "logic": f"EXACT {best['window']}-SEQ PATTERN",
        "confidence": confidence,
        "stake": "1x" if confidence >= 0.72 else "0.5x"
    }

# ============================================================
# HYBRID / FALLBACK
# ============================================================
def markov_prob(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if len(sizes) < 3:
        return 0.5
    recent_pair = tuple(sizes[-2:])
    count, big = 0, 0
    for i in range(len(sizes) - 2):
        if tuple(sizes[i:i + 2]) == recent_pair:
            count += 1
            if sizes[i + 2] == 1:
                big += 1
    return (big / count) if count > 0 else 0.5

def parity_prob(history: List[Dict[str, Any]]) -> float:
    nums = [x["num"] for x in history[-30:]]
    if not nums:
        return 0.5
    evens = sum(1 for n in nums if n % 2 == 0)
    return evens / len(nums)

def result_bias(history: List[Dict[str, Any]]) -> float:
    seq = [x["size"] for x in history[-40:]]
    if not seq:
        return 0.5
    return sum(seq) / len(seq)

def hybrid_prediction(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    markov = markov_prob(history)
    parity = parity_prob(history)
    bias = result_bias(history)
    vol = volatility([x["size"] for x in history], 20)

    score = (markov * 0.36) + (bias * 0.28) + (parity * 0.18) + ((1.0 - min(vol, 1.0)) * 0.18)
    size = "BIG" if score >= 0.5 else "SMALL"
    confidence = clamp(max(score, 1.0 - score), 0.55, 0.78)

    return {
        "size": size,
        "logic": "HYBRID FALLBACK",
        "confidence": confidence,
        "stake": "0.5x" if confidence < 0.68 else "1x"
    }

# ============================================================
# NUMBER SELECTION
# ============================================================
def family_for_size(size: str) -> List[int]:
    return [5, 6, 7, 8, 9] if size == "BIG" else [0, 1, 2, 3, 4]

def is_same_family(num: int, size: str) -> bool:
    return num in family_for_size(size)

def number_repeat_score(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    nums = [x["num"] for x in history[-30:]]
    if not nums:
        return {"value": 0, "score": 0.0, "logic": "NONE"}
    most_common_num, most_count = Counter(nums).most_common(1)[0]
    repeat_ratio = most_count / len(nums)
    return {"value": most_common_num, "score": repeat_ratio, "logic": "REPEATED_NUMBER" if repeat_ratio >= 0.2 else "NONE"}

def number_gap_score(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    nums = [x["num"] for x in history[-12:]]
    if len(nums) < 3:
        return {"value": 0, "score": 0.0, "logic": "NONE"}
    diffs = [abs(nums[i] - nums[i - 1]) for i in range(1, len(nums))]
    if not diffs:
        return {"value": 0, "score": 0.0, "logic": "NONE"}
    avg_gap = sum(diffs) / len(diffs)
    last_gap = abs(nums[-1] - nums[-2])
    if avg_gap < 2:
        return {"value": nums[-1], "score": 0.7, "logic": "LOW_GAP"}
    if last_gap >= 3:
        return {"value": max(0, nums[-1] - 1), "score": 0.6, "logic": "GAP_JUMP"}
    return {"value": 0, "score": 0.0, "logic": "NONE"}

def choose_number_for_size(size: str, history: List[Dict[str, Any]]) -> int:
    valid = family_for_size(size)

    for w in range(3, 9):
        if len(history) < w + 1:
            continue
        nums = [x["num"] for x in history]
        suffix = tuple(nums[-w:])
        counts = defaultdict(int)
        next_counts = defaultdict(int)
        for i in range(len(nums) - w):
            key = tuple(nums[i:i + w])
            counts[key] += 1
            next_counts[(key, nums[i + w])] += 1
        if suffix not in counts:
            continue
        total = counts[suffix]
        best_num = -1
        best_freq = 0
        for (k, num), freq in next_counts.items():
            if k == suffix and freq > best_freq:
                best_freq = freq
                best_num = num
        if best_num != -1:
            confidence = (best_freq / total) * 100.0
            if confidence >= 20 and best_num in valid:
                return best_num

    repeat = number_repeat_score(history)
    if repeat["score"] >= 0.2 and repeat["value"] in valid:
        return repeat["value"]

    gap = number_gap_score(history)
    if gap["score"] > 0.0 and gap["value"] in valid:
        return gap["value"]

    family_nums = [x["num"] for x in history if is_same_family(x["num"], size)]
    if family_nums:
        best_num, _ = Counter(family_nums).most_common(1)[0]
        if best_num in valid:
            return best_num

    recent = [x["num"] for x in history[-15:]]
    if recent and recent[-1] in valid:
        return recent[-1]

    return random.choice(valid)

# ============================================================
# AUTO ADAPTIVE ENSEMBLE
# ============================================================
def weighted_ensemble(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    exact = choose_exact_prediction(history)
    hybrid = hybrid_prediction(history)

    final_size = exact["size"] if exact else hybrid["size"]
    final_score = exact["confidence"] if exact else hybrid["confidence"]

    pattern_signal = strong_pattern_signal(history)
    if pattern_signal:
        final_size = pattern_signal["size"]
        final_score = max(final_score, pattern_signal["confidence"])

    ctx = context_shift_signal(history)
    if ctx:
        final_size = ctx["size"]
        final_score = max(final_score, ctx["confidence"])

    mom = momentum_decay_signal(history)
    if mom:
        final_size = mom["size"]
        final_score = max(final_score, mom["confidence"])

    trend = trend_signal(history)
    if trend:
        final_size = trend["size"]
        final_score = max(final_score, trend["confidence"])

    final_conf = clamp(final_score, 0.55, 0.94)

    # auto-correct if recent repeated losses
    recent_fail_count = 0
    if len(STATE.history) >= 8:
        recent_slice = STATE.history[-8:]
        recent_fail_count = sum(1 for x in recent_slice if x.get("status") == "LOSS") if recent_slice else 0

    if recent_fail_count >= 3:
        final_conf = max(0.55, final_conf * 0.75)

    logic = "AUTO ADAPTIVE ENSEMBLE"

    return {
        "size": final_size,
        "logic": logic,
        "confidence": final_conf,
        "stake": "2x" if final_conf >= 0.82 else "1x" if final_conf >= 0.72 else "0.5x"
    }

# ============================================================
# PREDICTION BUILD
# ============================================================
def build_prediction(history: List[Dict[str, Any]], issue: Any) -> Dict[str, Any]:
    if len(history) < CFG.min_history:
        n = random.randint(0, 9)
        return {
            "issue": str(issue),
            "type": "SIZE",
            "size": "BIG" if n >= 5 else "SMALL",
            "num": n,
            "raw_conf": 50,
            "logic": "WARMING UP",
            "stake": "0.5x",
            "details": {}
        }

    ensemble = weighted_ensemble(history)
    pred_size = ensemble["size"]
    pred_logic = ensemble["logic"]
    pred_conf = ensemble["confidence"]
    pred_stake = ensemble["stake"]

    # Auto self-correction: repeated failure reduces confidence and stake
    recent_fail_count = 0
    if len(STATE.history) >= 8:
        recent_slice = STATE.history[-8:]
        recent_fail_count = sum(1 for x in recent_slice if x.get("status") == "LOSS") if recent_slice else 0

    if recent_fail_count >= 3:
        pred_conf = max(0.55, pred_conf * 0.75)
        pred_stake = "0.5x" if pred_conf < 0.75 else "1x"

    final_num = choose_number_for_size(pred_size, history)
    final_issue = str(issue)

    return {
        "issue": final_issue,
        "type": "SIZE",
        "size": pred_size,
        "num": final_num,
        "raw_conf": int(pred_conf * 100),
        "logic": pred_logic,
        "stake": pred_stake,
        "details": {
            "signal_strength": signal_strength(history),
            "regime": detect_regime([x["size"] for x in history]),
            "side_balance": round(side_balance(history, 20), 4),
            "trend_phase": trend_phase(history)
        }
    }

# ============================================================
# PARSE HISTORY / FETCH
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            d = payload["data"]
            items = d.get("list", []) if isinstance(d.get("list"), list) else d if isinstance(d, list) else []
        else:
            items = payload.get("result", payload.get("items", payload.get("history", [])))
    else:
        items = []

    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue

        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None:
            continue

        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if not (0 <= num <= 9):
            continue

        key = str(issue)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "issue": key,
            "num": num,
            "size": 1 if num >= 5 else 0
        })

    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_history(session):
    for url in CFG.api_urls():
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200:
                    continue
                parsed = parse_history_payload(await response.json(content_type=None))
                if parsed:
                    STATE.history = parsed[-CFG.max_history:]
                    STATE.save_cache()
                    return True
        except Exception:
            pass
    return False

# ============================================================
# FORMAT / SEND
# ============================================================
def normalize_recent_row(row: Any):
    if not isinstance(row, (tuple, list)):
        return "", "", "", ""
    if len(row) >= 4:
        return str(row[0]), str(row[1]), str(row[2]), ""
    if len(row) >= 3:
        return str(row[0]), str(row[1]), str(row[2]), ""
    if len(row) >= 2:
        return str(row[0]), str(row[1]), "", ""
    return "", "", "", ""

def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_lines = []
    for r in recent_rows[-CFG.recent_limit:]:
        issue, size, status, _extra = normalize_recent_row(r)
        if not issue:
            continue
        recent_lines.append(
            f"➜ <code>{h(issue[-4:])}</code> {h(size)} → <b>{h(status)}</b>" if status else
            f"➜ <code>{h(issue[-4:])}</code> {h(size)}"
        )

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
    return (
        f"🎯 <b>Result</b>\n"
        f"Period: <code>{h(res['issue'])}</code>\n"
        f"Pred: <b>{h(res['size'])} {h(res['num'])}</b>\n"
        f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
        f"<b>{h(res['status'])}</b>"
    )

async def send_msg(bot, text: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=CFG.parse_chat_id(),
                    text=text,
                    parse_mode="HTML"
                ),
                timeout=CFG.request_timeout
            )
            return True
        except Exception:
            await asyncio.sleep(1)
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
                        STATE.win_streak += 1
                        STATE.loss_streak = 0
                        STATE.recovery_mode = False
                    else:
                        STATE.loss_streak += 1
                        STATE.win_streak = 0
                        STATE.recovery_mode = STATE.loss_streak >= 2

                    ADAPTIVE.update(res["win"])
                    if res["size"] and res["actual_size"]:
                        pattern_key = f"{res['type']}:{res['logic']}"
                        STORE.record_pattern_penalty(pattern_key, not res["win"])

                    STATE.save_cache()
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = build_prediction(STATE.history, next_issue)
                    if pred:
                        STORE.save_prediction({
                            **pred,
                            "type": "SIZE",
                            "details": pred.get("details", {})
                        })
                        await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop exception: %s", e)
            await asyncio.sleep(2)

async def health_check(request):
    return web.Response(text="FTW AUTO ADAPT is Running")

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
