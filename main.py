"""
V300 ULTRA-ADAPTIVE
===================
Fixes:
  * Consensus Math Bug (1 signal = 100% fixed)
  * Kelly Criterion now uses Calibrated Confidence, not Raw.
  * Tilt Protection (Stops betting after 2 losses unless edge is massive)
  * Shadow Mode (3+ losses triggers a 2-round cool-down)
  * Strict SKIP threshold for weak edges
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
from collections import Counter, deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.error import TimedOut, NetworkError

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA"
    chat_id_raw: str = "1264164655"
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "predictor_v300.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V300 ULTRA-ADAPTIVE"
    min_history: int = 60
    warm_min: int = 25
    backtest_steps: int = 250
    admin_ids: tuple = ()

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

# =========================
# LOGGING
# =========================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v300.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v300")

def h(x: Any) -> str:
    return html.escape(str(x))

# =========================
# PROBABILITY & MATH
# =========================
class Beta:
    __slots__ = ("a", "b")
    def __init__(self, a: float = 1.0, b: float = 1.0):
        self.a = a
        self.b = b

    def update(self, correct: bool):
        if correct: self.a += 1.0
        else: self.b += 1.0

    def n(self) -> float:
        return max(0.0, self.a + self.b - 2.0)

    def mean(self) -> float:
        return self.a / (self.a + self.b)

    def sample_thompson(self) -> float:
        return random.betavariate(self.a, self.b)

    def to_dict(self):
        return {"a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d):
        return cls(d["a"], d["b"])

class PageHinkley:
    __slots__ = ("delta", "threshold", "m", "n", "sum_pos", "sum_neg")
    def __init__(self, delta: float = 0.01, threshold: float = 10.0):
        self.delta = delta
        self.threshold = threshold
        self.m = 0.5
        self.n = 1
        self.sum_pos = 0.0
        self.sum_neg = 0.0

    def update(self, x: float) -> bool:
        self.n += 1
        self.m += (x - self.m) / self.n
        self.sum_pos = max(0.0, self.sum_pos + x - self.m - self.delta)
        self.sum_neg = max(0.0, self.sum_neg + self.m - x - self.delta)
        if self.sum_pos > self.threshold or self.sum_neg > self.threshold:
            self.m = 0.5
            self.n = 1
            self.sum_pos = 0.0
            self.sum_neg = 0.0
            return True
        return False

class DirichletNumerics:
    __slots__ = ("counts", "decay")
    def __init__(self, prior: float = 0.35, decay: float = 0.995):
        self.counts = [prior] * 10
        self.decay = decay

    def update(self, num: int):
        if 0 <= num <= 9:
            self.counts = [c * self.decay for c in self.counts]
            self.counts[num] += 1.0

    def probs(self):
        total = sum(self.counts)
        return [c / total for c in self.counts]

    def reset(self):
        self.counts = [0.35] * 10

class Scoring:
    __slots__ = ("brier", "logloss")
    def __init__(self, maxlen: int = 1000):
        self.brier = deque(maxlen=maxlen)
        self.logloss = deque(maxlen=maxlen)

    def add(self, p_big: float, actual_big: bool):
        y = 1.0 if actual_big else 0.0
        self.brier.append((p_big - y) ** 2)
        p = p_big if actual_big else (1.0 - p_big)
        p = max(1e-9, min(1.0 - 1e-9, p))
        self.logloss.append(-math.log(p))

    def brier_score(self) -> Optional[float]:
        return sum(self.brier) / len(self.brier) if self.brier else None

    def logloss_score(self) -> Optional[float]:
        return sum(self.logloss) / len(self.logloss) if self.logloss else None

class Calibration:
    def __init__(self, maxlen: int = 400):
        self.samples = deque(maxlen=maxlen)
    def add(self, conf: int, correct: bool):
        self.samples.append((conf, 1 if correct else 0))
    def calibrate(self, raw_conf: int) -> int:
        if len(self.samples) < 20: return 50
        n = w = W = 0
        for c, win in self.samples:
            W += win
            if abs(c - raw_conf) <= 6:
                n += 1; w += win
        base = (W + 5.0) / (len(self.samples) + 10)
        p = (w + 12 * base) / (n + 12)
        return int(max(0.5, min(0.85, p)) * 100)

# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER, status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER, actual_size TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT, p_big REAL, calibrated_conf INTEGER,
                    consensus REAL, stake TEXT, created_at REAL
                )
            """)
            for col, typ in [
                ("sigs", "TEXT"), ("raw_conf", "INTEGER"), ("regime", "TEXT"), ("details", "TEXT"),
                ("p_big", "REAL"), ("calibrated_conf", "INTEGER"), ("consensus", "REAL"), ("stake", "TEXT")
            ]:
                try: c.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError: pass
            c.execute("CREATE TABLE IF NOT EXISTS state_kv (key TEXT PRIMARY KEY, value TEXT)")

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, mode, logic, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["mode"], p["logic"], json.dumps(p.get("sigs", [])),
                p.get("raw_conf", 50), p.get("regime", "BALANCED"), json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50), p.get("consensus", 0.5), p.get("stake", "1x"), time.time()
            ))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, mode, sigs, raw_conf, regime, details, p_big, calibrated_conf, consensus, stake
                FROM predictions WHERE status='PENDING'
            """).fetchall()
        for row in rows:
            (issue, p_size, p_num, mode, sigs_json, raw_conf, regime, details_json, p_big, cal_conf, consensus, stake) = row
            if issue not in history_dict: continue
            act = history_dict[issue]
            act_size_str = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size_str)
            status = ("JACK 🏆" if p_num == act["num"] else "WIN ✅") if win else "LOSS ❌"
            with self._conn() as c:
                c.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?", (status, act["num"], act_size_str, issue))
            resolved.append({
                "issue": issue, "size": p_size, "num": p_num, "actual_num": act["num"], "actual_size": act_size_str,
                "status": status, "mode": mode, "win": win, "raw_conf": raw_conf, "regime": regime,
                "details": json.loads(details_json) if details_json else {}, "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5, "calibrated_conf": cal_conf or 50, "consensus": consensus or 0.5, "stake": stake or "1x"
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def overall_stats(self):
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*), SUM(CASE WHEN status LIKE 'WIN%' OR status LIKE 'JACK%' THEN 1 ELSE 0 END) FROM predictions WHERE status!='PENDING'").fetchone()
        return (row[0] or 0), (row[1] or 0)

    def load_kv(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM state_kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_kv(self, key: str, value: str):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO state_kv (key, value) VALUES (?, ?)", (key, value))

STORE = Storage(CFG.db_path)

# =========================
# DETECTORS & REGIME
# =========================
@dataclass
class Regime:
    regime: str
    confidence: float
    entropy: float
    bias: float
    switch_rate: float
    max_streak: int

def detect_regime(sizes):
    N = len(sizes)
    if N < CFG.warm_min: return Regime("WARMING", 0, 1.0, 0, 0.5, 1)
    w = min(20, N)
    recent = sizes[-w:]
    runs = []
    ln, val = 1, recent[0]
    for x in recent[1:]:
        if x == val: ln += 1
        else: runs.append((val, ln)); ln, val = 1, x
    runs.append((val, ln))
    max_streak = max(r[1] for r in runs)
    avg_streak = sum(r[1] for r in runs) / len(runs)
    switch_rate = (len(runs) - 1) / (w - 1) if w > 1 else 0.5
    big_count = sum(recent)
    bias = abs(big_count / w - 0.5)
    H = 0.0
    for c in (w - big_count, big_count):
        if c:
            p = c / w
            H -= p * math.log2(p)
    if max_streak >= 4 and avg_streak > 1.7: return Regime("TRENDING", min(1, (max_streak - 2) / 4), H, bias, switch_rate, max_streak)
    if switch_rate > 0.58: return Regime("CHOPPY", min(1, (switch_rate - 0.5) * 4), H, bias, switch_rate, max_streak)
    if H < 0.88 or bias > 0.13: return Regime("BIASED", min(1, (1 - H) * 5 + bias), H, bias, switch_rate, max_streak)
    return Regime("BALANCED", 0, H, bias, switch_rate, max_streak)

@dataclass
class Detector:
    key: str
    name: str
    dir: int
    conf: int
    weight: float
    family: str = "generic"

def run_detectors(sizes, nums):
    out = []
    n = len(sizes)
    if n < CFG.warm_min: return out

    # 1. Markov
    for ord_ in range(6, 0, -1):
        if n < ord_ + 15: continue
        ctx = 0
        for j in range(n - ord_, n): ctx = (ctx << 1) | sizes[j]
        bN = tN = 0
        for i in range(ord_, n):
            c = 0
            for j in range(i - ord_, i): c = (c << 1) | sizes[j]
            if c != ctx: continue
            tN += 1
            if sizes[i] == 1: bN += 1
        minS = {6: 36, 5: 30, 4: 20, 3: 12, 2: 8, 1: 6}[ord_]
        if tN < minS: continue
        pB = (bN + 0.5) / (tN + 1)
        sf = min(1, tN / (minS * 1.5))
        strength = min(1, abs(pB - 0.5) * 4) * sf
        if strength >= 0.10:
            out.append(Detector(f"Markov{ord_}", f"Markov-L{ord_}", 1 if pB >= 0.5 else 0, int(50 + strength * 30), 0.6 + strength * 1.2, "markov"))
        break

    # 2. Bayes
    bb = bs = 2.0
    for num in nums[-30:]:
        w = 1.0 / (1 + abs(num - 4.5) * 0.15)
        if num >= 5: bb += w
        else: bs += w
    pB = bb / (bb + bs)
    strength = abs(pB - 0.5) * 2.5
    if strength >= 0.05:
        out.append(Detector("Bayes", f"Bayes", 1 if pB > 0.52 else 0, int(50 + min(30, strength * 30)), 0.6 + strength, "bayes"))

    # 3. Freq
    w30 = sizes[-30:]
    pF = sum(w30) / len(w30)
    if abs(pF - 0.5) >= 0.08:
        strength = min(1, (abs(pF - 0.5) - 0.08) * 2.5)
        out.append(Detector("Freq30", f"Freq", 1 if pF > 0.5 else 0, int(50 + strength * 25), 0.6 + strength * 1.2, "freq"))

    # 4. Streak / Dragon
    last = sizes[-1]
    run = 1
    for i in range(n - 2, -1, -1):
        if sizes[i] == last: run += 1
        else: break
    if 2 <= run <= 4:
        out.append(Detector(f"Streak{run}", f"Streak×{run}", last, 56 + run * 4, 0.9, "streak"))
    if run >= 5:
        out.append(Detector("Dragon", f"Dragon×{run}", last, min(94, 68 + run * 4), 1.9 + min(run, 6) * 0.15, "dragon"))

    # 5. Reversal
    prior = sizes[max(0, n - 25):n - 5]
    if len(prior) >= 15:
        pBp = sum(prior) / len(prior)
        if (pBp > 0.68 and last == 0) or (pBp < 0.32 and last == 1):
            rrun = 1
            for i in range(n - 2, max(0, n - 6), -1):
                if sizes[i] == last: rrun += 1
                else: break
            out.append(Detector("Reversal", "Reversal", last, min(86, 62 + rrun * 7), 1.7 + min(rrun, 3) * 0.2, "reversal"))

    # 6. Zigzag
    switches = sum(1 for i in range(min(6, n - 1)) if sizes[-1 - i] != sizes[-2 - i])
    if switches >= 5:
        out.append(Detector("Zigzag", "Zigzag", 1 - last, 62 + switches * 3, 1.4, "zigzag"))

    # 7. ACF
    for lag in (1, 2):
        N = min(30, n)
        if N < lag + 5: continue
        arr = sizes[-N:]
        mean = sum(arr) / N
        num_ = sum((arr[i] - mean) * (arr[i + lag] - mean) for i in range(N - lag))
        den_ = sum((arr[i] - mean) ** 2 for i in range(N))
        if den_ <= 0: continue
        acf = num_ / den_
        if abs(acf) >= 0.10:
            out.append(Detector(f"ACF{lag}", f"ACF-L{lag}", last if acf > 0 else 1 - last, int(min(68, 55 + abs(acf) * 40)), 0.9 + abs(acf), f"acf{lag}"))

    # 8. Bollinger & ZScore
    if n >= 20:
        w20 = sizes[-20:]
        ma = sum(w20) / 20
        std = math.sqrt(sum((x - ma) ** 2 for x in w20) / 20)
        if std >= 0.15:
            if last >= ma + 2 * std: out.append(Detector("BollingerRev", "Bollinger-Rev", 0, 78, 1.5, "bollinger"))
            elif last <= ma - 2 * std: out.append(Detector("BollingerRev", "Bollinger-Rev", 1, 78, 1.5, "bollinger"))
    if n >= 30:
        z = (sum(sizes[-30:]) - 15.0) / math.sqrt(30 * 0.25)
        if abs(z) >= 1.2:
            out.append(Detector("ZScore", f"ZScore", 0 if z > 0 else 1, int(min(75, 55 + abs(z) * 8)), 0.9 + abs(z) * 0.3, "zscore"))

    return out

# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.beta_global = {}
        self.beta_regime = {}
        self.drift = {}
        self.drift_last = {}
        self.calibration = {}
        self.global_calibration = Calibration()
        self.scoring = {}
        self.global_scoring = Scoring()
        self.numerics = DirichletNumerics()
        self.loss_streak = 0
        self.shadow_rounds = 0  # Shadow mode cool-down
        self.paused = False
        self._load_persisted()

    def _load_persisted(self):
        raw = STORE.load_kv("state_v300")
        if not raw: return
        try:
            d = json.loads(raw)
            for k, v in d.get("beta_global", {}).items(): self.beta_global[k] = Beta.from_dict(v)
            for k, v in d.get("beta_regime", {}).items():
                reg, det = k.split("|", 1)
                self.beta_regime[(reg, det)] = Beta.from_dict(v)
            self.loss_streak = d.get("loss_streak", 0)
        except Exception: pass

    def save_persisted(self):
        try:
            d = {
                "beta_global": {k: b.to_dict() for k, b in self.beta_global.items()},
                "beta_regime": {f"{r}|{det}": b.to_dict() for (r, det), b in self.beta_regime.items()},
                "loss_streak": self.loss_streak,
            }
            STORE.save_kv("state_v300", json.dumps(d))
        except Exception: pass

STATE = BotState()

def get_beta(det: str) -> Beta:
    if det not in STATE.beta_global: STATE.beta_global[det] = Beta()
    return STATE.beta_global[det]

def get_beta_regime(regime: str, det: str) -> Beta:
    key = (regime, det)
    if key not in STATE.beta_regime: STATE.beta_regime[key] = Beta()
    return STATE.beta_regime[key]

def get_calibration(regime: Optional[str]) -> Calibration:
    if regime is None: return STATE.global_calibration
    if regime not in STATE.calibration: STATE.calibration[regime] = Calibration()
    return STATE.calibration[regime]

def get_scoring(regime: Optional[str]) -> Scoring:
    if regime is None: return STATE.global_scoring
    if regime not in STATE.scoring: STATE.scoring[regime] = Scoring()
    return STATE.scoring[regime]

# =========================
# ENSEMBLE
# =========================
def merge_detectors(detectors, regime_name):
    if not detectors: return None
    fam_count = Counter(d.family for d in detectors)
    big_score = small_score = total_w = 0.0
    used = []
    now = time.time()

    for d in detectors:
        regime_beta = get_beta_regime(regime_name, d.key) if regime_name else None
        beta = regime_beta if (regime_beta and regime_beta.n() >= 20) else get_beta(d.key)

        sampled_accuracy = beta.sample_thompson()
        flip = (sampled_accuracy < 0.40 and beta.n() >= 20)
        direction = (1 - d.dir) if flip else d.dir

        drift_fired = STATE.drift_last.get(d.key, 0)
        drift_mult = 0.4 if (now - drift_fired) < 30 else 1.0

        fc = fam_count[d.family]
        fam_mult = 0.55 if fc >= 3 else (0.75 if fc == 2 else 1.0)

        w = (d.conf / 100.0) * d.weight * (sampled_accuracy + 0.1) * fam_mult * drift_mult
        if flip: w *= 0.85

        if direction == 1: big_score += w
        else: small_score += w
        total_w += w

        used.append({
            "name": d.name, "key": d.key, "dir": direction, "flipped": flip,
            "beta_mean": round(beta.mean(), 3), "weight": round(w, 3), "raw_dir": d.dir
        })

    if total_w < 0.01: return None
    
    p_big = big_score / total_w
    raw_consensus = max(p_big, 1 - p_big)
    
    # 🔥 FIX: If only 1 signal fires, consensus cannot be 100%. It scales with number of signals.
    n_signals = len(used)
    consensus_penalty = min(n_signals, 4) / 4.0  # 1 sig = 25%, 2 sigs = 50%, 4 sigs = 100%
    consensus = raw_consensus * consensus_penalty
    
    edge = abs(p_big - 0.5) * 2
    raw_conf = int(50 + edge * 45)
    pred_dir = 1 if p_big >= 0.5 else 0

    return {"p_big": p_big, "dir": pred_dir, "raw_conf": raw_conf, "consensus": consensus, "used": used, "total_weight": total_w}

def generate_prediction():
    if len(STATE.history) < CFG.min_history: return None
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    
    regime = detect_regime(sizes)
    detectors = run_detectors(sizes, nums)
    merged = merge_detectors(detectors, regime.regime)
    if not merged: return None

    # Calibration
    cal = get_calibration(regime.regime)
    calibrated_conf = cal.calibrate(merged["raw_conf"])

    # 🔥 FIX: Kelly Stake based on CALIBRATED confidence, not raw.
    p_true = calibrated_conf / 100.0
    kelly = (p_true * 2) - 1  # 1:1 odds
    if kelly < 0: kelly = 0.0
    
    if kelly >= 0.15: stake = "2x"
    elif kelly >= 0.08: stake = "1.5x"
    elif kelly >= 0.03: stake = "1x"
    else: stake = "SKIP"

    # 🔥 FIX: Tilt Protection & Recovery Mode
    warning_msg = None
    if STATE.loss_streak >= 2:
        warning_msg = "RECOVERY"
        if merged["raw_conf"] < 80 or len(merged["used"]) < 3:
            stake = "SKIP"
            warning_msg = "TILT_PROTECTION"
        else:
            stake = "0.5x" # High confidence but still cautious
    elif STATE.loss_streak == 1:
        warning_msg = "RECOVERY"
        stake = "0.5x" if stake == "1x" else stake

    # 🔥 FIX: Shadow Mode (Cool-down after 3 losses)
    if STATE.shadow_rounds > 0:
        stake = "SKIP"
        warning_msg = "SHADOW_MODE"

    issue = str(int(STATE.history[-1]["issue"]) + 1)
    top_sigs = merged["used"][:6]
    
    # Number Picker (Simple Dirichlet + Transitions)
    probs = STATE.numerics.probs()
    pool = range(5, 10) if merged["dir"] == 1 else range(0, 5)
    num = max(pool, key=lambda x: probs[x])

    return {
        "issue": issue, "size": "BIG" if merged["dir"] == 1 else "SMALL", "num": num,
        "p_big": merged["p_big"], "confidence": calibrated_conf, "raw_conf": merged["raw_conf"],
        "consensus": merged["consensus"], "mode": regime.regime, "regime": regime.regime,
        "logic": ", ".join(s["name"] for s in top_sigs) or "no signals",
        "sigs": top_sigs, "deferral": warning_msg, "stake": stake,
        "details": {
            "detectors": [{"key": s["key"], "dir": s["dir"], "name": s["name"]} for s in merged["used"]],
            "regime": regime.regime, "p_big": merged["p_big"], "consensus": merged["consensus"]
        },
    }

def learn_from_resolution(res: dict):
    actual_size = 1 if res["actual_size"] == "BIG" else 0
    actual_big = (actual_size == 1)
    details = res.get("details") or {}
    regime = details.get("regime") or res.get("regime") or "BALANCED"
    p_big = res.get("p_big", 0.5)

    for d in details.get("detectors", []):
        correct = (d["dir"] == actual_size)
        get_beta(d["key"]).update(correct)
        get_beta_regime(regime, d["key"]).update(correct)
        if STATE.drift.get(d["key"], PageHinkley()).update(1.0 if correct else 0.0):
            STATE.drift_last[d["key"]] = time.time()

    raw = res.get("raw_conf")
    if isinstance(raw, int):
        get_calibration(regime).add(raw, res["win"])
        get_calibration(None).add(raw, res["win"])

    get_scoring(regime).add(p_big, actual_big)
    get_scoring(None).add(p_big, actual_big)

    if res.get("actual_num") is not None:
        STATE.numerics.update(int(res["actual_num"]))

    STATE.save_persisted()

async def fetch_data(session):
    try:
        async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
            if r.status != 200: return False
            data = await r.json()
            items = data.get("data", {}).get("list", [])
            if not items: return False
            items.sort(key=lambda x: int(x["issueNumber"]))
            out = []
            seen = set()
            for item in items:
                try:
                    issue = str(item["issueNumber"])
                    if issue in seen: continue
                    seen.add(issue)
                    num = int(item["number"])
                    out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                except Exception: continue
            STATE.history = out
            return True
    except Exception: return False

# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n" for r in recent_rows[-12:])
    sig_text = "\n".join(f"  {h(s['name'])} → {'▲ BIG' if s['dir']==1 else '▼ SMALL'}{' ⇄' if s.get('flipped') else ''}" for s in p["sigs"][:6]) or "  <i>no active signals</i>"
    
    p_big_pct = p["p_big"] * 100
    consensus_pct = p["consensus"] * 100

    warning = ""
    if p.get("deferral") == "TILT_PROTECTION":
        warning = "\n🛑 <b>TILT PROTECTION</b> — Weak signals after losses. SKIPPING.\n"
    elif p.get("deferral") == "SHADOW_MODE":
        warning = "\n👻 <b>SHADOW MODE</b> — 3+ Losses. Observing silently.\n"
    elif p.get("deferral") == "RECOVERY":
        warning = "\n🛡️ <b>RECOVERY MODE</b> — Reduced risk.\n"

    stake_emoji = {"2x": "🔥 2x", "1.5x": "🟢 1.5x", "1x": "🟡 1x", "0.5x": "🔴 0.5x", "SKIP": "⛔ SKIP"}.get(p['stake'], "1x")

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

🎲 <b>P(BIG)</b>: <b>{p_big_pct:.1f}%</b>
📈 <b>Confidence</b>: <b>{p['confidence']}%</b> <i>(raw {p['raw_conf']}%)</i>
🧠 <b>Regime</b>: <b>{h(p['regime'])}</b> · Consensus {consensus_pct:.0f}%
💰 <b>Stake (Kelly)</b>: <b>{stake_emoji}</b>
{warning}
🔎 <b>Active Signals</b>:
{sig_text}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""

def format_result(res):
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])} <i>(Stake: {res['stake']})</i>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except (TimedOut, NetworkError): await asyncio.sleep(2)
        except Exception as e: log.error(f"Send error: {e}"); break
    return False

# =========================
# COMMANDS
# =========================
async def cmd_start(update, ctx):
    await update.message.reply_text(f"🤖 <b>{h(CFG.bot_name)}</b>\n\n/status - Engine overview\n/stats - Accuracy\n/brier - Calibration scores", parse_mode="HTML")

async def cmd_status(update, ctx):
    total, wins = STORE.overall_stats()
    acc = round(wins / total * 100, 1) if total else 0
    await update.message.reply_text(f"📊 <b>Status</b>\nHistory: {len(STATE.history)}\nAccuracy: {acc}% ({wins}/{total})\nLoss streak: {STATE.loss_streak}\nShadow Mode: {STATE.shadow_rounds} rounds left\nPaused: {STATE.paused}", parse_mode="HTML")

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info(f"{CFG.bot_name} started...")
    last_processed_issue = None

    while True:
        try:
            if await fetch_data(session):
                hist_dict = {x["issue"]: x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)
                
                for res in resolved:
                    learn_from_resolution(res)
                    if res["win"]: 
                        STATE.loss_streak = 0
                        STATE.shadow_rounds = 0 # Exit shadow mode on win
                    else: 
                        STATE.loss_streak += 1
                        if STATE.loss_streak >= 3:
                            STATE.shadow_rounds = 2 # Activate shadow mode for 2 rounds
                    
                    # Always send result, even if skipped
                    await send_msg(app, format_result(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)

                if latest_issue != last_processed_issue and not STATE.paused:
                    if not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            
                            # If Shadow Mode is active, don't send to TG, just decrement
                            if STATE.shadow_rounds > 0:
                                STATE.shadow_rounds -= 1
                                log.info(f"Shadow Mode active: {STATE.shadow_rounds} rounds left. Skipping prediction.")
                            else:
                                await send_msg(app, format_prediction(p, STORE.recent_outcomes(12)))
                                
                            last_processed_issue = latest_issue
            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception(f"Loop error: {e}"); await asyncio.sleep(2)

# =========================
# MAIN
# =========================
async def main():
    CFG.validate()
    if not CFG.telegram_token: raise RuntimeError("TG_TOKEN env variable is empty")

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
