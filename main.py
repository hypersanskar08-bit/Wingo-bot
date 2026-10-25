"""
V916 HYPER PRO PREDICTOR (FINAL ADVANCED RECOVERY MODE)
======================================================
- Fixed database sync issues
- Fixed color logic
- One final prediction per period only
- Recovery mode after losses
- Chooses SIZE or COLOR dynamically
- Predicts BIG / SMALL / RED / GREEN per strongest edge
- Hardcoded token applied as requested
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
from collections import Counter
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
    telegram_token: str = "8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM"
    chat_id_raw: str = "1264164655"
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_hyper_final.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 30.0
    bot_name: str = "V916 HYPER PRO"
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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916_hyper_final.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v916")


def h(x: Any) -> str:
    return html.escape(str(x))


async def health_check(request):
    return web.Response(text="V916 Hyper Pro Final is Running!")


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
    """Logic uses 0,2,4,6,8 as RED and 1,3,5,7,9 as GREEN."""
    if num in (0, 2, 4, 6, 8):
        return "RED"
    return "GREEN"

def get_display_color(num: int) -> str:
    """Display only, to avoid weird violet confusion."""
    if num in (0, 5):
        return "VIOLET"
    if num in (2, 4, 6, 8):
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
    if run >= 3:
        return "TRAP"
    if run >= 5:
        return "TREND"
    if abs(ratio - 0.5) <= 0.12:
        return "CHOPPY"
    return "CYCLE"


# ============================================================
# STATE
# ============================================================
class BayesianWeights:
    def __init__(self):
        self.size = {
            "Markov": 1.0,
            "EMA": 1.0,
            "Streak": 1.0,
            "Trend": 1.0,
            "Alternation": 1.0,
            "Bias": 1.0,
            "ReverseTrap": 1.0
        }
        self.color = {
            "ColorMarkov": 1.0,
            "ColorStreak": 1.0,
            "ColorFreq": 1.0,
            "ColorAlternation": 1.0,
            "ReverseTrap": 1.0
        }

    def adjust(self, winner: str, actual_side: str, is_size: bool):
        if is_size:
            for name in self.size:
                self.size[name] = clamp(
                    self.size[name] * (1.08 if winner == actual_side else 0.92),
                    0.25,
                    3.0
                )
        else:
            for name in self.color:
                self.color[name] = clamp(
                    self.color[name] * (1.08 if winner == actual_side else 0.92),
                    0.25,
                    3.0
                )

    def recovery_boost(self, is_size: bool):
        if is_size:
            for k in self.size:
                self.size[k] = clamp(self.size[k] + 0.08, 0.25, 3.0)
        else:
            for k in self.color:
                self.color[k] = clamp(self.color[k] + 0.08, 0.25, 3.0)


class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.weights = BayesianWeights()
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
            # Safe migration for older DBs
            try:
                c.execute("ALTER TABLE predictions ADD COLUMN pred_type TEXT")
            except sqlite3.OperationalError:
                pass

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
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?",
                    (status, act["num"], actual_size, actual_color, issue)
                )

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
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(seq) < w:
                continue
            suffix = tuple(seq[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self._conn() as c:
                row = c.execute(
                    "SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?",
                    (key,)
                ).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)",
                        (key, w, time.time())
                    )
                    row = (0, 0, 0)

                b, s, total = row
                if actual_size == 1:
                    b += 1
                else:
                    s += 1
                total += 1
                c.execute(
                    "UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?",
                    (b, s, total, time.time(), key)
                )

    def update_color_memory(self, colors: List[str], actual_color: str):
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(colors) < w:
                continue
            suffix = tuple(colors[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self._conn() as c:
                row = c.execute(
                    "SELECT red_count, green_count, total_count FROM color_stats WHERE color=?",
                    (key,)
                ).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO color_stats (color, red_count, green_count, total_count, last_seen) VALUES (?, 0, 0, 0, ?)",
                        (key, time.time())
                    )
                    row = (0, 0, 0)

                r, g, total = row
                if actual_color == "RED":
                    r += 1
                else:
                    g += 1
                total += 1
                c.execute(
                    "UPDATE color_stats SET red_count=?, green_count=?, total_count=?, last_seen=? WHERE color=?",
                    (r, g, total, time.time(), key)
                )

    def get_pattern_prob(self, seq: List[int]) -> float:
        matches = []
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(seq) < w:
                continue
            suffix = tuple(seq[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self.conn() as c:
                row = c.execute(
                    "SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?",
                    (key,)
                ).fetchone()
            if row and row[2] > 0:
                matches.append((row[0] / row[2], row[2]))
        if not matches:
            return 0.5
        total_count = sum(cnt for _, cnt in matches)
        weighted = sum(p * cnt for p, cnt in matches)
        return weighted / total_count if total_count else 0.5

    def get_color_prob(self, colors: List[str]) -> Dict[str, float]:
        matches = []
        for w in [5, 6, 7, 8, 9, 10, 11, 12]:
            if len(colors) < w:
                continue
            suffix = tuple(colors[-w:])
            key = f"{w}:{json.dumps(list(suffix))}"
            with self.conn() as c:
                row = c.execute(
                    "SELECT red_count, green_count, total_count FROM color_stats WHERE color=?",
                    (key,)
                ).fetchone()
            if row and row[2] > 0:
                matches.append((row[0] / row[2], row[1] / row[2], row[2]))
        if not matches:
            return {"RED": 0.5, "GREEN": 0.5}
        red_weighted = 0.0
        green_weighted = 0.0
        total = 0
        for r, g, cnt in matches:
            red_weighted += r * cnt
            green_weighted += g * cnt
            total += cnt
        return {
            "RED": red_weighted / total if total else 0.5,
            "GREEN": green_weighted / total if total else 0.5
        }


STORE = Storage(CFG.db_path)


# ============================================================
# PREDICTION MODELS
# ============================================================
def model_markov_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    if len(sizes) < 3:
        return 0.5
    seq = tuple(sizes[-2:])
    count = 0
    big = 0
    for i in range(len(sizes) - 2):
        if tuple(sizes[i:i + 2]) == seq:
            count += 1
            if sizes[i + 2] == 1:
                big += 1
    if count == 0:
        return 0.5
    return big / count

def model_ema_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history[-20:]]
    if not sizes:
        return 0.5
    ema = sizes[0]
    alpha = 0.35
    for v in sizes[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return clamp(ema, 0.05, 0.95)

def model_streak_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    run = trailing_run(sizes)
    last = sizes[-1]
    if run >= 3:
        return 0.95 if last == 1 else 0.05
    if run == 2:
        return 0.80 if last == 1 else 0.20
    return 0.5

def model_trend_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    run = trailing_run(sizes)
    if run >= 5:
        return 1.0 if sizes[-1] == 1 else 0.0
    if run >= 3:
        return 1.0 if sizes[-1] == 0 else 0.0
    return 0.5

def model_alternation_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    alt = alternating_score(sizes, 12)
    if alt >= 0.6:
        return 1.0 - sizes[-1]
    return 0.5

def model_bias_size(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    ratio = ratio_big(sizes, 20)
    if abs(ratio - 0.5) > 0.18:
        return 1.0 if ratio >= 0.5 else 0.0
    return 0.5

def model_color_markov(history: List[Dict[str, Any]]) -> float:
    colors = [get_color_for_logic(x["num"]) for x in history]
    if len(colors) < 3:
        return 0.5
    step = tuple(colors[-2:])
    count = 0
    red = 0
    for i in range(len(colors) - 2):
        if tuple(colors[i:i + 2]) == step:
            count += 1
            if colors[i + 2] == "RED":
                red += 1
    if count == 0:
        return 0.5
    return red / count

def model_color_streak(history: List[Dict[str, Any]]) -> float:
    colors = [get_color_for_logic(x["num"]) for x in history]
    if not colors:
        return 0.5
    last = colors[-1]
    run = trailing_run([1 if c == "RED" else 0 for c in colors])
    if run >= 3:
        return 0.9 if last == "RED" else 0.1
    return 0.5

def model_color_freq(history: List[Dict[str, Any]]) -> float:
    colors = [get_color_for_logic(x["num"]) for x in history[-20:]]
    if not colors:
        return 0.5
    return colors.count("RED") / len(colors)

def model_color_alternation(history: List[Dict[str, Any]]) -> float:
    colors = [get_color_for_logic(x["num"]) for x in history]
    return 1.0 if alternating_score([1 if c == "RED" else 0 for c in colors], 12) >= 0.6 and colors[-1] == "GREEN" else 0.0 if alternating_score([1 if c == "RED" else 0 for c in colors], 12) >= 0.6 else 0.5

def compute_size_score(history: List[Dict[str, Any]]) -> Dict[str, float]:
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    exact = STORE.get_pattern_prob(seq)
    p_markov = model_markov_size(history)
    p_ema = model_ema_size(history)
    p_streak = model_streak_size(history)
    p_trend = model_trend_size(history)
    p_alt = model_alternation_size(history)
    p_bias = model_bias_size(history)

    base = {
        "Markov": p_markov,
        "EMA": p_ema,
        "Streak": p_streak,
        "Trend": p_trend,
        "Alternation": p_alt,
        "Bias": p_bias,
        "Exact": exact
    }

    # Recovery mode boosts opposite side after loss streak
    if STATE.recovery_mode:
        for k in base:
            if base[k] > 0.5:
                base[k] = clamp(base[k] - 0.12, 0.05, 0.95)
        if seq[-1] == 1:
            base["Exact"] = clamp(base["Exact"] - 0.15, 0.05, 0.95)
        else:
            base["Exact"] = clamp(base["Exact"] + 0.15, 0.05, 0.95)

    weights = {
        "Markov": STATE.weights.size.get("Markov", 1.0),
        "EMA": STATE.weights.size.get("EMA", 1.0),
        "Streak": STATE.weights.size.get("Streak", 1.0),
        "Trend": STATE.weights.size.get("Trend", 1.0),
        "Alternation": STATE.weights.size.get("Alternation", 1.0),
        "Bias": STATE.weights.size.get("Bias", 1.0),
        "Exact": STATE.weights.size.get("ReverseTrap", 1.0)
    }

    # regime adjustments
    if regime == "TREND":
        base["Trend"] = clamp(base["Trend"] + 0.12, 0.05, 0.95)
    if regime == "TRAP":
        base["Trend"] = clamp(base["Trend"] - 0.08, 0.05, 0.95)
        base["Streak"] = clamp(base["Streak"] - 0.10, 0.05, 0.95)
    if regime == "ALTERNATING":
        base["Alternation"] = clamp(base["Alternation"] + 0.18, 0.05, 0.95)

    weighted = sum(v * weights[k] for k, v in base.items())
    total = sum(weights.values())
    p_big = weighted / total
    return clamp(p_big, 0.05, 0.95)

def compute_color_score(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [get_color_for_logic(x["num"]) for x in history]
    regime = detect_regime([x["size"] for x in history])

    exact = STORE.get_color_prob(colors)
    p_markov = model_color_markov(history)
    p_streak = model_color_streak(history)
    p_freq = model_color_freq(history)
    p_alt = model_color_alternation(history)

    base = {
        "ColorMarkov": p_markov,
        "ColorStreak": p_streak,
        "ColorFreq": p_freq,
        "ColorAlternation": p_alt,
        "ExactRed": exact["RED"],
        "ExactGreen": exact["GREEN"]
    }

    weights = {
        "ColorMarkov": STATE.weights.color.get("ColorMarkov", 1.0),
        "ColorStreak": STATE.weights.color.get("ColorStreak", 1.0),
        "ColorFreq": STATE.weights.color.get("ColorFreq", 1.0),
        "ColorAlternation": STATE.weights.color.get("ColorAlternation", 1.0),
        "ExactRed": STATE.weights.color.get("ReverseTrap", 1.0),
        "ExactGreen": STATE.weights.color.get("ReverseTrap", 1.0)
    }

    # recovery mode reduces current dominant side and encourages opposite
    if STATE.recovery_mode:
        if colors[-1] == "RED":
            base["ExactGreen"] = clamp(base["ExactGreen"] + 0.12, 0.05, 0.95)
            base["ExactRed"] = clamp(base["ExactRed"] - 0.12, 0.05, 0.95)
        else:
            base["ExactRed"] = clamp(base["ExactRed"] + 0.12, 0.05, 0.95)
            base["ExactGreen"] = clamp(base["ExactGreen"] - 0.12, 0.05, 0.95)

    red_score = (base["ColorMarkov"] * weights["ColorMarkov"] + base["ColorStreak"] * weights["ColorStreak"] + base["ColorFreq"] * weights["ColorFreq"] + base["ColorAlternation"] * weights["ColorAlternation"] + base["ExactRed"] * weights["ExactRed"]) / (
        weights["ColorMarkov"] + weights["ColorStreak"] + weights["ColorFreq"] + weights["ColorAlternation"] + weights["ExactRed"]
    )

    green_score = (base["ColorMarkov"] * weights["ColorMarkov"] + base["ColorStreak"] * weights["ColorStreak"] + (1.0 - base["ColorFreq"]) * weights["ColorFreq"] + (1.0 - base["ColorAlternation"]) * weights["ColorAlternation"] + base["ExactGreen"] * weights["ExactGreen"]) / (
        weights["ColorMarkov"] + weights["ColorStreak"] + weights["ColorFreq"] + weights["ColorAlternation"] + weights["ExactGreen"]
    )

    return {"RED": clamp(red_score, 0.05, 0.95), "GREEN": clamp(green_score, 0.05, 0.95)}

def choose_final_winner(history: List[Dict[str, Any]]):
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)

    size_p = compute_size_score(history)
    color_p = compute_color_score(history)

    # Strong anti-repeat behavior
    if trailing_run(seq) >= 4:
        if seq[-1] == 1:
            size_p = clamp(size_p - 0.18, 0.05, 0.95)
        else:
            size_p = clamp(size_p + 0.18, 0.05, 0.95)

    if regime == "ALTERNATING":
        size_p = clamp(1.0 - seq[-1], 0.05, 0.95)

    # Dynamic choice between SIZE and COLOR
    size_edge = abs(size_p - 0.5)
    color_edge = abs(max(color_p["RED"], color_p["GREEN"]) - 0.5)
    if color_edge > size_edge + 0.04:
        pred_type = "COLOR"
        if color_p["RED"] >= color_p["GREEN"]:
            winner = "RED"
            conf = color_p["RED"]
        else:
            winner = "GREEN"
            conf = color_p["GREEN"]
    else:
        pred_type = "SIZE"
        winner = "BIG" if size_p > 0.5 else "SMALL"
        conf = size_p if winner == "BIG" else 1.0 - size_p

    return pred_type, winner, conf, size_p, color_p

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
            "details": {}
        }

    history = STATE.history[-2000:]
    pred_type, winner, conf, size_p, color_p = choose_final_winner(history)
    num = choose_number_for_winner(winner, history)

    if STATE.loss_streak >= 2:
        STATE.recovery_mode = True
    else:
        STATE.recovery_mode = False

    if pred_type == "SIZE":
        size_value = winner
        color_value = ""
        stake = "3x" if conf > 0.75 else "2x" if conf > 0.65 else "1x" if conf > 0.55 else "0.5x"
    else:
        size_value = ""
        color_value = winner
        stake = "2x" if conf > 0.75 else "1.5x" if conf > 0.65 else "1x" if conf > 0.55 else "0.5x"

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue,
        "type": pred_type,
        "size": size_value,
        "color": color_value,
        "num": num,
        "raw_conf": int(conf * 100),
        "regime": detect_regime([x["size"] for x in history]),
        "logic": f"{pred_type}:{winner}; recovery={STATE.recovery_mode}",
        "stake": stake,
        "details": {
            "size_p": size_p,
            "color_p": color_p,
            "winner": winner,
            "recovery_mode": STATE.recovery_mode,
            "regime": detect_regime([x["size"] for x in history]),
            "signal_strength": signal_strength(history)
        }
    }


# ============================================================
# LEARN RESULT
# ============================================================
def learn_from_resolution(res: Dict[str, Any]):
    # update memory
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

    if STATE.loss_streak >= 2:
        STATE.recovery_mode = True
    else:
        STATE.recovery_mode = False

    STATE.save_cache()

# ============================================================
# PARSE API
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

    out: List[Dict[str, Any]] = []
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
            out.append({
                "issue": str(issue),
                "num": num,
                "size": 1 if num >= 5 else 0
            })

    out.sort(key=lambda x: int(x["issue"]))
    return out

# ============================================================
# FETCH
# ============================================================
async def fetch_data(session):
    urls = CFG.api_urls()
    last_good = list(STATE.history)

    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200:
                    continue
                payload = await r.json(content_type=None)
                parsed = parse_history_payload(payload)
                if parsed:
                    STATE.history = parsed[-1000:]
                    STATE.save_cache()
                    return True
        except Exception:
            pass

    if last_good:
        STATE.history = last_good
        STATE.save_cache()
        return True

    return False

# ============================================================
# FORMATTING
# ============================================================
def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_text = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n"
        for r in recent_rows[-8:]
    )

    if pred["type"] == "SIZE":
        target_line = f"📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>"
    else:
        target_line = f"🎨 <b>Target Color</b>: <b>{h(pred['color'])}</b>"

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
{target_line}
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Pattern</b>: {h(pred.get('logic', 'N/A'))}
💡 <b>Mode</b>: <b>{h(pred['type'])}</b>
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
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
# TELEGRAM
# ============================================================
async def send_msg(app, text: str, retries: int = 3):
    for _ in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except Exception as e:
            log.exception("Send failed: %s", e)
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
                    await send_msg(app, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = generate_prediction()
                    if pred:
                        STORE.save_prediction(pred)
                        await send_msg(app, format_prediction(pred, STORE.recent_outcomes(8)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)

        except Exception as e:
            log.exception("Worker loop error: %s", e)
            await asyncio.sleep(2)

# ============================================================
# MAIN
# ============================================================
async def main():
    CFG.validate()
    STATE.load_cache()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()

    async with app:
        await app.start()
        bot_info = await app.bot.get_me()
        log.info("Bot started: %s", bot_info.username)

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
