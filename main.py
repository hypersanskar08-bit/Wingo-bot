"""
V916 STATE-BASED DEEP PREDICTOR
================================
- Tracks exact suffix patterns in 5..12 windows
- Tracks size and color transitions separately
- Detects trend, trap, reversal, alternation, volatility
- Anti-repeat logic to avoid stuck same-side bias
- One final best winner at a time
- Winner chosen among: BIG / SMALL / RED / GREEN
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
    tg_token: str = os.environ.get("8611789455:AAGYPljbnGjbOnM8CZcL6NnDmUlqEBkKOgM", "")
    tg_chat: str = os.environ.get("1264164655", "")
    api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "v916_state_deep.db")
    cache_file: str = "history_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    timeout: float = 25.0
    min_history: int = 50

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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "v916_state_deep.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v916")


def html_escape(x: Any) -> str:
    return html.escape(str(x))


# ============================================================
# HELPERS
# ============================================================
def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

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

def get_color(num: int) -> str:
    if num in (0, 2, 4, 6, 8):
        return "RED"
    if num in (1, 3, 5, 7, 9):
        return "GREEN"
    return "UNKNOWN"

def size_of_num(num: int) -> int:
    return 1 if num >= 5 else 0


# ============================================================
# STATE MATH
# ============================================================
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

def alternation(seq: List[int], window: int = 12) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    changes = 0
    for i in range(1, len(arr)):
        if arr[i] != arr[i - 1]:
            changes += 1
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

def volatility(seq: List[int], window: int = 20) -> float:
    if len(seq) < 2:
        return 0.0
    arr = seq[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8:
        return "COLD"

    run = trailing_run(seq)
    alt = alternation(seq, 10)
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

def signal_strength(history: List[Dict[str, Any]]) -> float:
    if len(history) < 8:
        return 0.2
    sizes = [x["size"] for x in history]
    regime = detect_regime(sizes)
    run = trailing_run(sizes)
    alt = alternation(sizes, 12)
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

def detect_shift(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < 20:
        return {"direction": "NONE", "confidence": 0.0}

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

    confidence = clamp(abs(diff) * 2.4, 0.0, 1.0)
    return {"direction": direction, "confidence": confidence}


# ============================================================
# STATE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.last_predicted_winner: Optional[str] = None
        self.loss_streak = 0
        self.win_streak = 0

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
# SQLITE
# ============================================================
class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=20)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init_db(self):
        with self.conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY,
                    size TEXT,
                    num INTEGER,
                    color TEXT,
                    status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER,
                    actual_size TEXT,
                    actual_color TEXT,
                    mode TEXT,
                    logic TEXT,
                    sigs TEXT,
                    raw_conf INTEGER,
                    regime TEXT,
                    details TEXT,
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
        with self.conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, color, mode, logic, sigs, raw_conf, regime, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["color"], p["mode"], p["logic"],
                json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "UNKNOWN"),
                json.dumps(p.get("details", {})),
                time.time()
            ))

    def exists(self, issue: str) -> bool:
        with self.conn() as c:
            row = c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
            return row is not None

    def update_pattern_memory(self, seq: List[int], actual_size: int):
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
                if row is None:
                    c.execute(
                        "INSERT INTO pattern_stats (pattern_key, window, big_count, small_count, total_count, last_seen) VALUES (?, ?, 0, 0, 0, ?)",
                        (key, w, time.time()),
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
                    (b, s, total, time.time(), key),
                )

    def update_color_memory(self, colors: List[str], actual_color: str):
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
                if row is None:
                    c.execute(
                        "INSERT INTO color_stats (color, red_count, green_count, total_count, last_seen) VALUES (?, 0, 0, 0, ?)",
                        (key, time.time()),
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
                    (r, g, total, time.time(), key),
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
                p_big = row[0] / row[2]
                matches.append((p_big, row[2]))
        if not matches:
            return 0.5
        weighted = 0.0
        total_count = 0
        for p_big, cnt in matches:
            weighted += p_big * cnt
            total_count += cnt
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
                red_prob = row[0] / row[2]
                green_prob = row[1] / row[2]
                matches.append((red_prob, green_prob, row[2]))
        if not matches:
            return {"RED": 0.5, "GREEN": 0.5}
        red_weighted = 0.0
        green_weighted = 0.0
        total_count = 0
        for r, g, cnt in matches:
            red_weighted += r * cnt
            green_weighted += g * cnt
            total_count += cnt
        return {
            "RED": red_weighted / total_count if total_count else 0.5,
            "GREEN": green_weighted / total_count if total_count else 0.5,
        }

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self.conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, color, mode, sigs, raw_conf, regime, details
                FROM predictions WHERE status='PENDING'
            """).fetchall()
        for row in rows:
            issue, p_size, p_num, p_color, mode, sigs_json, raw_conf, regime, details_json = row
            act = history_by_issue.get(str(issue))
            if act is None:
                continue
            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            actual_color = get_color(act["num"])
            size_match = (p_size == actual_size) if p_size else True
            color_match = (p_color == actual_color) if p_color else True
            win = size_match and color_match

            status = "JACK" if win and p_num == act["num"] else ("WIN" if win else "LOSS")
            with self.conn() as c:
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=?, actual_color=? WHERE issue=?",
                    (status, act["num"], actual_size, actual_color, issue)
                )
            resolved.append({
                "issue": issue,
                "pred_size": p_size,
                "pred_color": p_color,
                "pred_num": p_num,
                "actual_num": act["num"],
                "actual_size": actual_size,
                "actual_color": actual_color,
                "win": win,
                "status": status,
            })
        return resolved

    def recent(self, limit=10):
        with self.conn() as c:
            rows = c.execute("""
                SELECT issue, size, color, status
                FROM predictions
                WHERE status!='PENDING'
                ORDER BY CAST(issue AS INTEGER) DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]


STORE = Storage(CFG.db_path)


# ============================================================
# FUSION SCORING
# ============================================================
def size_proba(history: List[Dict[str, Any]]) -> float:
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    exact = STORE.get_pattern_prob(seq)

    # trend pressure
    run = trailing_run(seq)
    if regime == "TREND":
        exact = clamp(exact + 0.16 if seq[-1] == 1 else exact - 0.16, 0.05, 0.95)

    # trap reversal pressure
    if regime == "TRAP":
        exact = clamp(exact - 0.12 if seq[-1] == 1 else exact + 0.12, 0.05, 0.95)

    # alternating pressure
    if regime == "ALTERNATING":
        exact = 1.0 - seq[-1]

    # anti-repeat
    if run >= 4:
        exact = clamp(1.0 - seq[-1], 0.05, 0.95)

    # volatility pressure
    if volatility(seq, 16) > 0.5:
        exact = clamp(exact + 0.08 if seq[-1] == 0 else exact - 0.08, 0.05, 0.95)

    return clamp(exact, 0.05, 0.95)

def color_proba(history: List[Dict[str, Any]]) -> Dict[str, float]:
    nums = [x["num"] for x in history]
    colors = [get_color(n) for n in nums]
    red_ratio = colors.count("RED") / max(1, len(colors))
    green_ratio = colors.count("GREEN") / max(1, len(colors))

    # streak
    last = colors[-1]
    streak = 1
    for c in reversed(colors[:-1]):
        if c == last:
            streak += 1
        else:
            break

    if streak >= 3:
        if last == "RED":
            red_ratio += 0.18
        else:
            green_ratio += 0.18

    # anti-repeat: if same color repeated, expect opposite
    if len(colors) >= 3:
        if colors[-1] == colors[-2] == colors[-3]:
            if last == "RED":
                green_ratio += 0.20
            else:
                red_ratio += 0.20

    # exact color suffix memory
    hist_color_prob = STORE.get_color_prob(colors)
    red_ratio = (red_ratio * 0.55) + (hist_color_prob["RED"] * 0.45)
    green_ratio = (green_ratio * 0.55) + (hist_color_prob["GREEN"] * 0.45)

    return {
        "RED": clamp(red_ratio, 0.05, 0.95),
        "GREEN": clamp(green_ratio, 0.05, 0.95),
    }

def choose_winner(history: List[Dict[str, Any]]):
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    p_big = size_proba(history)
    p_small = 1.0 - p_big

    color_probs = color_proba(history)
    p_red = color_probs["RED"]
    p_green = color_probs["GREEN"]

    # forced reversal if recent run is too long
    if trailing_run(seq) >= 4:
        if seq[-1] == 1:
            p_small += 0.18
            p_big *= 0.8
        else:
            p_big += 0.18
            p_small *= 0.8

    # if regime is strongly alternation, prefer opposite of last
    if regime == "ALTERNATING":
        if seq[-1] == 1:
            p_small = clamp(p_small + 0.20, 0.05, 0.95)
            p_big = clamp(p_big - 0.20, 0.05, 0.95)
        else:
            p_big = clamp(p_big + 0.20, 0.05, 0.95)
            p_small = clamp(p_small - 0.20, 0.05, 0.95)

    # if regime is trend, reinforce current side
    if regime == "TREND":
        if seq[-1] == 1:
            p_big = clamp(p_big + 0.12, 0.05, 0.95)
        else:
            p_small = clamp(p_small + 0.12, 0.05, 0.95)

    candidates = {
        "BIG": p_big,
        "SMALL": p_small,
        "RED": p_red,
        "GREEN": p_green,
    }
    winner = max(candidates, key=candidates.get)
    return winner, candidates

def choose_num_from_winner(winner: str, history: List[Dict[str, Any]]) -> int:
    if winner == "BIG":
        valid = [n for n in range(10) if n >= 5]
    elif winner == "SMALL":
        valid = [n for n in range(10) if n < 5]
    elif winner == "RED":
        valid = [n for n in range(10) if get_color(n) == "RED"]
    else:
        valid = [n for n in range(10) if get_color(n) == "GREEN"]

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
        if n % 2 == 0 and get_color(n) == "RED":
            s += 1.2
        if n % 2 != 0 and get_color(n) == "GREEN":
            s += 1.2
        score[n] = s

    return max(score, key=score.get) if score else random.randint(0, 9)

def generate_prediction() -> Dict[str, Any]:
    if len(STATE.history) < CFG.min_history:
        n = random.randint(0, 9)
        return {
            "issue": "1",
            "winner": "BIG" if n >= 5 else "SMALL",
            "size": "BIG" if n >= 5 else "SMALL",
            "num": n,
            "color": "",
            "raw_conf": 55,
            "mode": "FALLBACK",
            "logic": "fallback",
            "sigs": [{"name": "fallback"}],
            "regime": "COLD",
            "details": {},
        }

    history = STATE.history[-2000:]
    winner, candidates = choose_winner(history)
    num = choose_num_from_winner(winner, history)

    best_conf = max(candidates.values())
    if best_conf > 0.75:
        stake = "1x"
    elif best_conf > 0.63:
        stake = "0.5x"
    else:
        stake = "0.25x"

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    if winner == "BIG":
        size = "BIG"
        color = ""
    elif winner == "SMALL":
        size = "SMALL"
        color = ""
    elif winner == "RED":
        size = ""
        color = "RED"
    else:
        size = ""
        color = "GREEN"

    return {
        "issue": issue,
        "winner": winner,
        "size": size,
        "num": num,
        "color": color,
        "raw_conf": int(best_conf * 100),
        "mode": "STATE-BASED",
        "logic": f"winner={winner}; regime={detect_regime([x['size'] for x in history])}",
        "sigs": [{"name": "state-base", "winner": winner}],
        "regime": detect_regime([x["size"] for x in history]),
        "details": {
            "candidates": candidates,
            "regime": detect_regime([x["size"] for x in history]),
            "signal_strength": signal_strength(history),
        },
        "stake": stake
    }

# ============================================================
# LEARN FROM RESULT
# ============================================================
def learn_from_result(res: Dict[str, Any]):
    if not STATE.history:
        return

    actual_size = 1 if res["actual_size"] == "BIG" else 0
    actual_color = get_color(res["actual_num"])
    seq_sizes = [x["size"] for x in STATE.history]
    seq_colors = [get_color(x["num"]) for x in STATE.history]

    STORE.update_pattern_memory(seq_sizes, actual_size)
    STORE.update_color_memory(seq_colors, actual_color)

    # tune based on win/loss
    if res["win"]:
        STATE.win_streak += 1
        STATE.loss_streak = 0
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

# ============================================================
# HISTORY PARSE
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
            "size": 1 if num >= 5 else 0,
        })

    out.sort(key=lambda x: int(x["issue"]))
    return out

# ============================================================
# FETCH DATA
# ============================================================
async def fetch_history():
    urls = [CFG.api_url]
    last_good = list(STATE.history)

    for url in urls:
        try:
            async with aiohttp.ClientSession() as session:
                for _ in range(4):
                    try:
                        async with session.get(url, timeout=CFG.timeout) as r:
                            if r.status != 200:
                                await asyncio.sleep(1)
                                continue
                            payload = await r.json(content_type=None)
                            parsed = parse_history_payload(payload)
                            if parsed:
                                STATE.history = parsed[-1000:]
                                STATE.save_cache()
                                return True
                    except Exception:
                        await asyncio.sleep(1)
        except Exception:
            pass

    if last_good:
        STATE.history = last_good
        return True
    return False

# ============================================================
# TELEGRAM FORMATS
# ============================================================
def pred_text(pred: Dict[str, Any]) -> str:
    stake = pred.get("stake", "1x")
    return (
        f"<b>Winner:</b> {pred['winner']}\n"
        f"<b>Target No:</b> {pred['num']}\n"
        f"<b>Regime:</b> {pred['regime']}\n"
        f"<b>Confidence:</b> {pred['raw_conf']}%\n"
        f"<b>Stake:</b> {stake}"
    )

def result_text(res: Dict[str, Any]) -> str:
    return (
        f"<b>Result</b>\n"
        f"Actual: {res['actual_size']} {res['actual_num']}\n"
        f"Status: {res['status']}"
    )

# ============================================================
# TELEGRAM SEND
# ============================================================
async def send_msg(bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return True
    except Exception:
        return False

# ============================================================
# MAIN LOOP
# ============================================================
async def run_bot():
    if not CFG.tg_token or not CFG.tg_chat:
        log.error("Set TG_TOKEN and TG_CHAT environment variables.")
        return

    app = Application.builder().token(CFG.tg_token).build()
    await app.initialize()
    await app.start()

    chat_id = int(CFG.tg_chat)
    STATE.load_cache()

    while True:
        try:
            if await fetch_history():
                history_by_issue = {str(x["issue"]): x for x in STATE.history}

                pending = STORE.resolve_pending(history_by_issue)
                for res in pending:
                    learn_from_result(res)
                    await send_msg(app.bot, chat_id, result_text(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if not STORE.exists(next_issue):
                    pred = generate_prediction()
                    STORE.save_prediction(pred)
                    await send_msg(app.bot, chat_id, pred_text(pred))

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)

        except Exception as e:
            log.exception("Loop error: %s", e)
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(run_bot())
