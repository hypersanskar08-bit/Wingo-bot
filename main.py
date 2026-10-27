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
    telegram_token: str = os.environ.get("TELEGRAM_TOKEN", "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo")
    chat_id_raw: str = os.environ.get("CHAT_ID", "-1004485961768")

    source_api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_final_pattern.db")
    cache_file: str = os.environ.get("CACHE_FILE", "ftw_final_pattern_cache.json")
    log_dir: str = os.environ.get("LOG_DIR", "logs")
    fetch_every: float = float(os.environ.get("FETCH_EVERY", "1.0"))
    request_timeout: float = float(os.environ.get("REQUEST_TIMEOUT", "15.0"))
    bot_name: str = os.environ.get("BOT_NAME", "FTW FINAL PATTERN")
    min_history: int = int(os.environ.get("MIN_HISTORY", "8"))
    recent_limit: int = int(os.environ.get("RECENT_LIMIT", "12"))
    max_history: int = int(os.environ.get("MAX_HISTORY", "1000"))
    min_repeat: int = int(os.environ.get("MIN_REPEAT", "3"))

    def parse_chat_id(self):
        raw = str(self.chat_id_raw).strip()
        if raw == "":
            raise ValueError("CHAT_ID is empty. Set environment variable CHAT_ID.")
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls or [
            "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
        ]

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        if not self.telegram_token:
            raise ValueError("TELEGRAM_TOKEN is missing. Set environment variable TELEGRAM_TOKEN.")
        if not self.chat_id_raw:
            raise ValueError("CHAT_ID is missing. Set environment variable CHAT_ID.")
        if not self.source_api_urls:
            raise ValueError("API_URLS is missing.")

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
            Path(CFG.log_dir) / "ftw_final_pattern.log",
            maxBytes=1_000_000,
            backupCount=3
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_final_pattern")


# ============================================================
# HELPERS
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

def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)

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
    if not arr:
        return 0.5
    return sum(arr) / max(1, len(arr))

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8:
        return "COLD"

    last10 = seq[-10:]
    run = trailing_run(seq)
    alt = sum(1 for i in range(1, len(last10)) if last10[i] != last10[i - 1]) / max(1, len(last10) - 1)
    ratio = ratio_big(seq, 10)

    if alt >= 0.65:
        return "ALTERNATING"
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
    alt = sum(1 for i in range(1, len(seq[-12:])) if seq[-12:][i] != seq[-12:][i - 1]) / max(1, len(seq[-12:]) - 1)
    s = 0.0
    if regime == "TRAP":
        s += 0.35
    if regime == "TREND":
        s += 0.25
    if regime == "ALTERNATING":
        s += 0.30
    if run >= 3:
        s += 0.25
    if alt >= 0.6:
        s += 0.15
    return clamp(min(1.0, s), 0.0, 1.0)


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
                    confidence REAL DEFAULT 0.0,
                    created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_penalties (
                    pattern_key TEXT PRIMARY KEY,
                    fail_streak INTEGER DEFAULT 0,
                    penalty REAL DEFAULT 1.0,
                    last_seen REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_memory (
                    pattern_key TEXT PRIMARY KEY,
                    consec_losses INTEGER DEFAULT 0,
                    is_flipped INTEGER DEFAULT 0,
                    total_preds INTEGER DEFAULT 0,
                    total_wins INTEGER DEFAULT 0
                )
            """)

    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, logic, details, stake, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"],
                p["type"],
                p.get("size", ""),
                p.get("num", 0),
                p.get("logic", "N/A"),
                json.dumps(p.get("details", {})),
                p.get("stake", "1x"),
                float(p.get("confidence", 0.0)),
                time.time()
            ))

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, pred_type, size, num, details, stake, confidence
                FROM predictions
                WHERE status='PENDING'
            """).fetchall()

        for issue, pred_type, p_size, p_num, details_json, stake, confidence in rows:
            act = history_by_issue.get(str(issue))
            if not act:
                continue

            actual_num = int(act["num"])
            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            size_win = bool(p_size == actual_size) if p_size else False
            exact_win = bool(p_num == actual_num) if p_num is not None else False
            win = size_win
            status = "JACK 🏆" if (size_win and exact_win) else "WIN ✅" if size_win else "LOSS ❌"

            with self._conn() as c2:
                c2.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=?, confidence=? WHERE issue=?",
                    (status, actual_num, actual_size, confidence, issue)
                )

            resolved.append({
                "issue": issue,
                "type": pred_type or "SIZE",
                "size": p_size,
                "num": p_num,
                "stake": stake,
                "actual_num": actual_num,
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

    def recent_accuracy(self, limit: int = 20) -> float:
        with self._conn() as c:
            rows = c.execute("""
                SELECT status
                FROM predictions
                WHERE status != 'PENDING'
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
        if not rows:
            return 0.50
        wins = sum(1 for (status,) in rows if "WIN" in str(status) or "JACK" in str(status))
        return wins / len(rows)

    def get_penalty(self, pattern_key: str) -> float:
        with self._conn() as c:
            row = c.execute("SELECT penalty FROM pattern_penalties WHERE pattern_key=?", (pattern_key,)).fetchone()
        return float(row[0]) if row else 1.0

    def penalize_pattern(self, pattern_key: str, success: bool):
        with self._conn() as c:
            row = c.execute("SELECT fail_streak, penalty FROM pattern_penalties WHERE pattern_key=?", (pattern_key,)).fetchone()
            fail_streak, penalty = (0, 1.0) if row is None else row
            if success:
                fail_streak = max(0, fail_streak - 1)
                penalty = min(1.0, penalty + 0.08)
            else:
                fail_streak += 1
                penalty = max(0.10, penalty * 0.75)
            c.execute("""
                INSERT INTO pattern_penalties (pattern_key, fail_streak, penalty, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    fail_streak=excluded.fail_streak,
                    penalty=excluded.penalty,
                    last_seen=excluded.last_seen
            """, (pattern_key, fail_streak, penalty, time.time()))

    def get_pattern_memory(self, key: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("""
                SELECT consec_losses, is_flipped, total_preds, total_wins
                FROM pattern_memory
                WHERE pattern_key=?
            """, (key,)).fetchone()
        return {"consec_losses": 0, "is_flipped": False, "total_preds": 0, "total_wins": 0} if not row else {
            "consec_losses": row[0],
            "is_flipped": bool(row[1]),
            "total_preds": row[2],
            "total_wins": row[3],
        }

    def record_pattern_outcome(self, key: str, won: bool):
        mem = self.get_pattern_memory(key)
        consec, is_flipped = mem["consec_losses"], mem["is_flipped"]
        preds, wins = mem["total_preds"] + 1, mem["total_wins"] + (1 if won else 0)

        if won:
            consec = 0
            is_flipped = False
        else:
            consec += 1
            if consec >= 2:
                is_flipped = True

        with self._conn() as c:
            c.execute("""
                INSERT INTO pattern_memory (pattern_key, consec_losses, is_flipped, total_preds, total_wins)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    consec_losses=excluded.consec_losses,
                    is_flipped=excluded.is_flipped,
                    total_preds=excluded.total_preds,
                    total_wins=excluded.total_wins
            """, (key, consec, int(is_flipped), preds, wins))


STORE = Storage(CFG.db_path)


# ============================================================
# ADVANCED PATTERN MATCHING
# ============================================================
def get_block_candidates(seq_str: str, min_len: int = 5, max_len: int = 16) -> List[str]:
    if not seq_str:
        return []
    blocks = []
    current_char, current_len = seq_str[0], 0
    for c in seq_str:
        if c == current_char:
            current_len += 1
        else:
            blocks.append(current_char * current_len)
            current_char, current_len = c, 1
    blocks.append(current_char * current_len)

    candidates = []
    for i in range(len(blocks)):
        candidate = "".join(blocks[i:])
        if min_len <= len(candidate) <= max_len:
            candidates.append(candidate)
    return candidates

def exact_size_sequence(history: List[Dict[str, Any]]):
    seq = [1 if x["size"] == 1 else 0 for x in history[-CFG.max_history:]]
    if len(seq) < 5:
        return None

    tail_str = "".join("B" if v == 1 else "S" for v in seq[-16:])
    candidates = get_block_candidates(tail_str, min_len=5, max_len=16)

    best_signal = None
    best_score = 0.0

    for cand in candidates:
        cand_seq = [1 if c == 'B' else 0 for c in cand]
        cand_len = len(cand_seq)

        match_indices = [i for i in range(len(seq) - cand_len) if seq[i:i+cand_len] == cand_seq]
        if len(match_indices) >= CFG.min_repeat:
            next_bits = [seq[i + cand_len] for i in match_indices if i + cand_len < len(seq)]
            if not next_bits:
                continue

            total = len(next_bits)
            decay = 0.85
            weights = [decay ** (total - 1 - k) for k in range(total)]
            w_big = sum(w for w, v in zip(weights, next_bits) if v == 1)
            w_small = sum(w for w, v in zip(weights, next_bits) if v == 0)
            w_total = w_big + w_small or 1.0

            big_prob = w_big / w_total
            small_prob = w_small / w_total
            predicted_bit_normal = 1 if big_prob >= small_prob else 0
            win_rate = max(big_prob, small_prob)

            sample_bonus = min(total / 10.0, 1.0) * 0.1
            length_bonus = (cand_len / 16.0) * 0.05
            score = win_rate + sample_bonus + length_bonus

            if win_rate >= 0.60 and score > best_score:
                best_score = score
                info = {
                    "length": cand_len,
                    "pattern_text": cand,
                    "predicted_bit_normal": predicted_bit_normal,
                    "total_count": total,
                    "big_prob": big_prob,
                    "small_prob": small_prob,
                    "confidence": abs(big_prob - small_prob),
                    "win_rate": win_rate,
                    "pattern_key": cand,
                }
                mem = STORE.get_pattern_memory(cand)
                if mem["is_flipped"]:
                    info["predicted_bit"] = 1 - predicted_bit_normal
                    info["is_flipped"] = True
                else:
                    info["predicted_bit"] = predicted_bit_normal
                    info["is_flipped"] = False

                info["predicted_side"] = "BIG" if info["predicted_bit"] == 1 else "SMALL"
                best_signal = info

    return best_signal

def choose_valid_pattern(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    exact = exact_size_sequence(history)
    if exact:
        penalty = STORE.get_penalty(exact["pattern_key"])
        if penalty > 0.25:
            flip_text = " [FLIPPED]" if exact.get("is_flipped") else ""
            return {
                "size": exact["predicted_side"],
                "logic": f"SEQ {exact['length']} | {exact['pattern_text']} | n={exact['total_count']} | WR={exact['win_rate']*100:.1f}%{flip_text}",
                "confidence": clamp((exact["win_rate"] * 0.6) + (exact["confidence"] * 0.8), 0.55, 0.92),
                "stake": "1x" if exact["win_rate"] >= 0.72 else "0.5x",
                "pattern_key": exact["pattern_key"],
            }
    return None


# ============================================================
# TREND / MARKOV / FEATURE ENGINE
# ============================================================
def advanced_markov(seq: List[int], max_n: int = 4) -> Tuple[float, float]:
    if len(seq) < 5:
        return 0.5, 0.0
    total_score, total_weight = 0.0, 0.0
    for n in range(2, max_n + 1):
        if len(seq) <= n:
            continue
        pattern = tuple(seq[-n:])
        match_count, big_count = 0, 0
        for i in range(len(seq) - n):
            if tuple(seq[i:i+n]) == pattern:
                match_count += 1
                if seq[i+n] == 1:
                    big_count += 1
        if match_count >= 2:
            prob = big_count / match_count
            weight = match_count * (n ** 1.5)
            total_score += prob * weight
            total_weight += weight
    return (total_score / total_weight, total_weight) if total_weight > 0 else (0.5, 0.0)

def extract_features(history: List[Dict[str, Any]]) -> Dict[str, float]:
    if not history:
        return {}
    seq = [x["size"] for x in history]
    last10 = seq[-10:]
    last20 = seq[-20:]
    last5 = seq[-5:]
    run = trailing_run(seq)
    regime = detect_regime(seq)
    alt_ratio = sum(1 for i in range(1, len(last10)) if last10[i] != last10[i - 1]) / max(1, len(last10) - 1)

    features = {
        "len": float(len(seq)),
        "last1": float(seq[-1]),
        "last2": float(seq[-2]) if len(seq) >= 2 else float(seq[-1]),
        "run": float(run),
        "ratio_5": float(sum(last5) / max(1, len(last5))),
        "ratio_10": float(sum(last10) / max(1, len(last10))),
        "ratio_20": float(sum(last20) / max(1, len(last20))),
        "vol_10": volatility(seq, 10),
        "vol_20": volatility(seq, 20),
        "alt_ratio_10": alt_ratio,
        "trend_bias": float(sum(seq) / max(1, len(seq))),
        "regime_score": {
            "TREND": 1.0,
            "TRAP": 0.8,
            "ALTERNATING": 0.7,
            "CYCLE": 0.5,
            "CHOPPY": 0.4,
            "COLD": 0.2,
        }.get(regime, 0.5),
    }
    return features

def score_size(history: List[Dict[str, Any]]) -> Tuple[float, str]:
    if not history:
        return 0.5, "EMPTY"

    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    features = extract_features(history)
    pattern = choose_valid_pattern(history)

    score = 0.0
    logic_parts = []

    # trend continuation
    run = trailing_run(seq)
    if run >= 3:
        if seq[-1] == 1:
            score += 0.75
            logic_parts.append(f"trend+{run}")
        else:
            score -= 0.75
            logic_parts.append(f"trend-{run}")

    # sma cross
    if len(seq) >= 20:
        sma5 = sum(seq[-5:]) / 5
        sma20 = sum(seq[-20:]) / 20
        if sma5 > sma20 + 0.15:
            score += 0.30
            logic_parts.append("SMA5>20")
        elif sma5 < sma20 - 0.15:
            score -= 0.30
            logic_parts.append("SMA5<20")

    # alternation / reversal
    last10 = seq[-10:]
    alt_ratio = sum(1 for i in range(1, len(last10)) if last10[i] != last10[i - 1]) / max(1, len(last10) - 1)
    if alt_ratio >= 0.65:
        # alternating sequence often means reversal
        if seq[-1] == 1:
            score -= 0.20
            logic_parts.append("alt-reverse")
        else:
            score += 0.20
            logic_parts.append("alt-follow")

    # markov
    markov_prob, _ = advanced_markov(seq)
    score += (markov_prob - 0.5) * 1.40
    logic_parts.append(f"markov={markov_prob:.2f}")

    # pattern / exact sequence
    if pattern:
        pside = pattern["size"]
        pconf = pattern["confidence"]
        if pside == "BIG":
            score += (pconf - 0.5) * 1.6
            logic_parts.append(f"seq-big:{pattern['pattern_key'][:6]}")
        else:
            score -= (pconf - 0.5) * 1.6
            logic_parts.append(f"seq-small:{pattern['pattern_key'][:6]}")

    # bias and volatility control
    ratio = features.get("ratio_10", 0.5)
    if ratio > 0.6:
        score += 0.15
    elif ratio < 0.4:
        score -= 0.15

    vol = features.get("vol_20", 0)
    if vol > 0.9:
        score *= 0.85
        logic_parts.append("high-vol")

    # regime adjustments
    if regime == "TREND":
        score += 0.1
    elif regime == "TRAP":
        score -= 0.15
    elif regime == "ALTERNATING":
        score += 0.05
    elif regime == "CHOPPY":
        score *= 0.95

    final_prob = clamp(sigmoid(score), 0.08, 0.92)
    return final_prob, " | ".join(logic_parts) if logic_parts else "hybrid"

def exact_number_distribution(history: List[Dict[str, Any]], pred_size: str, regime: str) -> Dict[int, float]:
    entries = [x["num"] for x in history if isinstance(x, dict) and "num" in x and isinstance(x.get("num"), int)]
    if not entries:
        base = {n: 0.1 for n in range(10)}
        return base

    recent = entries[-min(len(entries), 60):]
    weighted = defaultdict(float)
    for idx, num in enumerate(recent):
        weight = 1.0 + (idx / max(1, len(recent)))
        weighted[int(num)] += weight

    # size preference
    low, high = (5, 9) if pred_size == "BIG" else (0, 4)
    for n in range(10):
        if n < low or n > high:
            weighted[n] *= 0.25

    # regime adjustment
    if regime == "ALTERNATING":
        for n in range(10):
            if n >= 5:
                weighted[n] *= 1.2 if pred_size == "BIG" else 0.8
            else:
                weighted[n] *= 1.2 if pred_size == "SMALL" else 0.8
    elif regime == "TREND":
        recent_values = recent[-8:]
        tail_bias = Counter(recent_values)
        for n, val in tail_bias.items():
            weighted[n] *= 1.25 + (val * 0.1)

    total = sum(weighted.values()) or 1.0
    dist = {n: float(weighted.get(n, 0.0)) / total for n in range(10)}
    return dist

def choose_number(distribution: Dict[int, float]) -> int:
    if not distribution:
        return random.randint(0, 9)
    items = list(distribution.items())
    total = sum(v for _, v in items)
    if total <= 0:
        return random.randint(0, 9)
    r = random.random() * total
    acc = 0.0
    for num, p in items:
        acc += p
        if r <= acc:
            return int(num)
    return int(items[-1][0])

def calibrate_confidence(raw_conf: float, recent_accuracy: float) -> float:
    # If recent accuracy is weak, reduce confidence
    if recent_accuracy < 0.35:
        return clamp(raw_conf * 0.68, 0.55, 0.78)
    if recent_accuracy < 0.45:
        return clamp(raw_conf * 0.78, 0.55, 0.82)
    if recent_accuracy < 0.58:
        return clamp(raw_conf * 0.88, 0.55, 0.88)
    return clamp(raw_conf, 0.55, 0.92)

def calculate_stake(confidence: float, recent_accuracy: float, loss_streak: int) -> str:
    if loss_streak >= 2:
        return "0.5x"
    if recent_accuracy < 0.40:
        return "0.5x"
    if confidence >= 0.82 and recent_accuracy >= 0.60:
        return "2x"
    if confidence >= 0.72 and recent_accuracy >= 0.52:
        return "1x"
    return "0.5x"


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

    if STATE.loss_streak >= 2:
        return {
            "issue": str(issue),
            "type": "SIZE",
            "size": "SKIP",
            "num": 0,
            "raw_conf": 0,
            "logic": f"COOLDOWN (Loss Streak: {STATE.loss_streak})",
            "stake": "0x",
            "details": {}
        }

    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    size_prob, logic = score_size(history)
    pred_size = "BIG" if size_prob >= 0.5 else "SMALL"

    # exact number distribution
    dist = exact_number_distribution(history, pred_size, regime)
    pred_num = choose_number(dist)

    recent_accuracy = STORE.recent_accuracy(limit=20)
    base_conf = clamp(size_prob if pred_size == "BIG" else 1.0 - size_prob, 0.55, 0.92)
    confidence = calibrate_confidence(base_conf, recent_accuracy)
    stake = calculate_stake(confidence, recent_accuracy, STATE.loss_streak)

    return {
        "issue": str(issue),
        "type": "SIZE",
        "size": pred_size,
        "num": pred_num,
        "raw_conf": int(confidence * 100),
        "logic": logic,
        "stake": stake,
        "details": {
            "signal_strength": signal_strength(history),
            "regime": regime,
            "probability_big": size_prob,
            "recent_accuracy": recent_accuracy,
            "distribution": dist,
        }
    }


# ============================================================
# PARSE & FETCH
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("data"), dict):
            data_block = payload["data"]
            items = data_block.get("list", []) if isinstance(data_block.get("list"), list) else []
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
        out.append({"issue": key, "num": num, "size": 1 if num >= 5 else 0})
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
        except Exception as e:
            log.warning("Fetch failed from %s: %s", url, e)
    return False


# ============================================================
# FORMAT / SEND
# ============================================================
def normalize_recent_row(row: Any):
    if not isinstance(row, (tuple, list)):
        return "", "", "", ""
    if len(row) >= 3:
        return str(row[0]), str(row[1]), str(row[2]), ""
    return "", "", "", ""

def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_lines = []
    for r in recent_rows[-CFG.recent_limit:]:
        issue, size, status, _ = normalize_recent_row(r)
        if not issue:
            continue
        recent_lines.append(
            f"➜ <code>{h(issue[-4:])}</code> {h(size)} → <b>{h(status)}</b>"
            if status else f"➜ <code>{h(issue[-4:])}</code> {h(size)}"
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
                bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"),
                timeout=CFG.request_timeout
            )
            return True
        except Exception as e:
            log.warning("Telegram send failed attempt %s: %s", attempt + 1, e)
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

                    details = res.get("details", {})
                    pattern_key = details.get("pattern_key") if isinstance(details, dict) else None
                    if pattern_key:
                        STORE.penalize_pattern(str(pattern_key), bool(res["win"]))
                        STORE.record_pattern_outcome(str(pattern_key), bool(res["win"]))

                    STATE.save_cache()
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max(
                    [normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None],
                    default=0
                ) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = build_prediction(STATE.history, next_issue)

                    if pred["size"] == "SKIP":
                        skip_msg = (
                            f"⚠️ <b>COOLDOWN ACTIVE</b>\n"
                            f"Lagatar {STATE.loss_streak} loss hue hain. Bot 1 period observe kar raha hai.\n"
                            f"Period: <code>{next_issue}</code>"
                        )
                        await send_msg(app.bot, skip_msg)
                        STATE.loss_streak = 0
                        last_processed_issue = latest_issue
                        await asyncio.sleep(CFG.fetch_every)
                        continue

                    if pred["raw_conf"] < 62:
                        skip_msg = (
                            f"⚠️ <b>LOW-CONFIDENCE SKIP</b>\n"
                            f"Bot confidence kam hai, prediction avoid kiya gaya.\n"
                            f"Period: <code>{next_issue}</code>"
                        )
                        await send_msg(app.bot, skip_msg)
                        last_processed_issue = latest_issue
                        await asyncio.sleep(CFG.fetch_every)
                        continue

                    STORE.save_prediction({**pred, "type": "SIZE"})
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
    return web.Response(text="FTW FINAL PATTERN is Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()


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
    import math
    asyncio.run(main())
