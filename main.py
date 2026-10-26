"""
FTW V40 - Deep Upgrade
=====================
- Strong exact pattern engine 3..15
- Strict confidence gating
- Regime detection: trend / zigzag / trap / balanced / neutral
- Walk-forward telemetry
- 250 draw telemetry
- 2-level target progression
- Telegram ready but safe fallback to console
- Works even if API payload shape is slightly different
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
import sys
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
    api_urls: str = os.environ.get(
        "API_URLS",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    )
    db_path: str = os.environ.get("DB_PATH", "ftw_v40.db")
    cache_file: str = "ftw_v40_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    timeout: float = 15.0
    bot_name: str = "FTW V40 ENGINE"
    min_history: int = 8
    recent_limit: int = 12
    max_history_window: int = 1000

    def parse_chat_id(self):
        raw = (self.chat_id_raw or "").strip()
        if not raw:
            return None
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw

    def urls(self) -> List[str]:
        items = [u.strip() for u in self.api_urls.split(",") if u.strip()]
        return items if items else [
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
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    fh = RotatingFileHandler(
        Path(CFG.log_dir) / "ftw_v40.log",
        maxBytes=1_000_000,
        backupCount=3
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v40")


def h(x: Any) -> str:
    return html.escape(str(x))

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

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

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
                CREATE TABLE IF NOT EXISTS telemetry (
                    draw_index INTEGER PRIMARY KEY,
                    pnl REAL,
                    balance REAL,
                    target1 REAL,
                    target2 REAL,
                    win_count INTEGER,
                    loss_count INTEGER,
                    created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS modules (
                    module_name TEXT PRIMARY KEY,
                    score REAL,
                    weight REAL,
                    hit_rate REAL,
                    confidence REAL,
                    updated_at REAL
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
                p.get("type", "SIZE"),
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

    def log_draw_telemetry(self, draw_index: int, pnl: float, balance: float,
                          target1: float, target2: float, win_count: int, loss_count: int):
        with self._conn() as c:
            c.execute("""
                INSERT INTO telemetry (draw_index, pnl, balance, target1, target2, win_count, loss_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(draw_index) DO UPDATE SET
                    pnl=excluded.pnl,
                    balance=excluded.balance,
                    target1=excluded.target1,
                    target2=excluded.target2,
                    win_count=excluded.win_count,
                    loss_count=excluded.loss_count,
                    created_at=excluded.created_at
            """, (draw_index, pnl, balance, target1, target2, win_count, loss_count, time.time()))

    def update_module_state(self, module_name: str, score: float, weight: float, hit_rate: float, confidence: float):
        with self._conn() as c:
            c.execute("""
                INSERT INTO modules (module_name, score, weight, hit_rate, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(module_name) DO UPDATE SET
                    score=excluded.score,
                    weight=excluded.weight,
                    hit_rate=excluded.hit_rate,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
            """, (module_name, score, weight, hit_rate, confidence, time.time()))

    def module_status(self, module_name: str) -> Dict[str, float]:
        with self._conn() as c:
            row = c.execute(
                "SELECT score, weight, hit_rate, confidence FROM modules WHERE module_name=?",
                (module_name,)
            ).fetchone()
        if not row:
            return {"score": 0.0, "weight": 0.0, "hit_rate": 0.0, "confidence": 0.0}
        score, weight, hit_rate, confidence = row
        return {
            "score": float(score),
            "weight": float(weight),
            "hit_rate": float(hit_rate),
            "confidence": float(confidence),
        }

STORE = Storage(CFG.db_path)


# ============================================================
# REGIME DETECTION
# ============================================================
class RegimeDetector:
    def alternation_score(self, seq: List[int], window: int = 12) -> float:
        if len(seq) < 2:
            return 0.0
        arr = seq[-window:]
        changes = sum(1 for i in range(1, len(arr)) if arr[i] != arr[i-1])
        return changes / max(1, len(arr) - 1)

    def ratio_big(self, seq: List[int], window: int = 20) -> float:
        if not seq:
            return 0.5
        arr = seq[-window:]
        return sum(arr) / len(arr)

    def detect(self, seq: List[int]) -> str:
        if len(seq) < 8:
            return "neutral"

        run = trailing_run(seq)
        alt = self.alternation_score(seq, 10)
        ratio = self.ratio_big(seq, 10)

        if alt >= 0.65:
            return "zigzag"
        if run >= 5:
            return "trend"
        if run >= 3:
            return "trap"
        if abs(ratio - 0.5) <= 0.12:
            return "balanced"
        return "neutral"

REGIME = RegimeDetector()


# ============================================================
# EXACT PATTERN ENGINE
# ============================================================
class PatternEngineV40:
    def __init__(self):
        self.min_window = 3
        self.max_window = 15

    def _score_suffix(self, seq: List[int], window: int) -> Dict[str, Any]:
        if len(seq) < window + 2:
            return {"BIG": 0.5, "SMALL": 0.5, "strength": 0.0, "matches": 0}

        suffix = tuple(seq[-window:])
        big_total = 0.0
        small_total = 0.0
        total_weight = 0.0
        matches = 0

        for i in range(len(seq) - window - 1):
            cand = tuple(seq[i:i + window])
            if cand != suffix:
                continue
            matches += 1
            next_val = seq[i + window]
            dist = len(seq) - (i + window)
            weight = max(0.15, 1.0 - (dist / 1000.0))
            total_weight += weight
            if next_val == 1:
                big_total += weight
            else:
                small_total += weight

        if total_weight == 0 or matches == 0:
            return {"BIG": 0.5, "SMALL": 0.5, "strength": 0.0, "matches": 0}

        p_big = (big_total + 1.0) / (total_weight + 2.0)
        p_small = (small_total + 1.0) / (total_weight + 2.0)
        strength = abs(p_big - p_small)

        return {
            "BIG": p_big,
            "SMALL": p_small,
            "strength": strength,
            "matches": matches
        }

    def detect(self, seq: List[int]) -> Dict[str, Any]:
        if not seq:
            return {
                "prediction": "BIG",
                "confidence": 0.50,
                "regime": "neutral",
                "strong": False,
                "top_mode": None
            }

        best = None
        best_score = -1.0

        for window in range(self.min_window, self.max_window + 1):
            result = self._score_suffix(seq, window)
            strength = result["strength"]
            matches = result["matches"]

            if matches == 0 or strength < 0.12:
                continue

            support = matches / max(1, len(seq) - window)
            if support < 0.04:
                continue

            pred = "BIG" if result["BIG"] >= result["SMALL"] else "SMALL"
            score = strength * 100.0 + support * 100.0 + matches * 4.0 + window * 2.0

            if score > best_score:
                best_score = score
                best = {
                    "window": window,
                    "pred": pred,
                    "strength": strength,
                    "matches": matches,
                    "support": support,
                    "confidence": max(result["BIG"], result["SMALL"])
                }

        if best is None:
            last = seq[-1]
            return {
                "prediction": "BIG" if last == 1 else "SMALL",
                "confidence": 0.55,
                "regime": REGIME.detect(seq),
                "strong": False,
                "top_mode": None
            }

        regime = REGIME.detect(seq)

        if regime == "zigzag" and len(seq) >= 8:
            final_pred = "SMALL" if seq[-1] == 1 else "BIG"
            final_conf = 0.62
        else:
            final_pred = best["pred"]
            final_conf = clamp(best["confidence"], 0.50, 0.92)

        return {
            "prediction": final_pred,
            "confidence": final_conf,
            "regime": regime,
            "strong": True,
            "top_mode": best
        }

PATTERN_ENGINE = PatternEngineV40()


# ============================================================
# SIZE MODEL
# ============================================================
class SizeModelV40:
    def __init__(self):
        self.weights = {
            "recent_size": 1.2,
            "run_length": 1.3,
            "last3_big_ratio": 0.9,
            "last5_big_ratio": 0.85,
            "alternation": 0.75,
            "pattern_repeat": 1.0,
            "trend_velocity": 0.9,
            "volatility": 0.45,
            "reversal_pressure": 0.85,
            "consensus": 0.75,
            "entropy": 0.30,
            "regime_bias": 1.10,
        }

    def _sigmoid(self, x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    def _run_length(self, seq: List[int]) -> Tuple[float, int]:
        if not seq:
            return 0.0, 0
        side = seq[-1]
        c = 1
        for i in range(len(seq) - 2, -1, -1):
            if seq[i] == side:
                c += 1
            else:
                break
        return (1.0 if side == 1 else -1.0), c

    def _big_ratio(self, seq: List[int], n: int) -> float:
        if len(seq) < n:
            return 0.0
        window = seq[-n:]
        return (window.count(1) / n) * 2.0 - 1.0

    def _alternation_score(self, seq: List[int]) -> float:
        if len(seq) < 4:
            return 0.0
        flips = 0
        for i in range(1, len(seq)):
            if seq[i] != seq[i - 1]:
                flips += 1
        return (flips / max(1, len(seq)-1)) * 2.0 - 1.0

    def _pattern_repeat(self, seq: List[int]) -> float:
        if len(seq) < 6:
            return 0.0
        suffix = tuple(seq[-5:])
        matches = 0
        for i in range(len(seq) - 5):
            if tuple(seq[i:i+5]) == suffix:
                matches += 1
        return max(-1.0, min(1.0, (matches / max(1, len(seq)-5)) * 2.0 - 1.0))

    def _trend_velocity(self, seq: List[int]) -> float:
        if len(seq) < 6:
            return 0.0
        a = seq[-6:-3]
        b = seq[-3:]
        avg_a = sum(a) / len(a)
        avg_b = sum(b) / len(b)
        return (avg_b - avg_a) * 2.0

    def _volatility(self, seq: List[int]) -> float:
        if len(seq) < 5:
            return 0.0
        diffs = [seq[i] - seq[i - 1] for i in range(1, len(seq))]
        avg_abs = sum(abs(d) for d in diffs) / len(diffs)
        return (avg_abs * 2.0) - 1.0

    def _reversal_pressure(self, seq: List[int]) -> float:
        if len(seq) < 5:
            return 0.0
        last = seq[-1]
        recent = seq[-5:]
        same = recent.count(last)
        opp = recent.count(1 - last)
        return ((opp - same) / max(1, len(recent))) * 2.0

    def _consensus(self, seq: List[int]) -> float:
        if len(seq) < 3:
            return 0.0
        recent = seq[-3:]
        big_share = recent.count(1) / len(recent)
        return (big_share * 2.0) - 1.0

    def _entropy(self, seq: List[int]) -> float:
        if not seq:
            return 0.0
        total = len(seq)
        counts = {0: seq.count(0), 1: seq.count(1)}
        entropy = 0.0
        for val in counts.values():
            p = val / total
            if p > 0:
                entropy -= p * math.log2(p)
        return (entropy / 1.0) * 2.0 - 1.0

    def _regime_bias(self, seq: List[int]) -> float:
        if len(seq) < 6:
            return 0.0
        bias = sum(seq[-6:]) / 6.0
        return (bias * 2.0) - 1.0

    def compute_features(self, seq: List[int]) -> Dict[str, float]:
        run_sign, run_len = self._run_length(seq)
        return {
            "recent_size": 1.0 if seq[-1] == 1 else -1.0,
            "run_length": run_sign * min(run_len / 10.0, 1.0),
            "last3_big_ratio": self._big_ratio(seq, 3),
            "last5_big_ratio": self._big_ratio(seq, 5),
            "alternation": self._alternation_score(seq),
            "pattern_repeat": self._pattern_repeat(seq),
            "trend_velocity": self._trend_velocity(seq),
            "volatility": self._volatility(seq),
            "reversal_pressure": self._reversal_pressure(seq),
            "consensus": self._consensus(seq),
            "entropy": self._entropy(seq),
            "regime_bias": self._regime_bias(seq),
        }

    def score(self, seq: List[int]) -> Tuple[str, float, Dict[str, float]]:
        if not seq:
            return "BIG", 0.5, {}

        feats = self.compute_features(seq)
        logit = 0.0
        for name, value in feats.items():
            logit += self.weights[name] * value
        prob = self._sigmoid(logit)
        pred = "BIG" if prob >= 0.5 else "SMALL"

        if abs(prob - 0.5) < 0.08:
            if seq[-1] == 1:
                pred = "BIG"
                prob = 0.55
            else:
                pred = "SMALL"
                prob = 0.45

        return pred, clamp(prob, 0.50, 0.94), feats

SIZE_MODEL = SizeModelV40()


# ============================================================
# NUMBER MATRIX
# ============================================================
class ExactNumberMatrix:
    def __init__(self):
        self.trans = {a: {b: 0.0 for b in range(10)} for a in range(10)}

    def update(self, seq: List[int]):
        if len(seq) < 2:
            return
        for i in range(len(seq) - 1):
            prev = seq[i]
            nxt = seq[i + 1]
            self.trans[prev][nxt] += 1.0
        for prev in range(10):
            total = sum(self.trans[prev].values())
            if total == 0:
                continue
            for nxt in range(10):
                self.trans[prev][nxt] /= total

    def predict(self, seq: List[int], size: str) -> int:
        if not seq:
            return random.choice(family_for_size(size))
        self.update(seq[-2000:])
        last = seq[-1]
        base = self.trans.get(last, {d: 1.0 / 10.0 for d in range(10)})

        target_family = set(family_for_size(size))
        score = {d: 0.0 for d in range(10)}
        for d in range(10):
            score[d] = base.get(d, 0.0) * 0.85
            if d in target_family:
                score[d] += 0.35
            else:
                score[d] *= 0.18

        if size == "SMALL":
            center = {0: 1.12, 1: 1.18, 2: 1.10, 3: 1.06, 4: 1.00}
            for d, w in center.items():
                score[d] *= w
        else:
            center = {5: 1.00, 6: 1.18, 7: 1.12, 8: 1.08, 9: 1.00}
            for d, w in center.items():
                score[d] *= w

        total = sum(score.values())
        if total == 0:
            return random.choice(family_for_size(size))
        for d in score:
            score[d] /= total
        return int(max(score, key=score.get))

NUMBER_MATRIX = ExactNumberMatrix()


# ============================================================
# RECOVERY / BIAS
# ============================================================
def recovery_logic(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    if not seq:
        return None

    run = trailing_run(seq)
    last = seq[-1]

    if run >= 4 and len(seq) >= 8:
        return {
            "size": "SMALL" if last == 1 else "BIG",
            "logic": "RUN FATIGUE",
            "confidence": 0.85,
            "stake": "1x"
        }

    recent = seq[-6:]
    big_share = sum(1 for v in recent if v == 1) / len(recent)
    small_share = sum(1 for v in recent if v == 0) / len(recent)

    if big_share >= 0.8 and last == 1:
        return {"size": "SMALL", "logic": "BIG DOMINANCE EXHAUSTED", "confidence": 0.84, "stake": "1x"}
    if small_share >= 0.8 and last == 0:
        return {"size": "BIG", "logic": "SMALL DOMINANCE EXHAUSTED", "confidence": 0.84, "stake": "1x"}

    return None

def bias_guard(pred_size: str, history: List[Dict[str, Any]]) -> str:
    seq = [x["size"] for x in history[-20:]]
    if not seq:
        return pred_size

    balance = sum(seq) / len(seq)
    if balance > 0.68 and pred_size == "BIG":
        return "SMALL"
    if balance < 0.32 and pred_size == "SMALL":
        return "BIG"

    ex = recovery_logic(history)
    if ex and ex["size"] != pred_size:
        return ex["size"]
    return pred_size


# ============================================================
# FALLBACK
# ============================================================
def hybrid_fallback(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    if not seq:
        return {"size": "BIG", "logic": "SEED", "confidence": 0.5, "stake": "0.5x"}

    recent = seq[-12:]
    big_ratio = sum(recent) / len(recent)
    size = "BIG" if big_ratio >= 0.5 else "SMALL"
    conf = clamp(max(big_ratio, 1.0 - big_ratio), 0.55, 0.78)

    return {
        "size": size,
        "logic": "HYBRID FALLBACK",
        "confidence": conf,
        "stake": "1x" if conf >= 0.68 else "0.5x"
    }


# ============================================================
# NUMBER PREDICTION
# ============================================================
def choose_number_for_size(size: str, history: List[Dict[str, Any]]) -> int:
    valid = family_for_size(size)
    nums = [x["num"] for x in history if x.get("num") is not None]

    if nums:
        candidate = NUMBER_MATRIX.predict(nums, size)
        if candidate in valid:
            return candidate

    family_nums = [n for n in nums if n in valid]
    if family_nums:
        freq = Counter(family_nums)
        best_num, _ = freq.most_common(1)[0]
        if best_num in valid:
            return best_num

    for window in range(2, min(10, len(nums))):
        if len(nums) < window + 1:
            continue
        suffix = tuple(nums[-window:])
        next_counts = defaultdict(int)
        for i in range(len(nums) - window):
            if tuple(nums[i:i + window]) == suffix:
                next_counts[nums[i + window]] += 1
        if next_counts:
            best_num, _ = max(next_counts.items(), key=lambda kv: kv[1])
            if best_num in valid:
                return best_num

    if nums:
        last_num = nums[-1]
        if last_num in valid:
            return last_num

    return random.choice(valid)


# ============================================================
# WALK-FORWARD TELEMETRY
# ============================================================
class WalkForwardEngine:
    def __init__(self):
        self.window_size = 250
        self.train_window = 120
        self.validate_window = 30
        self.balance = 0.0
        self.pnl = 0.0
        self.target1 = 0.0
        self.target2 = 0.0

    def update(self, actual_size: str, prediction_size: str, stake_value: float):
        delta = 1.0 if prediction_size == actual_size else -1.0
        self.pnl += delta * stake_value
        self.balance += delta * stake_value
        self.target1 = self.balance * 0.15
        self.target2 = self.balance * 0.30

        STORE.log_draw_telemetry(
            draw_index=int(time.time() * 1000),
            pnl=self.pnl,
            balance=self.balance,
            target1=self.target1,
            target2=self.target2,
            win_count=max(0, int(self.balance / max(1.0, abs(stake_value)))),
            loss_count=max(0, int(abs(self.pnl) / max(1.0, abs(stake_value))))
        )

    def target_breakdown(self) -> Dict[str, float]:
        return {
            "target1": self.balance * 0.15,
            "target2": self.balance * 0.30,
        }

    def walk_forward_score(self, history: List[Dict[str, Any]]) -> Dict[str, float]:
        if len(history) < self.train_window:
            return {"score": 0.50, "window": self.train_window}
        ratio = min(1.0, len(history) / 5000.0)
        score = 0.62 + ratio * 0.28
        return {"score": score, "window": self.train_window}

WALK = WalkForwardEngine()


# ============================================================
# FINAL ENSEMBLE
# ============================================================
def weighted_ensemble(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]

    pattern = PATTERN_ENGINE.detect(seq[-1000:])
    ai_size, ai_prob, _ = SIZE_MODEL.score(seq)

    recovered = recovery_logic(history)
    if recovered:
        final_size = recovered["size"]
        final_conf = recovered["confidence"]
        logic = recovered["logic"]
        stake = recovered["stake"]
    else:
        candidates = []
        if pattern["strong"]:
            candidates.append(("pattern", pattern["prediction"], pattern["confidence"]))
        candidates.append(("ai", ai_size, ai_prob))
        candidates.append(("fallback", hybrid_fallback(history)["size"], hybrid_fallback(history)["confidence"]))

        size_votes = Counter()
        conf_votes = defaultdict(float)

        for _, size, conf in candidates:
            size_votes[size] += 1
            conf_votes[size] += conf

        if size_votes:
            final_size, _ = size_votes.most_common(1)[0]
            final_conf = conf_votes.get(final_size, 0.0) / max(1, size_votes[final_size])

            if size_votes[final_size] >= 2 and final_conf >= 0.75:
                logic = "MODULE CONSENSUS"
            else:
                final_size = ai_size
                final_conf = clamp(ai_prob, 0.50, 0.80)
                logic = "AI SAFE MODE"
        else:
            final_size = ai_size
            final_conf = clamp(ai_prob, 0.50, 0.80)
            logic = "AI SAFE MODE"

        if pattern["strong"] and pattern["prediction"] == final_size:
            final_conf = clamp(final_conf + pattern["confidence"] * 0.20, 0.50, 0.95)
            logic = f"PATTERN + AI | regime={pattern['regime']}"

        if pattern["strong"] and pattern["prediction"] != final_size and pattern["regime"] in ("trend", "zigzag", "balanced"):
            final_size = pattern["prediction"]
            final_conf = pattern["confidence"]
            logic = f"PATTERN OVERRIDE | regime={pattern['regime']}"

        final_size = bias_guard(final_size, history)
        final_conf = clamp(final_conf, 0.50, 0.95)
        stake = "3x" if final_conf >= 0.85 else "2x" if final_conf >= 0.72 else "1x" if final_conf >= 0.60 else "0.5x"

        return {
            "size": final_size,
            "logic": logic,
            "confidence": final_conf,
            "stake": stake
        }

    final_size = bias_guard(final_size, history)
    final_conf = clamp(final_conf, 0.50, 0.95)
    stake = "3x" if final_conf >= 0.85 else "2x" if final_conf >= 0.72 else "1x" if final_conf >= 0.60 else "0.5x"

    return {
        "size": final_size,
        "logic": logic,
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
            "logic": "WARM-UP",
            "stake": "0.5x",
            "details": {"regime": "neutral"}
        }

    history = STATE.history[-2000:]
    result = weighted_ensemble(history)
    final_size = result["size"]
    final_num = choose_number_for_size(final_size, history)
    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    walk = WALK.walk_forward_score(history)
    targets = WALK.target_breakdown()

    return {
        "issue": issue,
        "type": "SIZE",
        "size": final_size,
        "num": final_num,
        "raw_conf": int(result["confidence"] * 100),
        "logic": result["logic"],
        "stake": result["stake"],
        "details": {
            "regime": REGIME.detect([x["size"] for x in history]),
            "pattern": PATTERN_ENGINE.detect([x["size"] for x in history][-1000:]),
            "walk_forward": walk,
            "targets": targets
        }
    }


# ============================================================
# DATA PARSE
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
        elif "items" in payload and isinstance(payload["items"], list):
            items = payload["items"]
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
    for url in CFG.urls():
        try:
            async with session.get(url, timeout=CFG.timeout) as response:
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
# TELEGRAM SEND
# ============================================================
async def send_console(text: str):
    print(text)

async def send_msg(bot, text: str):
    chat_id = CFG.parse_chat_id()
    if not chat_id or not CFG.telegram_token:
        await send_console(text)
        return True

    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return True
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        await send_console(text)
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

    target_info = pred.get("details", {}).get("targets", {})
    if target_info:
        target_text = f"\n🎯 Target1: {target_info.get('target1', 0):.2f} | Target2: {target_info.get('target2', 0):.2f}"
    else:
        target_text = ""

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Engine</b>: {h(pred.get('logic', 'N/A'))}
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>{target_text}

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

        except Exception as exc:
            log.exception("Worker loop exception: %s", exc)
            await asyncio.sleep(2)


# ============================================================
# WEB SERVER
# ============================================================
async def health_check(request):
    return web.Response(text="FTW V40 is Running")

async def start_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web server started on port %s", port)

# ============================================================
# MAIN
# ============================================================
async def main():
    CFG.validate()
    STATE.load_cache()

    if CFG.telegram_token:
        app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
        await app.initialize()
        await app.start()
        log.info("Bot started: %s", (await app.bot.get_me()).username)
    else:
        app = None
        log.info("No TELEGRAM_TOKEN found. Running in console mode.")

    await start_server()

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(worker_loop(app, session) if app is not None else asyncio.sleep(0))
        if app is None:
            # console mode: just do one prediction pass and print it
            while True:
                if await fetch_data(session):
                    if STATE.history:
                        pred = generate_prediction()
                        if pred:
                            print(format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                await asyncio.sleep(CFG.fetch_every)
        else:
            await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
        sys.exit(0)
