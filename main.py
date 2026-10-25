"""
V916 ULTIMATE WINNER-ONLY PREDICTOR
===================================
- Final winner selected from: BIG / SMALL / RED / GREEN
- Best practical adaptive pattern engine
- Trend shift + anti-trap + phase detection
- Strong recency weighting + exact sequence memory
- Render-safe + Telegram-safe
- One final prediction only
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
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_ultimate_winner.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 30.0
    bot_name: str = "V916 ULTIMATE WINNER"
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
# LOGGING + HEALTH
# ============================================================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916_ultimate_winner.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v916")


def h(x: Any) -> str:
    return html.escape(str(x))


async def health_check(request):
    return web.Response(text="V916 Ultimate Winner Bot is Running!")


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


def get_color_from_num(num: int) -> str:
    if num in (0, 2, 4, 6, 8):
        return "RED"
    if num in (1, 3, 5, 7, 9):
        return "GREEN"
    return "UNKNOWN"


# ============================================================
# PATTERN / TREND METRICS
# ============================================================
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


def detect_trend_shift(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < 20:
        return {"direction": "NONE", "confidence": 0.0, "recent_ratio": 0.5, "prev_ratio": 0.5}

    recent = [x["size"] for x in history[-12:]]
    prev = [x["size"] for x in history[-24:-12]]

    recent_ratio = ratio_big(recent, 12)
    prev_ratio = ratio_big(prev, 12)
    diff = recent_ratio - prev_ratio

    if diff > 0.22:
        direction = "SHIFT_TO_BIG"
    elif diff < -0.22:
        direction = "SHIFT_TO_SMALL"
    else:
        direction = "NONE"

    confidence = clamp(abs(diff) * 2.5, 0.0, 1.0)
    return {
        "direction": direction,
        "confidence": confidence,
        "recent_ratio": recent_ratio,
        "prev_ratio": prev_ratio,
    }


# ============================================================
# STATE + CACHE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.loss_streak = 0
        self.win_streak = 0

    def save_cache(self):
        try:
            with open(CFG.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-2000:], f)
        except Exception as e:
            log.warning(f"Cache save failed: {e}")

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        self.history = data
                        log.info(f"Loaded {len(self.history)} records from cache.")
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
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER, color TEXT, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, actual_color TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT, p_big REAL, calibrated_conf INTEGER,
                    consensus REAL, stake TEXT, created_at REAL
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

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, color, mode, logic, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["color"], p["mode"], p["logic"], json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "UNKNOWN"), json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50), p.get("consensus", 0.5), p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict: Dict[str, Dict[str, Any]]):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, color, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()

        for row in rows:
            issue, p_size, p_num, p_color, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake = row
            act = history_dict.get(str(issue)) or history_dict.get(issue)
            if act is None:
                continue

            act_size = "BIG" if act["size"] == 1 else "SMALL"
            act_color = get_color_from_num(act["num"])
            win_size = (p_size == act_size) if p_size else True
            win_color = (p_color == act_color) if p_color else True
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
                    (status, act["num"], act_size, act_color, issue)
                )

            resolved.append({
                "issue": issue,
                "size": p_size,
                "num": p_num,
                "color": p_color,
                "actual_num": act["num"],
                "actual_size": act_size,
                "actual_color": act_color,
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
            rows = c.execute("""
                SELECT issue, size, color, status
                FROM predictions
                WHERE status!='PENDING'
                ORDER BY CAST(issue AS INTEGER) DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

    def update_pattern_memory(self, sizes: List[int], actual_size: int):
        for window in [3, 5, 8, 12, 20, 30, 50, 100]:
            if len(sizes) < window:
                continue
            seq = tuple(sizes[-window:])
            key = f"{window}:{json.dumps(list(seq))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)", (key, window, time.time()))
                    row = (0, 0, 0)
                big_count, small_count, total_count = row
                if actual_size == 1:
                    big_count += 1
                else:
                    small_count += 1
                total_count += 1
                c.execute("UPDATE pattern_stats SET big_count=?, small_count=?, total_count=?, last_seen=? WHERE pattern_key=?", (big_count, small_count, total_count, time.time(), key))

    def get_exact_pattern_probability(self, sizes: List[int]) -> float:
        matches = []
        for window in [3, 5, 8, 12, 20, 30, 50, 100]:
            if len(sizes) < window:
                continue
            seq = tuple(sizes[-window:])
            key = f"{window}:{json.dumps(list(seq))}"
            with self._conn() as c:
                row = c.execute("SELECT big_count, small_count, total_count FROM pattern_stats WHERE pattern_key=?", (key,)).fetchone()
            if row and row[2] > 0:
                matches.append({"p_big": row[0] / row[2], "count": int(row[2])})
        if not matches:
            return 0.5
        total_count = sum(m["count"] for m in matches)
        return sum(m["p_big"] * m["count"] for m in matches) / total_count


STORE = Storage(CFG.db_path)


# ============================================================
# ADVANCED MODEL ENGINE
# ============================================================
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


def model_run(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    last = sizes[-1]
    run = trailing_run(sizes)
    if run >= 3:
        return 1.0 if last == 1 else 0.0
    return 0.5


def model_volatility(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    return 1.0 - sizes[-1] if volatility(sizes, 20) > 0.5 else 0.5


def model_bias(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    ratio = ratio_big(sizes, 20)
    if abs(ratio - 0.5) > 0.18:
        return 1.0 if ratio >= 0.5 else 0.0
    return 0.5


def model_parity(history: List[Dict[str, Any]]) -> float:
    nums = [x["num"] for x in history[-30:]]
    if not nums:
        return 0.5
    even = sum(1 for n in nums if n % 2 == 0)
    odd = sum(1 for n in nums if n % 2 != 0)
    return 1.0 if even > odd else 0.0


def color_probability(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [get_color_from_num(x["num"]) for x in history[-50:]]
    if not colors:
        return {"RED": 0.5, "GREEN": 0.5}
    red = colors.count("RED") / len(colors)
    green = colors.count("GREEN") / len(colors)
    return {"RED": red, "GREEN": green}


def color_streak_probability(history: List[Dict[str, Any]]) -> Dict[str, float]:
    colors = [get_color_from_num(x["num"]) for x in history[-30:]]
    if not colors:
        return {"RED": 0.5, "GREEN": 0.5}
    last = colors[-1]
    streak = 1
    for c in reversed(colors[:-1]):
        if c == last:
            streak += 1
        else:
            break

    red_prob = colors.count("RED") / len(colors)
    green_prob = colors.count("GREEN") / len(colors)

    if streak >= 3:
        if last == "RED":
            return {"RED": 0.7, "GREEN": 0.3}
        return {"RED": 0.3, "GREEN": 0.7}

    return {"RED": red_prob, "GREEN": green_prob}


def ensemble_size_probability(history: List[Dict[str, Any]]) -> float:
    sizes = [x["size"] for x in history]
    exact = STORE.get_exact_pattern_probability(sizes)
    shift = detect_trend_shift(history)

    models = [
        ("Exact", exact),
        ("Trend", model_trend(history)),
        ("Alternation", model_alternation(history)),
        ("Block", model_block(history)),
        ("Run", model_run(history)),
        ("Volatility", model_volatility(history)),
        ("Bias", model_bias(history)),
        ("Parity", model_parity(history)),
    ]

    weights = {
        "Exact": 0.28,
        "Trend": 0.14,
        "Alternation": 0.12,
        "Block": 0.12,
        "Run": 0.10,
        "Volatility": 0.10,
        "Bias": 0.08,
        "Parity": 0.06,
    }

    regime = detect_regime(sizes)
    if regime == "TRAP":
        weights["Alternation"] += 0.08
        weights["Run"] += 0.08
    if regime == "TREND":
        weights["Trend"] += 0.12
    if regime == "ALTERNATING":
        weights["Alternation"] += 0.12
    if shift["direction"] == "SHIFT_TO_BIG":
        weights["Trend"] += 0.10
        weights["Bias"] += 0.05
    elif shift["direction"] == "SHIFT_TO_SMALL":
        weights["Trend"] -= 0.08
        weights["Bias"] += 0.05

    total = sum(weights.values())
    weighted = 0.0
    for name, val in models:
        weighted += val * weights.get(name, 0.10)

    final = weighted / total
    if shift["direction"] == "SHIFT_TO_BIG":
        final = clamp(final + 0.10, 0.05, 0.95)
    elif shift["direction"] == "SHIFT_TO_SMALL":
        final = clamp(final - 0.10, 0.05, 0.95)

    stale = stale_pattern_penalty(history, 12)
    if stale > 0.12:
        final = clamp(final * 0.93, 0.05, 0.95)

    return clamp(final, 0.05, 0.95)


def ensemble_color_probability(history: List[Dict[str, Any]]) -> Dict[str, float]:
    col_prob = color_probability(history)
    streak_prob = color_streak_probability(history)
    shift = detect_trend_shift(history)

    red = (col_prob["RED"] * 0.62) + (streak_prob["RED"] * 0.38)
    green = (col_prob["GREEN"] * 0.62) + (streak_prob["GREEN"] * 0.38)

    if shift["direction"] == "SHIFT_TO_BIG":
        red = red + 0.06
    elif shift["direction"] == "SHIFT_TO_SMALL":
        green = green + 0.06

    total = red + green
    if total == 0:
        return {"RED": 0.5, "GREEN": 0.5}
    return {"RED": red / total, "GREEN": green / total}


def choose_best_number(valid_nums: List[int], history: List[Dict[str, Any]]) -> int:
    if not valid_nums:
        return random.randint(0, 9)

    nums = [x["num"] for x in history[-250:]]
    freq = Counter(nums)
    score = {}

    for n in valid_nums:
        s = freq.get(n, 0) * 3.0
        if nums and nums[-1] == n:
            s *= 0.7
        if n in (1, 3, 7, 9):
            s += 1.2
        if n in (2, 4, 6, 8):
            s += 1.0
        if n % 2 == 0 and get_color_from_num(n) == "RED":
            s += 1.2
        if n % 2 != 0 and get_color_from_num(n) == "GREEN":
            s += 1.2
        score[n] = s

    return max(score, key=score.get)


def score_winner(p_big: float, p_small: float, p_red: float, p_green: float) -> Dict[str, float]:
    return {
        "BIG": p_big,
        "SMALL": p_small,
        "RED": p_red,
        "GREEN": p_green,
    }


# ============================================================
# WINNER ONLY FINAL PREDICTION
# ============================================================
def generate_prediction() -> Optional[Dict[str, Any]]:
    if len(STATE.history) < CFG.min_history:
        issue = str(int(STATE.history[-1]["issue"]) + 1) if STATE.history else "1"
        num = random.randint(0, 9)
        base = "BIG" if num >= 5 else "SMALL"
        return {
            "issue": issue,
            "size": base,
            "num": num,
            "color": "",
            "p_big": 0.5,
            "size_conf": 0.5,
            "color_conf": 0.5,
            "confidence": 55,
            "raw_conf": 55,
            "consensus": 0.5,
            "mode": "FALLBACK",
            "regime": "COLD",
            "logic": "Fallback",
            "sigs": [{"name": "Fallback", "dir": 1 if num >= 5 else 0}],
            "stake": "0.25x",
            "details": {"fallback": True},
        }

    history = STATE.history[-2000:]
    sizes = [x["size"] for x in history]

    p_big = ensemble_size_probability(history)
    p_small = 1.0 - p_big
    color_probs = ensemble_color_probability(history)
    p_red = color_probs["RED"]
    p_green = color_probs["GREEN"]

    all_scores = score_winner(p_big, p_small, p_red, p_green)
    best_choice = max(all_scores, key=all_scores.get)
    best_conf = all_scores[best_choice]

    if best_choice == "BIG":
        final_size = "BIG"
        final_color = ""
        valid_nums = [n for n in range(10) if n >= 5]
    elif best_choice == "SMALL":
        final_size = "SMALL"
        final_color = ""
        valid_nums = [n for n in range(10) if n < 5]
    elif best_choice == "RED":
        final_size = ""
        final_color = "RED"
        valid_nums = [n for n in range(10) if get_color_from_num(n) == "RED"]
    else:
        final_size = ""
        final_color = "GREEN"
        valid_nums = [n for n in range(10) if get_color_from_num(n) == "GREEN"]

    best_num = choose_best_number(valid_nums, history)

    if best_conf > 0.70:
        confidence, stake = 78, "1x"
    elif best_conf > 0.58:
        confidence, stake = 68, "0.5x"
    else:
        confidence, stake = 56, "0.25x"

    issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue,
        "size": final_size,
        "num": best_num,
        "color": final_color,
        "winner": best_choice,
        "p_big": round(p_big, 4),
        "size_conf": round(max(p_big, p_small), 4),
        "color_conf": round(max(p_red, p_green), 4),
        "confidence": confidence,
        "raw_conf": confidence,
        "consensus": best_conf,
        "mode": "SUPER-ADAPTIVE",
        "regime": detect_regime(sizes),
        "logic": f"Winner selected: {best_choice}",
        "sigs": [{"name": "Adaptive Winner", "dir": 1 if best_choice in ("BIG", "RED") else 0}],
        "stake": stake,
        "details": {
            "winner": best_choice,
            "best_conf": best_conf,
            "p_big": p_big,
            "p_small": p_small,
            "p_red": p_red,
            "p_green": p_green,
            "regime": detect_regime(sizes),
            "trend_shift": detect_trend_shift(history),
            "loss_streak": STATE.loss_streak,
        },
    }


def learn_from_resolution(res: dict):
    if res["win"]:
        STATE.loss_streak = 0
        STATE.win_streak += 1
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    if STATE.history:
        actual_size = 1 if res["actual_size"] == "BIG" else 0
        STORE.update_pattern_memory([x["size"] for x in STATE.history], actual_size)

    STATE.save_cache()


# ============================================================
# PARSE API RESPONSE
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
# FETCH DATA
# ============================================================
async def fetch_data(session):
    urls = CFG.api_urls()
    if not urls:
        log.warning("No API_URLS configured. Waiting.")
        return False

    last_good = [x for x in STATE.history]

    for url in urls:
        try:
            for retry in range(4):
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=CFG.request_timeout),
                        headers={"User-Agent": "Mozilla/5.0"}
                    ) as r:
                        if r.status == 429:
                            await asyncio.sleep((retry + 1) * 2)
                            continue

                        if r.status != 200:
                            log.warning("HTTP %s from %s", r.status, url)
                            break

                        ctype = (r.headers.get("Content-Type") or "").lower()
                        if "json" not in ctype:
                            body = await r.text()
                            log.warning("Non-JSON response from %s -> %s | sample=%r", url, ctype, body[:200])
                            break

                        payload = await r.json(content_type=None)
                        parsed = parse_history_payload(payload)

                        if parsed:
                            STATE.history = parsed[-1000:]
                            STATE.save_cache()
                            log.info("Fetched %d records from %s", len(parsed), url)
                            return True

                        log.warning("Empty payload from %s", url)
                        break

                except asyncio.TimeoutError:
                    log.warning("Timeout for %s (retry %s)", url, retry + 1)
                    await asyncio.sleep((retry + 1) * 2)
                    continue
                except Exception as e:
                    log.warning("Retry fail on %s: %s", url, e)
                    await asyncio.sleep((retry + 1) * 2)
                    continue

        except Exception as e:
            log.warning("Fetch exception on %s: %s", url, e)

    if last_good:
        STATE.history = last_good
        log.warning("Using last known good history because all endpoints failed.")
        return True

    log.warning("All endpoints failed and last good history absent.")
    return False


# ============================================================
# FORMATTING
# ============================================================
def format_prediction(p, recent_rows):
    recent_text = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(r[1] or '')} {h(r[2] or '')} → <b>{h(r[3])}</b>\n"
        for r in recent_rows[-12:]
    )

    info_lines = []
    if p.get("size") and p.get("size") != "":
        info_lines.append(f"📊 <b>Size</b>: <b>{h(p['size'])}</b> ({p['size_conf'] * 100:.1f}%)")
    if p.get("color") and p.get("color") != "":
        info_lines.append(f"🎨 <b>Color</b>: <b>{h(p['color'])}</b> ({p['color_conf'] * 100:.1f}%)")

    info_str = "\n".join(info_lines) if info_lines else ""
    stake_emoji = {"2x": "🔥 2x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x", "0.25x": "🧯 0.25x"}.get(p["stake"], "1x")

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Winner</b>: <b>{h(p['winner'])}</b>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
{info_str}

🧠 <b>Mode</b>: <b>{h(p['mode'])}</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""


def format_result(res):
    if res.get("size") and res.get("size") != "":
        pred_str = f"{res['size']} {res['num']}"
    elif res.get("color") and res.get("color") != "":
        pred_str = f"{res['color']} {res['num']}"
    else:
        pred_str = f"{res['num']}"

    return (
        f"🎯 <b>Result</b>\n"
        f"Period: <code>{h(res['issue'][-4:])}</code>\n"
        f"Pred: <b>{h(pred_str)}</b>\n"
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
# WORKER LOOP
# ============================================================
async def worker_loop(app, session):
    last_processed_issue = None

    while True:
        try:
            if await fetch_data(session):
                hist_dict = {str(x["issue"]): x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)

                for res in resolved:
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    p = generate_prediction()
                    if p:
                        STORE.save_prediction(p)
                        await send_msg(app, format_prediction(p, STORE.recent_outcomes(12)))
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
        ok = await verify_telegram(app)
        if not ok:
            log.error("Bot validation failed. Check TG_TOKEN and TG_CHAT.")
            return

        await start_web_server()

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
