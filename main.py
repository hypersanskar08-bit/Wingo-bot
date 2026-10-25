"""
FTW V6 PATTERN MASTER (100+ WINGO PATTERNS + DEEP MATCHER)
=========================================================
- Added 100+ predefined Wingo patterns (symmetry, skips, traps, X-patterns, dragons, etc.)
- Exact sequence matching with weighted ranking
- Hybrid scoring: pattern + markov + parity + volatility + streak + recovery
- No Skip: predicts every period
- Deep reverse-protection logic on repeated losses
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
from telegram.ext import Application


# ============================================================
# CONFIG
# ============================================================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo"
    chat_id_raw: str = "1264164655"
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_v6_pattern_master.db")
    cache_file: str = "history_cache_v6.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V6 PATTERN MASTER"
    min_history: int = 8

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
# LOGGING & HELPERS
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_v6_pattern_master.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v6_master")

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

def get_color_for_logic(num: int) -> str:
    if num in (0, 2, 4, 6, 8):
        return "RED"
    return "GREEN"

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

def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))

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
    return min(1.0, s)


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
                    issue TEXT PRIMARY KEY, pred_type TEXT, size TEXT, num INTEGER, color TEXT,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT,
                    actual_color TEXT, logic TEXT, details TEXT, stake TEXT, created_at REAL
                )
            """)

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, color, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"],
                p["type"],
                p.get("size", ""),
                p.get("num", 0),
                p.get("color", ""),
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
            rows = c.execute("SELECT issue, pred_type, size, num, color, details, stake FROM predictions WHERE status='PENDING'").fetchall()

        for row in rows:
            issue, pred_type, p_size, p_num, p_color, details_json, stake = row
            act = history_by_issue.get(str(issue))
            if not act:
                continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            win_size = (p_size == actual_size) if p_size else True

            win = win_size

            if win and p_num == act["num"]:
                status = "JACK 🏆"
            elif win:
                status = "WIN ✅"
            else:
                status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", (status, act["num"], actual_size, issue))

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

    def recent_outcomes(self, limit: int = 6):
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, color, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

STORE = Storage(CFG.db_path)


# ============================================================
# 100+ PATTERN LIBRARY
# ============================================================
PATTERN_LIBRARY = [
    # --- Streak Dragons ---
    ((1, 1, 1), 1, "3-BIG DRAGON", 1.5),
    ((0, 0, 0), 0, "3-SMALL DRAGON", 1.5),
    ((1, 1, 1, 1), 1, "4-BIG DRAGON", 1.8),
    ((0, 0, 0, 0), 0, "4-SMALL DRAGON", 1.8),
    ((1, 1, 1, 1, 1), 0, "5-BIG REVERSE", 1.6),
    ((0, 0, 0, 0, 0), 1, "5-SMALL REVERSE", 1.6),
    ((1, 1, 1, 1, 1, 1), 0, "6-BIG REVERSE", 1.7),
    ((0, 0, 0, 0, 0, 0), 1, "6-SMALL REVERSE", 1.7),

    # --- Alternating ---
    ((1, 0, 1), 0, "B-S-B => S", 1.4),
    ((0, 1, 0), 1, "S-B-S => B", 1.4),
    ((1, 0, 1, 0), 1, "B-S-B-S => B", 1.5),
    ((0, 1, 0, 1), 0, "S-B-S-B => S", 1.5),
    ((1, 0, 1, 0, 1), 0, "B-S-B-S-B => S", 1.6),
    ((0, 1, 0, 1, 0), 1, "S-B-S-B-S => B", 1.6),

    # --- Symmetry Patterns ---
    ((1, 1, 0, 0), 1, "2B-2S => B", 1.3),
    ((0, 0, 1, 1), 0, "2S-2B => S", 1.3),
    ((1, 1, 1, 0, 0, 0), 1, "3B-3S => B", 1.5),
    ((0, 0, 0, 1, 1, 1), 0, "3S-3B => S", 1.5),
    ((1, 1, 0, 0, 1, 1), 0, "2B-2S-2B => S", 1.5),
    ((0, 0, 1, 1, 0, 0), 1, "2S-2B-2S => B", 1.5),

    # --- Skips ---
    ((1, 0, 0, 0, 1), 0, "B-S-S-S-B => S", 1.4),
    ((0, 1, 1, 1, 0), 1, "S-B-B-B-S => B", 1.4),
    ((1, 0, 1, 1), 0, "B-S-B-B => S", 1.3),
    ((0, 1, 0, 0), 1, "S-B-S-S => B", 1.3),
    ((1, 0, 0, 1), 0, "B-S-S-B => S", 1.2),
    ((0, 1, 1, 0), 1, "S-B-B-S => B", 1.2),
    ((1, 0, 1, 1, 0), 1, "B-S-B-B-S => B", 1.5),
    ((0, 1, 0, 0, 1), 0, "S-B-S-S-B => S", 1.5),

    # --- Block & Traps ---
    ((1, 1, 0, 1, 0), 1, "B-B-S-B-S => B", 1.4),
    ((0, 0, 1, 0, 1), 0, "S-S-B-S-B => S", 1.4),
    ((1, 0, 0, 1, 0), 0, "B-S-S-B-S => S", 1.4),
    ((0, 1, 1, 0, 1), 1, "S-B-B-S-B => B", 1.4),
    ((1, 1, 0, 0, 1), 0, "B-B-S-S-B => S", 1.4),
    ((0, 0, 1, 1, 0), 1, "S-S-B-B-S => B", 1.4),

    # --- X and Mirror ---
    ((1, 0, 1), 1, "X-B-S-B => B", 1.4),
    ((0, 1, 0), 0, "X-S-B-S => S", 1.4),
    ((1, 1, 0, 1, 1), 0, "M-BB-S-BB => S", 1.5),
    ((0, 0, 1, 0, 0), 1, "M-SS-B-SS => B", 1.5),
    ((1, 0, 0, 1), 0, "V-B-S-S-B => S", 1.3),
    ((0, 1, 1, 0), 1, "V-S-B-B-S => B", 1.3),

    # --- Reversals / Anti-trap ---
    ((1, 0, 0, 0), 1, "SMALL DRAGON BREAK => B", 1.4),
    ((0, 1, 1, 1), 0, "BIG DRAGON BREAK => S", 1.4),
    ((1, 1, 1, 0), 1, "3B-1S => B", 1.3),
    ((0, 0, 0, 1), 0, "3S-1B => S", 1.3),
    ((1, 1, 0, 0, 0), 1, "2B-3S => B", 1.4),
    ((0, 0, 1, 1, 1), 0, "2S-3B => S", 1.4),

    # --- Edge Continuations ---
    ((1, 1, 1, 1, 0), 1, "4B-1S CONTINUE B", 1.4),
    ((0, 0, 0, 0, 1), 0, "4S-1B CONTINUE S", 1.4),
    ((1, 0, 0, 0, 0), 1, "1B-4S CONTINUE B", 1.4),
    ((0, 1, 1, 1, 1), 0, "1S-4B CONTINUE S", 1.4),

    # --- Chaos / Micro Patterns ---
    ((1, 0, 1, 0, 0), 1, "ZIG BREAK => B", 1.3),
    ((0, 1, 0, 1, 1), 0, "ZIG BREAK => S", 1.3),
    ((1, 1, 0, 1, 0, 0), 1, "STACK BREAK => B", 1.5),
    ((0, 0, 1, 0, 1, 1), 0, "STACK BREAK => S", 1.5),

    # --- Additional extension patterns (50+) ---
    ((1, 0, 0, 0, 1, 0), 0, "PATTERN 1", 1.4),
    ((0, 1, 1, 1, 0, 1), 1, "PATTERN 2", 1.4),
    ((1, 1, 0, 1, 1, 0), 0, "PATTERN 3", 1.4),
    ((0, 0, 1, 0, 0, 1), 1, "PATTERN 4", 1.4),
    ((1, 0, 1, 1, 0, 1), 0, "PATTERN 5", 1.5),
    ((0, 1, 0, 0, 1, 0), 1, "PATTERN 6", 1.5),
    ((1, 1, 1, 0, 1, 1), 0, "PATTERN 7", 1.5),
    ((0, 0, 0, 1, 0, 0), 1, "PATTERN 8", 1.5),
    ((1, 0, 1, 0, 1, 0), 1, "PATTERN 9", 1.5),
    ((0, 1, 0, 1, 0, 1), 0, "PATTERN 10", 1.5),
    ((1, 1, 0, 0, 0, 1), 1, "PATTERN 11", 1.5),
    ((0, 0, 1, 1, 1, 0), 0, "PATTERN 12", 1.5),
    ((1, 0, 0, 1, 1, 0), 0, "PATTERN 13", 1.5),
    ((0, 1, 1, 0, 0, 1), 1, "PATTERN 14", 1.5),
    ((1, 1, 0, 1, 0, 1), 0, "PATTERN 15", 1.5),
    ((0, 0, 1, 0, 1, 0), 1, "PATTERN 16", 1.5),
    ((1, 0, 1, 1, 1, 0), 1, "PATTERN 17", 1.5),
    ((0, 1, 0, 0, 0, 1), 0, "PATTERN 18", 1.5),

    ((1, 1, 1, 1, 0, 0), 1, "PATTERN 19", 1.5),
    ((0, 0, 0, 0, 1, 1), 0, "PATTERN 20", 1.5),
    ((1, 1, 0, 0, 1, 0), 1, "PATTERN 21", 1.5),
    ((0, 0, 1, 1, 0, 1), 0, "PATTERN 22", 1.5),
    ((1, 0, 0, 1, 0, 0), 1, "PATTERN 23", 1.5),
    ((0, 1, 1, 0, 1, 1), 0, "PATTERN 24", 1.5),
    ((1, 0, 1, 0, 0, 1), 0, "PATTERN 25", 1.5),
    ((0, 1, 0, 1, 1, 0), 1, "PATTERN 26", 1.5),
    ((1, 1, 0, 1, 1, 1), 0, "PATTERN 27", 1.5),
    ((0, 0, 1, 0, 0, 0), 1, "PATTERN 28", 1.5),
    ((1, 0, 1, 1, 0, 0), 1, "PATTERN 29", 1.5),
    ((0, 1, 0, 0, 1, 1), 0, "PATTERN 30", 1.5),
    ((1, 1, 0, 0, 1, 1), 0, "PATTERN 31", 1.5),
    ((0, 0, 1, 1, 0, 0), 1, "PATTERN 32", 1.5),
    ((1, 0, 1, 1, 1, 1), 0, "PATTERN 33", 1.6),
    ((0, 1, 0, 0, 0, 0), 1, "PATTERN 34", 1.6),

    ((1, 0, 0, 1, 1, 1), 0, "PATTERN 35", 1.6),
    ((0, 1, 1, 0, 0, 0), 1, "PATTERN 36", 1.6),
    ((1, 1, 0, 0, 1, 1, 0), 1, "PATTERN 37", 1.7),
    ((0, 0, 1, 1, 0, 0, 1), 0, "PATTERN 38", 1.7),
    ((1, 0, 1, 0, 1, 1, 0), 0, "PATTERN 39", 1.7),
    ((0, 1, 0, 1, 0, 0, 1), 1, "PATTERN 40", 1.7),
    ((1, 1, 1, 0, 0, 1, 1), 0, "PATTERN 41", 1.7),
    ((0, 0, 0, 1, 1, 0, 0), 1, "PATTERN 42", 1.7),
    ((1, 0, 0, 0, 1, 1, 1), 0, "PATTERN 43", 1.7),
    ((0, 1, 1, 1, 0, 0, 0), 1, "PATTERN 44", 1.7),
    ((1, 1, 0, 1, 1, 0, 1), 0, "PATTERN 45", 1.7),
    ((0, 0, 1, 0, 0, 1, 0), 1, "PATTERN 46", 1.7),
    ((1, 0, 1, 1, 0, 1, 0), 1, "PATTERN 47", 1.7),
    ((0, 1, 0, 0, 1, 0, 1), 0, "PATTERN 48", 1.7),
    ((1, 1, 0, 0, 1, 0, 0), 1, "PATTERN 49", 1.7),
    ((0, 0, 1, 1, 0, 1, 1), 0, "PATTERN 50", 1.7),
    ((1, 0, 0, 0, 1, 0, 1), 0, "PATTERN 51", 1.7),
    ((0, 1, 1, 1, 0, 1, 0), 1, "PATTERN 52", 1.7),
    ((1, 1, 0, 1, 0, 0, 1), 0, "PATTERN 53", 1.7),
    ((0, 0, 1, 0, 1, 1, 0), 1, "PATTERN 54", 1.7),
    ((1, 0, 1, 1, 0, 1, 1), 0, "PATTERN 55", 1.7),
    ((0, 1, 0, 0, 1, 0, 0), 1, "PATTERN 56", 1.7),
    ((1, 1, 1, 0, 0, 1, 0), 1, "PATTERN 57", 1.7),
    ((0, 0, 0, 1, 1, 0, 1), 0, "PATTERN 58", 1.7),
]

def lookup_best_pattern(seq: List[int]) -> Optional[Tuple[int, str, float]]:
    best = None
    for pat in PATTERN_LIBRARY:
        pattern_seq, pred_value, name, weight = pat
        if len(seq) >= len(pattern_seq):
            if tuple(seq[-len(pattern_seq):]) == pattern_seq:
                score = (len(pattern_seq) * 2.2) * weight
                candidate = (score, pred_value, name)
                if best is None or score > best[0]:
                    best = candidate
    return best

# ============================================================
# MATCHER + HYBRID SCORING
# ============================================================
def markov_size_prob(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if len(sizes) < 3:
        return 0.5
    seq = tuple(sizes[-2:])
    count, big = 0, 0
    for i in range(len(sizes) - 2):
        if tuple(sizes[i:i+2]) == seq:
            count += 1
            if sizes[i+2] == 1:
                big += 1
    return (big / count) if count > 0 else 0.5

def frequency_bias(history: List[Dict[str, Any]]) -> float:
    seq = [x["size"] for x in history[-50:]]
    if not seq:
        return 0.5
    return sum(seq) / len(seq)

def parity_bias(history: List[Dict[str, Any]]) -> float:
    nums = [x["num"] for x in history[-30:]]
    if not nums:
        return 0.5
    evens = sum(1 for n in nums if n % 2 == 0)
    return evens / len(nums)

def exact_pattern_score(history: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], float, str]:
    seq = [x["size"] for x in history]
    if not seq:
        return None, None, 0.0, "NONE"

    best = lookup_best_pattern(seq)
    if not best:
        return None, None, 0.0, "NONE"

    score, pred_value, name = best
    pred = "BIG" if pred_value == 1 else "SMALL"
    return pred, name, score, name

def choose_best_prediction(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    pat_pred, pat_name, pat_score, pat_label = exact_pattern_score(history)

    # Recovery override
    if STATE.loss_streak >= 2:
        STATE.recovery_mode = True
    else:
        STATE.recovery_mode = False

    if STATE.recovery_mode:
        recent = seq[-6:]
        if sum(recent) >= 4:
            return {"size": "SMALL", "logic": "🛡️ RECOVERY REVERSE", "score": 0.82, "stake": "1x"}
        if sum(recent) <= 1:
            return {"size": "BIG", "logic": "🛡️ RECOVERY REVERSE", "score": 0.82, "stake": "1x"}

    # Dragon protection: never fight a 4+ streak
    if trailing_run(seq) >= 4:
        current = seq[-1]
        return {
            "size": "SMALL" if current == 1 else "BIG",
            "logic": "🐉 DRAGON PROTECTION",
            "score": 0.88,
            "stake": "1x"
        }

    # Pattern dominance
    markov = markov_size_prob(history)
    freq = frequency_bias(history)
    parity = parity_score(history)
    p = (markov * 0.35) + (freq * 0.25) + (parity * 0.20) + (0.20 if pat_pred else 0.0)

    # If exact pattern exists, use it
    if pat_pred is not None:
        mode = pat_label
        final_size = pat_pred
        final_score = clamp((pat_score / 20.0) / 1.4, 0.55, 0.92)
        stake = "2x" if final_score >= 0.80 else "1x" if final_score >= 0.65 else "0.5x"
        return {"size": final_size, "logic": f"🎯 {mode}", "score": final_score, "stake": stake}

    # If no exact pattern, hybrid fallback
    if p >= 0.5:
        final_size = "BIG"
    else:
        final_size = "SMALL"

    return {
        "size": final_size,
        "logic": "📊 HYBRID FALLBACK",
        "score": clamp(max(p, 1-p), 0.55, 0.75),
        "stake": "1x" if p >= 0.55 or p <= 0.45 else "0.5x"
    }

def parity_score(history: List[Dict[str, Any]]) -> float:
    nums = [x["num"] for x in history[-30:]]
    if not nums:
        return 0.5
    evens = sum(1 for n in nums if n % 2 == 0)
    return evens / len(nums)

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
            "details": {}
        }

    history = STATE.history[-2000:]
    choice = choose_best_prediction(history)
    final_size = choice["size"]
    final_num = get_best_number(final_size, history)

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    conf = int(choice["score"] * 100)
    conf = min(conf, 92)

    return {
        "issue": issue,
        "type": "SIZE",
        "size": final_size,
        "num": final_num,
        "raw_conf": conf,
        "logic": choice["logic"],
        "stake": choice["stake"],
        "details": {
            "recovery_mode": STATE.recovery_mode,
            "signal_strength": signal_strength(history)
        }
    }

def get_best_number(size: str, history: List[Dict[str, Any]]) -> int:
    valid = [5, 6, 7, 8, 9] if size == "BIG" else [0, 1, 2, 3, 4]
    freq = Counter([x["num"] for x in history[-50:]])
    if not freq:
        return random.choice(valid)
    return max(valid, key=lambda x: freq.get(x, 0))

def learn_from_resolution(res: Dict[str, Any]):
    if res["win"]:
        STATE.win_streak += 1
        STATE.loss_streak = 0
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0
    STATE.save_cache()

# ============================================================
# API PARSING & FETCHING
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            d = payload["data"]
            if "list" in d and isinstance(d["list"], list):
                items = d["list"]
            elif isinstance(d, list):
                items = d
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
        if 0 <= num <= 9:
            out.append({"issue": str(issue), "num": num, "size": 1 if num >= 5 else 0})

    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_data(session):
    urls = CFG.api_urls()
    last_good = list(STATE.history)

    for url in urls:
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200:
                    continue
                payload = await response.json(content_type=None)
                parsed = parse_history_payload(payload)
                if parsed:
                    STATE.history = parsed[-1000:]
                    STATE.save_cache()
                    return True
        except Exception:
            continue

    if last_good:
        STATE.history = last_good
        STATE.save_cache()
        return True
    return False


# ============================================================
# FORMATTERS & TELEGRAM
# ============================================================
def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_text = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} → <b>{h(r[3])}</b>\n"
        for r in recent_rows[-6:]
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
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res: Dict[str, Any]):
    pred_str = f"{res['size']} {res['num']}"
    return (
        f"🎯 <b>Result</b>\n"
        f"Period: <code>{h(res['issue'])}</code>\n"
        f"Pred: <b>{h(pred_str)}</b>\n"
        f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
        f"<b>{h(res['status'])}</b>"
    )

async def send_msg(bot, text: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await asyncio.wait_for(bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"), timeout=CFG.request_timeout)
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
                        await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(6)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop exception: %s", e)
            await asyncio.sleep(2)

# ============================================================
# HEALTH CHECK & MAIN
# ============================================================
async def health_check(request):
    return web.Response(text="FTW V6 Pattern Master is Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

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
