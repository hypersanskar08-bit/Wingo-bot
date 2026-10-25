"""
FTW V2 ADVANCED STATE MINER + RECOVERY + PATTERN ENGINE
========================================================
- Uses last ~1000 recent results
- Exact suffix mining window 3..12
- State transition and reversal logic
- Trend / trap / alternation / volatility / parity analysis
- Recovery override on repeated losses
- Strict stake control
- One final prediction per issue
- NO SKIP
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
    telegram_token: str = "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo"
    chat_id_raw: str = "1264164655"
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_v2_advanced_state.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V2"
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

    fh = RotatingFileHandler(
        Path(CFG.log_dir) / "ftw_v2_advanced_state.log",
        maxBytes=1_000_000,
        backupCount=2
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v1")

def h(x: Any) -> str:
    return html.escape(str(x))

# ============================================================
# HEALTH CHECK
# ============================================================
async def health_check(request):
    return web.Response(text="FTW V1 Advanced State Miner is Running")

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

def ratio_big(seq: List[int], window: int = 20) -> float:
    if not seq:
        return 0.5
    arr = seq[-window:]
    return sum(arr) / len(arr)

def alternating_score(seq: List[int], window: int = 12) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    changes = sum(1 for i in range(1, len(arr)) if arr[i] != arr[i - 1])
    return changes / max(1, len(arr) - 1)

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

def volatility(seq: List[int], window: int = 20) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

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
# STATE
# ============================================================
class WeightModel:
    def __init__(self):
        self.size = {
            "suffix": 1.0,
            "trend": 1.0,
            "streak": 1.0,
            "alternation": 1.0,
            "volatility": 1.0,
            "parity": 1.0,
            "transition": 1.0,
            "reversal": 1.0,
            "recovery": 1.0
        }
        self.color = {
            "suffix": 1.0,
            "streak": 1.0,
            "frequency": 1.0,
            "alternation": 1.0,
            "transition": 1.0,
            "reversal": 1.0,
            "recovery": 1.0
        }

class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.weights = WeightModel()
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
                    color TEXT,
                    status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER,
                    actual_size TEXT,
                    actual_color TEXT,
                    logic TEXT,
                    details TEXT,
                    stake TEXT,
                    created_at REAL
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
                CREATE TABLE IF NOT EXISTS color_stats (
                    color TEXT PRIMARY KEY,
                    red_count INTEGER DEFAULT 0,
                    green_count INTEGER DEFAULT 0,
                    total_count INTEGER DEFAULT 0,
                    last_seen REAL DEFAULT 0
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
            row = c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
            return row is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, pred_type, size, num, color, details, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()

        for row in rows:
            issue, pred_type, p_size, p_num, p_color, details_json, stake = row
            act = history_by_issue.get(str(issue))
            if not act:
                continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            actual_color = get_color_for_logic(act["num"])

            win_size = (p_size == actual_size) if p_size else True
            win_color = (p_color == actual_color) if p_color else True
            win = win_size and win_color

            if win and p_num == act["num"]:
                status = "JACK 🏆"
            elif win:
                status = "WIN ✅"
            else:
                status = "LOSS ❌"

            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?", (status, act["num"], actual_size, actual_color, issue))

            resolved.append({
                "issue": issue,
                "type": pred_type or "SIZE",
                "size": p_size,
                "num": p_num,
                "color": p_color,
                "stake": stake,
                "actual_num": act["num"],
                "actual_size": actual_size,
                "actual_color": actual_color,
                "status": status,
                "win": win,
                "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 8):
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, size, color, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return rows[::-1]

    def update_pattern_memory(self, seq: List[int], actual_size: int):
        for w in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            if len(seq) < w:
                continue
            key = f"{w}:{json.dumps(list(seq[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)", (key, w, time.time()))
                    row = (0, 0, 0)
                b, s, total = row
                if actual_size == 1:
                    b += 1
                else:
                    s += 1
                total += 1
                c.execute("UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?", (b, s, total, time.time(), key))

    def update_color_memory(self, colors: List[str], actual_color: str):
        for w in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            if len(colors) < w:
                continue
            key = f"{w}:{json.dumps(list(colors[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT red_count, green_count, total_count FROM color_stats WHERE color=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO color_stats (color, red_count, green_count, total_count, last_seen) VALUES (?, 0, 0, 0, ?)", (key, time.time()))
                    row = (0, 0, 0)
                r, g, total = row
                if actual_color == "RED":
                    r += 1
                else:
                    g += 1
                total += 1
                c.execute("UPDATE color_stats SET red_count=?, green_count=?, total_count=?, last_seen=? WHERE color=?", (r, g, total, time.time(), key))

    def get_pattern_prob(self, seq: List[int]) -> float:
        matches = []
        for w in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            if len(seq) < w:
                continue
            key = f"{w}:{json.dumps(list(seq[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
            if row and row[2] > 0:
                matches.append((row[0] / row[2], row[2]))
        if not matches:
            return 0.5
        total = sum(cnt for _, cnt in matches)
        return sum(p * cnt for p, cnt in matches) / total

    def get_color_prob(self, colors: List[str]) -> Dict[str, float]:
        matches = []
        for w in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            if len(colors) < w:
                continue
            key = f"{w}:{json.dumps(list(colors[-w:]))}"
            with self._conn() as c:
                row = c.execute("SELECT red_count, green_count, total_count FROM color_stats WHERE color=?", (key,)).fetchone()
            if row and row[2] > 0:
                matches.append((row[0] / row[2], row[1] / row[2], row[2]))
        if not matches:
            return {"RED": 0.5, "GREEN": 0.5}
        total = sum(cnt for _, _, cnt in matches)
        return {
            "RED": sum(r * cnt for r, _, cnt in matches) / total,
            "GREEN": sum(g * cnt for _, g, cnt in matches) / total
        }

STORE = Storage(CFG.db_path)


# ============================================================
# PATTERN MINER
# ============================================================
class PatternMiner:
    def __init__(self, history: List[Dict[str, Any]]):
        self.history = history
        self.sizes = [x["size"] for x in history]
        self.nums = [x["num"] for x in history]
        self.colors = [get_color_for_logic(n) for n in self.nums]

    def exact_suffix_big_probability(self, window: int = 8) -> float:
        if len(self.sizes) < window:
            return 0.5
        suffix = tuple(self.sizes[-window:])
        counts = defaultdict(int)
        next_big = defaultdict(int)

        for i in range(len(self.sizes) - window):
            key = tuple(self.sizes[i:i + window])
            counts[key] += 1
            if self.sizes[i + window] == 1:
                next_big[key] += 1

        if suffix not in counts:
            return 0.5
        return next_big.get(suffix, 0) / counts[suffix]

    def transition_matrix(self) -> Dict[str, float]:
        if len(self.sizes) < 2:
            return {"BIG": 0.5, "SMALL": 0.5}

        last = self.sizes[-1]
        same = sum(1 for i in range(1, len(self.sizes)) if self.sizes[i] == self.sizes[i - 1])
        opp = len(self.sizes) - 1 - same

        if last == 1:
            return {
                "BIG": clamp(0.65 + (same / max(1, len(self.sizes))), 0.05, 0.95),
                "SMALL": clamp(0.35 - (same / max(1, len(self.sizes))), 0.05, 0.95)
            }
        return {
            "BIG": clamp(0.35 + (opp / max(1, len(self.sizes))), 0.05, 0.95),
            "SMALL": clamp(0.65 - (opp / max(1, len(self.sizes))), 0.05, 0.95)
        }

    def run_bias(self) -> float:
        if not self.sizes:
            return 0.5
        run = trailing_run(self.sizes)
        last = self.sizes[-1]
        if run >= 5:
            return 0.95 if last == 1 else 0.05
        if run >= 3:
            return 0.8 if last == 1 else 0.2
        return 0.5

    def anti_repeat(self) -> float:
        if len(self.sizes) < 4:
            return 0.5
        if self.sizes[-1] == self.sizes[-2] == self.sizes[-3]:
            return 1.0 - self.sizes[-1]
        return 0.5

    def parity_bias(self) -> float:
        if not self.nums:
            return 0.5
        evens = sum(1 for n in self.nums[-20:] if n % 2 == 0)
        odds = sum(1 for n in self.nums[-20:] if n % 2 != 0)
        return 0.5 + (evens - odds) / max(1, evens + odds) * 0.5

    def exact_color_probability(self, window: int = 8) -> float:
        if len(self.colors) < window:
            return 0.5
        suffix = tuple(self.colors[-window:])
        counts = defaultdict(int)
        red_counts = defaultdict(int)

        for i in range(len(self.colors) - window):
            key = tuple(self.colors[i:i + window])
            counts[key] += 1
            if self.colors[i + window] == "RED":
                red_counts[key] += 1

        if suffix not in counts:
            return 0.5
        return red_counts.get(suffix, 0) / counts[suffix]

    def color_streak_bias(self) -> float:
        if not self.colors:
            return 0.5
        run = trailing_run([1 if c == "RED" else 0 for c in self.colors])
        last = self.colors[-1]
        if run >= 3:
            return 0.9 if last == "RED" else 0.1
        return 0.5

    def color_transition(self) -> Dict[str, float]:
        if len(self.colors) < 2:
            return {"RED": 0.5, "GREEN": 0.5}
        last = self.colors[-1]
        same = sum(1 for i in range(1, len(self.colors)) if self.colors[i] == self.colors[i - 1])
        diff = len(self.colors) - 1 - same
        if last == "RED":
            return {
                "RED": clamp(0.65 + (same / max(1, len(self.colors))), 0.05, 0.95),
                "GREEN": clamp(0.35 - (same / max(1, len(self.colors))), 0.05, 0.95),
            }
        return {
            "RED": clamp(0.35 + (diff / max(1, len(self.colors))), 0.05, 0.95),
            "GREEN": clamp(0.65 - (diff / max(1, len(self.colors))), 0.05, 0.95),
        }

    def build_features(self) -> Dict[str, Dict[str, float]]:
        size_feats = {
            "suffix": self.exact_suffix_big_probability(8),
            "trend": self.run_bias(),
            "streak": self.run_bias(),
            "alternation": 1.0 - self.sizes[-1] if alternating_score(self.sizes, 12) >= 0.6 else 0.5,
            "volatility": clamp(0.5 + (0.5 - abs(volatility(self.sizes, 20) - 0.5)), 0.05, 0.95),
            "parity": self.parity_bias(),
            "transition": self.transition_matrix()["BIG"],
            "reversal": self.anti_repeat(),
        }

        color_feats = {
            "suffix": self.exact_color_probability(8),
            "streak": self.color_streak_bias(),
            "frequency": self.colors.count("RED") / max(1, len(self.colors)),
            "alternation": self.color_transition()["RED"],
            "transition": self.color_transition()["RED"],
            "reversal": 1.0 - self.color_streak_bias(),
        }

        return {"size": size_feats, "color": color_feats}


# ============================================================
# FINAL DECISION
# ============================================================
def decide_final_winner(history: List[Dict[str, Any]]):
    miner = PatternMiner(history)
    feats = miner.build_features()

    size_score = (
        feats["size"]["suffix"] * 0.24 +
        feats["size"]["trend"] * 0.18 +
        feats["size"]["streak"] * 0.18 +
        feats["size"]["alternation"] * 0.12 +
        feats["size"]["volatility"] * 0.08 +
        feats["size"]["parity"] * 0.08 +
        feats["size"]["transition"] * 0.08 +
        feats["size"]["reversal"] * 0.12
    ) / 1.08

    color_score = (
        feats["color"]["suffix"] * 0.22 +
        feats["color"]["streak"] * 0.22 +
        feats["color"]["frequency"] * 0.2 +
        feats["color"]["alternation"] * 0.14 +
        feats["color"]["transition"] * 0.14 +
        feats["color"]["reversal"] * 0.08
    ) / 1.0

    recent = [x["size"] for x in history[-6:]]
    if STATE.loss_streak >= 2:
        STATE.recovery_mode = True
    elif len(recent) >= 4 and sum(recent) >= 4:
        STATE.recovery_mode = True
    elif len(recent) >= 4 and sum(recent) <= 1:
        STATE.recovery_mode = True
    else:
        STATE.recovery_mode = False

    if STATE.recovery_mode:
        if sum(recent) >= 4:
            return "SIZE", "SMALL", 0.82
        if sum(recent) <= 1:
            return "SIZE", "BIG", 0.82

    size_edge = abs(size_score - 0.5)
    color_edge = abs(color_score - 0.5)

    if color_edge > size_edge + 0.04:
        pred_type = "COLOR"
        winner = "RED" if color_score > 0.5 else "GREEN"
        conf = color_score if winner == "RED" else 1.0 - color_score
    else:
        pred_type = "SIZE"
        winner = "BIG" if size_score > 0.5 else "SMALL"
        conf = size_score if winner == "BIG" else 1.0 - size_score

    return pred_type, winner, clamp(conf, 0.05, 0.95)

def choose_number_for_winner(winner: str, history: List[Dict[str, Any]]) -> int:
    if winner == "BIG":
        valid = [n for n in range(10) if n >= 5]
    elif winner == "SMALL":
        valid = [n for n in range(10) if n < 5]
    elif winner == "RED":
        valid = [n for n in range(10) if get_color_for_logic(n) == "RED"]
    else:
        valid = [n for n in range(10) if get_color_for_logic(n) == "GREEN"]

    nums = [x["num"] for x in history[-250:]]
    freq = Counter(nums)
    score = {}

    for n in valid:
        s = freq.get(n, 0) * 3.0
        if nums and nums[-1] == n:
            s *= 0.7
        if n in (1, 3, 7, 9):
            s += 1.2
        if n in (2, 4, 6, 8):
            s += 1.0
        if n % 2 == 0 and get_color_for_logic(n) == "RED":
            s += 1.2
        if n % 2 != 0 and get_color_for_logic(n) == "GREEN":
            s += 1.2
        score[n] = s

    return max(score, key=score.get) if score else random.choice(valid)

def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {
            "issue": "1",
            "type": "SIZE",
            "size": "BIG" if n >= 5 else "SMALL",
            "color": "",
            "num": n,
            "raw_conf": 55,
            "regime": "COLD",
            "logic": "fallback",
            "stake": "0.5x",
            "details": {"recovery_mode": False}
        }

    history = STATE.history[-2000:]
    pred_type, winner, conf = decide_final_winner(history)
    num = choose_number_for_winner(winner, history)

    if pred_type == "SIZE":
        size_value = winner
        color_value = ""
        if STATE.recovery_mode:
            stake = "1x" if conf >= 0.75 else "0.5x"
        else:
            if conf >= 0.80:
                stake = "2x"
            elif conf >= 0.70:
                stake = "1x"
            elif conf >= 0.60:
                stake = "0.5x"
            else:
                stake = "0.25x"
    else:
        size_value = ""
        color_value = winner
        if STATE.recovery_mode:
            stake = "1x" if conf >= 0.75 else "0.5x"
        else:
            if conf >= 0.80:
                stake = "1x"
            elif conf >= 0.70:
                stake = "0.5x"
            else:
                stake = "0.25x"

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue,
        "type": pred_type,
        "size": size_value,
        "color": color_value,
        "num": num,
        "raw_conf": int(clamp(conf * 100, 0, 85)),
        "regime": detect_regime([x["size"] for x in history]),
        "logic": f"{pred_type}:{winner}; recovery={STATE.recovery_mode}",
        "stake": stake,
        "details": {
            "recovery_mode": STATE.recovery_mode,
            "signal_strength": signal_strength(history)
        }
    }

def learn_from_resolution(res: Dict[str, Any]):
    if STATE.history:
        seq = [x["size"] for x in STATE.history]
        STORE.update_pattern_memory(seq, 1 if res["actual_size"] == "BIG" else 0)
        STORE.update_color_memory([get_color_for_logic(x["num"]) for x in STATE.history], get_color_for_logic(res["actual_num"]))

    if res["win"]:
        STATE.win_streak += 1
        STATE.loss_streak = 0
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    recent = [x["size"] for x in STATE.history[-6:]]
    if STATE.loss_streak >= 2:
        STATE.recovery_mode = True
    elif len(recent) >= 4 and sum(recent) >= 4:
        STATE.recovery_mode = True
    elif len(recent) >= 4 and sum(recent) <= 1:
        STATE.recovery_mode = True
    else:
        STATE.recovery_mode = False

    STATE.save_cache()


# ============================================================
# API PARSING
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


# ============================================================
# FETCH WITH TIMEOUT
# ============================================================
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
        except asyncio.TimeoutError:
            log.warning("Timeout while fetching %s", url)
            continue
        except Exception as exc:
            log.warning("Fetch exception for %s: %s", url, exc)
            continue

    if last_good:
        STATE.history = last_good
        STATE.save_cache()
        return True

    return False


# ============================================================
# FORMATTERS
# ============================================================
def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_text = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n"
        for r in recent_rows[-6:]
    )

    if pred["type"] == "SIZE":
        target_line = f"📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>"
    else:
        target_line = f"🎨 <b>Target Color</b>: <b>{h(pred['color'])}</b>"

    rec_tag = " (Recovery ON)" if pred["details"].get("recovery_mode") else ""

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
{target_line}
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Pattern</b>: {h(pred.get('logic', 'N/A'))}{rec_tag}
💡 <b>Mode</b>: <b>{h(pred['type'])}</b>
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res: Dict[str, Any]):
    if res["type"] == "SIZE":
        pred_str = f"{res['size']} {res['num']}"
    else:
        pred_str = f"{res['color']} {res['num']}"

    return (
        f"🎯 <b>Result</b>\n"
        f"Period: <code>{h(res['issue'])}</code>\n"
        f"Pred: <b>{h(pred_str)}</b>\n"
        f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
        f"<b>{h(res['status'])}</b>"
    )

# ============================================================
# TELEGRAM SEND WITH TIMEOUT
# ============================================================
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
