"""
FTW AI V31 FULL CODE
====================
- Strongest pattern detector
- Pattern length: 5..15
- Match against recent 1000 result history
- Super advanced trend shift detector
- Stronger number prediction
- Telegram bot
- SQLite tracking
- API fetch loop
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
    db_path: str = os.environ.get("DB_PATH", "ftw_ai_v31_full.db")
    cache_file: str = "ftw_ai_v31_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW AI V31"
    min_history: int = 8
    recent_limit: int = 12
    max_history_window: int = 1000

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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_ai_v31_full.log", maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_ai_v31")

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

def trailing_run(seq: List[int]) -> int:
    if not seq:
        return 0
    last = seq[-1]
    r = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            r += 1
        else:
            break
    return r

def family_for_size(size: str) -> List[int]:
    return [5, 6, 7, 8, 9] if size == "BIG" else [0, 1, 2, 3, 4]

# ============================================================
# PATTERN + TREND SHIFT DETECTOR
# ============================================================
class PatternDetectorV31:
    def __init__(self):
        self.min_window = 5
        self.max_window = 15
        self.recent_limit = 1000

    def trailing_run(self, seq: List[int]) -> int:
        return trailing_run(seq)

    def last_n(self, seq: List[int], n: int) -> List[int]:
        return seq[-n:] if len(seq) >= n else seq

    def ratio_big(self, seq: List[int], n: int) -> float:
        if len(seq) < n:
            return 0.0
        window = seq[-n:]
        return (window.count(1) / n) * 2.0 - 1.0

    def alternation_score(self, seq: List[int]) -> float:
        if len(seq) < 4:
            return 0.0
        flips = 0
        for i in range(1, len(seq)):
            if seq[i] != seq[i - 1]:
                flips += 1
        return (flips / max(1, len(seq) - 1)) * 2.0 - 1.0

    def volatility(self, seq: List[int]) -> float:
        if len(seq) < 5:
            return 0.0
        diffs = [seq[i] - seq[i-1] for i in range(1, len(seq))]
        avg_abs = sum(abs(d) for d in diffs) / len(diffs)
        return (avg_abs * 2.0) - 1.0

    def trend_velocity(self, seq: List[int]) -> float:
        if len(seq) < 6:
            return 0.0
        a = seq[-6:-3]
        b = seq[-3:]
        avg_a = sum(a) / len(a)
        avg_b = sum(b) / len(b)
        return (avg_b - avg_a) * 2.0

    def reversal_pressure(self, seq: List[int]) -> float:
        if len(seq) < 5:
            return 0.0
        last = seq[-1]
        recent = seq[-5:]
        same = recent.count(last)
        opp = recent.count(1 - last)
        return ((opp - same) / max(1, len(recent))) * 2.0

    def exact_pattern_search(self, seq: List[int], window: int) -> Dict[str, float]:
        if len(seq) < window + 2:
            return {"BIG": 0.5, "SMALL": 0.5, "strength": 0.0}

        suffix = tuple(seq[-window:])
        big = 0.0
        small = 0.0
        total = 0.0

        # Search recent 1000 only, but in local seq already limited
        for i in range(len(seq) - window - 1):
            candidate = tuple(seq[i:i + window])
            if candidate != suffix:
                continue

            next_val = seq[i + window]
            distance = len(seq) - (i + window)
            weight = max(0.15, 1.0 - (distance / 1000.0))
            total += weight
            if next_val == 1:
                big += weight
            else:
                small += weight

        if total == 0:
            return {"BIG": 0.5, "SMALL": 0.5, "strength": 0.0}

        p_big = (big + 1.0) / (total + 2.0)
        p_small = (small + 1.0) / (total + 2.0)

        return {
            "BIG": p_big,
            "SMALL": p_small,
            "strength": abs(p_big - p_small),
        }

    def detect_super_pattern(self, seq: List[int]) -> Dict[str, Any]:
        best = {"window": 0, "pred": "BIG", "confidence": 0.0, "score": 0.0, "details": {}}

        for window in range(self.min_window, self.max_window + 1):
            if len(seq) < window + 2:
                continue

            result = self.exact_pattern_search(seq, window)
            p_big = result["BIG"]
            p_small = result["SMALL"]
            strength = result["strength"]

            if strength <= 0.0:
                continue

            pred = "BIG" if p_big >= p_small else "SMALL"
            confidence = max(p_big, p_small)
            score = strength * 100.0 + (window * 2.0)

            if score > best["score"]:
                best = {
                    "window": window,
                    "pred": pred,
                    "confidence": confidence,
                    "score": score,
                    "details": {
                        "p_big": p_big,
                        "p_small": p_small,
                        "strength": strength,
                    }
                }

        return best

    def detect_regime(self, seq: List[int]) -> str:
        if len(seq) < 8:
            return "neutral"

        run = self.trailing_run(seq)
        alt = self.alternation_score(seq)
        vel = self.trend_velocity(seq)
        last3 = seq[-3:]
        last5 = seq[-5:]

        # trend up/down
        if run >= 4 and seq[-1] == 1:
            return "trend_up"
        if run >= 4 and seq[-1] == 0:
            return "trend_down"

        # very strong alternation
        if alt > 0.45:
            return "zigzag"

        # switch detection
        if vel > 0.75 and sum(last3) >= 2:
            return "shift_up"
        if vel < -0.75 and sum(last3) <= 1:
            return "shift_down"

        # if last 5 is balanced
        if abs(sum(last5) - 2.5) < 1.0:
            return "balanced"

        return "neutral"

    def detect_trend_shift(self, seq: List[int]) -> Dict[str, Any]:
        if len(seq) < 12:
            return {"direction": "neutral", "strength": 0.0, "confidence": 0.0}

        # compare recent 3 vs previous 3
        recent3 = seq[-3:]
        prev3 = seq[-6:-3]
        recent5 = seq[-5:]
        prev5 = seq[-10:-5]

        recent_big = sum(recent3)
        prev_big = sum(prev3)
        recent_big5 = sum(recent5)
        prev_big5 = sum(prev5)

        delta = recent_big - prev_big
        delta5 = recent_big5 - prev_big5

        if delta > 0 and delta5 > 0:
            direction = "up"
            strength = abs(delta) + abs(delta5) * 0.5
        elif delta < 0 and delta5 < 0:
            direction = "down"
            strength = abs(delta) + abs(delta5) * 0.5
        else:
            direction = "neutral"
            strength = 0.0

        # detect full reversal / shift
        if self.alternation_score(seq) > 0.45:
            direction = "zigzag"
            strength += 1.5

        confidence = clamp((abs(strength) / 8.0), 0.0, 0.95)
        return {
            "direction": direction,
            "strength": strength,
            "confidence": confidence,
        }

    def detect(self, seq: List[int]) -> Dict[str, Any]:
        if not seq:
            return {
                "prediction": "BIG",
                "confidence": 0.50,
                "regime": "neutral",
                "pattern": {"window": 0, "pred": "BIG", "confidence": 0.0},
                "trend_shift": {"direction": "neutral", "strength": 0.0, "confidence": 0.0},
                "signals": {}
            }

        # exact pattern search
        pattern = self.detect_super_pattern(seq)
        regime = self.detect_regime(seq)
        trend_shift = self.detect_trend_shift(seq)

        # base scoring
        score_map = {"BIG": 0.0, "SMALL": 0.0}

        # pattern signal
        if pattern["pred"] == "BIG":
            score_map["BIG"] += pattern["confidence"] * 1.9
        else:
            score_map["SMALL"] += pattern["confidence"] * 1.9

        # run signal
        run = self.trailing_run(seq)
        last = seq[-1]
        if run >= 3:
            if last == 1:
                score_map["BIG"] += 0.9 + min(run * 0.15, 1.1)
            else:
                score_map["SMALL"] += 0.9 + min(run * 0.15, 1.1)

        # alternation
        alt = self.alternation_score(seq)
        if alt > 0.25:
            if seq[-1] == 1:
                score_map["SMALL"] += 0.8 + alt
            else:
                score_map["BIG"] += 0.8 + abs(alt)
        elif alt < -0.25:
            if seq[-1] == 1:
                score_map["BIG"] += 0.8 + abs(alt)
            else:
                score_map["SMALL"] += 0.8 + abs(alt)

        # trend shift
        if trend_shift["direction"] == "up":
            score_map["BIG"] += 0.7 + trend_shift["confidence"] * 1.5
        elif trend_shift["direction"] == "down":
            score_map["SMALL"] += 0.7 + trend_shift["confidence"] * 1.5
        elif trend_shift["direction"] == "zigzag":
            # in zigzag, don't chase recent side blindly
            if seq[-1] == 1:
                score_map["SMALL"] += 0.8
            else:
                score_map["BIG"] += 0.8

        # reversal pressure
        rev = self.reversal_pressure(seq)
        if rev > 0.5:
            score_map["SMALL"] += 0.3 + rev * 0.4
        elif rev < -0.5:
            score_map["BIG"] += 0.3 + abs(rev) * 0.4

        # final decision
        final_pred = "BIG" if score_map["BIG"] >= score_map["SMALL"] else "SMALL"
        total_score = score_map["BIG"] + score_map["SMALL"]
        confidence = 0.50 if total_score == 0 else abs(score_map["BIG"] - score_map["SMALL"]) / total_score

        # strong trap protection:
        # if there is a long run and the pattern contradicts, preserve current side
        if run >= 4 and pattern["confidence"] < 0.60 and trend_shift["direction"] in ("neutral", "zigzag"):
            if seq[-1] == 1:
                final_pred = "BIG"
            else:
                final_pred = "SMALL"

        confidence = clamp(confidence + 0.42, 0.50, 0.95)

        return {
            "prediction": final_pred,
            "confidence": confidence,
            "regime": regime,
            "pattern": pattern,
            "trend_shift": trend_shift,
            "signals": {
                "score_map": score_map,
                "run": run,
                "alternation": alt,
                "reversal_pressure": rev,
            }
        }

PATTERN_DETECTOR = PatternDetectorV31()

# ============================================================
# SIZE SCORER (AI)
# ============================================================
class AdvancedSizeScorer:
    def __init__(self):
        self.weights = {
            "recent_size": 1.25,
            "run_length": 1.35,
            "last3_big_ratio": 0.95,
            "last5_big_ratio": 0.90,
            "alternation": 0.85,
            "pattern_repeat": 1.10,
            "trend_velocity": 0.95,
            "volatility": 0.45,
            "reversal_pressure": 0.90,
            "consensus": 0.80,
            "entropy": 0.30,
            "regime_bias": 1.20,
        }

    def _sigmoid(self, x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    def _run_length(self, seq: List[int]) -> Tuple[float, int]:
        if not seq:
            return 0.0, 0
        side = seq[-1]
        r = 1
        for i in range(len(seq) - 2, -1, -1):
            if seq[i] == side:
                r += 1
            else:
                break
        return (1.0 if side == 1 else -1.0), r

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
        diffs = [seq[i] - seq[i-1] for i in range(1, len(seq))]
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
        side_bias = sum(seq[-6:]) / 6.0
        return (side_bias * 2.0) - 1.0

    def compute_features(self, seq: List[int]) -> Dict[str, float]:
        run_sign, run_len = self._run_length(seq)
        return {
            "recent_size": 1.0 if seq[-1] == 1 else -1.0 if seq[-1] == 0 else 0.0,
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
        features = self.compute_features(seq)
        logit = 0.0
        for name, value in features.items():
            logit += self.weights[name] * value

        prob = self._sigmoid(logit)
        label = "BIG" if prob >= 0.5 else "SMALL"

        # Extra protection against false trend chasing
        run_sign, run_len = self._run_length(seq)
        if run_len >= 3 and abs(prob - 0.5) < 0.12:
            if run_sign > 0:
                label = "BIG"
                prob = 0.65
            else:
                label = "SMALL"
                prob = 0.35

        return label, clamp(prob, 0.50, 0.95), features

SIZE_SCORER = AdvancedSizeScorer()

# ============================================================
# ADVANCED NUMBER PREDICTOR
# ============================================================
class AdvancedNumberPredictor:
    def __init__(self):
        pass

    def _digit_distribution(self, seq: List[int]) -> Dict[int, float]:
        if not seq:
            return {d: 1/10 for d in range(10)}
        counts = Counter(seq)
        total = len(seq)
        dist = {d: 0.0 for d in range(10)}
        for d in range(10):
            dist[d] = counts.get(d, 0) / total

        # recency boost
        for idx, d in enumerate(reversed(seq[-25:])):
            weight = 1.0 + (idx / 25.0)
            dist[d] += 0.05 * weight

        s = sum(dist.values())
        if s == 0:
            return {d: 1/10 for d in range(10)}
        for d in dist:
            dist[d] /= s
        return dist

    def _suffix_pattern_distribution(self, seq: List[int]) -> Dict[int, float]:
        dist = {d: 0.0 for d in range(10)}
        if len(seq) < 3:
            return {d: 1/10 for d in range(10)}

        for window in range(2, min(8, len(seq))):
            suffix = tuple(seq[-window:])
            count = 0
            for i in range(len(seq) - window):
                if tuple(seq[i:i + window]) == suffix:
                    nxt = seq[i + window]
                    count += 1
                    dist[nxt] += 1.0 / (window * 1.25)

        total = sum(dist.values())
        if total == 0:
            return {d: 1/10 for d in range(10)}
        for d in dist:
            dist[d] /= total
        return dist

    def _family_bias(self, predicted_size: str) -> Dict[int, float]:
        target_family = set(family_for_size(predicted_size))
        dist = {d: 0.0 for d in range(10)}
        for d in range(10):
            if d in target_family:
                dist[d] = 1.0
            else:
                dist[d] = 0.10

        if predicted_size == "SMALL":
            center = {0: 1.0, 1: 1.05, 2: 1.15, 3: 1.10, 4: 1.0}
            for d, val in center.items():
                dist[d] *= val
        else:
            center = {5: 1.0, 6: 1.15, 7: 1.10, 8: 1.05, 9: 1.0}
            for d, val in center.items():
                dist[d] *= val

        total = sum(dist.values())
        for d in dist:
            dist[d] /= total
        return dist

    def _last_digit_bias(self, seq: List[int]) -> Dict[int, float]:
        dist = {d: 0.15 for d in range(10)}
        if not seq:
            return {d: 1/10 for d in range(10)}
        last = seq[-1]
        dist[last] += 1.5
        for d in range(10):
            if abs(d - last) <= 1:
                dist[d] += 0.35
        total = sum(dist.values())
        for d in dist:
            dist[d] /= total
        return dist

    def _parity_bias(self, seq: List[int]) -> Dict[int, float]:
        if not seq:
            return {d: 1/10 for d in range(10)}
        even = sum(1 for x in seq if x % 2 == 0)
        odd = sum(1 for x in seq if x % 2 != 0)
        dist = {d: 0.0 for d in range(10)}
        for d in range(10):
            if d % 2 == 0:
                dist[d] = 0.60 if even >= odd else 0.40
            else:
                dist[d] = 0.60 if odd > even else 0.40
        total = sum(dist.values())
        for d in dist:
            dist[d] /= total
        return dist

    def _trend_bias(self, seq: List[int]) -> Dict[int, float]:
        dist = {d: 0.0 for d in range(10)}
        if len(seq) < 3:
            return {d: 1/10 for d in range(10)}
        avg = sum(seq[-3:]) / 3
        for d in range(10):
            dist[d] = 0.5 + (0.15 * (1 - abs(d - avg) / 10.0))
        total = sum(dist.values())
        for d in dist:
            dist[d] /= total
        return dist

    def predict(self, seq: List[int], predicted_size: str) -> int:
        if not seq:
            return random.choice(family_for_size(predicted_size))

        base = self._digit_distribution(seq)
        suffix = self._suffix_pattern_distribution(seq)
        fam = self._family_bias(predicted_size)
        last_bias = self._last_digit_bias(seq)
        parity = self._parity_bias(seq)
        trend = self._trend_bias(seq)

        combined = {d: 0.0 for d in range(10)}
        for d in range(10):
            combined[d] = (
                base.get(d, 0.0) * 0.25 +
                suffix.get(d, 0.0) * 0.30 +
                fam.get(d, 0.0) * 0.25 +
                last_bias.get(d, 0.0) * 0.08 +
                parity.get(d, 0.0) * 0.06 +
                trend.get(d, 0.0) * 0.06
            )

        target_family = set(family_for_size(predicted_size))
        for d in range(10):
            if d not in target_family:
                combined[d] *= 0.30

        # reduce collapse to single digit
        winner = max(combined, key=combined.get)
        return int(winner)

NUMBER_MODEL = AdvancedNumberPredictor()

# ============================================================
# STATE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.last_prediction: Optional[Dict[str, Any]] = None

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
            row = c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
            return row is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, pred_type, size, num, details, stake
                FROM predictions
                WHERE status='PENDING'
            """).fetchall()

        for row in rows:
            issue, pred_type, p_size, p_num, details_json, stake = row
            act = history_by_issue.get(str(issue))
            if not act:
                continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            actual_num = act["num"]

            if p_size == actual_size:
                status = "JACK 🏆" if p_num == actual_num else "WIN ✅"
            else:
                status = "LOSS ❌"

            win = status != "LOSS ❌"

            with self._conn() as c:
                c.execute("""
                    UPDATE predictions
                    SET status=?, actual_num=?, actual_size=?
                    WHERE issue=?
                """, (status, actual_num, actual_size, issue))

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

STORE = Storage(CFG.db_path)

# ============================================================
# BUILD PREDICTION
# ============================================================
def build_prediction(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < CFG.min_history:
        size = "BIG" if random.random() >= 0.5 else "SMALL"
        num = random.choice(family_for_size(size))
        return {
            "issue": "1",
            "type": "SIZE",
            "size": size,
            "num": num,
            "raw_conf": 50,
            "logic": "WARM-UP",
            "stake": "0.5x",
            "details": {}
        }

    seq = [x["size"] for x in history]
    pattern_result = PATTERN_DETECTOR.detect(seq[-1000:])
    size_signal, prob, _ = SIZE_SCORER.score(seq)

    # Combine pattern + AI score
    if pattern_result["prediction"] == size_signal:
        final_size = size_signal
        confidence = clamp(prob + (pattern_result["confidence"] * 0.25), 0.50, 0.96)
    else:
        # if pattern contradicts AI, use pattern only if trend_shift strong
        if pattern_result["trend_shift"]["direction"] in ("up", "down", "zigzag") and pattern_result["confidence"] > 0.68:
            final_size = pattern_result["prediction"]
            confidence = pattern_result["confidence"]
        else:
            final_size = size_signal
            confidence = clamp(prob * 0.9, 0.50, 0.95)

    # Number prediction
    num_seq = [x["num"] for x in history if x.get("num") is not None]
    target_num = NUMBER_MODEL.predict(num_seq, final_size)

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    stake = "3x" if confidence >= 0.85 else "2x" if confidence >= 0.72 else "1x" if confidence >= 0.60 else "0.5x"

    return {
        "issue": issue,
        "type": "SIZE",
        "size": final_size,
        "num": target_num,
        "raw_conf": int(confidence * 100),
        "logic": f"PATTERN+AI | regime={pattern_result['regime']} | shift={pattern_result['trend_shift']['direction']}",
        "stake": stake,
        "details": {
            "size_probability": prob,
            "pattern": pattern_result,
            "regime": pattern_result["regime"],
            "trend_shift": pattern_result["trend_shift"]
        }
    }

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
            d = payload["data"]
            if isinstance(d.get("list"), list):
                items = d["list"]
            elif isinstance(d, list):
                items = d
            else:
                items = payload.get("result", [])
        else:
            items = payload.get("result", [])
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
        if num < 0 or num > 9:
            continue

        out.append({
            "issue": str(issue),
            "num": num,
            "size": 1 if num >= 5 else 0
        })

    out.sort(key=lambda x: int(x["issue"]))
    return out

# ============================================================
# API FETCH
# ============================================================
async def fetch_data(session):
    for url in CFG.api_urls():
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200:
                    continue
                data = await response.json(content_type=None)
                parsed = parse_history_payload(data)
                if parsed:
                    STATE.history = parsed[-CFG.max_history_window:]
                    STATE.save_cache()
                    return True
        except Exception as exc:
            log.warning("Fetch failed for %s: %s", url, exc)
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
    recent_block = "\n".join(recent_lines) if recent_lines else "➜ no recent results"

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Engine</b>: {h(pred.get('logic', 'N/A'))}
📈 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent</b>:
{recent_block}
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
                timeout=CFG.request_timeout,
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
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = build_prediction(STATE.history)
                    if pred:
                        STORE.save_prediction(pred)
                        await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                    last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception("Worker loop failed: %s", e)
            await asyncio.sleep(2)

# ============================================================
# WEB SERVER
# ============================================================
async def health_check(request):
    return web.Response(text="FTW AI V31 is Running")

async def main():
    CFG.validate()
    STATE.load_cache()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    await app.initialize()
    await app.start()
    me = await app.bot.get_me()
    log.info("Bot started: %s", me.username)

    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(worker_loop(app, session))
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
