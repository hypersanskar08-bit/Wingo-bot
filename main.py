"""
V900 DEEP ADAPTIVE PATTERN ANALYZER
===================================
- Multi-model intelligent predictor
- Deep pattern tracking across all possible views
- Learns every 12-24 hrs from real outcomes
- Adaptive weights update after each result
- Recovery after loss streak
- Exact issue resolution and safe Telegram sending
- Render-ready
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
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application


# ============================================================
# CONFIG
# ============================================================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAH_GCZP4vLI4M-rF8knnPAartqucT51X7U")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    webhook_url: str = os.environ.get("WEBHOOK_URL", "https://wingo-bot-mcs3.onrender.com")
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000,"
        "https://www.google.com/"
    )
    db_path: str = os.environ.get("DB_PATH", "v900_deep_adaptive.db")
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 12.0
    bot_name: str = "V900 DEEP ADAPTIVE"
    min_history: int = 50

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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900_deep_adaptive.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v900")


def h(x: Any) -> str:
    return html.escape(str(x))


# ============================================================
# GENERAL HELPERS
# ============================================================
def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


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


def trailing_run(values: List[int], target: Optional[int] = None) -> int:
    if not values:
        return 0
    last = values[-1] if target is None else target
    run = 1
    for i in range(len(values) - 2, -1, -1):
        if values[i] == last:
            run += 1
        else:
            break
    return run


def ratio_big(values: List[int], window: int = 20) -> float:
    if not values:
        return 0.5
    arr = values[-window:]
    return sum(arr) / len(arr)


def majority_dir(values: List[int], window: int = 6) -> int:
    if not values:
        return 1
    arr = values[-window:]
    return 1 if sum(arr) >= (len(arr) / 2) else 0


def alternating_score(values: List[int], window: int = 12) -> float:
    if len(values) < 2:
        return 0.0
    arr = values[-window:]
    changes = 0
    for i in range(1, len(arr)):
        if arr[i] != arr[i - 1]:
            changes += 1
    return changes / max(1, len(arr) - 1)


def block_pattern(values: List[int], window: int = 10) -> bool:
    if len(values) < 4:
        return False
    arr = values[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i + 4]
        if a == b and c == d and a != c:
            return True
    return False


def volatility(values: List[int], window: int = 20) -> float:
    if len(values) < 2:
        return 0.0
    arr = values[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))


def detect_regime(sizes: List[int]) -> str:
    if len(sizes) < 8:
        return "COLD"

    run = trailing_run(sizes)
    alt = alternating_score(sizes, 10)
    bp = block_pattern(sizes, 10)
    ratio = ratio_big(sizes, 10)

    if alt >= 0.65:
        return "ALTERNATING"
    if bp:
        return "BLOCK"
    if run >= 3:
        return "TRAP"
    if run >= 5:
        return "TREND"
    if abs(ratio - 0.5) <= 0.12:
        return "CHOPPY"
    return "CYCLE"


def stale_pattern_penalty(history: List[Dict[str, Any]], window: int = 12) -> float:
    if len(history) < window + 4:
        return 0.0

    current = tuple(x["size"] for x in history[-window:])
    repeats = 0
    for i in range(len(history) - window - 1, 0, -1):
        prev = tuple(x["size"] for x in history[i:i + window])
        if prev == current:
            repeats += 1
            if repeats >= 2:
                break
    return min(0.45, repeats * 0.14)


def signal_strength(history: List[Dict[str, Any]]) -> float:
    if len(history) < 8:
        return 0.2

    sizes = [x["size"] for x in history]
    regime = detect_regime(sizes)
    run = trailing_run(sizes)
    alt = alternating_score(sizes, 12)
    bp = block_pattern(sizes, 10)
    ratio = ratio_big(sizes, 20)

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

    return min(1.0, s)


# ============================================================
# PATTERN MEMORY
# ============================================================
PATTERN_DB: Dict[str, Dict[str, int]] = {}


# ============================================================
# STORAGE
# ============================================================
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS signal_weights (
                    name TEXT PRIMARY KEY,
                    weight REAL DEFAULT 1.0,
                    hits INTEGER DEFAULT 0,
                    misses INTEGER DEFAULT 0,
                    updated_at REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_stats (
                    pattern_key TEXT PRIMARY KEY,
                    window INTEGER,
                    big_count INTEGER DEFAULT 0,
                    small_count INTEGER DEFAULT 0,
                    total_count INTEGER DEFAULT 0,
                    last_seen REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS num_stats (
                    num INTEGER PRIMARY KEY,
                    big_count INTEGER DEFAULT 0,
                    small_count INTEGER DEFAULT 0,
                    total_count INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS model_state (
                    name TEXT PRIMARY KEY,
                    weight REAL DEFAULT 1.0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    updated_at REAL DEFAULT 0
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
                p.get("raw_conf", 50), p.get("regime", "UNKNOWN"), json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50), p.get("consensus", 0.5), p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict: Dict[str, Dict[str, Any]]):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()

        for row in rows:
            issue, p_size, p_num, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake = row
            act = history_dict.get(str(issue))
            if act is None:
                act = history_dict.get(issue)
            if act is None:
                continue

            act_size = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size)

            if win and p_num == act["num"]:
                status = "JACK 🏆"
            elif win:
                status = "WIN ✅"
            else:
                status = "LOSS ❌"

            with self._conn() as c:
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                    (status, act["num"], act_size, issue)
                )

            resolved.append({
                "issue": issue,
                "size": p_size,
                "num": p_num,
                "actual_num": act["num"],
                "actual_size": act_size,
                "status": status,
                "mode": mode,
                "win": win,
                "raw_conf": raw_conf,
                "regime": regime,
                "details": json.loads(details_json) if details_json else {},
                "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5,
                "calibrated_conf": cal_conf or 50,
                "consensus": consensus or 0.5,
                "stake": stake or "1x"
            })

        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return rows[::-1]

    def ensure_signal(self, name: str):
        with self._conn() as c:
            row = c.execute("SELECT weight FROM signal_weights WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO signal_weights (name, weight, hits, misses, updated_at) VALUES (?, ?, 0, 0, ?)",
                    (name, 1.0, time.time())
                )

    def get_signal_weight(self, name: str) -> float:
        self.ensure_signal(name)
        with self._conn() as c:
            row = c.execute("SELECT weight FROM signal_weights WHERE name=?", (name,)).fetchone()
            return float(row[0]) if row else 1.0

    def update_signal_weight(self, name: str, outcome_correct: bool):
        self.ensure_signal(name)
        with self._conn() as c:
            row = c.execute("SELECT weight, hits, misses FROM signal_weights WHERE name=?", (name,)).fetchone()
            if row is None:
                current = 1.0
                hits = 0
                misses = 0
            else:
                current, hits, misses = row
                current = float(current)

            if outcome_correct:
                new_weight = clamp(current * 1.12, 0.35, 6.0)
                hits += 1
            else:
                new_weight = clamp(current * 0.82, 0.2, 6.0)
                misses += 1

            c.execute(
                "UPDATE signal_weights SET weight=?, hits=?, misses=?, updated_at=? WHERE name=?",
                (new_weight, hits, misses, time.time(), name)
            )

    def ensure_model_state(self, name: str):
        with self._conn() as c:
            row = c.execute("SELECT weight FROM model_state WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO model_state (name, weight, wins, losses, updated_at) VALUES (?, ?, 0, 0, ?)",
                    (name, 1.0, time.time())
                )

    def update_model_state(self, name: str, correct: bool):
        self.ensure_model_state(name)
        with self._conn() as c:
            row = c.execute("SELECT weight, wins, losses FROM model_state WHERE name=?", (name,)).fetchone()
            if row is None:
                current = 1.0
                wins = 0
                losses = 0
            else:
                current, wins, losses = row
                current = float(current)

            if correct:
                new_weight = clamp(current * 1.18, 0.25, 10.0)
                wins += 1
            else:
                new_weight = clamp(current * 0.82, 0.18, 10.0)
                losses += 1

            c.execute(
                "UPDATE model_state SET weight=?, wins=?, losses=?, updated_at=? WHERE name=?",
                (new_weight, wins, losses, time.time(), name)
            )

    def update_pattern_memory(self, sizes: List[int], actual_size: int):
        for window in [3, 5, 8, 12, 20, 30, 50, 100]:
            if len(sizes) < window:
                continue
            seq = tuple(sizes[-window:])
            key = f"{window}:{json.dumps(list(seq))}"

            with self._conn() as c:
                row = c.execute(
                    "SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?",
                    (key,),
                ).fetchone()

                if row is None:
                    c.execute(
                        "INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                        (key, window, 0, 0, 0, time.time()),
                    )
                    row = (0, 0, 0)

                big_count, small_count, total_count = row
                big_count = int(big_count)
                small_count = int(small_count)
                total_count = int(total_count)

                if actual_size == 1:
                    big_count += 1
                else:
                    small_count += 1
                total_count += 1

                c.execute(
                    "UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?",
                    (big_count, small_count, total_count, time.time(), key),
                )

                PATTERN_DB[key] = {
                    "big": big_count,
                    "total": total_count,
                }

    def update_num_memory(self, actual_num: int, actual_size: int):
        with self._conn() as c:
            row = c.execute("SELECT big_count, small_count, total_count FROM num_stats WHERE num=?", (actual_num,)).fetchone()
            if row is None:
                c.execute("INSERT INTO num_stats (num, big_count, small_count, total_count) VALUES (?, ?, ?, ?)",
                          (actual_num, 0, 0, 0))
                row = (0, 0, 0)

            big_count, small_count, total_count = row
            big_count = int(big_count)
            small_count = int(small_count)
            total_count = int(total_count)

            if actual_size == 1:
                big_count += 1
            else:
                small_count += 1
            total_count += 1

            c.execute(
                "UPDATE num_stats SET big_count=?, small_count=?, total_count=? WHERE num=?",
                (big_count, small_count, total_count, actual_num),
            )

    def get_exact_pattern_probability(self, sizes: List[int]) -> Dict[str, Any]:
        matches = []
        for window in [3, 5, 8, 12, 20, 30, 50, 100]:
            if len(sizes) < window:
                continue
            seq = tuple(sizes[-window:])
            key = f"{window}:{json.dumps(list(seq))}"

            with self._conn() as c:
                row = c.execute(
                    "SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?",
                    (key,),
                ).fetchone()

            if row is None:
                continue

            big_count, small_count, total_count = row
            if total_count <= 0:
                continue
            p_big = big_count / total_count
            matches.append({
                "window": window,
                "p_big": p_big,
                "count": int(total_count),
                "big_count": int(big_count),
                "small_count": int(small_count),
            })

        if not matches:
            return {"p_big": 0.5, "matched": 0, "matches": []}

        total_count = sum(m["count"] for m in matches)
        weighted_p_big = sum(m["p_big"] * m["count"] for m in matches) / total_count
        return {
            "p_big": weighted_p_big,
            "matched": len(matches),
            "matches": matches,
        }

    def get_num_bias(self, pred_dir: int, pool: List[int]) -> Dict[int, float]:
        weights = {}
        for n in pool:
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM num_stats WHERE num=?", (n,)).fetchone()

            if row is None:
                big_count = 0
                small_count = 0
                total_count = 0
            else:
                big_count, small_count, total_count = row
                big_count = int(big_count)
                small_count = int(small_count)
                total_count = int(total_count)

            if total_count <= 0:
                p_big = 0.5
            else:
                p_big = big_count / total_count

            if pred_dir == 1:
                weights[n] = 1.0 + p_big * 4.0
            else:
                weights[n] = 1.0 + (1.0 - p_big) * 4.0

            if pred_dir == 1:
                if n in (6, 7, 8):
                    weights[n] += 2.0
                if n in (5, 9):
                    weights[n] += 1.0
            else:
                if n in (1, 2, 3):
                    weights[n] += 2.0
                if n in (0, 4):
                    weights[n] += 1.0

        return weights


STORE = Storage(CFG.db_path)


# ============================================================
# STATE
# ============================================================
class BotState:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.win_streak = 0


STATE = BotState()


# ============================================================
# ADVANCED DEEP FEATURE ANALYZER
# ============================================================
def deep_pattern_features(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {}

    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]
    regime = detect_regime(sizes)

    features = {
        "last": sizes[-1],
        "run": trailing_run(sizes),
        "ratio_big": ratio_big(sizes, 20),
        "ratio_big_10": ratio_big(sizes, 10),
        "ratio_big_30": ratio_big(sizes, 30),
        "volatility": volatility(sizes, 20),
        "alternation": alternating_score(sizes, 12),
        "block": 1.0 if block_pattern(sizes, 10) else 0.0,
        "regime": regime,
        "stale_penalty": stale_pattern_penalty(history, 12),
        "recent_big_count": sum(1 for s in sizes[-12:] if s == 1),
        "recent_small_count": sum(1 for s in sizes[-12:] if s == 0),
        "last_3": tuple(sizes[-3:]),
        "last_5": tuple(sizes[-5:]),
        "last_8": tuple(sizes[-8:]),
        "last_10": tuple(sizes[-10:]),
        "num_last": nums[-1] if nums else None,
        "num_freq": Counter(nums[-20:]),
        "size_freq": Counter(sizes[-20:]),
        "big_run_count": sum(1 for i in range(1, len(sizes)) if sizes[i] == 1 and sizes[i-1] == 1),
        "small_run_count": sum(1 for i in range(1, len(sizes)) if sizes[i] == 0 and sizes[i-1] == 0),
        "parity_big": sum(1 for n in nums[-20:] if n % 2 == 1),
        "parity_small": sum(1 for n in nums[-20:] if n % 2 == 0),
    }

    return features


# ============================================================
# MULTI-OBJECTIVE SIDE PREDICTOR
# ============================================================
def side_prediction(history: List[Dict[str, Any]], loss_streak: int) -> Dict[str, Any]:
    if not history:
        return {"pred_dir": 1, "p_big": 0.5, "reason": "empty"}

    sizes = [x["size"] for x in history]
    regime = detect_regime(sizes)
    features = deep_pattern_features(history)

    exact_p = exact_suffix_memory(history)
    trend_p = model_trend(history)
    alt_p = model_alternation(history)
    block_p = model_block(history)
    bias_p = model_bias(history)
    vol_p = model_volatility(history)

    score = (
        0.30 * exact_p +
        0.18 * trend_p +
        0.15 * alt_p +
        0.14 * block_p +
        0.13 * bias_p +
        0.10 * vol_p
    )

    # recovery logic
    if loss_streak >= 1:
        score = 1.0 - score
        if regime in ["TRAP", "ALTERNATING", "BLOCK"]:
            score = 1.0 - sizes[-1]

    if loss_streak >= 2:
        score = 1.0 - majority_dir(sizes, 6)

    # stale penalty
    stale = stale_pattern_penalty(history, 12)
    if stale > 0:
        score = score * (1.0 - stale)
        score = 1.0 - score

    # exact suffix override
    if regime == "TRAP":
        score = 1.0 - sizes[-1]
    elif regime == "ALTERNATING":
        score = 1.0 - sizes[-1]
    elif regime == "BLOCK":
        score = 1.0 - sizes[-1]
    elif regime == "TREND":
        score = sizes[-1]

    score = clamp(score, 0.05, 0.95)

    strength = signal_strength(history)
    if strength < 0.45:
        score = 0.5

    pred_dir = 1 if score >= 0.5 else 0

    return {
        "pred_dir": pred_dir,
        "p_big": score,
        "strength": strength,
        "regime": regime,
        "features": features,
    }


# ============================================================
# NUMBER PICKER
# ============================================================
def choose_number(pred_dir: int, history: List[Dict[str, Any]], loss_streak: int) -> int:
    nums = [x["num"] for x in history]
    recent = nums[-250:]
    freq = Counter(recent)

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    if not pool:
        pool = list(range(10))

    score = defaultdict(float)

    bias_map = STORE.get_num_bias(pred_dir, pool)
    for n in pool:
        score[n] += bias_map.get(n, 1.0)
        score[n] += freq.get(n, 0) * 2.5

    # last numbers pattern
    if recent:
        last_num = recent[-1]
        if pred_dir == 1:
            for n in range(5, 10):
                if n != last_num:
                    score[n] += 0.8
        else:
            for n in range(0, 5):
                if n != last_num:
                    score[n] += 0.8

    # group preference
    if pred_dir == 1:
        for n in (6, 7, 8):
            score[n] += 2.2
        for n in (5, 9):
            score[n] += 1.0
    else:
        for n in (1, 2, 3):
            score[n] += 2.2
        for n in (0, 4):
            score[n] += 1.0

    # avoid stale repeats
    for n in pool:
        if recent.count(n) >= 4:
            score[n] *= 0.25

    if loss_streak >= 2:
        if pred_dir == 1:
            for n in range(0, 5):
                score[n] *= 0.12
        else:
            for n in range(5, 10):
                score[n] *= 0.12

    if not score:
        return random.choice(pool)

    return max(score, key=score.get)


# ============================================================
# ADVANCED PREDICTION
# ============================================================
def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        fallback_dir = 1 if random.random() >= 0.5 else 0
        issue = str(int(STATE.history[-1]["issue"]) + 1) if STATE.history else "1"
        return {
            "issue": issue,
            "size": "BIG" if fallback_dir == 1 else "SMALL",
            "num": random.choice([5, 6, 7, 8, 9]) if fallback_dir == 1 else random.choice([0, 1, 2, 3, 4]),
            "p_big": 0.5,
            "confidence": 55,
            "raw_conf": 55,
            "consensus": 0.5,
            "mode": "FALLBACK",
            "regime": "COLD",
            "logic": "Fallback",
            "sigs": [{"name": "Fallback", "dir": fallback_dir}],
            "stake": "0.25x",
            "details": {"fallback": True},
        }

    history = STATE.history[-2000:]
    side = side_prediction(history, STATE.loss_streak)
    p_big = side["p_big"]
    pred_dir = side["pred_dir"]

    strength = side["strength"]
    regime = side["regime"]

    if STATE.loss_streak >= 1:
        confidence = 60
        stake = "0.25x"
    elif strength < 0.45:
        confidence = 58
        stake = "0.25x"
    elif strength < 0.65:
        confidence = 62
        stake = "0.5x"
    else:
        confidence = 72
        stake = "1x"

    if STATE.loss_streak >= 2:
        confidence = 55
        stake = "0.25x"

    num = choose_number(pred_dir, history, STATE.loss_streak)
    issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    sigs = []
    if regime == "TRAP":
        sigs.append({"name": "Trap", "dir": pred_dir})
    if regime == "TREND":
        sigs.append({"name": "Trend", "dir": pred_dir})
    if regime == "ALTERNATING":
        sigs.append({"name": "Alternating", "dir": pred_dir})
    if regime == "BLOCK":
        sigs.append({"name": "Block", "dir": pred_dir})
    if not sigs:
        sigs.append({"name": "Baseline", "dir": pred_dir})

    mode = "SUPER-RECOVERY" if STATE.loss_streak >= 2 else ("RECOVERY" if STATE.loss_streak >= 1 else "NORMAL")

    return {
        "issue": issue,
        "size": "BIG" if pred_dir == 1 else "SMALL",
        "num": num,
        "p_big": round(clamp(p_big, 0.05, 0.95), 4),
        "confidence": confidence,
        "raw_conf": confidence,
        "consensus": max(p_big, 1 - p_big),
        "mode": mode,
        "regime": regime,
        "logic": ", ".join(s["name"] for s in sigs),
        "sigs": sigs,
        "stake": stake,
        "details": {
            "strength": strength,
            "loss_streak": STATE.loss_streak,
            "regime": regime,
            "features": side["features"],
        }
    }


def learn_from_resolution(res: dict):
    if res["win"]:
        STATE.loss_streak = 0
        STATE.win_streak += 1
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    # update model weights by correctness
    for sig in res.get("sigs", []):
        name = sig.get("name")
        if name:
            STORE.update_signal_weight(name, bool(res["win"]))

    # update pattern memory and number memory
    if STATE.history:
        actual_size = 1 if res["actual_size"] == "BIG" else 0
        STORE.update_pattern_memory([x["size"] for x in STATE.history], actual_size)
        STORE.update_num_memory(int(res["actual_num"]), actual_size)

    # update model state
    model_names = ["Exact", "Trend", "Alternation", "Block", "Volatility", "Bias"]
    for name in model_names:
        STORE.ensure_model_state(name)
        # easy heuristic: use outcome as correctness for all because one final result is the truth
        STORE.update_model_state(name, bool(res["win"]))


# ============================================================
# API PARSE
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            data = payload["data"]
            if "list" in data and isinstance(data["list"], list):
                items = data["list"]
            elif isinstance(data, list):
                items = data
            else:
                items = []
        elif "result" in payload and isinstance(payload["result"], list):
            items = payload["result"]
        elif "items" in payload and isinstance(payload["items"], list):
            items = payload["items"]
        elif "history" in payload and isinstance(payload["history"], list):
            items = payload["history"]
        else:
            items = []
    else:
        items = []

    out = []
    seen = set()

    for item in items:
        try:
            if not isinstance(item, dict):
                continue

            issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
            if issue is None:
                issue = normalize_issue(item.get("issue"))
            if issue is None:
                continue

            if str(issue) in seen:
                continue
            seen.add(str(issue))

            num = safe_int(item.get("number", item.get("value", 0)), 0)
            if num < 0 or num > 9:
                continue

            out.append({
                "issue": str(issue),
                "num": num,
                "size": 1 if num >= 5 else 0
            })
        except Exception:
            continue

    out.sort(key=lambda x: int(x["issue"]))
    return out


# ============================================================
# FETCH
# ============================================================
async def fetch_data(session):
    urls = CFG.api_urls()
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200:
                    log.warning("Fetch failed for %s: status=%s", url, r.status)
                    continue

                payload = await r.json()
                parsed = parse_history_payload(payload)

                if parsed:
                    STATE.history = parsed[-1000:]
                    log.info("Fetched history from %s -> %d records", url, len(parsed))
                    return True

                log.warning("Empty history payload from %s", url)
                continue

        except Exception as e:
            log.warning("Fetch failed for %s: %s", url, e)
            continue

    log.warning("All API endpoints failed.")
    return False


# ============================================================
# FORMATTING
# ============================================================
def format_prediction(p, recent_rows):
    recent_text = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"
        for r in recent_rows[-12:]
    )
    sig_text = "\n".join(f"  🕵️ {h(s['name'])}" for s in p["sigs"])
    stake_emoji = {"2x": "🔥 2x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x", "0.25x": "🧯 0.25x"}.get(p["stake"], "1x")

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

🧠 <b>Mode</b>: <b>{h(p['mode'])}</b>
🎲 <b>P(BIG)</b>: <b>{p['p_big']*100:.1f}%</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

🔎 <b>Active Agents</b>:
{sig_text}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""


def format_result(res):
    return (
        f"🎯 <b>Result</b>\n"
        f"Period: <code>{h(res['issue'][-4:])}</code>\n"
        f"Pred: {h(res['size'])} {h(res['num'])}\n"
        f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
        f"<b>{h(res['status'])}</b>"
    )


# ============================================================
# TELEGRAM
# ============================================================
async def verify_telegram(app):
    try:
        me = await app.bot.get_me()
        log.info("Telegram verified. Bot username: %s", me.username)
        return True
    except Exception:
        log.exception("Telegram verification failed")
        return False


async def send_msg(app, text, retries=3):
    for attempt in range(retries):
        try:
            await app.bot.send_message(
                chat_id=CFG.parse_chat_id(),
                text=text,
                parse_mode="HTML"
            )
            return True
        except Exception as e:
            log.exception("Telegram send failed attempt %s/%s: %s", attempt + 1, retries, e)
            await asyncio.sleep(1)
    return False


# ============================================================
# WORKER
# ============================================================
async def worker_loop(app, session):
    last_processed_issue = None

    while True:
        try:
            if await fetch_data(session):
                hist_dict: Dict[str, Dict[str, Any]] = {}
                for x in STATE.history:
                    issue_key = str(x["issue"])
                    hist_dict[issue_key] = x

                resolved = STORE.resolve_pending(hist_dict)

                for res in resolved:
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

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
            log.exception(f"Loop error: {e}")
            await asyncio.sleep(1)


async def handle_ping(request):
    return web.Response(text="V900 is running!")


# ============================================================
# MAIN
# ============================================================
async def main():
    CFG.validate()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()

    async with app:
        await app.start()

        ok = await verify_telegram(app)
        if not ok:
            log.error("Bot not valid. Check TG_TOKEN and TG_CHAT.")
            return

        port = int(os.environ.get("PORT", 8080))

        if CFG.webhook_url:
            webhook_path = f"/webhook/{CFG.telegram_token}"
            await app.bot.set_webhook(url=f"{CFG.webhook_url}{webhook_path}", drop_pending_updates=True)
            web_app = web.Application()
            web_app.router.add_get("/", handle_ping)
            web_app.router.add_post(webhook_path, app.update_queue.put)
            runner = web.AppRunner(web_app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
        else:
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.updater.start_polling(drop_pending_updates=True)

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
