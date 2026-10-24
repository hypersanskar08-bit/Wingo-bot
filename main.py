"""
V900 FINAL SMART EDGE PREDICTOR
==============================
- Reads last 1000 results from API
- Learns exact multi-window patterns
- Tracks signal quality over time
- Uses soft safety when evidence is weak
- Always returns a prediction, but lowers stake on weak evidence
- Telegram + SQLite integration
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


# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAH_GCZP4vLI4M-rF8knnPAartqucT51X7U")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    webhook_url: str = os.environ.get("WEBHOOK_URL", "https://wingo-bot-mcs3.onrender.com")
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "v900_smart_edge.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V900 SMART EDGE"
    min_history: int = 50

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

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900_smart_edge.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v900")


def h(x: Any) -> str:
    return html.escape(str(x))


# =========================
# HELPERS
# =========================
def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


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
    return min(0.35, repeats * 0.12)


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

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()

        for row in rows:
            (issue, p_size, p_num, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake) = row
            if issue not in history_dict:
                continue

            act = history_dict[issue]
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
                    (status, act["num"], act_size, issue),
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
                "stake": stake or "1x",
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
                (new_weight, hits, misses, time.time(), name),
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


# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.win_streak = 0


STATE = BotState()


# =========================
# PATTERN / SIGNAL LOGIC
# =========================
def candidate_signals(history: List[Dict[str, Any]]) -> List[Tuple[str, int, float, str]]:
    if not history:
        return []

    sizes = [x["size"] for x in history]
    regime = detect_regime(sizes)
    run = trailing_run(sizes)
    alt = alternating_score(sizes, 12)
    bp = block_pattern(sizes, 10)
    ratio = ratio_big(sizes, 20)
    vol = volatility(sizes, 20)

    sigs = []
    if run >= 3:
        sigs.append(("Trap", 1 - sizes[-1], 1.9, "Streak probably traps and reverses."))
    if run >= 5:
        sigs.append(("Trend", sizes[-1], 1.7, "Trend continuation is alive."))
    if alt >= 0.6:
        sigs.append(("Alternating", 1 - sizes[-1], 1.8, "Alternation points to flip."))
    if bp:
        sigs.append(("Block", 1 - sizes[-1], 1.6, "Block break pattern is active."))
    if abs(ratio - 0.5) > 0.18:
        sigs.append(("Bias", 1 if ratio >= 0.5 else 0, 1.4, "Side imbalance is present."))
    if vol > 0.5:
        sigs.append(("Volatile", 1 - sizes[-1], 1.05, "Volatility increase makes reversal more likely."))
    if regime in ("TRAP", "BLOCK", "ALTERNATING"):
        sigs.append(("Regime-Flip", 1 - sizes[-1], 1.35, "Regime is break-sensitive."))

    return sigs


def choose_number(pred_dir: int, history: List[Dict[str, Any]], loss_streak: int) -> int:
    nums = [x["num"] for x in history]
    recent = nums[-200:]
    freq = Counter(recent)

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    if not pool:
        pool = list(range(10))

    score = defaultdict(float)

    num_bias = STORE.get_num_bias(pred_dir, pool)
    for n in pool:
        score[n] += num_bias.get(n, 1.0)

    for n in pool:
        score[n] += freq.get(n, 0) * 3.0

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
                score[n] *= 0.2
        else:
            for n in range(5, 10):
                score[n] *= 0.2

    if not score:
        return random.choice(pool)

    return max(score, key=score.get)


# =========================
# PREDICTION ENGINE
# =========================
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

    history = STATE.history[-1000:]
    sizes = [x["size"] for x in history]

    pattern_match = STORE.get_exact_pattern_probability(sizes)
    exact_p_big = pattern_match["p_big"]

    # signal aggregate
    signals = candidate_signals(history)
    big_w = 0.0
    small_w = 0.0
    total_w = 0.0

    for name, dir_val, raw_weight, reason in signals:
        calibrated = STORE.get_signal_weight(name) * raw_weight
        total_w += calibrated
        if dir_val == 1:
            big_w += calibrated
        else:
            small_w += calibrated

    if total_w > 0:
        signal_p_big = big_w / total_w
    else:
        signal_p_big = ratio_big(sizes, 20)

    # blend pattern + signal + base ratio
    p_big = (signal_p_big * 0.55) + (exact_p_big * 0.35) + (ratio_big(sizes, 20) * 0.10)

    stale = stale_pattern_penalty(history, 12)
    if stale > 0:
        p_big = p_big * (1.0 - stale)

    regime = detect_regime(sizes)
    if regime == "ALTERNATING":
        p_big = 1.0 - sizes[-1]
    elif regime == "TRAP":
        p_big = 1.0 - sizes[-1]
    elif regime == "BLOCK":
        p_big = 1.0 - sizes[-1]
    elif regime == "TREND":
        p_big = sizes[-1]
    elif regime == "CHOPPY":
        p_big = 0.5 + (ratio_big(sizes, 20) - 0.5) * 0.7

    if STATE.loss_streak >= 1:
        p_big = 1.0 - sizes[-1]
    if STATE.loss_streak >= 2:
        p_big = 1.0 - majority_dir(sizes, 6)

    if trailing_run(sizes) >= 3:
        p_big = 1.0 - sizes[-1]
    if trailing_run(sizes) >= 5:
        p_big = sizes[-1]

    p_big = clamp(p_big, 0.05, 0.95)
    pred_dir = 1 if p_big >= 0.5 else 0

    # Smart edge-based safety: weak evidence => low-risk prediction
    strength = signal_strength(history)
    if strength < 0.45:
        mode = "LOW-EDGE"
        stake = "0.25x"
        confidence = 58
        pred_dir = majority_dir(sizes, 6)
        p_big = 0.5
    elif strength < 0.65:
        mode = "SOFT"
        stake = "0.5x"
        confidence = 62
    else:
        stake = "1x" if STATE.loss_streak < 2 else "0.5x"
        confidence = 70
        mode = "DYNAMIC"

    if STATE.loss_streak >= 2:
        pred_dir = 1 - pred_dir
        p_big = 1.0 - p_big
        mode = "CONTRARIAN"
        stake = "0.25x"

    if confidence < 58:
        confidence = 58

    num = choose_number(pred_dir, history, STATE.loss_streak)
    issue = str(int(STATE.history[-1]["issue"]) + 1)

    sig_list = []
    for name, dir_val, raw_weight, reason in signals:
        sig_list.append({
            "name": name,
            "dir": "BIG" if dir_val == 1 else "SMALL",
            "weight": round(STORE.get_signal_weight(name) * raw_weight, 3),
            "reason": reason,
        })

    if not sig_list:
        sig_list.append({"name": "Baseline", "dir": "BIG" if pred_dir == 1 else "SMALL", "weight": 1.0, "reason": "Fallback"})

    return {
        "issue": issue,
        "size": "BIG" if pred_dir == 1 else "SMALL",
        "num": num,
        "p_big": round(p_big, 4),
        "confidence": confidence,
        "raw_conf": confidence,
        "consensus": max(p_big, 1 - p_big),
        "mode": mode,
        "regime": regime,
        "logic": ", ".join(s["name"] for s in sig_list),
        "sigs": [{"name": s["name"], "dir": pred_dir} for s in sig_list],
        "stake": stake,
        "details": {
            "pattern_match": pattern_match,
            "stale_penalty": stale,
            "signal_strength": strength,
            "regime": regime,
            "loss_streak": STATE.loss_streak,
            "win_streak": STATE.win_streak,
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
        STORE.update_num_memory(int(res["actual_num"]), actual_size)


# =========================
# API FETCH
# =========================
async def fetch_data(session):
    try:
        async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
            if r.status != 200:
                return False

            payload = await r.json()
            if not isinstance(payload, dict):
                return False

            items = payload.get("data", {}).get("list", [])
            if not items:
                return False

            items.sort(key=lambda x: int(x.get("issueNumber", x.get("issue", 0))))

            out = []
            seen = set()
            for item in items:
                try:
                    issue = str(item.get("issueNumber", item.get("issue", "")))
                    if not issue or issue in seen:
                        continue
                    seen.add(issue)

                    num = safe_int(item.get("number", item.get("value", 0)), 0)
                    if num < 0 or num > 9:
                        continue

                    out.append({
                        "issue": issue,
                        "num": num,
                        "size": 1 if num >= 5 else 0
                    })
                except Exception:
                    continue

            if out:
                STATE.history = out[-1000:]
                return True
            return False
    except Exception as e:
        log.warning(f"Fetch failed: {e}")
        return False


# =========================
# FORMATTING
# =========================
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


# =========================
# TELEGRAM SEND
# =========================
async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Send error: {e}")
            break
    return False


# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    last_processed_issue = None

    while True:
        try:
            if await fetch_data(session):
                hist_dict = {x["issue"]: x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)

                for res in resolved:
                    learn_from_resolution(res)
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
            log.exception(f"Loop error: {e}")
            await asyncio.sleep(2)


async def handle_ping(request):
    return web.Response(text="V900 is running!")


# =========================
# MAIN
# =========================
async def main():
    CFG.validate()
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()

    async with app:
        await app.start()
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
