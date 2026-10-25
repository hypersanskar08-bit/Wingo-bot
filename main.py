"""
V900 FULL FIXED WORKING CODE
================================
- No invalid HTML fallback
- Safe JSON-only fetching
- Handles 429 / bad status / non-JSON / empty response
- Purple supported as valid color
- Final single best prediction only
- Telegram bot ready
- Render-safe
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
from telegram.ext import Application


# ============================================================
# CONFIG
# ============================================================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAGIFSfpOd4b-Vl2QVHzhotq4uVmyrTuiNU")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v900_fixed_full.db")
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 12.0
    bot_name: str = "V900 FINAL"
    min_history: int = 50

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls if urls else []

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

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    fh = RotatingFileHandler(
        Path(CFG.log_dir) / "v900_fixed_full.log",
        maxBytes=1_000_000,
        backupCount=2,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v900")


def h(x: Any) -> str:
    return html.escape(str(x))


# ============================================================
# HELPERS
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


def normalize_color(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("red", "r", "1"):
            return "RED"
        if v in ("black", "blk", "b", "2"):
            return "BLACK"
        if v in ("purple", "purpl", "violet", "vio", "p", "3"):
            return "PURPLE"
        if v in ("green", "gr", "g", "4"):
            return "GREEN"
    if isinstance(value, int):
        if value == 1:
            return "RED"
        if value == 2:
            return "BLACK"
        if value == 3:
            return "PURPLE"
        if value == 4:
            return "GREEN"
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
# STATE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.loss_streak = 0
        self.win_streak = 0


STATE = BotState()
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
                CREATE TABLE IF NOT EXISTS transition_stats (
                    key TEXT PRIMARY KEY,
                    big_count INTEGER DEFAULT 0,
                    small_count INTEGER DEFAULT 0,
                    total_count INTEGER DEFAULT 0,
                    last_seen REAL DEFAULT 0
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
                (limit,),
            ).fetchall()
        return rows[::-1]

    def ensure_signal(self, name: str):
        with self._conn() as c:
            row = c.execute("SELECT weight FROM signal_weights WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO signal_weights (name, weight, hits, misses, updated_at) VALUES (?, ?, 0, 0, ?)",
                    (name, 1.0, time.time()),
                )

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
                (new_weight, hits, misses, time.time(), name),
            )

    def ensure_model_state(self, name: str):
        with self._conn() as c:
            row = c.execute("SELECT weight FROM model_state WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO model_state (name, weight, wins, losses, updated_at) VALUES (?, ?, 0, 0, ?)",
                    (name, 1.0, time.time()),
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
                (new_weight, wins, losses, time.time(), name),
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
                PATTERN_DB[key] = {"big": big_count, "total": total_count}

    def update_transition_memory(self, prev_sizes: List[int], actual_size: int):
        if len(prev_sizes) < 2:
            return
        prev = tuple(prev_sizes[-3:])
        key = f"trans:{json.dumps(list(prev))}"
        with self._conn() as c:
            row = c.execute(
                "SELECT big_count, small_count, total_count FROM transition_stats WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO transition_stats (key, big_count, small_count, total_count, last_seen) VALUES (?, ?, ?, ?, ?)",
                    (key, 0, 0, 0, time.time()),
                )
                row = (0, 0, 0)

            big_count, small_count, total_count = row
            if actual_size == 1:
                big_count += 1
            else:
                small_count += 1
            total_count += 1

            c.execute(
                "UPDATE transition_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE key=?",
                (big_count, small_count, total_count, time.time(), key),
            )

    def update_num_memory(self, actual_num: int, actual_size: int):
        with self._conn() as c:
            row = c.execute("SELECT big_count, small_count, total_count FROM num_stats WHERE num=?", (actual_num,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO num_stats (num, big_count, small_count, total_count) VALUES (?, ?, ?, ?)",
                    (actual_num, 0, 0, 0),
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
                "UPDATE num_stats SET big_count=?, small_count=?, total_count=? WHERE num=?",
                (big_count, small_count, total_count, actual_num),
            )

    def get_num_bias(self, pred_dir: int, pool: List[int]) -> Dict[int, float]:
        weights = {}
        for n in pool:
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM num_stats WHERE num=?", (n,)).fetchone()

            if row is None:
                p_big = 0.5
            else:
                big_count, small_count, total_count = row
                p_big = big_count / total_count if total_count > 0 else 0.5

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
# PATTERN / COLOR ANALYSIS
# ============================================================
def color_probability(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [x.get("color") for x in history if x.get("color")]
    if not colors:
        return {"RED": 0.33, "BLACK": 0.33, "PURPLE": 0.34}

    red = sum(1 for c in colors if c == "RED")
    black = sum(1 for c in colors if c == "BLACK")
    purple = sum(1 for c in colors if c == "PURPLE")
    total = len(colors)
    return {
        "RED": red / total if total else 0.33,
        "BLACK": black / total if total else 0.33,
        "PURPLE": purple / total if total else 0.34,
    }


def color_streak(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [x.get("color") for x in history if x.get("color")]
    if not colors:
        return {"RED": 0.33, "BLACK": 0.33, "PURPLE": 0.34}

    last = colors[-1]
    base = color_probability(history)
    out = {"RED": base["RED"], "BLACK": base["BLACK"], "PURPLE": base["PURPLE"]}

    if last == "RED":
        out["RED"] += 0.20
    elif last == "BLACK":
        out["BLACK"] += 0.20
    elif last == "PURPLE":
        out["PURPLE"] += 0.20

    total = sum(out.values())
    if total > 0:
        for key in list(out.keys()):
            out[key] = out[key] / total

    return out


def exact_suffix_memory(history: List[Dict[str, Any]]) -> float:
    if len(history) < 8:
        return 0.5
    sizes = [x["size"] for x in history]
    matches = []
    for window in [3, 5, 8, 12, 20, 30, 50, 100]:
        if len(sizes) < window:
            continue
        seq = tuple(sizes[-window:])
        key = f"{window}:{json.dumps(list(seq))}"
        if key in PATTERN_DB:
            total = PATTERN_DB[key]["total"]
            big = PATTERN_DB[key]["big"]
            if total > 0:
                matches.append((window, big / total, total))
    if not matches:
        return 0.5
    total_weight = sum(v for _, _, v in matches)
    if total_weight <= 0:
        return 0.5
    return sum(p * v for _, p, v in matches) / total_weight


def deep_feature_analysis(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {}

    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]
    regime = detect_regime(sizes)

    return {
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


def model_trend(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    run = trailing_run(sizes)
    if run >= 5:
        return 1.0 if sizes[-1] == 1 else 0.0
    if run >= 3:
        return 1.0 if sizes[-1] == 0 else 0.0
    return 0.5


def model_alternation(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    alt = alternating_score(sizes, 12)
    if alt >= 0.6:
        return 1.0 - sizes[-1]
    return 0.5


def model_block(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if block_pattern(sizes, 10):
        return 1.0 - sizes[-1]
    return 0.5


def model_volatility(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    vol = volatility(sizes, 20)
    if vol > 0.5:
        return 1.0 - sizes[-1]
    return 0.5


def model_bias(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    ratio = ratio_big(sizes, 20)
    if abs(ratio - 0.5) > 0.18:
        return 1.0 if ratio >= 0.5 else 0.0
    return 0.5


def side_prediction(history: List[Dict[str, Any]], loss_streak: int) -> Dict[str, Any]:
    if not history:
        return {"pred_dir": 1, "p_big": 0.5, "reason": "empty"}

    sizes = [x["size"] for x in history]
    regime = detect_regime(sizes)
    features = deep_feature_analysis(history)

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

    if loss_streak >= 1:
        score = 1.0 - score
        if regime in ["TRAP", "ALTERNATING", "BLOCK"]:
            score = 1.0 - sizes[-1]

    if loss_streak >= 2:
        score = 1.0 - majority_dir(sizes, 6)

    stale = stale_pattern_penalty(history, 12)
    if stale > 0:
        score = score * (1.0 - stale)
        score = 1.0 - score

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


def choose_best_outcome(history: List[Dict[str, Any]], loss_streak: int) -> Dict[str, Any]:
    side = side_prediction(history, loss_streak)
    p_big = side["p_big"]
    p_small = 1.0 - p_big
    color_probs = color_streak(history)
    red = color_probs.get("RED", 0.33)
    black = color_probs.get("BLACK", 0.33)
    purple = color_probs.get("PURPLE", 0.34)

    candidates = {
        "BIG": p_big * 0.85 + red * 0.15,
        "SMALL": p_small * 0.85 + black * 0.15,
        "RED": red * 0.85 + p_big * 0.15,
        "BLACK": black * 0.85 + p_small * 0.15,
        "PURPLE": purple * 0.90 + (0.10 * (1.0 - max(red, black, purple))),
    }

    winner = max(candidates, key=candidates.get)
    return {
        "winner": winner,
        "score": candidates[winner],
        "p_big": p_big,
        "p_small": p_small,
        "color_probs": color_probs,
    }


# ============================================================
# FINAL PREDICTION
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

    best = choose_best_outcome(history, STATE.loss_streak)
    final_winner = best["winner"]

    if final_winner in ("BIG", "SMALL"):
        final_size = final_winner
        final_num = choose_number(1 if final_size == "BIG" else 0, history, STATE.loss_streak)
    else:
        final_size = "BIG" if p_big >= 0.5 else "SMALL"
        final_num = choose_number(1 if final_size == "BIG" else 0, history, STATE.loss_streak)

    issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

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

    return {
        "issue": issue,
        "size": final_size,
        "num": final_num,
        "p_big": round(clamp(p_big, 0.05, 0.95), 4),
        "best_outcome": final_winner,
        "best_score": best["score"],
        "confidence": confidence,
        "raw_conf": confidence,
        "consensus": max(p_big, 1 - p_big),
        "mode": "SUPER-RECOVERY" if STATE.loss_streak >= 2 else ("RECOVERY" if STATE.loss_streak >= 1 else "NORMAL"),
        "regime": regime,
        "logic": f"Final winner: {final_winner}",
        "sigs": [{"name": "Adaptive Final", "dir": 1 if final_size == "BIG" else 0}],
        "stake": stake,
        "details": {
            "strength": strength,
            "loss_streak": STATE.loss_streak,
            "regime": regime,
            "color_probs": best["color_probs"],
            "best_outcome": final_winner,
        },
    }


def learn_from_resolution(res: dict):
    if res["win"]:
        STATE.loss_streak = 0
        STATE.win_streak += 1
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    for sig in res.get("sigs", []):
        name = sig.get("name")
        if name:
            STORE.update_signal_weight(name, bool(res["win"]))

    if STATE.history:
        actual_size = 1 if res["actual_size"] == "BIG" else 0
        STORE.update_pattern_memory([x["size"] for x in STATE.history], actual_size)
        STORE.update_transition_memory([x["size"] for x in STATE.history], actual_size)
        STORE.update_num_memory(int(res["actual_num"]), actual_size)

    model_names = ["Exact", "Trend", "Alternation", "Block", "Volatility", "Bias"]
    for name in model_names:
        STORE.ensure_model_state(name)
        STORE.update_model_state(name, bool(res["win"]))


# ============================================================
# PARSE PAYLOAD
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

            color = normalize_color(
                item.get("color")
                or item.get("colour")
                or item.get("color_name")
                or item.get("colour_name")
                or item.get("c")
            )

            out.append({
                "issue": str(issue),
                "num": num,
                "size": 1 if num >= 5 else 0,
                "color": color,
            })
        except Exception:
            continue

    out.sort(key=lambda x: int(x["issue"]))
    return out


# ============================================================
# FETCH DATA
# ============================================================
async def fetch_data(session):
    urls = CFG.api_urls()
    if not urls:
        log.warning("No API_URLS configured. Waiting without fetch.")
        return False

    last_good = [x for x in STATE.history]

    for url in urls:
        try:
            for retry in range(3):
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=CFG.request_timeout),
                    ) as r:
                        body = await r.text()

                        if r.status == 429:
                            wait = (retry + 1) * 2
                            log.warning("429 from %s. Retrying in %ss", url, wait)
                            await asyncio.sleep(wait)
                            continue

                        if r.status != 200:
                            log.warning("HTTP %s from %s", r.status, url)
                            break

                        ctype = (r.headers.get("Content-Type") or "").lower()
                        if "json" not in ctype:
                            log.warning("Non-JSON response from %s -> %s | body=%r", url, ctype, body[:200])
                            break

                        try:
                            payload = json.loads(body)
                        except Exception as e:
                            log.warning("JSON parse failed for %s: %s | body=%r", url, e, body[:200])
                            break

                        parsed = parse_history_payload(payload)
                        if parsed:
                            STATE.history = parsed[-1000:]
                            log.info("Fetched %d records from %s", len(parsed), url)
                            return True

                        log.warning("Empty payload from %s", url)
                        break

                except Exception as e:
                    log.warning("Retry %s failed for %s: %s", retry + 1, url, e)
                    await asyncio.sleep((retry + 1) * 2)
                    continue

        except Exception as e:
            log.warning("Fetch exception on %s: %s", url, e)

    if last_good:
        STATE.history = last_good
        log.warning("Using last-known-good history because all endpoints failed.")
        return True

    log.warning("No endpoint succeeded and no last-good history exists.")
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
                parse_mode="HTML",
            )
            return True
        except Exception as e:
            log.exception("Telegram send failed attempt %s/%s: %s", attempt + 1, retries, e)
            await asyncio.sleep(1)
    return False


# ============================================================
# WORKER LOOP
# ============================================================
async def worker_loop(app, session):
    last_processed_issue = None

    while True:
        try:
            if not CFG.api_urls():
                log.warning("No API URLs configured. Worker sleeping.")
                await asyncio.sleep(CFG.fetch_every)
                continue

            if await fetch_data(session):
                hist_dict: Dict[str, Dict[str, Any]] = {}
                for x in STATE.history:
                    hist_dict[str(x["issue"])] = x

                resolved = STORE.resolve_pending(hist_dict)

                for res in resolved:
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

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
            log.exception("Worker loop error: %s", e)
            await asyncio.sleep(1)


# ============================================================
# MAIN
# ============================================================
async def main():
    CFG.source_api_urls = CFG.source_api_urls.strip()
    CFG.validate()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()

    async with app:
        await app.start()
        ok = await verify_telegram(app)
        if not ok:
            log.error("Bot validation failed. Check TG_TOKEN and TG_CHAT.")
            return

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
