"""
FTW V48 ADVANCED PREDICTOR
=========================
Strong multi-signal engine:
- exact section pattern matching (5..15)
- long-run exhaustion detection
- momentum decay reverse detection
- alternation / zigzag / balanced / trend regime detection
- anti-loss stop logic
- walk-forward trend score
- play-ready gating
- no fake "best timing" label
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
    db_path: str = os.environ.get("DB_PATH", "ftw_v48.db")
    cache_file: str = "ftw_v48_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    timeout: float = 15.0
    bot_name: str = "FTW V48 ENGINE"
    min_history: int = 8
    recent_limit: int = 12
    max_history_window: int = 1200

    def parse_chat_id(self):
        raw = (self.chat_id_raw or "").strip()
        if not raw:
            return None
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw

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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_v48.log", maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v48")


def h(x: Any) -> str:
    return html.escape(str(x))

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return safe_int(default, 0)

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

STORE = Storage(CFG.db_path)


# ============================================================
# SECTION PATTERN TRACKING
# ============================================================
def split_sections(seq: List[int]) -> List[Tuple[int, int]]:
    if not seq:
        return []

    sections: List[Tuple[int, int]] = []
    start = 0
    current = seq[0]

    for i in range(1, len(seq)):
        if seq[i] != current:
            sections.append((current, i - start))
            current = seq[i]
            start = i

    sections.append((current, len(seq) - start))
    return sections


def exact_section_match_score(history: List[Dict[str, Any]], max_section_count: int = 15) -> Optional[Dict[str, Any]]:
    seq = [int(x["size"]) for x in history if "size" in x and x["size"] in (0, 1)]
    if len(seq) < 6:
        return None

    current_signature = split_sections(seq)
    total_sections = len(current_signature)
    if total_sections < 2:
        return None

    candidates = []

    for section_count in range(5, min(max_section_count, total_sections) + 1):
        tail = current_signature[-section_count:]

        next_counts = defaultdict(int)
        matches = 0

        for i in range(total_sections - section_count):
            prev = current_signature[i:i + section_count]
            if prev != tail:
                continue
            matches += 1
            if i + section_count < total_sections:
                nxt = current_signature[i + section_count]
                next_counts[nxt] += 1

        if matches == 0:
            continue

        if section_count >= 10 and matches < 5:
            continue

        next_key, next_count = max(next_counts.items(), key=lambda kv: kv[1]) if next_counts else (None, 0)
        if next_key is None:
            continue

        total_follow = sum(next_counts.values())
        if total_follow == 0:
            continue

        win_rate = (next_count / total_follow) * 100.0
        edge = abs((next_count / total_follow) - 0.5) * 200.0

        valid = matches >= 5 and (edge >= 25.0 or win_rate >= 75.0)

        next_val, _ = next_key
        next_side = "BIG" if next_val == 1 else "SMALL"

        candidates.append({
            "pattern": tail,
            "section_count": section_count,
            "repeats": matches,
            "next_section": next_key,
            "next_side": next_side,
            "win_rate": round(win_rate, 2),
            "edge": round(edge, 2),
            "valid": valid,
            "score": (edge * 2.5) + (win_rate * 0.8) + (matches * 2.0),
        })

    if not candidates:
        return None

    valid_candidates = [c for c in candidates if c["valid"]]
    if valid_candidates:
        valid_candidates.sort(key=lambda x: (x["edge"], x["win_rate"], x["score"]), reverse=True)
        return valid_candidates[0]

    weak_candidates = [c for c in candidates if c["repeats"] >= 5 and c["edge"] < 25.0 and c["win_rate"] < 75.0]
    if weak_candidates:
        weak_candidates.sort(key=lambda x: (x["edge"], x["win_rate"]), reverse=True)
        chosen = weak_candidates[0]
        return {
            "pattern": chosen["pattern"],
            "section_count": chosen["section_count"],
            "repeats": chosen["repeats"],
            "next_section": chosen["next_section"],
            "next_side": "SMALL" if chosen["next_side"] == "BIG" else "BIG",
            "win_rate": chosen["win_rate"],
            "edge": chosen["edge"],
            "valid": False,
            "opposite_fallback": True,
            "score": chosen["score"]
        }

    return None


# ============================================================
# TREND / FATIGUE / MOMENTUM
# ============================================================
def dominant_side_signal(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 8:
        return None

    seq = [x["size"] for x in history]
    current = seq[-1]

    recent = seq[-12:]
    share_current = sum(1 for v in recent if v == current) / len(recent)

    last3 = seq[-3:]
    prev3 = seq[-6:-3]
    if len(last3) == 3 and len(prev3) == 3:
        last_same = sum(1 for v in last3 if v == current)
        prev_same = sum(1 for v in prev3 if v == current)
        momentum_decay = last_same < prev_same
    else:
        momentum_decay = False

    if share_current >= 0.75 and momentum_decay:
        opposite = "SMALL" if current == 1 else "BIG"
        return {
            "size": opposite,
            "logic": "RUN FATIGUE + DOMINANCE DECAY",
            "confidence": 0.92,
            "stake": "2x"
        }

    if trailing_run(seq) >= 6 and share_current >= 0.78:
        opposite = "SMALL" if current == 1 else "BIG"
        return {
            "size": opposite,
            "logic": "LONG TREND EXHAUSTION",
            "confidence": 0.88,
            "stake": "2x"
        }

    return None


def forced_recovery_override(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    if not seq:
        return None

    current = seq[-1]
    recent = seq[-6:]
    big_share = sum(1 for v in recent if v == 1) / len(recent)
    small_share = sum(1 for v in recent if v == 0) / len(recent)

    if trailing_run(seq) >= 5:
        if current == 1 and big_share >= 0.75:
            return {"size": "SMALL", "logic": "LONG BIG RUN FATIGUE", "confidence": 0.90, "stake": "2x"}
        if current == 0 and small_share >= 0.75:
            return {"size": "BIG", "logic": "LONG SMALL RUN FATIGUE", "confidence": 0.90, "stake": "2x"}

    if len(seq) >= 8:
        last3 = seq[-3:]
        prev3 = seq[-6:-3]
        if len(last3) == 3 and len(prev3) == 3:
            last_same = sum(1 for v in last3 if v == current)
            prev_same = sum(1 for v in prev3 if v == current)
            if last_same <= prev_same:
                opposite = "SMALL" if current == 1 else "BIG"
                return {"size": opposite, "logic": "MOMENTUM DECAY REVERSE", "confidence": 0.85, "stake": "2x"}

    return None


# ============================================================
# REGIME / ALTERNATION / MOMENTUM
# ============================================================
class RegimeEngine:
    def detect(self, seq: List[int]) -> str:
        if len(seq) < 4:
            return "neutral"

        run = trailing_run(seq)
        alt = 0.0
        for i in range(1, len(seq)):
            if seq[i] != seq[i - 1]:
                alt += 1
        alt = alt / max(1, len(seq) - 1)

        big_share = sum(1 for v in seq[-8:] if v == 1) / max(1, len(seq[-8:]))
        small_share = sum(1 for v in seq[-8:] if v == 0) / max(1, len(seq[-8:]))

        if alt >= 0.6:
            return "zigzag"
        if run >= 5:
            return "trend"
        if abs(big_share - 0.5) <= 0.12 and abs(small_share - 0.5) <= 0.12:
            return "balanced"
        if run >= 3:
            return "trap"
        return "neutral"

REGIME_ENGINE = RegimeEngine()


# ============================================================
# HYBRID MODEL
# ============================================================
class SizeModelV48:
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
        return (flips / max(1, len(seq) - 1)) * 2.0 - 1.0

    def _pattern_repeat(self, seq: List[int]) -> float:
        if len(seq) < 6:
            return 0.0
        suffix = tuple(seq[-5:])
        matches = 0
        for i in range(len(seq) - 5):
            if tuple(seq[i:i + 5]) == suffix:
                matches += 1
        return max(-1.0, min(1.0, (matches / max(1, len(seq) - 5)) * 2.0 - 1.0))

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

SIZE_MODEL = SizeModelV48()


# ============================================================
# EXACT NUMBER MATRIX
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
# HYBRID FALLBACK
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
# NUMBER CHOICE
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

    if nums:
        last_num = nums[-1]
        if last_num in valid:
            return last_num

    return random.choice(valid)


# ============================================================
# SIGNAL GATING
# ============================================================
def can_play(history: List[Dict[str, Any]], section_match: Optional[Dict[str, Any]], engine_signal: str) -> bool:
    if len(history) < 8:
        return False

    seq = [int(x["size"]) for x in history if "size" in x and x["size"] in (0, 1)]
    if not seq:
        return False

    run = trailing_run(seq)
    current = seq[-1]

    recent8 = seq[-8:]
    same_share = sum(1 for v in recent8 if v == current) / len(recent8)

    if len(seq) >= 6:
        last3 = seq[-3:]
        prev3 = seq[-6:-3]
        if len(last3) == 3 and len(prev3) == 3:
            last_same = sum(1 for v in last3 if v == current)
            prev_same = sum(1 for v in prev3 if v == current)
            if run >= 6 and same_share >= 0.8 and last_same <= 1 and prev_same >= 2:
                return True

    if section_match:
        if section_match.get("valid") and section_match.get("win_rate", 0) >= 75:
            return True

    if section_match and section_match.get("opposite_fallback") and section_match.get("win_rate", 0) < 75:
        return False

    return False


# ============================================================
# FINAL ENGINE
# ============================================================
def build_signal(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [int(x["size"]) for x in history if "size" in x and x["size"] in (0, 1)]
    if len(seq) < CFG.min_history:
        return {
            "size": "BIG" if random.random() >= 0.5 else "SMALL",
            "logic": "WARM-UP",
            "confidence": 0.50,
            "stake": "0.5x",
            "play_ready": False,
            "pattern_catalog": []
        }

    section_match = exact_section_match_score(history, max_section_count=15)
    pattern_catalog = []
    if section_match:
        pattern_catalog.append(section_match)

    run_reversal = forced_recovery_override(history)
    run_fatigue = dominant_side_signal(history)

    ai_size, ai_prob, _ = SIZE_MODEL.score(seq)
    fallback = hybrid_fallback(history)

    candidates = [
        ("ai", ai_size, ai_prob),
        ("fallback", fallback["size"], fallback["confidence"]),
    ]

    if section_match:
        if section_match.get("opposite_fallback"):
            candidates.append(("section", section_match["next_side"], 0.60))
        else:
            candidates.append(("section", section_match["next_side"], clamp(section_match["win_rate"] / 100.0, 0.50, 0.92)))

    if run_reversal:
        return {
            "size": run_reversal["size"],
            "logic": run_reversal["logic"],
            "confidence": run_reversal["confidence"],
            "stake": run_reversal["stake"],
            "play_ready": True,
            "pattern_catalog": pattern_catalog
        }

    if run_fatigue:
        return {
            "size": run_fatigue["size"],
            "logic": run_fatigue["logic"],
            "confidence": run_fatigue["confidence"],
            "stake": run_fatigue["stake"],
            "play_ready": True,
            "pattern_catalog": pattern_catalog
        }

    size_votes = Counter()
    conf_votes = defaultdict(float)
    for _, side, conf in candidates:
        size_votes[side] += 1
        conf_votes[side] += conf

    if size_votes:
        final_size, _ = size_votes.most_common(1)[0]
        final_conf = conf_votes.get(final_size, 0.0) / max(1, size_votes[final_size])
        logic = "MULTI-ENGINE CONSENSUS"
    else:
        final_size = ai_size
        final_conf = clamp(ai_prob, 0.50, 0.80)
        logic = "AI SAFE MODE"

    if section_match and section_match.get("next_side") and section_match.get("valid"):
        if section_match["next_side"] != final_size and section_match["win_rate"] >= 75:
            final_size = section_match["next_side"]
            final_conf = max(final_conf, section_match["win_rate"] / 100.0)
            logic = "SECTION OVERRIDE"

    final_size = bias_guard(final_size, history)
    final_conf = clamp(final_conf, 0.50, 0.95)
    stake = "3x" if final_conf >= 0.85 else "2x" if final_conf >= 0.72 else "1x" if final_conf >= 0.60 else "0.5x"

    can_ready = can_play(history, section_match, logic)
    if not can_ready:
        final_conf = min(final_conf, 0.65)
        stake = "0.5x"

    return {
        "size": final_size,
        "logic": logic,
        "confidence": final_conf,
        "stake": stake,
        "play_ready": can_ready,
        "pattern_catalog": pattern_catalog
    }


def bias_guard(pred_size: str, history: List[Dict[str, Any]]) -> str:
    seq = [x["size"] for x in history[-20:]]
    if not seq:
        return pred_size

    balance = sum(seq) / len(seq)
    if balance > 0.68 and pred_size == "BIG":
        return "SMALL"
    if balance < 0.32 and pred_size == "SMALL":
        return "BIG"

    rec = forced_recovery_override(history)
    if rec and rec["size"] != pred_size:
        return rec["size"]

    return pred_size


# ============================================================
# PREDICTION GENERATION
# ============================================================
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
            "details": {"regime": "neutral", "play_ready": False}
        }

    history = STATE.history[-2000:]
    engine = build_signal(history)
    final_size = engine["size"]
    final_num = choose_number_for_size(final_size, history)
    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)

    return {
        "issue": issue,
        "type": "SIZE",
        "size": final_size,
        "num": final_num,
        "raw_conf": int(engine["confidence"] * 100),
        "logic": engine["logic"],
        "stake": engine["stake"],
        "details": {
            "regime": REGIME_ENGINE.detect([x["size"] for x in history]),
            "pattern_catalog": engine.get("pattern_catalog", []),
            "play_ready": engine.get("play_ready", False),
            "run_fatigue": dominant_side_signal(history),
            "recovery": forced_recovery_override(history),
        }
    }


# ============================================================
# API PARSE + FETCH
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
    for url in CFG.api_urls.split(","):
        url = url.strip()
        if not url:
            continue
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

    pattern_catalog = pred.get("details", {}).get("pattern_catalog", [])
    pattern_lines = []
    for idx, item in enumerate(pattern_catalog[:3], start=1):
        if isinstance(item.get("pattern"), tuple):
            pat_str = " | ".join(f"{side}:{length}" for side, length in item["pattern"])
        else:
            pat_str = str(item.get("pattern", ""))
        pattern_lines.append(
            f"{idx}. Pattern: {pat_str} | Repeats: {item['repeats']} | Next: {item['next_side']} | WR: {item['win_rate']}% | Edge: {item['edge']}%"
        )

    pattern_block = "\n".join(pattern_lines) if pattern_lines else "No mined pattern found"

    play_ready = pred.get("details", {}).get("play_ready", False)
    play_state = "✅ PLAY READY" if play_ready else "⏸️ SKIP"

    return f"""
📌 <b>Period</b>: <code>{h(pred['issue'])}</code>
📊 <b>Target Size</b>: <b>{h(pred['size'])}</b>
🎯 <b>Target No</b>: <code>{h(pred['num'])}</code>

🧠 <b>Engine</b>: {h(pred.get('logic', 'N/A'))}
📊 <b>Confidence</b>: <b>{pred['raw_conf']}%</b>
💰 <b>Stake</b>: <b>{h(pred['stake'])}</b>
⏱️ <b>Signal</b>: {play_state}

━━ Section Pattern Catalog ━━
{pattern_block}

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
                    else:
                        STATE.loss_streak += 1
                        STATE.win_streak = 0

                    if app is not None:
                        await send_msg(app.bot, format_result(res))
                    else:
                        print(format_result(res))

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
                        if app is not None:
                            await send_msg(app.bot, format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                        else:
                            print(format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
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
    return web.Response(text="FTW V48 is Running")

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
        if app is not None:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()
        else:
            while True:
                if await fetch_data(session):
                    if STATE.history:
                        pred = generate_prediction()
                        if pred:
                            print(format_prediction(pred, STORE.recent_outcomes(CFG.recent_limit)))
                await asyncio.sleep(CFG.fetch_every)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
        sys.exit(0)
