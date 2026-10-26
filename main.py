"""
FTW V35
========
Upgrade of V16 base logic with:
- stronger exact pattern engine
- profile-aware trend detection
- recovery / reversal pressure
- exact number matrix
- anti-bias guard
- Telegram ready
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
from collections import Counter, defaultdict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from telegram.ext import Application


# ============================================================
# CONFIG
# ============================================================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo"
    chat_id_raw: str = "-1004485961768"
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_v35_full.db")
    cache_file: str = "ftw_v35_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V35 UPGRADED"
    min_history: int = 8
    recent_limit: int = 12
    max_history_window: int = 1000

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
# LOGGING
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(
        Path(CFG.log_dir) / "ftw_v35_full.log",
        maxBytes=1_000_000,
        backupCount=2
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v35")

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
    c = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            c += 1
        else:
            break
    return c

def family_for_size(size: str) -> List[int]:
    return [5, 6, 7, 8, 9] if size == "BIG" else [0, 1, 2, 3, 4]

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
                json.dump(self.history[-2000:], f)
        except Exception:
            pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data
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
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=WAL")
        return c

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

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"],
                p["type"],
                p.get("size", ""),
                p.get("num", 0),
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
            rows = c.execute(
                "SELECT issue, pred_type, size, num, details, stake FROM predictions WHERE status='PENDING'"
            ).fetchall()

        for row in rows:
            issue, pred_type, p_size, p_num, details_json, stake = row
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

            with self._conn() as c:
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                    (status, act["num"], actual_size, issue)
                )

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
            rows = c.execute(
                "SELECT issue, size, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return rows[::-1]

    def update_pattern_stats(self, pat_key: str, result: int):
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT hit_count, miss_count, last_seen, support, confidence FROM pattern_stats WHERE pattern_key=?",
                (pat_key,)
            ).fetchone()
            if row is None:
                hit, miss, last_seen, support, confidence = 0, 0, 0.0, 0.0, 0.0
            else:
                hit, miss, last_seen, support, confidence = row

            if result == 1:
                hit += 1
            else:
                miss += 1

            total = hit + miss
            if total:
                support = hit / total
                confidence = abs(support - 0.5) * 2.0
            else:
                support = 0.0
                confidence = 0.0

            c.execute("""
                INSERT INTO pattern_stats (pattern_key, hit_count, miss_count, last_seen, support, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    hit_count=excluded.hit_count,
                    miss_count=excluded.miss_count,
                    last_seen=excluded.last_seen,
                    support=excluded.support,
                    confidence=excluded.confidence
            """, (pat_key, hit, miss, now, support, confidence))

    def pattern_strength(self, pat_key: str) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT support, confidence, last_seen FROM pattern_stats WHERE pattern_key=?",
                (pat_key,)
            ).fetchone()
        if row is None:
            return 0.0

        support, confidence, last_seen = row
        age = max(0.0, 1.0 - ((time.time() - float(last_seen)) / 600.0))
        return (support * 0.7 + confidence * 0.3) * (0.5 + age * 0.5)

STORE = Storage(CFG.db_path)

# ============================================================
# REGIME / TREND / RECOVERY
# ============================================================
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
    return sum(arr) / len(arr)

def block_pattern(seq: List[int], window: int = 10) -> bool:
    if len(seq) < 4:
        return False
    arr = seq[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i+4]
        if a == b and c == d and a != c:
            return True
    return False

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8:
        return "cold"

    run = trailing_run(seq)
    alt = alternating_score(seq, 10)
    bp = block_pattern(seq, 10)
    ratio = ratio_big(seq, 10)

    if alt >= 0.65:
        return "zigzag"
    if bp:
        return "block"
    if run >= 5:
        return "trend"
    if run >= 3:
        return "trap"
    if abs(ratio - 0.5) <= 0.12:
        return "choppy"
    return "neutral"

def trend_phase(history: List[Dict[str, Any]]) -> str:
    if len(history) < 8:
        return "cold"

    seq = [x["size"] for x in history]
    run = trailing_run(seq)
    current_side = seq[-1]
    recent = seq[-10:]
    same_share = sum(1 for v in recent if v == current_side) / len(recent)

    # momentum decay check
    last3 = seq[-3:]
    prev3 = seq[-6:-3]
    if len(last3) == 3 and len(prev3) == 3:
        last_same = sum(1 for v in last3 if v == current_side)
        prev_same = sum(1 for v in prev3 if v == current_side)
        momentum_decay = last_same < prev_same
    else:
        momentum_decay = False

    if run >= 5 and same_share >= 0.75 and momentum_decay:
        return "exhausted"
    if run >= 4 and same_share >= 0.68:
        return "dominant"
    if run >= 2:
        return "building"
    return "cold"

def trend_exhaustion(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 8:
        return None

    seq = [x["size"] for x in history]
    phase = trend_phase(history)
    current_side = seq[-1]
    recent = seq[-12:]
    big_share = sum(1 for v in recent if v == 1) / len(recent)
    small_share = sum(1 for v in recent if v == 0) / len(recent)

    if phase == "exhausted":
        if current_side == 1 and big_share >= 0.75:
            return {"size": "SMALL", "logic": "BIG DOMINANCE EXHAUSTED", "confidence": 0.92, "stake": "1x"}
        if current_side == 0 and small_share >= 0.75:
            return {"size": "BIG", "logic": "SMALL DOMINANCE EXHAUSTED", "confidence": 0.92, "stake": "1x"}

    if phase == "dominant":
        if current_side == 1 and big_share >= 0.68:
            return {"size": "SMALL", "logic": "BIG FATIGUE", "confidence": 0.82, "stake": "1x"}
        if current_side == 0 and small_share >= 0.68:
            return {"size": "BIG", "logic": "SMALL FATIGUE", "confidence": 0.82, "stake": "1x"}

    return None

def reversal_signal(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    if len(seq) < 8:
        return None

    phase = trend_phase(history)
    if phase not in ("exhausted", "dominant"):
        return None

    current_side = seq[-1]
    opposite = "SMALL" if current_side == 1 else "BIG"

    recent = seq[-12:]
    current_share = sum(1 for v in recent if v == current_side) / len(recent)
    if current_share < 0.68:
        return None

    last3 = seq[-3:]
    prev3 = seq[-6:-3]
    if len(last3) == 3 and len(prev3) == 3:
        last_same = sum(1 for v in last3 if v == current_side)
        prev_same = sum(1 for v in prev3 if v == current_side)
        if last_same <= prev_same:
            return {
                "size": opposite,
                "logic": "REVERSAL PRESSURE: momentum fading",
                "confidence": 0.84,
                "stake": "1x"
            }

    return None

# ============================================================
# EXACT PATTERN CANDIDATES (3..15)
# ============================================================
def exact_pattern_candidates(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    candidates = []

    for window in range(3, 16):
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

        # strict gate: don't trust weak pattern
        if support < 0.02 and confidence < 15:
            continue

        key = f"{window}:{suffix}"
        score = confidence + (support * 100.0) + (window * 2.0)

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
    if best["confidence"] < 12 and best["support"] < 0.04:
        return None

    pred = best["pred"]
    confidence = clamp(best["confidence"] / 100.0, 0.55, 0.92)

    return {
        "size": pred,
        "logic": f"EXACT {best['window']}-SEQ PATTERN",
        "confidence": confidence,
        "stake": "2x" if confidence >= 0.82 else "1x" if confidence >= 0.68 else "0.5x"
    }

# ============================================================
# HYBRID STAT MODELS
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

def trend_signal(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    if len(seq) < 3:
        return {"size": "BIG" if random.random() > 0.5 else "SMALL", "confidence": 0.5}

    run = trailing_run(seq)
    last = seq[-1]

    if run >= 4:
        return {"size": "SMALL" if last == 1 else "BIG", "confidence": 0.8}

    if run >= 2 and len(seq) >= 8:
        return {"size": "SMALL" if last == 1 else "BIG", "confidence": 0.7}

    return {"size": "BIG" if result_bias(history) >= 0.5 else "SMALL", "confidence": 0.6}

def trend_shift_score(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    if len(seq) < 6:
        return {"dir": "NONE", "score": 0.0, "pred": "BIG" if random.random() > 0.5 else "SMALL"}

    recent_4 = seq[-4:]
    recent_6 = seq[-6:]
    recent_8 = seq[-8:]

    def big_share(arr):
        return sum(1 for v in arr if v == 1) / len(arr)

    s4 = big_share(recent_4)
    s6 = big_share(recent_6)
    s8 = big_share(recent_8)

    score = (s8 - s6) + (s6 - s4)

    if score > 0.18:
        return {"dir": "BIG_UP", "score": score, "pred": "BIG"}
    if score < -0.18:
        return {"dir": "SMALL_UP", "score": abs(score), "pred": "SMALL"}

    if s8 > 0.75 and seq[-1] == 1:
        return {"dir": "BLOCKED_BIG", "score": 0.25, "pred": "SMALL"}
    if s8 < 0.25 and seq[-1] == 0:
        return {"dir": "BLOCKED_SMALL", "score": 0.25, "pred": "BIG"}

    return {"dir": "NONE", "score": 0.0, "pred": "BIG" if s8 >= 0.5 else "SMALL"}

def hybrid_fallback(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    sizes = [x["size"] for x in history]
    if not sizes:
        return {"size": "BIG", "logic": "SEED", "confidence": 0.5, "stake": "0.5x"}

    recent = sizes[-12:]
    big_ratio = sum(recent) / len(recent)
    size = "BIG" if big_ratio >= 0.5 else "SMALL"
    conf = clamp(max(big_ratio, 1.0 - big_ratio), 0.55, 0.78)

    return {
        "size": size,
        "logic": "HYBRID FALLBACK",
        "confidence": conf,
        "stake": "1x" if conf >= 0.68 else "0.5x"
    }

def bias_guard(pred_size: str, history: List[Dict[str, Any]]) -> str:
    seq = [x["size"] for x in history[-20:]]
    if not seq:
        return pred_size

    balance = sum(seq) / len(seq)
    # 0 = small, 1 = big
    if balance > 0.68 and pred_size == "BIG":
        return "SMALL"
    if balance < 0.32 and pred_size == "SMALL":
        return "BIG"

    ex = trend_exhaustion(history)
    if ex and ex["size"] != pred_size:
        return ex["size"]

    return pred_size


# ============================================================
# NUMBER FAMILY ANALYSIS
# ============================================================
def is_same_family(num: int, size: str) -> bool:
    return num in family_for_size(size)

def number_repeat_score(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    nums = [x["num"] for x in history[-30:]]
    if not nums:
        return {"value": 0, "score": 0.0, "logic": "NONE"}

    counts = Counter(nums)
    most_common_num, most_count = counts.most_common(1)[0]
    repeat_ratio = most_count / len(nums)

    if repeat_ratio >= 0.2:
        return {"value": most_common_num, "score": repeat_ratio, "logic": "REPEATED_NUMBER"}
    return {"value": 0, "score": 0.0, "logic": "NONE"}

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

def number_transition_candidates(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    nums = [x["num"] for x in history]
    candidates = []

    for window in range(2, 8):
        if len(nums) < window + 1:
            continue

        suffix = tuple(nums[-window:])
        next_counts = defaultdict(int)

        for i in range(len(nums) - window):
            key = tuple(nums[i:i + window])
            if key == suffix:
                next_counts[nums[i + window]] += 1

        if not next_counts:
            continue

        best_num, best_freq = Counter(next_counts).most_common(1)[0]
        total_follow = sum(next_counts.values())
        support = total_follow / max(1, len(nums) - window)
        confidence = (best_freq / total_follow) * 100.0

        if support < 0.03 and confidence < 20:
            continue

        candidates.append({
            "window": window,
            "num": best_num,
            "support": support,
            "confidence": confidence,
            "score": confidence + (support * 100.0),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:10]

def choose_number_for_size(size: str, history: List[Dict[str, Any]]) -> int:
    valid = family_for_size(size)
    nums = [x["num"] for x in history if x.get("num") is not None]

    # exact transition candidates
    candidates = number_transition_candidates(history)
    if candidates:
        best = candidates[0]
        if best["num"] in valid:
            return best["num"]

    # repeated family number
    family_nums = [n for n in nums if n in valid]
    if family_nums:
        freq = Counter(family_nums)
        best_num, _ = freq.most_common(1)[0]
        if best_num in valid:
            return best_num

    repeat = number_repeat_score(history)
    if repeat["score"] >= 0.2 and repeat["value"] in valid:
        return repeat["value"]

    gap = number_gap_score(history)
    if gap["score"] > 0.0:
        num = gap["value"]
        if num in valid:
            return num

    if family_nums:
        even_count = sum(1 for n in family_nums if n % 2 == 0)
        odd_count = sum(1 for n in family_nums if n % 2 != 0)
        if even_count > odd_count:
            pool = [n for n in valid if n % 2 == 0]
        else:
            pool = [n for n in valid if n % 2 != 0]
        if pool:
            return random.choice(pool)

    if nums:
        last_num = nums[-1]
        if last_num in valid:
            return last_num

    return random.choice(valid)


# ============================================================
# ENSEMBLE PREDICTOR
# ============================================================
def weighted_ensemble(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]

    exact = choose_exact_prediction(history)
    hybrid = hybrid_fallback(history)
    trend = trend_signal(history)

    base_size = hybrid["size"]
    base_conf = hybrid["confidence"]

    if exact:
        base_size = exact["size"]
        base_conf = exact["confidence"]

    shift = trend_shift_score(history)
    if shift["dir"] != "NONE":
        if shift["pred"] == "BIG":
            base_size = "BIG"
            base_conf = max(base_conf, 0.72)
        else:
            base_size = "SMALL"
            base_conf = max(base_conf, 0.72)

    reversal = reversal_signal(history)
    if reversal:
        base_size = reversal["size"]
        base_conf = max(base_conf, reversal["confidence"])

    ex = trend_exhaustion(history)
    if ex:
        base_size = ex["size"]
        base_conf = max(base_conf, ex["confidence"])

    base_size = bias_guard(base_size, history)
    final_conf = clamp(base_conf, 0.55, 0.94)
    stake = "3x" if final_conf >= 0.85 else "2x" if final_conf >= 0.72 else "1x" if final_conf >= 0.60 else "0.5x"

    return {
        "size": base_size,
        "logic": exact["logic"] if exact else hybrid["logic"],
        "confidence": final_conf,
        "stake": stake
    }

def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {
            "issue": "1",
            "type": "SIZE",
            "size": "BIG" if n >= 5 else "SMALL",
            "num": n,
            "raw_conf": 50,
            "logic": "WARMING UP",
            "stake": "0.5x",
            "details": {"regime": "neutral"}
        }

    history = STATE.history[-2000:]
    ensemble = weighted_ensemble(history)
    final_size = ensemble["size"]
    final_num = choose_number_for_size(final_size, history)

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue,
        "type": "SIZE",
        "size": final_size,
        "num": final_num,
        "raw_conf": int(ensemble["confidence"] * 100),
        "logic": ensemble["logic"],
        "stake": ensemble["stake"],
        "details": {
            "regime": detect_regime([x["size"] for x in history]),
            "phase": trend_phase(history),
            "shift": trend_shift_score(history),
            "reversal": reversal_signal(history),
            "exhaustion": trend_exhaustion(history),
        }
    }


# ============================================================
# API PARSE & FETCH
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            d = payload["data"]
            if isinstance(d.get("list"), list):
                items = d["list"]
            elif isinstance(d, list):
                items = d
            else:
                items = []
        elif "result" in payload and isinstance(payload["result"], list):
            items = payload["result"]
        elif "history" in payload and isinstance(payload["history"], list):
            items = payload["history"]
        else:
            items = []
    else:
        items = []

    out: List[Dict[str, Any]] = []
    seen = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None:
            continue
        key = str(issue)
        if key in seen:
            continue
        seen.add(key)

        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if 0 <= num <= 9:
            out.append({"issue": key, "num": num, "size": 1 if num >= 5 else 0})

    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data(session):
    for url in CFG.api_urls():
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200:
                    continue
                payload = await response.json(content_type=None)
                parsed = parse_history_payload(payload)
                if parsed:
                    STATE.history = parsed[-CFG.max_history_window:]
                    STATE.save_cache()
                    return True
        except Exception as exc:
            log.warning("Fetch failed for %s: %s", url, exc)
    return False


# ============================================================
# FORMATTERS
# ============================================================
def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_lines = []
    for r in recent_rows[-CFG.recent_limit:]:
        issue, size, status = str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else ""
        if status:
            recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> {h(size)} → <b>{h(status)}</b>")
        else:
            recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> {h(size)}")

    recent_text = "\n".join(recent_lines) if recent_lines else "➜ no recent results"

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Engine</b>: {h(pred.get('logic', 'N/A'))}
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent</b>:
{recent_text}
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
                bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"),
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
            if await fetch_data(session):
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

                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(
                    max(
                        [normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None],
                        default=0
                    ) + 1
                )

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

# ============================================================
# WEB SERVER
# ============================================================
async def health_check(request):
    return web.Response(text="FTW V35 is Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

# ============================================================
# MAIN
# ============================================================
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
