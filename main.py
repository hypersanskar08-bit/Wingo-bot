# bot.py — FTW VIP PREDICTOR (ULTRA V5)
# Python 3.10+
# Dependency: pip install aiohttp

import asyncio
import contextlib
import html
import json
import logging
import math
import os
import random
import signal
import socket
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    token: str = os.getenv("TELEGRAM_TOKEN", "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo").strip()
    chat: str = os.getenv("CHAT_ID", "-1004485961768").strip()
    urls: tuple = tuple(
        x.strip()
        for x in os.getenv(
            "API_URLS",
            "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
        ).split(",")
        if x.strip()
    )
    db_path: str = os.getenv("DB_PATH", "predictor_v5.db")
    port: int = int(os.getenv("PORT", "8080"))
    fetch_every: float = float(os.getenv("FETCH_EVERY", "3"))
    timeout: float = 15.0
    max_history: int = 4000
    min_history: int = int(os.getenv("MIN_HISTORY", "150"))
    min_support: int = int(os.getenv("MIN_PATTERN_SUPPORT", "10"))
    min_edge: float = float(os.getenv("MIN_EDGE", "0.06"))
    cooldown_rounds: int = int(os.getenv("COOLDOWN_ROUNDS", "2"))
    loss_limit: int = 3
    learning_rate: float = 0.035
    meta_lr: float = 0.03
    decay: float = 0.985
    max_pattern: int = 10
    round_seconds: float = 60.0
    delivery_ttl: float = 40.0
    min_confidence: float = float(os.getenv("MIN_CONFIDENCE", "0.54"))
    kelly_cap: float = 1.5
    name: str = "FTW VIP PREDICTOR (ULTRA V5)"

    def validate(self):
        if not self.token or not self.chat or not self.urls:
            raise ValueError("Set TELEGRAM_TOKEN, CHAT_ID and API_URLS.")
        if self.fetch_every <= 0 or self.min_history < 30:
            raise ValueError("Invalid polling interval or history minimum.")
        if self.min_support < 3 or not 0 < self.min_edge < 0.5:
            raise ValueError("Invalid support or edge configuration.")


CFG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("predictor")


# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────
def clip(x, low=0.02, high=0.98):
    return max(low, min(high, float(x)))


def logit(p, eps=0.02):
    p = max(eps, min(1 - eps, float(p)))
    return math.log(p / (1 - p))


def sigmoid(z):
    z = max(-20.0, min(20.0, float(z)))
    return 1.0 / (1.0 + math.exp(-z))


def escape(x):
    return html.escape(str(x))


def run_length(seq):
    if not seq:
        return 0
    n = 0
    for x in reversed(seq):
        if x != seq[-1]:
            break
        n += 1
    return n


def entropy(seq):
    if not seq:
        return 0.0
    p1 = sum(seq) / len(seq)
    p0 = 1.0 - p1
    e = 0.0
    if p0 > 0:
        e -= p0 * math.log2(p0)
    if p1 > 0:
        e -= p1 * math.log2(p1)
    return e


def regime(seq):
    tail = seq[-24:]
    if len(tail) < 8:
        return "COLD"
    flips = sum(a != b for a, b in zip(tail, tail[1:]))
    rate = flips / (len(tail) - 1)
    if run_length(tail) >= 4:
        return "RUN"
    if rate >= 0.70:
        return "ALT"
    if abs(sum(tail) / len(tail) - 0.5) > 0.20:
        return "IMBAL"
    return "MIXED"


def numeric(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def parse_history(payload):
    obj = payload
    for _ in range(6):
        if isinstance(obj, list):
            break
        if not isinstance(obj, dict):
            return []
        obj = next(
            (
                obj[k]
                for k in ("data", "list", "result", "items", "history")
                if isinstance(obj.get(k), (list, dict))
            ),
            None,
        )
    if not isinstance(obj, list):
        return []

    records = {}
    for item in obj:
        if not isinstance(item, dict):
            continue
        issue = numeric(item.get("issueNumber", item.get("issue")))
        number = numeric(item.get("number", item.get("value")))
        if issue is None or number is None or not 0 <= number <= 9:
            continue
        key = str(issue)
        if key in records and records[key]["num"] != number:
            raise ValueError(f"Conflicting duplicate issue: {key}")
        records[key] = {"issue": key, "num": number, "size": int(number >= 5)}
    return sorted(records.values(), key=lambda x: int(x["issue"]))


def weighted_probability(observations):
    sw = sum(w for _, w in observations)
    sw2 = sum(w * w for _, w in observations)
    effective_n = sw * sw / sw2 if sw2 else 0.0
    probability = (4 + sum(y * w for y, w in observations)) / (8 + sw)
    return probability, effective_n


def logistic(weights, features):
    z = sum(w * x for w, x in zip(weights, features))
    return sigmoid(z)


# ─────────────────────────────────────────────────────────────
# FEATURE ENGINE — 32 rich features
# ─────────────────────────────────────────────────────────────
def trend_features(seq):
    if not seq:
        return [0.0] * 32

    def mean(k):
        arr = seq[-k:]
        return sum(arr) / len(arr) if arr else 0.5

    def ema(k):
        if not seq:
            return 0.5
        alpha = 2.0 / (k + 1)
        val = float(seq[0])
        for x in seq[1:]:
            val = alpha * x + (1 - alpha) * val
        return val

    def rsi(period=14):
        if len(seq) < period + 1:
            return 50.0
        gains = losses = 0.0
        for i in range(-period, 0):
            change = seq[i] - seq[i - 1]
            if change > 0:
                gains += change
            else:
                losses -= change
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    def std(k):
        arr = seq[-k:]
        if len(arr) < 2:
            return 0.0
        m = sum(arr) / len(arr)
        return math.sqrt(sum((x - m) ** 2 for x in arr) / len(arr))

    tail = seq[-24:]
    alternation = sum(a != b for a, b in zip(tail, tail[1:])) / max(1, len(tail) - 1)
    signed_run = (2 * seq[-1] - 1) * min(run_length(seq), 10) / 10.0
    rsi_n = (rsi(14) / 100.0) * 2.0 - 1.0
    macd = (ema(12) - ema(26)) * 2.0

    autocorr = 0.0
    if len(seq) >= 25 and std(24) > 0:
        m24 = mean(24)
        num = sum((seq[i] - m24) * (seq[i - 1] - m24) for i in range(-23, 0))
        den = 24.0 * std(24) ** 2
        autocorr = num / den if den > 0 else 0.0

    return [
        1.0,
        2 * mean(3) - 1,
        2 * mean(5) - 1,
        2 * mean(10) - 1,
        2 * mean(24) - 1,
        2 * mean(48) - 1,
        2 * mean(96) - 1,
        mean(3) - mean(10),
        mean(5) - mean(24),
        mean(10) - mean(48),
        signed_run,
        2 * seq[-1] - 1,
        (2 * alternation - 1) * (2 * seq[-1] - 1),
        rsi_n,
        macd,
        (2 * std(10) - 1),
        (2 * std(24) - 1),
        entropy(seq[-24:]) - 1,
        entropy(seq[-48:]) - 1,
        autocorr,
        1.0 if mean(5) > 0.5 else -1.0,
        (2 * seq[-1] - 1) * (2 * mean(24) - 1),
        abs(mean(24) - 0.5) * 2,
        (2 * seq[-1] - 1) * (mean(10) - mean(48)),
        (2 * mean(10) - 1) / (std(10) + 0.1),
        (2 * mean(24) - 1) / (std(24) + 0.1),
        (sum(seq[-10:]) / 10.0) - 0.5,
        (sum(seq[-5:]) / 5.0) - 0.5,
        (2 * mean(5) - 1) * (2 * mean(24) - 1),
        math.tanh((mean(10) - 0.5) * 6),
        math.tanh((mean(48) - 0.5) * 6),
        (2 * mean(3) - 1) * (2 * mean(96) - 1),
    ]


EXPERT_NAMES = [
    "patterns", "markov1", "markov2", "markov3", "markov4",
    "run", "regime", "base", "neighbors", "trend", "momentum", "cyclic",
]


# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────
class Database:
    def __init__(self, path):
        self.c = sqlite3.connect(path, isolation_level=None, timeout=20)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS history(
                issue TEXT PRIMARY KEY,
                num INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS predictions(
                issue TEXT PRIMARY KEY,
                created REAL NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                actual INTEGER
            );
            CREATE TABLE IF NOT EXISTS models(
                key TEXT PRIMARY KEY,
                n REAL NOT NULL,
                loss REAL NOT NULL,
                inverse_loss REAL NOT NULL,
                failures INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                issue TEXT,
                text TEXT NOT NULL,
                created REAL NOT NULL,
                expires REAL,
                sent INTEGER NOT NULL DEFAULT 0
            );
        """)

    @contextmanager
    def transaction(self):
        self.c.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.c.execute(
            "INSERT INTO meta VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def ingest(self, records):
        with self.transaction():
            for r in records:
                old = self.c.execute(
                    "SELECT num FROM history WHERE issue=?", (r["issue"],)
                ).fetchone()
                if old and old["num"] != r["num"]:
                    raise ValueError(f"Source changed settled issue {r['issue']}")
                self.c.execute(
                    "INSERT OR IGNORE INTO history VALUES (?,?)",
                    (r["issue"], r["num"]),
                )
            self.c.execute(
                "DELETE FROM history WHERE issue NOT IN "
                "(SELECT issue FROM history "
                "ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?)",
                (CFG.max_history,),
            )

    def history(self):
        rows = self.c.execute(
            "SELECT issue,num FROM history ORDER BY CAST(issue AS INTEGER)"
        ).fetchall()
        return [
            {"issue": r["issue"], "num": r["num"], "size": int(r["num"] >= 5)}
            for r in rows
        ]

    def memory(self, key):
        row = self.c.execute("SELECT * FROM models WHERE key=?", (key,)).fetchone()
        return dict(row) if row else {
            "n": 0.0, "loss": 0.0, "inverse_loss": 0.0, "failures": 0,
        }

    def learn(self, key, probability, actual):
        m = self.memory(key)
        n = CFG.decay * m["n"] + 1
        loss = CFG.decay * m["loss"] + (probability - actual) ** 2
        inv = CFG.decay * m["inverse_loss"] + (1 - probability - actual) ** 2
        wrong = int(probability >= 0.5) != actual
        failures = m["failures"] + 1 if wrong else 0
        self.c.execute(
            "INSERT INTO models VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "n=excluded.n,loss=excluded.loss,"
            "inverse_loss=excluded.inverse_loss,"
            "failures=excluded.failures",
            (key, n, loss, inv, failures),
        )

    def adapt(self, key, probability):
        m = self.memory(key)
        n = m["n"]
        if n < 1:
            return probability, 0.65, False

        brier = m["loss"] / n
        inverse_brier = m["inverse_loss"] / n

        flipped = (
            n >= 25
            and inverse_brier + 0.04 < brier
            and inverse_brier < 0.245
        )
        effective_loss = inverse_brier if flipped else brier
        p = 1 - probability if flipped else probability

        reliability = clip((0.28 - effective_loss) / 0.08, 0.0, 1.0)
        maturity = min(n / 30.0, 1.0)
        shrink = (1 - maturity) * 0.65 + maturity * reliability
        p = 0.5 + (p - 0.5) * shrink

        weight = clip(math.exp(-8 * (effective_loss - 0.25)), 0.15, 2.0)
        if m["failures"] >= 3 and not flipped:
            weight *= 0.35
        return clip(p), weight, flipped

    def queue(self, kind, issue, text, expires=None):
        self.c.execute(
            "INSERT INTO outbox(kind,issue,text,created,expires) VALUES (?,?,?,?,?)",
            (kind, issue, text, time.time(), expires),
        )


# ─────────────────────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────────────────────
class Engine:
    def __init__(self, db):
        self.db = db

    # ---- pattern mining (exact + skip-gram + 1-mismatch) ----
    def mine_patterns(self, seq):
        n = len(seq)
        candidates = {}

        def weight(idx):
            return 0.5 ** ((n - 1 - idx) / 300.0)

        # 1) Exact suffix matches
        for length in range(2, CFG.max_pattern + 1):
            if n < length * 3:
                break
            suffix = tuple(seq[-length:])
            obs = []
            last = -length - 1
            for i in range(length, n - length):
                if tuple(seq[i - length:i]) == suffix and i - last >= length + 1:
                    obs.append((seq[i], weight(i)))
                    last = i
            if len(obs) >= CFG.min_support:
                p, eff_n = weighted_probability(obs)
                if eff_n >= CFG.min_support * 0.5:
                    bits = "".join("B" if x else "S" for x in suffix)
                    key = f"exact:{bits}"
                    rank = abs(p - 0.5) * math.sqrt(eff_n)
                    candidates[key] = (rank, p, len(obs), eff_n, key)

        # 2) Skip-gram patterns (1 wildcard, length 5-8)
        for length in range(5, min(9, CFG.max_pattern + 1)):
            if n < length * 3:
                continue
            for gap in range(length - 1):
                suffix_items = tuple(x for i, x in enumerate(seq[-length:]) if i != gap)
                obs = []
                last = -length - 1
                for i in range(length, n - length):
                    cand = tuple(x for j, x in enumerate(seq[i - length:i]) if j != gap)
                    if cand == suffix_items and i - last >= length + 1:
                        obs.append((seq[i], weight(i) * 0.9))
                        last = i
                if len(obs) >= CFG.min_support + 3:
                    p, eff_n = weighted_probability(obs)
                    if eff_n >= (CFG.min_support + 3) * 0.5:
                        bits = "".join("B" if x else "S" for x in suffix_items)
                        key = f"gap{gap}:{bits}"
                        rank = abs(p - 0.5) * math.sqrt(eff_n) * 0.85
                        if key not in candidates or candidates[key][0] < rank:
                            candidates[key] = (rank, p, len(obs), eff_n, key)

        # 3) Mismatch tolerance (Hamming ≤ 1, length 6-8)
        for length in range(6, min(9, CFG.max_pattern + 1)):
            if n < length * 3:
                continue
            suffix = tuple(seq[-length:])
            obs = []
            last = -length - 1
            for i in range(length, n - length):
                window = tuple(seq[i - length:i])
                mm = sum(a != b for a, b in zip(window, suffix))
                if mm <= 1 and i - last >= length + 1:
                    w = weight(i) * (1.0 if mm == 0 else 0.55)
                    obs.append((seq[i], w))
                    last = i
            if len(obs) >= CFG.min_support + 3:
                p, eff_n = weighted_probability(obs)
                if eff_n >= (CFG.min_support + 3) * 0.5:
                    bits = "".join("B" if x else "S" for x in suffix)
                    key = f"mm1:{bits}"
                    rank = abs(p - 0.5) * math.sqrt(eff_n) * 0.75
                    if key not in candidates or candidates[key][0] < rank:
                        candidates[key] = (rank, p, len(obs), eff_n, key)

        ranked = sorted(candidates.values(), key=lambda x: x[0], reverse=True)
        return ranked[:8]

    # ---- 12 experts ----
    def mine_experts(self, seq):
        n = len(seq)
        experts = {}

        def weight(idx):
            return 0.5 ** ((n - 1 - idx) / 300.0)

        # Patterns expert
        patterns = self.mine_patterns(seq)
        if patterns:
            num = den = 0.0
            for rank, p, support, eff_n, key in patterns:
                adjusted, w, _ = self.db.adapt(key, p)
                w *= math.sqrt(eff_n)
                num += adjusted * w
                den += w
            if den > 0:
                experts["patterns"] = num / den

        # Markov chains (order 1-4)
        for order in (1, 2, 3, 4):
            suffix = tuple(seq[-order:])
            obs = [
                (seq[i], weight(i))
                for i in range(order, n - order)
                if tuple(seq[i - order:i]) == suffix
            ]
            if len(obs) >= CFG.min_support:
                experts[f"markov{order}"] = weighted_probability(obs)[0]

        # Run expert
        current_run = min(run_length(seq), 8)
        run_obs = []
        for i in range(24, n):
            past = seq[max(0, i - 96):i]
            if past and past[-1] == seq[-1] and min(run_length(past), 8) == current_run:
                run_obs.append((seq[i], weight(i)))
        if len(run_obs) >= CFG.min_support:
            experts["run"] = weighted_probability(run_obs)[0]

        # Regime expert
        current_regime = regime(seq)
        regime_obs = []
        for i in range(24, n):
            past = seq[max(0, i - 96):i]
            if past and regime(past) == current_regime:
                regime_obs.append((seq[i], weight(i)))
        if len(regime_obs) >= CFG.min_support:
            experts["regime"] = weighted_probability(regime_obs)[0]

        # Base expert
        experts["base"] = weighted_probability(
            [(y, weight(i)) for i, y in enumerate(seq)]
        )[0]

        # Neighbors expert (KNN)
        length = 10
        if n >= 3 * length:
            suffix = tuple(seq[-length:])
            nearest = []
            for i in range(length, n - length):
                prev = seq[i - length:i]
                dist = sum(a != b for a, b in zip(prev, suffix))
                if dist <= 2:
                    nearest.append((seq[i], weight(i) * 0.4 ** dist))
            if len(nearest) >= 20:
                experts["neighbors"] = weighted_probability(nearest)[0]

        # Trend expert (logistic regression on 32 features)
        features = trend_features(seq)
        weights = self.db.get("trend_weights")
        if not isinstance(weights, list) or len(weights) != len(features):
            weights = [0.0] * len(features)
        experts["trend"] = logistic(weights, features)

        # Momentum expert
        recent = seq[-5:]
        momentum = sum(recent) / len(recent) if recent else 0.5
        experts["momentum"] = 0.2 + (momentum * 0.6)

        # Cyclic expert
        cyclic_p = self._cyclic_expert(seq)
        if cyclic_p is not None:
            experts["cyclic"] = cyclic_p

        return experts, patterns, features

    def _cyclic_expert(self, seq):
        n = len(seq)
        if n < 30:
            return None
        best_period = None
        best_match = 0.0
        for period in range(2, 7):
            if n < period * 4:
                continue
            matches = total = 0
            for i in range(n - 1, max(-1, n - 30), -1):
                if i - period < 0:
                    break
                total += 1
                if seq[i] == seq[i - period]:
                    matches += 1
            if total > 0:
                rate = matches / total
                if rate > best_match:
                    best_match = rate
                    best_period = period
        if best_period is None or best_match < 0.62:
            return None
        base_idx = n - best_period
        if base_idx >= 0:
            return float(seq[base_idx])
        return None

    # ---- meta-learner ----
    def _meta_predict(self, experts):
        feats = [logit(clip(experts.get(name, 0.5), 0.05, 0.95)) for name in EXPERT_NAMES]
        feats.append(1.0)
        weights = self.db.get("meta_weights")
        if not isinstance(weights, list) or len(weights) != len(feats):
            weights = [0.0] * len(feats)
        z = sum(w * f for w, f in zip(weights, feats))
        return sigmoid(z), feats

    def _update_meta(self, feats, y, lr):
        weights = self.db.get("meta_weights")
        if not isinstance(weights, list) or len(weights) != len(feats):
            weights = [0.0] * len(feats)
        z = sum(w * f for w, f in zip(weights, feats))
        p = sigmoid(z)
        grad_scale = 2.0 * (y - p) * p * (1.0 - p)
        new_weights = []
        for w, f in zip(weights, feats):
            nw = w * 0.999 + lr * grad_scale * f
            new_weights.append(max(-4.0, min(4.0, nw)))
        self.db.set("meta_weights", new_weights)
        self.db.set("meta_n", self.db.get("meta_n", 0) + 1)

    # ---- calibration ----
    def _calibrate(self, p_raw, current_regime):
        bucket = min(19, int(p_raw * 20))
        n_g, s_g = self.db.get(f"cal20:global:{bucket}", [0.0, 0.0])
        p_global = (30 * p_raw + s_g) / (30 + n_g)
        n_r, s_r = self.db.get(f"cal20:{current_regime}:{bucket}", [0.0, 0.0])
        p_regime = (30 * p_raw + s_r) / (30 + n_r)
        regime_weight = min(n_r / 40.0, 1.0)
        p = regime_weight * p_regime + (1 - regime_weight) * p_global
        return clip(p)

    # ---- number predictor ----
    def _predict_number(self, seq_nums, pred_side):
        valid = list(range(5, 10)) if pred_side == "BIG" else list(range(0, 5))
        if not seq_nums:
            return random.choice(valid), {}

        last_num = seq_nums[-1]
        matrix = self.db.get("num_matrix", {})
        trans = matrix.get(str(last_num), {})
        total_trans = sum(trans.values()) if trans else 0

        freq = self.db.get("num_freq", {})
        total_freq = sum(freq.values()) if freq else 0

        recent = seq_nums[-100:]
        recent_count = Counter(recent)

        scores = {}
        for n in valid:
            s = 0.0
            if total_trans > 0:
                s += 0.45 * (trans.get(str(n), 0) / total_trans)
            if total_freq > 0:
                s += 0.25 * (freq.get(str(n), 0) / total_freq)
            if recent_count:
                s += 0.30 * (recent_count.get(n, 0) / len(recent))
            scores[n] = s

        if not scores or max(scores.values()) == 0:
            return random.choice(valid), scores
        best = max(scores, key=scores.get)
        return best, scores

    # ---- Kelly stake ----
    def _kelly_stake(self, selected_prob):
        edge = 2.0 * selected_prob - 1.0
        if edge <= 0:
            return 0.5
        raw = 0.1 * edge
        mult = raw / 0.02
        return max(0.5, min(CFG.kelly_cap, round(mult * 4) / 4))

    # ───────────── MAIN PREDICT ─────────────
    def predict(self, history, issue):
        seq = [r["size"] for r in history]
        nums = [r["num"] for r in history]

        experts, patterns, features = self.mine_experts(seq)
        current_regime = regime(seq)
        current_streak = run_length(seq)

        # 1) per-expert adaptation
        adjusted = []
        flipped_models = []
        for name, p in experts.items():
            key = f"expert:{name}:{current_regime}"
            corrected, weight, flipped = self.db.adapt(key, p)
            if name == "momentum" and current_streak >= 3:
                weight *= 2.2
            if name == "trend" and current_regime in ("RUN", "IMBAL"):
                weight *= 1.4
            adjusted.append((corrected, weight))
            if flipped:
                flipped_models.append(name)

        # 2) weighted average
        total_w = sum(w for _, w in adjusted)
        p_avg = sum(p * w for p, w in adjusted) / total_w if total_w > 0 else 0.5

        # 3) meta-learner blend
        meta_p, meta_feats = self._meta_predict(experts)
        n_meta = self.db.get("meta_n", 0)
        alpha = min(n_meta / 80.0, 1.0)
        p_raw = alpha * meta_p + (1 - alpha) * p_avg

        # 4) momentum override during streak
        if current_streak >= 3:
            trend_side = seq[-1]
            momentum_p = 0.65 if trend_side == 1 else 0.35
            p_raw = 0.7 * p_raw + 0.3 * momentum_p

        # 5) calibration
        p_cal = self._calibrate(p_raw, current_regime)

        # 6) decide side
        pred_side = "BIG" if p_cal >= 0.5 else "SMALL"
        selected_prob = p_cal if p_cal >= 0.5 else 1.0 - p_cal

        # 7) number
        pred_num, num_scores = self._predict_number(nums, pred_side)

        # 8) agreement
        big_count = sum(1 for p, w in adjusted if p >= 0.5)
        total_count = len(adjusted)
        agreement = max(big_count, total_count - big_count) / total_count if total_count else 0.5

        # 9) stake
        stake = self._kelly_stake(selected_prob)

        return {
            "issue": issue,
            "p": p_cal,
            "p_raw": p_raw,
            "p_avg": p_avg,
            "p_meta": meta_p,
            "side": pred_side,
            "num": pred_num,
            "num_scores": {str(k): v for k, v in num_scores.items()},
            "action": "SIGNAL",
            "regime": current_regime,
            "experts": experts,
            "patterns": [
                {"key": key, "p": prob, "support": supp}
                for rank, prob, supp, eff_n, key in patterns
            ],
            "features": features,
            "meta_feats": meta_feats,
            "flipped": flipped_models,
            "agreement": agreement,
            "stake": stake,
            "selected_prob": selected_prob,
        }

    # ───────────── SETTLE ─────────────
    def settle(self, history):
        lookup = {r["issue"]: r for r in history}

        # Update transition matrix and frequency
        num_matrix = self.db.get("num_matrix", {})
        num_freq = self.db.get("num_freq", {})
        for i in range(1, len(history)):
            prev_num = str(history[i - 1]["num"])
            curr_num = str(history[i]["num"])
            if prev_num not in num_matrix:
                num_matrix[prev_num] = {}
            num_matrix[prev_num][curr_num] = num_matrix[prev_num].get(curr_num, 0) + 1
            num_freq[curr_num] = num_freq.get(curr_num, 0) + 1
        if sum(num_freq.values()) > 50000:
            num_freq = {k: v * 0.5 for k, v in num_freq.items()}
        self.db.set("num_matrix", num_matrix)
        self.db.set("num_freq", num_freq)

        pending = self.db.c.execute(
            "SELECT issue,payload FROM predictions WHERE status='PENDING' "
            "ORDER BY CAST(issue AS INTEGER)"
        ).fetchall()

        for row in pending:
            actual = lookup.get(row["issue"])
            if actual is None:
                continue

            prediction = json.loads(row["payload"])
            y = actual["size"]
            actual_num = actual["num"]
            pred_num = prediction.get("num")

            won_side = int(prediction["p"] >= 0.5) == y
            won_num = (actual_num == pred_num) if pred_num is not None else False
            status = "WIN" if won_side else "LOSS"

            with self.db.transaction():
                self.db.c.execute(
                    "UPDATE predictions SET status=?,actual=? WHERE issue=?",
                    (status, actual_num, row["issue"]),
                )

                # expert update
                for name, p in prediction["experts"].items():
                    self.db.learn(f"expert:{name}:{prediction['regime']}", p, y)

                # pattern update
                for pattern in prediction["patterns"]:
                    self.db.learn(pattern["key"], pattern["p"], y)

                # trend weights (logistic regression)
                weights = self.db.get("trend_weights")
                if isinstance(weights, list) and len(weights) == len(prediction["features"]):
                    error = y - prediction["experts"].get("trend", 0.5)
                    weights = [
                        clip(w + CFG.learning_rate * (error * x - 0.01 * w), -4, 4)
                        for w, x in zip(weights, prediction["features"])
                    ]
                    self.db.set("trend_weights", weights)

                # meta update
                meta_feats = prediction.get("meta_feats")
                if meta_feats:
                    self._update_meta(meta_feats, y, CFG.meta_lr)

                # calibration update
                p_raw = prediction.get("p_raw", prediction["p"])
                regime_name = prediction["regime"]
                bucket = min(19, int(p_raw * 20))
                for scope in ("global", regime_name):
                    key = f"cal20:{scope}:{bucket}"
                    n, s = self.db.get(key, [0.0, 0.0])
                    self.db.set(key, [0.995 * n + 1, 0.995 * s + y])

                # stats
                stats = self.db.get("stats", {"observed": 0, "wins": 0, "losses": 0, "brier": 0.0, "num_hits": 0})
                stats["observed"] += 1
                stats["brier"] += (prediction["p"] - y) ** 2
                if won_side:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                if won_num:
                    stats["num_hits"] = stats.get("num_hits", 0) + 1
                self.db.set("stats", stats)

                # streak / cooldown
                streak = 0 if won_side else self.db.get("loss_streak", 0) + 1
                self.db.set("loss_streak", streak)
                if streak >= CFG.loss_limit:
                    self.db.set("cooldown", CFG.cooldown_rounds)
                    self.db.set("loss_streak", 0)

                # emoji
                if won_side and won_num:
                    result_text = "🎰🎰🎰 <b>JACKPOT!</b>"
                elif won_side:
                    result_text = "🏆 <b>WIN</b>"
                else:
                    result_text = "❌ <b>LOSS</b>"

                self.db.queue("result", row["issue"], result_text)
                self.db.c.execute(
                    "UPDATE outbox SET sent=2 "
                    "WHERE kind='prediction' AND issue=? AND sent=0",
                    (row["issue"],),
                )


# ─────────────────────────────────────────────────────────────
# FORMATTER
# ─────────────────────────────────────────────────────────────
def format_prediction(p):
    conf_percent = p["selected_prob"] * 100
    filled = int(conf_percent // 10)
    bar = "█" * filled + "░" * (10 - filled)

    color_emoji = "🔴" if p["side"] == "BIG" else "🔵"
    side_word = "BIG" if p["side"] == "BIG" else "SMALL"

    if conf_percent >= 70:
        quality = "🔥 ULTRA VIP"
        emoji_q = "💎"
    elif conf_percent >= 64:
        quality = "🔥 HIGH VIP"
        emoji_q = "⭐"
    elif conf_percent >= 58:
        quality = "⭐ VIP"
        emoji_q = "✨"
    else:
        quality = "✅ NORMAL"
        emoji_q = "✅"

    num_scores = p.get("num_scores", {})
    if num_scores:
        sorted_nums = sorted(num_scores.items(), key=lambda x: -x[1])
        alts = [str(n) for n, sc in sorted_nums[1:4] if sc > 0]
    else:
        alts = []
    alt_text = " • ".join(alts) if alts else "—"

    stake = p.get("stake", 1.0)
    agreement = p.get("agreement", 0.5) * 10

    return (
        f"⚡️ <b>{escape(CFG.name)}</b> ⚡️\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>TARGET:</b> {color_emoji} <b>{side_word}</b>  →  <code>{p.get('num', '')}</code>\n"
        f"🎲 <b>ALT NUMBERS:</b> <code>{alt_text}</code>\n"
        f"📅 <b>PERIOD:</b> <code>{escape(p['issue'])}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>CONFIDENCE:</b> {conf_percent:.1f}% [{bar}]\n"
        f"{emoji_q} <b>QUALITY:</b> {quality}\n"
        f"💰 <b>STAKE:</b> {stake:.1f}x\n"
        f"🧠 <b>AGREEMENT:</b> {agreement:.0f}/10 experts\n"
        f"🌊 <b>REGIME:</b> {escape(p.get('regime', 'N/A'))}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


# ─────────────────────────────────────────────────────────────
# RUNTIME
# ─────────────────────────────────────────────────────────────
class Runtime:
    def __init__(self, db):
        self.db = db
        self.engine = Engine(db)
        self.stop = asyncio.Event()
        self.last_fetch = 0.0
        self.last_error = None

    async def fetch(self, session):
        best = None
        for url in CFG.urls:
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    records = parse_history(await response.json(content_type=None))
                    if records and (best is None or int(records[-1]["issue"]) > int(best[-1]["issue"])):
                        best = records
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                LOG.warning("A history source failed")
        if not best:
            raise RuntimeError("No valid history source available")
        return best

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def worker(self, session):
        failures = 0
        while not self.stop.is_set():
            try:
                records = await self.fetch(session)
                self.db.ingest(records)
                history = self.db.history()
                self.last_fetch = time.time()
                self.last_error = None
                failures = 0

                self.engine.settle(history)

                latest = int(history[-1]["issue"])
                seen = self.db.get("latest_seen")
                self.db.set("latest_seen", latest)

                if seen is None or latest <= seen:
                    await self.pause(CFG.fetch_every)
                    continue

                if latest != seen + 1:
                    LOG.warning("History gap or issue rollover: resynchronizing")
                    await self.pause(CFG.fetch_every)
                    continue

                start = len(history) - 1
                while start > 0 and (int(history[start]["issue"]) == int(history[start - 1]["issue"]) + 1):
                    start -= 1
                continuous = history[start:]
                if len(continuous) < 30:
                    await self.pause(CFG.fetch_every)
                    continue

                issue = str(latest + 1)
                exists = self.db.c.execute(
                    "SELECT 1 FROM predictions WHERE issue=?", (issue,)
                ).fetchone()
                if not exists:
                    p = self.engine.predict(continuous, issue)
                    with self.db.transaction():
                        self.db.c.execute(
                            "INSERT INTO predictions VALUES (?,?,?,?,NULL)",
                            (issue, time.time(), "PENDING", json.dumps(p)),
                        )
                        self.db.queue(
                            "prediction",
                            issue,
                            format_prediction(p),
                            time.time() + CFG.delivery_ttl,
                        )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.last_error = type(exc).__name__
                LOG.exception("Worker failure")

            delay = min(60.0, CFG.fetch_every * 2 ** min(failures, 4))
            await self.pause(delay + random.uniform(0, 0.3))

    async def sender(self, session):
        endpoint = f"https://api.telegram.org/bot{CFG.token}/sendMessage"
        backoff = 1.0

        while not self.stop.is_set():
            row = self.db.c.execute(
                "SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                await self.pause(0.5)
                continue

            if row["expires"] and time.time() >= row["expires"]:
                self.db.c.execute("UPDATE outbox SET sent=2 WHERE id=?", (row["id"],))
                continue

            delay = backoff
            try:
                async with session.post(
                    endpoint,
                    json={
                        "chat_id": CFG.chat,
                        "text": row["text"],
                        "parse_mode": "HTML",
                    },
                ) as response:
                    data = await response.json(content_type=None)

                    if response.status == 200 and data.get("ok"):
                        self.db.c.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
                        backoff = 1.0
                        continue

                    code = data.get("error_code", response.status)
                    if code == 429:
                        delay = float(data.get("parameters", {}).get("retry_after", 10))
                    elif code in (400, 401, 403, 404):
                        LOG.error("Telegram rejected delivery: code %s", code)
                        delay = 60
                    else:
                        LOG.warning("Telegram delivery failed: code %s", code)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning(f"Telegram transport failure: {type(e).__name__} - {str(e)}")

            await self.pause(delay)
            backoff = min(60.0, backoff * 2)

    async def health(self, request):
        age = time.time() - self.last_fetch if self.last_fetch else None
        healthy = age is not None and age < 90
        stats = self.db.get("stats", {})
        observed = stats.get("observed", 0)
        wins = stats.get("wins", 0)
        acc = (wins / observed * 100) if observed > 0 else 0
        brier = (stats.get("brier", 0.0) / observed) if observed > 0 else 0.0

        return web.json_response(
            {
                "status": "ok" if healthy else "degraded",
                "last_fetch_age_seconds": round(age, 1) if age else age,
                "last_error": self.last_error,
                "metrics": {
                    "observed": observed,
                    "wins": wins,
                    "losses": stats.get("losses", 0),
                    "accuracy": round(acc, 2),
                    "brier_avg": round(brier, 4),
                    "num_hits": stats.get("num_hits", 0),
                },
                "pending_messages": self.db.c.execute(
                    "SELECT COUNT(*) FROM outbox WHERE sent=0"
                ).fetchone()[0],
            },
            status=200 if healthy else 503,
        )


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
async def main():
    CFG.validate()
    db = Database(CFG.db_path)
    runtime = Runtime(db)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime.stop.set)

    app = web.Application()
    app.router.add_get("/", runtime.health)
    runner = web.AppRunner(app)
    tasks = []
    stop_task = None

    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", CFG.port).start()

        timeout = aiohttp.ClientTimeout(total=CFG.timeout)
        connector = aiohttp.TCPConnector(
            limit=8, ttl_dns_cache=300, ssl=False, family=socket.AF_INET
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks = [
                asyncio.create_task(runtime.worker(session), name="worker"),
                asyncio.create_task(runtime.sender(session), name="sender"),
            ]
            stop_task = asyncio.create_task(runtime.stop.wait())
            LOG.info("FTW ULTRA V5 Predictor Started")

            await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            runtime.stop.set()

            for task in tasks:
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc:
                        raise exc

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    finally:
        runtime.stop.set()
        for task in tasks:
            task.cancel()
        if stop_task:
            stop_task.cancel()
        await asyncio.gather(*tasks, *([stop_task] if stop_task else []), return_exceptions=True)
        await runner.cleanup()
        db.c.close()


if __name__ == "__main__":
    asyncio.run(main())
