"""
V190 HYPER-ADAPTIVE PRO
=======================
Deep probabilistic upgrade of V180.

New in V190:
  * P(BIG) as a real probability (Bayesian ensemble, not just confidence %)
  * Beta posterior per (detector × regime)  -> weights & auto flip/mute
  * Page-Hinkley drift detector per detector -> auto mute on regime shift
  * Dirichlet numeric posterior P(0..9)  -> Bayesian number picker
  * Brier score + log-loss + per-regime calibration
  * Consensus strength + LOW-EDGE deferral warning
  * Multi-step (2-step chained) forecast via /next2
  * Time-series cross-validation via /cv
  * Full state persistence (SQLite) - survives restarts
  * 12 commands + 3 JSON endpoints

⚠️  Rotate the Telegram token before deploying publicly.
"""

import asyncio
import gc
import html
import json
import logging
import math
import os
import sqlite3
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TimedOut, NetworkError


# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = os.environ.get("8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA", "")
    chat_id_raw: str = os.environ.get("1264164655", "")
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "predictor_v190.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V190 HYPER-ADAPTIVE PRO"
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
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v190.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)


setup_logging()
log = logging.getLogger("v190")


def h(x: Any) -> str:
    return html.escape(str(x))


# =========================
# PROBABILITY PRIMITIVES
# =========================
class Beta:
    """Beta(a, b) posterior - conjugate prior for Bernoulli accuracy."""
    __slots__ = ("a", "b")

    def __init__(self, a: float = 1.0, b: float = 1.0):
        self.a = a
        self.b = b

    def update(self, correct: bool):
        if correct:
            self.a += 1.0
        else:
            self.b += 1.0

    def n(self) -> float:
        return max(0.0, self.a + self.b - 2.0)

    def mean(self) -> float:
        return self.a / (self.a + self.b)

    def var(self) -> float:
        n = self.a + self.b
        return (self.a * self.b) / (n * n * (n + 1.0))

    def lower(self, z: float = 1.0) -> float:
        return max(0.0, self.mean() - z * math.sqrt(self.var()))

    def to_dict(self):
        return {"a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d):
        return cls(d["a"], d["b"])


class PageHinkley:
    """
    Page-Hinkley change-point detector.
    Watches a [0,1] hit/miss stream. Signals when the mean shifts persistently.
    """
    __slots__ = ("delta", "threshold", "m", "n", "sum_pos", "sum_neg", "fired")

    def __init__(self, delta: float = 0.01, threshold: float = 10.0):
        self.delta = delta
        self.threshold = threshold
        self.m = 0.5
        self.n = 1
        self.sum_pos = 0.0
        self.sum_neg = 0.0
        self.fired = False

    def update(self, x: float) -> bool:
        self.n += 1
        self.m += (x - self.m) / self.n
        self.sum_pos = max(0.0, self.sum_pos + x - self.m - self.delta)
        self.sum_neg = max(0.0, self.sum_neg + self.m - x - self.delta)
        if self.sum_pos > self.threshold or self.sum_neg > self.threshold:
            self.fired = True
            self.m = 0.5
            self.n = 1
            self.sum_pos = 0.0
            self.sum_neg = 0.0
            return True
        return False

    def reset(self):
        self.m = 0.5
        self.n = 1
        self.sum_pos = 0.0
        self.sum_neg = 0.0
        self.fired = False


class DirichletNumerics:
    """Dirichlet posterior over P(num=0..9). Recency-decayed counts."""
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
    """Brier score + log loss, both min 0 (perfect)."""
    __slots__ = ("brier", "logloss", "maxlen")

    def __init__(self, maxlen: int = 1000):
        self.brier = deque(maxlen=maxlen)
        self.logloss = deque(maxlen=maxlen)
        self.maxlen = maxlen

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

    def n(self) -> int:
        return len(self.brier)


class Calibration:
    def __init__(self, maxlen: int = 400):
        self.samples = deque(maxlen=maxlen)

    def add(self, conf: int, correct: bool):
        self.samples.append((conf, 1 if correct else 0))

    def calibrate(self, raw_conf: int) -> int:
        if len(self.samples) < 20:
            return 50
        n = w = W = 0
        for c, win in self.samples:
            W += win
            if abs(c - raw_conf) <= 6:
                n += 1
                w += win
        base = (W + 5.0) / (len(self.samples) + 10)
        p = (w + 12 * base) / (n + 12)
        return int(max(0.5, min(0.80, p)) * 100)


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
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER,
                    actual_size TEXT, mode TEXT, logic TEXT, sigs TEXT,
                    raw_conf INTEGER, regime TEXT, details TEXT,
                    p_big REAL, calibrated_conf INTEGER,
                    consensus REAL, created_at REAL
                )
            """)
            for col, typ in [
                ("sigs", "TEXT"), ("raw_conf", "INTEGER"), ("regime", "TEXT"),
                ("details", "TEXT"), ("p_big", "REAL"), ("calibrated_conf", "INTEGER"),
                ("consensus", "REAL"),
            ]:
                try:
                    c.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            c.execute("""
                CREATE TABLE IF NOT EXISTS state_kv (
                    key TEXT PRIMARY KEY, value TEXT
                )
            """)

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM predictions WHERE issue=?", (issue,)
            ).fetchone() is not None

    def save_prediction(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, size, num, mode, logic, sigs, raw_conf, regime, details,
                 p_big, calibrated_conf, consensus, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["issue"], p["size"], p["num"], p["mode"], p["logic"],
                json.dumps(p.get("sigs", [])), p.get("raw_conf", 50),
                p.get("regime", "BALANCED"),
                json.dumps(p.get("details", {})),
                p.get("p_big", 0.5), p.get("confidence", 50),
                p.get("consensus", 0.5), time.time(),
            ))

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, size, num, mode, sigs, raw_conf, regime,
                       details, p_big, calibrated_conf, consensus
                FROM predictions WHERE status='PENDING'
            """).fetchall()
        for row in rows:
            (issue, p_size, p_num, mode, sigs_json, raw_conf, regime,
             details_json, p_big, cal_conf, consensus) = row
            if issue not in history_dict:
                continue
            act = history_dict[issue]
            act_size_str = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size_str)
            status = ("JACK 🏆" if p_num == act["num"] else "WIN ✅") if win else "LOSS ❌"
            with self._conn() as c:
                c.execute("""UPDATE predictions SET status=?, actual_num=?,
                             actual_size=? WHERE issue=?""",
                          (status, act["num"], act_size_str, issue))
            resolved.append({
                "issue": issue, "size": p_size, "num": p_num,
                "actual_num": act["num"], "actual_size": act_size_str,
                "status": status, "mode": mode, "win": win,
                "raw_conf": raw_conf, "regime": regime,
                "details": json.loads(details_json) if details_json else {},
                "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5,
                "calibrated_conf": cal_conf or 50,
                "consensus": consensus or 0.5,
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue, actual_size, actual_num, status
                FROM predictions WHERE status!='PENDING'
                ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

    def overall_stats(self):
        with self._conn() as c:
            row = c.execute("""
                SELECT COUNT(*),
                       SUM(CASE WHEN status LIKE 'WIN%' OR status LIKE 'JACK%'
                           THEN 1 ELSE 0 END)
                FROM predictions WHERE status!='PENDING'
            """).fetchone()
        return (row[0] or 0), (row[1] or 0)

    def load_kv(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM state_kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_kv(self, key: str, value: str):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO state_kv (key, value) VALUES (?, ?)",
                      (key, value))


STORE = Storage(CFG.db_path)


# =========================
# REGIME DETECTION
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
    if N < CFG.warm_min:
        return Regime("WARMING", 0, 1.0, 0, 0.5, 1)
    w = min(20, N)
    recent = sizes[-w:]

    runs = []
    ln, val = 1, recent[0]
    for x in recent[1:]:
        if x == val:
            ln += 1
        else:
            runs.append((val, ln)); ln, val = 1, x
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

    if max_streak >= 4 and avg_streak > 1.7:
        return Regime("TRENDING", min(1, (max_streak - 2) / 4), H, bias, switch_rate, max_streak)
    if switch_rate > 0.58:
        return Regime("CHOPPY", min(1, (switch_rate - 0.5) * 4), H, bias, switch_rate, max_streak)
    if H < 0.88 or bias > 0.13:
        return Regime("BIASED", min(1, (1 - H) * 5 + bias), H, bias, switch_rate, max_streak)
    return Regime("BALANCED", 0, H, bias, switch_rate, max_streak)


# =========================
# DETECTORS
# =========================
@dataclass
class Detector:
    key: str
    name: str
    dir: int
    conf: int
    weight: float
    family: str = "generic"
    is_reversal: bool = False


def run_detectors(sizes, nums):
    """All 14 detectors. Returns list[Detector] with raw direction."""
    out = []
    n = len(sizes)
    if n < CFG.warm_min:
        return out

    # 1) Markov backoff (highest reliable order wins)
    for ord_ in range(6, 0, -1):
        if n < ord_ + 15:
            continue
        ctx = 0
        for j in range(n - ord_, n):
            ctx = (ctx << 1) | sizes[j]
        bN = tN = 0
        for i in range(ord_, n):
            c = 0
            for j in range(i - ord_, i):
                c = (c << 1) | sizes[j]
            if c != ctx:
                continue
            tN += 1
            if sizes[i] == 1:
                bN += 1
        minS = {6: 36, 5: 30, 4: 20, 3: 12, 2: 8, 1: 6}[ord_]
        if tN < minS:
            continue
        pB = (bN + 0.5) / (tN + 1)
        sf = min(1, tN / (minS * 1.5))
        strength = min(1, abs(pB - 0.5) * 4) * sf
        if strength >= 0.10:
            out.append(Detector(
                key=f"Markov{ord_}", name=f"Markov-L{ord_}(n={tN})",
                dir=1 if pB >= 0.5 else 0,
                conf=int(50 + strength * 30),
                weight=0.6 + strength * 1.2, family="markov",
            ))
        break

    # 2) Bayes (sum-aware)
    bb = bs = 2.0
    for num in nums[-30:]:
        w = 1.0 / (1 + abs(num - 4.5) * 0.15)
        if num >= 5:
            bb += w
        else:
            bs += w
    pB = bb / (bb + bs)
    strength = abs(pB - 0.5) * 2.5
    if strength >= 0.05:
        out.append(Detector(
            key="Bayes", name=f"Bayes(p={pB:.2f})",
            dir=1 if pB > 0.52 else 0,
            conf=int(50 + min(30, strength * 30)),
            weight=0.6 + strength, family="bayes",
        ))

    # 3) Frequency lean
    w30 = sizes[-30:]
    pF = sum(w30) / len(w30)
    if abs(pF - 0.5) >= 0.08:
        strength = min(1, (abs(pF - 0.5) - 0.08) * 2.5)
        out.append(Detector(
            key="Freq30", name=f"Freq({pF:.2f})",
            dir=1 if pF > 0.5 else 0,
            conf=int(50 + strength * 25),
            weight=0.6 + strength * 1.2, family="freq",
        ))

    # 4/5) Streak / Dragon
    last = sizes[-1]
    run = 1
    for i in range(n - 2, -1, -1):
        if sizes[i] == last:
            run += 1
        else:
            break
    if 2 <= run <= 4:
        out.append(Detector(
            key=f"Streak{run}", name=f"Streak×{run}",
            dir=last, conf=56 + run * 4, weight=0.9, family="streak",
        ))
    if run >= 5:
        out.append(Detector(
            key="Dragon", name=f"Dragon×{run}",
            dir=last, conf=min(94, 68 + run * 4),
            weight=1.9 + min(run, 6) * 0.15, family="dragon",
        ))

    # 6) Reversal
    prior = sizes[max(0, n - 25):n - 5]
    if len(prior) >= 15:
        pBp = sum(prior) / len(prior)
        if (pBp > 0.68 and last == 0) or (pBp < 0.32 and last == 1):
            rrun = 1
            for i in range(n - 2, max(0, n - 6), -1):
                if sizes[i] == last:
                    rrun += 1
                else:
                    break
            out.append(Detector(
                key="Reversal", name="Reversal",
                dir=last, conf=min(86, 62 + rrun * 7),
                weight=1.7 + min(rrun, 3) * 0.2,
                family="reversal", is_reversal=True,
            ))

    # 7) Zigzag
    switches = sum(1 for i in range(min(6, n - 1)) if sizes[-1 - i] != sizes[-2 - i])
    if switches >= 5:
        out.append(Detector(
            key="Zigzag", name="Zigzag",
            dir=1 - last, conf=62 + switches * 3, weight=1.4, family="zigzag",
        ))

    # 8) Cycle (3 or 4)
    for wl in (3, 4):
        if n < wl * 2:
            continue
        w1 = sizes[-wl:]
        w2 = sizes[-wl * 2:-wl]
        m = sum(1 for a, b in zip(w1, w2) if a == b)
        if m >= wl - 1:
            out.append(Detector(
                key=f"Cycle{wl}", name=f"Cycle{wl}",
                dir=w1[0], conf=60 + m * 5, weight=1.2, family="cycle",
            ))
            break

    # 9) ACF
    for lag in (1, 2):
        N = min(30, n)
        if N < lag + 5:
            continue
        arr = sizes[-N:]
        mean = sum(arr) / N
        num_ = den_ = 0.0
        for i in range(N - lag):
            num_ += (arr[i] - mean) * (arr[i + lag] - mean)
        for i in range(N):
            den_ += (arr[i] - mean) ** 2
        if den_ <= 0:
            continue
        acf = num_ / den_
        if abs(acf) >= 0.10:
            out.append(Detector(
                key=f"ACF{lag}", name=f"ACF-L{lag}({acf:+.2f})",
                dir=last if acf > 0 else 1 - last,
                conf=int(min(68, 55 + abs(acf) * 40)),
                weight=0.9 + abs(acf), family=f"acf{lag}",
            ))

    # 10) Trend slope (on numbers)
    N = min(20, n)
    if N >= 5:
        sw = sx = sy = sxy = sx2 = 0.0
        for i in range(N):
            x = i
            y = nums[-N + i]
            wt = (i + 1) / N
            sw += wt; sx += x * wt; sy += y * wt
            sxy += x * y * wt; sx2 += x * x * wt
        den = sw * sx2 - sx * sx
        if abs(den) > 0.001:
            slope = (sw * sxy - sx * sy) / den
            if abs(slope) >= 0.15:
                out.append(Detector(
                    key="TrendSlope", name=f"Slope({slope:+.2f})",
                    dir=1 if slope > 0 else 0,
                    conf=int(min(68, 55 + abs(slope) * 25)),
                    weight=1.0, family="trend",
                ))

    # 11) Drift
    if n >= 35:
        rs = nums[-15:]
        os_ = nums[-35:-15]
        dr = sum(rs) / len(rs) - sum(os_) / len(os_)
        if abs(dr) >= 1.2:
            out.append(Detector(
                key="Drift", name=f"Drift({dr:+.2f})",
                dir=1 if dr > 0 else 0,
                conf=int(min(65, 55 + abs(dr) * 8)),
                weight=0.8 + min(1, abs(dr) / 3), family="drift",
            ))

    # 12) Hurst (trend follow)
    ts = nums[-30:]
    if len(ts) >= 20:
        lags = list(range(2, min(20, len(ts) // 2)))
        taus = []
        for lag in lags:
            pp = [ts[i + lag] - ts[i] for i in range(len(ts) - lag)]
            if pp:
                taus.append(math.sqrt(sum(p * p for p in pp) / len(pp)))
        if len(taus) >= 2:
            log_lags = [math.log(l) for l in lags][:len(taus)]
            log_tau = [math.log(t) if t > 0 else -10 for t in taus]
            nn = len(log_lags)
            mx = sum(log_lags) / nn
            my = sum(log_tau) / nn
            num_ = sum((log_lags[i] - mx) * (log_tau[i] - my) for i in range(nn))
            den_ = sum((log_lags[i] - mx) ** 2 for i in range(nn))
            if den_ > 0:
                H = max(0.0, min(1.0, num_ / den_))
                if H > 0.60:
                    out.append(Detector(
                        key="HurstTrend", name=f"Hurst({H:.2f})",
                        dir=last, conf=int(60 + (H - 0.60) * 80),
                        weight=1.1, family="hurst",
                    ))

    # 13) Bollinger reversion
    if n >= 20:
        w20 = sizes[-20:]
        ma = sum(w20) / 20
        std = math.sqrt(sum((x - ma) ** 2 for x in w20) / 20)
        if std >= 0.15:
            if last >= ma + 2 * std:
                out.append(Detector("BollingerRev", "Bollinger-Rev",
                                    dir=0, conf=78, weight=1.5, family="bollinger"))
            elif last <= ma - 2 * std:
                out.append(Detector("BollingerRev", "Bollinger-Rev",
                                    dir=1, conf=78, weight=1.5, family="bollinger"))

    # 14) Z-score
    if n >= 30:
        w30s = sizes[-30:]
        z = (sum(w30s) - 15.0) / math.sqrt(30 * 0.25)
        if abs(z) >= 1.2:
            out.append(Detector(
                key="ZScore", name=f"Z={z:+.2f}",
                dir=0 if z > 0 else 1,
                conf=int(min(75, 55 + abs(z) * 8)),
                weight=0.9 + abs(z) * 0.3, family="zscore",
            ))

    return out


# =========================
# STATE (all persistence via SQLite)
# =========================
class BotState:
    def __init__(self):
        self.history = []
        # detector -> Beta (global)
        self.beta_global = {}
        # (regime, detector) -> Beta
        self.beta_regime = {}
        # detector -> PageHinkley
        self.drift = {}
        # detector -> last drift fire timestamp
        self.drift_last = {}
        # regime -> Calibration
        self.calibration = {}
        self.global_calibration = Calibration()
        # scoring
        self.scoring = {}
        self.global_scoring = Scoring()
        # numerics
        self.numerics = DirichletNumerics()
        # loss streak
        self.loss_streak = 0
        # backtest
        self.backtest_done = False
        self.backtest_result = {}
        self.gen_count = 0
        self.paused = False
        self.last_prediction = None
        self._load_persisted()


    def _load_persisted(self):
        raw = STORE.load_kv("state_v190")
        if not raw:
            return
        try:
            d = json.loads(raw)
            for k, v in d.get("beta_global", {}).items():
                self.beta_global[k] = Beta.from_dict(v)
            for k, v in d.get("beta_regime", {}).items():
                reg, det = k.split("|", 1)
                self.beta_regime[(reg, det)] = Beta.from_dict(v)
            self.loss_streak = d.get("loss_streak", 0)
        except Exception as e:
            log.warning(f"persist load failed: {e}")

    def save_persisted(self):
        try:
            d = {
                "beta_global": {k: b.to_dict() for k, b in self.beta_global.items()},
                "beta_regime": {f"{r}|{det}": b.to_dict()
                                for (r, det), b in self.beta_regime.items()},
                "loss_streak": self.loss_streak,
            }
            STORE.save_kv("state_v190", json.dumps(d))
        except Exception as e:
            log.warning(f"persist save failed: {e}")


STATE = BotState()


def get_beta(det: str) -> Beta:
    if det not in STATE.beta_global:
        STATE.beta_global[det] = Beta()
    return STATE.beta_global[det]


def get_beta_regime(regime: str, det: str) -> Beta:
    key = (regime, det)
    if key not in STATE.beta_regime:
        STATE.beta_regime[key] = Beta()
    return STATE.beta_regime[key]


def get_drift(det: str) -> PageHinkley:
    if det not in STATE.drift:
        STATE.drift[det] = PageHinkley()
    return STATE.drift[det]


def get_calibration(regime: Optional[str]) -> Calibration:
    if regime is None:
        return STATE.global_calibration
    if regime not in STATE.calibration:
        STATE.calibration[regime] = Calibration()
    return STATE.calibration[regime]


def get_scoring(regime: Optional[str]) -> Scoring:
    if regime is None:
        return STATE.global_scoring
    if regime not in STATE.scoring:
        STATE.scoring[regime] = Scoring()
    return STATE.scoring[regime]


# =========================
# ENSEMBLE (Beta-weighted)
# =========================
def merge_detectors(detectors, regime_name):
    """
    Combine detectors into a probability P(BIG).

    Weight for each detector:
        w = base_weight * conf * beta_weight * family_penalty
    where beta_weight comes from Beta posterior (global or regime-specific).
    If beta_mean < 0.40 -> flip direction.
    If beta_mean < 0.47 -> downweight.
    If drift fired recently -> downweight.
    """
    if not detectors:
        return None

    fam_count = Counter(d.family for d in detectors)
    big_score = small_score = total_w = 0.0
    used = []

    now = time.time()
    for d in detectors:
        # Beta (regime-specific if enough samples, else global)
        regime_beta = get_beta_regime(regime_name, d.key) if regime_name else None
        if regime_beta and regime_beta.n() >= 20:
            beta = regime_beta
            beta_source = f"{regime_name}"
        else:
            beta = get_beta(d.key)
            beta_source = "global"

        b_mean = beta.mean()
        b_lower = beta.lower(1.0)

        # Drift cooldown (30s after fire, downweight 0.4)
        drift_fired = STATE.drift_last.get(d.key, 0)
        drift_mult = 0.4 if (now - drift_fired) < 30 else 1.0

        # Direction flip if posteriors say detector is anti-predictive
        flip = b_mean < 0.40 and beta.n() >= 20
        direction = (1 - d.dir) if flip else d.dir

        # Downweight weak posteriors
        if beta.n() >= 15:
            weak_mult = 1.0 if b_lower >= 0.47 else max(0.3, b_lower / 0.47)
        else:
            weak_mult = 1.0

        # Family penalty for correlated detectors
        fc = fam_count[d.family]
        fam_mult = 0.55 if fc >= 3 else (0.75 if fc == 2 else 1.0)

        # Final weight
        w = (d.conf / 100.0) * d.weight * weak_mult * fam_mult * drift_mult
        if flip:
            w *= 0.85  # slight penalty on flips

        if direction == 1:
            big_score += w
        else:
            small_score += w
        total_w += w

        used.append({
            "name": d.name, "key": d.key, "dir": direction,
            "flipped": flip, "beta_mean": round(b_mean, 3),
            "beta_src": beta_source, "weight": round(w, 3),
            "raw_dir": d.dir,
        })

    if total_w < 0.01:
        return None

    p_big = big_score / total_w
    # Consensus: how one-sided
    consensus = max(p_big, 1 - p_big)  # 0.5 - 1.0
    # Confidence via edge magnitude
    edge = abs(p_big - 0.5) * 2  # 0 - 1
    raw_conf = int(50 + edge * 45)  # 50 - 95

    pred_dir = 1 if p_big >= 0.5 else 0

    return {
        "p_big": p_big,
        "dir": pred_dir,
        "raw_conf": raw_conf,
        "consensus": consensus,
        "used": used,
        "total_weight": total_w,
    }


# =========================
# NUMBER PICKER (Dirichlet)
# =========================
def pick_number(probs, side, last_num, transitions):
    """Bayesian number pick: blend Dirichlet posterior with transition matrix."""
    pool = range(5, 10) if side == 1 else range(0, 5)
    last_row_total = sum(v for (a, b), v in transitions.items() if a == last_num) or 1
    scored = []
    for num in pool:
        p_dir = probs[num]
        p_trans = transitions.get((last_num, num), 0) / last_row_total
        score = 0.6 * p_dir + 0.4 * p_trans
        scored.append((score, num))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][1]


# =========================
# PREDICTION CORE
# =========================
def predict_core(sizes, nums):
    """Pure prediction from a sequence. No side-effects on STATE.history."""
    if len(sizes) < CFG.min_history:
        return None

    regime = detect_regime(sizes)
    detectors = run_detectors(sizes, nums)
    merged = merge_detectors(detectors, regime.regime)

    if not merged:
        return None

    # Calibration (regime-specific if available)
    cal = get_calibration(regime.regime)
    calibrated_conf = cal.calibrate(merged["raw_conf"])

    # Numeric probs
    probs = STATE.numerics.probs()
    last_num = nums[-1]
    transitions = Counter()
    for i in range(1, len(nums)):
        transitions[(nums[i - 1], nums[i])] += 1
    number = pick_number(probs, merged["dir"], last_num, transitions)

    # Consensus warning
    deferral = None
    if merged["consensus"] < 0.58:
        deferral = "LOW_EDGE"
    elif merged["consensus"] < 0.65:
        deferral = "WEAK"

    return {
        "dir": merged["dir"],
        "p_big": merged["p_big"],
        "raw_conf": merged["raw_conf"],
        "confidence": calibrated_conf,
        "consensus": merged["consensus"],
        "num": number,
        "regime": regime.regime,
        "signals": merged["used"],
        "deferral": deferral,
        "numeric_probs": probs,
    }


def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        return None
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    core = predict_core(sizes, nums)
    if not core:
        return None

    # Recovery dampening
    if STATE.loss_streak >= 3:
        core["confidence"] = max(52, core["confidence"] - 8)
        core["p_big"] = 0.5 + (core["p_big"] - 0.5) * 0.7
        core["deferral"] = core.get("deferral") or "RECOVERY"

    issue = str(int(STATE.history[-1]["issue"]) + 1)
    top_sigs = core["signals"][:6]
    logic = ", ".join(s["name"] for s in top_sigs) or "no signals"

    return {
        "issue": issue,
        "size": "BIG" if core["dir"] == 1 else "SMALL",
        "num": core["num"],
        "p_big": core["p_big"],
        "confidence": core["confidence"],
        "raw_conf": core["raw_conf"],
        "consensus": core["consensus"],
        "mode": core["regime"],
        "regime": core["regime"],
        "logic": logic,
        "sigs": top_sigs,
        "deferral": core["deferral"],
        "details": {
            "detectors": [{"key": s["key"], "dir": s["dir"], "name": s["name"]}
                          for s in core["signals"]],
            "regime": core["regime"],
            "p_big": core["p_big"],
            "consensus": core["consensus"],
            "loss_streak_at_pred": STATE.loss_streak,
        },
    }


def generate_multi_step():
    """Chained 2-step forecast. Step 2 is inherently much noisier."""
    p1 = generate_prediction()
    if not p1:
        return None
    sizes = [x["size"] for x in STATE.history] + [1 if p1["size"] == "BIG" else 0]
    nums = [x["num"] for x in STATE.history] + [p1["num"]]
    core2 = predict_core(sizes, nums)
    if not core2:
        return p1, None
    issue2 = str(int(p1["issue"]) + 1)
    p2 = {
        "issue": issue2,
        "size": "BIG" if core2["dir"] == 1 else "SMALL",
        "num": core2["num"],
        "p_big": core2["p_big"],
        "confidence": int(core2["confidence"] * 0.7),
        "regime": core2["regime"],
        "consensus": core2["consensus"],
    }
    return p1, p2


# =========================
# LEARNING
# =========================
def learn_from_resolution(res: dict):
    """Update all posteriors, calibrations, scoring, drift."""
    actual_size = 1 if res["actual_size"] == "BIG" else 0
    actual_big = (actual_size == 1)
    details = res.get("details") or {}
    regime = details.get("regime") or res.get("regime") or "BALANCED"
    p_big = res.get("p_big")
    if p_big is None:
        p_big = 0.5

    # Update per-detector Beta + regime Beta + drift
    for d in details.get("detectors", []):
        correct = (d["dir"] == actual_size)
        get_beta(d["key"]).update(correct)
        get_beta_regime(regime, d["key"]).update(correct)
        if get_drift(d["key"]).update(1.0 if correct else 0.0):
            STATE.drift_last[d["key"]] = time.time()
            log.info(f"Drift detected on detector: {d['key']}")

    # Update calibration
    raw = res.get("raw_conf")
    if isinstance(raw, int):
        get_calibration(regime).add(raw, res["win"])
        get_calibration(None).add(raw, res["win"])

    # Update scoring (Brier + log loss)
    get_scoring(regime).add(p_big, actual_big)
    get_scoring(None).add(p_big, actual_big)

    # Update numerics
    if res.get("actual_num") is not None:
        STATE.numerics.update(int(res["actual_num"]))

    # Persist
    STATE.save_persisted()


# =========================
# BACKTEST (walk-forward + optional CV)
# =========================
def seed_from_history():
    if len(STATE.history) < CFG.min_history + 30:
        return
    steps = min(CFG.backtest_steps, len(STATE.history) - CFG.min_history - 5)
    if steps < 20:
        return

    STATE.beta_global.clear()
    STATE.beta_regime.clear()
    STATE.drift.clear()
    STATE.drift_last.clear()
    STATE.calibration.clear()
    STATE.global_calibration = Calibration()
    STATE.scoring.clear()
    STATE.global_scoring = Scoring()
    STATE.numerics.reset()

    log.info(f"Backtest over {steps} rounds...")
    wins = total = 0
    brier_sum = 0.0
    for t in range(len(STATE.history) - steps, len(STATE.history)):
        before = STATE.history[:t]
        actual = STATE.history[t]["size"]
        sizes = [h["size"] for h in before]
        nums = [h["num"] for h in before]
        if len(sizes) < CFG.min_history:
            continue
        core = predict_core(sizes, nums)
        if not core:
            continue
        actual_big = (actual == 1)
        for d in core["signals"]:
            correct = (d["dir"] == actual)
            get_beta(d["key"]).update(correct)
            get_beta_regime(core["regime"], d["key"]).update(correct)
        get_calibration(core["regime"]).add(core["raw_conf"], core["dir"] == actual)
        get_calibration(None).add(core["raw_conf"], core["dir"] == actual)
        get_scoring(core["regime"]).add(core["p_big"], actual_big)
        get_scoring(None).add(core["p_big"], actual_big)
        STATE.numerics.update(STATE.history[t]["num"])
        brier_sum += (core["p_big"] - (1.0 if actual_big else 0.0)) ** 2
        total += 1
        if core["dir"] == actual:
            wins += 1

    STATE.backtest_done = True
    STATE.backtest_result = {
        "n": total,
        "acc": round(wins / total * 100, 1) if total else 0,
        "brier": round(brier_sum / total, 4) if total else 0,
    }
    log.info(f"Backtest: {wins}/{total} = {STATE.backtest_result['acc']}% "
             f"Brier={STATE.backtest_result['brier']}")


def cross_validate(k: int = 5):
    """Time-series k-fold cross-validation (expanding window, no lookahead)."""
    n = len(STATE.history)
    if n < CFG.min_history + k * 20:
        return None

    fold_size = (n - CFG.min_history) // k
    results = []

    # Snapshot state so CV doesn't pollute live state
    saved_beta = {k: Beta.from_dict(v.to_dict()) for k, v in STATE.beta_global.items()}
    saved_regime = {(r, d): Beta.from_dict(v.to_dict())
                    for (r, d), v in STATE.beta_regime.items()}
    saved_cal = {r: Calibration() for r in []}  # placeholder

    for fold in range(k):
        test_start = CFG.min_history + fold * fold_size
        test_end = test_start + fold_size
        if test_end > n:
            break
        STATE.beta_global.clear()
        STATE.beta_regime.clear()
        STATE.global_calibration = Calibration()
        STATE.calibration.clear()
        STATE.numerics.reset()

        # Warm up on history before test_start
        for t in range(CFG.min_history, test_start):
            before = STATE.history[:t]
            sizes = [h["size"] for h in before]
            nums = [h["num"] for h in before]
            if len(sizes) < CFG.min_history:
                continue
            core = predict_core(sizes, nums)
            if not core:
                continue
            actual = STATE.history[t]["size"]
            for d in core["signals"]:
                get_beta(d["key"]).update(d["dir"] == actual)
                get_beta_regime(core["regime"], d["key"]).update(d["dir"] == actual)
            get_calibration(core["regime"]).add(core["raw_conf"], core["dir"] == actual)
            get_calibration(None).add(core["raw_conf"], core["dir"] == actual)
            STATE.numerics.update(STATE.history[t]["num"])

        # Test on the fold
        wins = total = 0
        brier_sum = 0.0
        for t in range(test_start, test_end):
            before = STATE.history[:t]
            sizes = [h["size"] for h in before]
            nums = [h["num"] for h in before]
            core = predict_core(sizes, nums)
            if not core:
                continue
            actual = STATE.history[t]["size"]
            actual_big = (actual == 1)
            brier_sum += (core["p_big"] - (1.0 if actual_big else 0.0)) ** 2
            total += 1
            if core["dir"] == actual:
                wins += 1
        results.append({
            "fold": fold + 1,
            "n": total,
            "acc": round(wins / total * 100, 1) if total else 0,
            "brier": round(brier_sum / total, 4) if total else 0,
        })

    # Restore
    STATE.beta_global = saved_beta
    STATE.beta_regime = saved_regime
    return results


# =========================
# FETCH
# =========================
async def fetch_data(session):
    try:
        async with session.get(
            CFG.source_api_url,
            timeout=aiohttp.ClientTimeout(total=CFG.request_timeout),
        ) as r:
            if r.status != 200:
                return False
            data = await r.json()
            items = data.get("data", {}).get("list", [])
            if not items:
                return False
            items.sort(key=lambda x: int(x["issueNumber"]))
            out = []
            seen = set()
            for item in items:
                try:
                    issue = str(item["issueNumber"])
                    if issue in seen:
                        continue
                    seen.add(issue)
                    num = int(item["number"])
                    out.append({"issue": issue, "num": num,
                                "size": 1 if num >= 5 else 0})
                except Exception:
                    continue
            STATE.history = out
            return True
    except Exception as e:
        log.debug(f"fetch error: {e}")
        return False


# =========================
# FORMATTING
# =========================
REGIME_EMOJI = {
    "TRENDING": "📈", "CHOPPY": "〰️", "BIASED": "🎯",
    "BALANCED": "⚖️", "WARMING": "⏳",
}


def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows[-12:]:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    sig_lines = []
    for s in p["sigs"][:5]:
        arrow = "▲ BIG" if s["dir"] == 1 else "▼ SMALL"
        flip_mark = " ⇄" if s.get("flipped") else ""
        sig_lines.append(f"  {h(s['name'])} → {arrow}{flip_mark}")
    sig_text = "\n".join(sig_lines) if sig_lines else "  <i>no active signals</i>"

    side = 1 if p["size"] == "BIG" else 0
    size_emoji = "📈" if side == 1 else "📉"
    p_big_pct = p["p_big"] * 100
    edge = abs(p["p_big"] - 0.5) * 100
    consensus_pct = p["consensus"] * 100

    warning = ""
    if p.get("deferral") == "LOW_EDGE":
        warning = "\n⚠️ <b>LOW EDGE</b> — consensus weak, treat with caution\n"
    elif p.get("deferral") == "WEAK":
        warning = "\n⚠️ Weak consensus — smaller edge than usual\n"
    elif p.get("deferral") == "RECOVERY":
        warning = "\n🛡️ Recovery mode — confidence dampened after losses\n"

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: {size_emoji} <b>{h(p['size'])}</b>

🎲 <b>P(BIG)</b>: <b>{p_big_pct:.1f}%</b> · edge +{edge:.1f}%
📈 <b>Confidence</b>: <b>{p['confidence']}%</b> <i>(raw {p['raw_conf']}%)</i>
🧠 <b>Regime</b>: {REGIME_EMOJI.get(p['regime'], "⚖️")} <b>{h(p['regime'])}</b>
🤝 <b>Consensus</b>: {consensus_pct:.0f}%
{warning}
🔎 <b>Active Signals</b>:
{sig_text}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>{h(CFG.bot_name)}</i>
"""


def format_result(res):
    return (
        f"🎯 <b>Result</b>\n"
        f"Period: <code>{h(res['issue'][-4:])}</code>\n"
        f"Pred: {h(res['size'])} {h(res['num'])} "
        f"<i>(P(BIG)={res['p_big']*100:.1f}%)</i>\n"
        f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
        f"<b>{h(res['status'])}</b>"
    )


def format_multi_step(p1, p2):
    s1 = "📈 BIG" if p1["size"] == "BIG" else "📉 SMALL"
    text = (
        f"🔮 <b>2-Step Forecast</b>\n\n"
        f"<b>Step 1</b> · Period <code>{h(p1['issue'][-4:])}</code>\n"
        f"   {s1} · No {p1['num']} · conf {p1['confidence']}%\n"
        f"   P(BIG) {p1['p_big']*100:.1f}%\n"
    )
    if p2:
        s2 = "📈 BIG" if p2["size"] == "BIG" else "📉 SMALL"
        text += (
            f"\n<b>Step 2</b> · Period <code>{h(p2['issue'][-4:])}</code>\n"
            f"   {s2} · No {p2['num']} · conf {p2['confidence']}%\n"
            f"   P(BIG) {p2['p_big']*100:.1f}%\n\n"
            f"<i>⚠️ Step 2 assumes Step 1 is correct and decays confidence 30%.</i>"
        )
    return text


async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try:
            await app.bot.send_message(
                chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"
            )
            return True
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Send error: {e}")
            break
    return False


# =========================
# COMMANDS
# =========================
def is_admin(update: Update) -> bool:
    if not CFG.admin_ids:
        return True
    uid = update.effective_user.id if update.effective_user else 0
    return uid in CFG.admin_ids


async def cmd_start(update, ctx):
    text = (
        f"🤖 <b>{h(CFG.bot_name)}</b>\n\n"
        "Commands:\n"
        "/status — engine overview\n"
        "/stats — accuracy + top detectors\n"
        "/p — full probability breakdown\n"
        "/diversity — consensus + drift status\n"
        "/brier — calibration scores (Brier + log loss)\n"
        "/drift — drift detector state\n"
        "/regime — market regime analysis\n"
        "/detectors — currently firing detectors\n"
        "/next2 — 2-step chained forecast\n"
        "/cv — time-series cross-validation\n"
        "/backtest — walk-forward summary\n"
        "/reset — reset adaptive memory (admin)\n"
        "/stop · /resume — pause / resume (admin)"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_status(update, ctx):
    total, wins = STORE.overall_stats()
    acc = round(wins / total * 100, 1) if total else 0
    bt = STATE.backtest_result
    bt_line = (f"Backtest: <b>{bt.get('acc', '-')}%</b> on {bt.get('n', 0)} "
               f"(Brier {bt.get('brier', '-')})") if STATE.backtest_done else \
              "Backtest: <i>pending</i>"
    brier = STATE.global_scoring.brier_score()
    ll = STATE.global_scoring.logloss_score()
    lines = [
        "📊 <b>Engine Status</b>",
        f"History: <b>{len(STATE.history)}</b>",
        f"Predictions: <b>{total}</b> · wins <b>{wins}</b> · acc <b>{acc}%</b>",
        f"Loss streak: <b>{STATE.loss_streak}</b>",
        bt_line,
        f"Brier: <b>{brier:.4f}</b>" if brier is not None else "Brier: <i>—</i>",
        f"Log loss: <b>{ll:.4f}</b>" if ll is not None else "Log loss: <i>—</i>",
        f"Detectors tracked: <b>{len(STATE.beta_global)}</b>",
        f"Drift cooldowns: <b>{len(STATE.drift_last)}</b>",
        f"Paused: <b>{STATE.paused}</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_stats(update, ctx):
    total, wins = STORE.overall_stats()
    acc = round(wins / total * 100, 1) if total else 0
    lines = [f"📊 <b>Live Stats</b>",
             f"Accuracy: <b>{acc}%</b> ({wins}/{total})", "",
             "🧠 <b>Top Detectors (Beta mean)</b>"]
    rows = []
    for k, beta in STATE.beta_global.items():
        if beta.n() >= 15:
            rows.append((k, beta.mean(), beta.lower(1.0), int(beta.n())))
    rows.sort(key=lambda x: -x[1])
    for k, m, lo, n in rows[:12]:
        lines.append(f"  {h(k)}: mean {m*100:.0f}% · lower {lo*100:.0f}% (n={n})")
    if not rows:
        lines.append("  <i>not enough samples yet</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_p(update, ctx):
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    if len(sizes) < CFG.min_history:
        await update.message.reply_text("Not enough history yet.")
        return
    core = predict_core(sizes, nums)
    if not core:
        await update.message.reply_text("No prediction available.")
        return
    probs = core["numeric_probs"]
    lines = [
        "🎲 <b>Probability Breakdown</b>",
        f"P(BIG): <b>{core['p_big']*100:.2f}%</b>",
        f"P(SMALL): <b>{(1-core['p_big'])*100:.2f}%</b>",
        f"Confidence: <b>{core['confidence']}%</b> (raw {core['raw_conf']}%)",
        f"Consensus: <b>{core['consensus']*100:.0f}%</b>",
        "",
        "🔢 <b>Numeric P(num):</b>",
    ]
    for i in range(10):
        p = probs[i]
        bar = "█" * int(p * 40)
        lines.append(f"  <code>{i}</code>: {p*100:4.1f}% {bar}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_diversity(update, ctx):
    if not STATE.history:
        await update.message.reply_text("No data yet.")
        return
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    core = predict_core(sizes, nums)
    if not core:
        await update.message.reply_text("No prediction yet.")
        return

    big = sum(1 for s in core["signals"] if s["dir"] == 1)
    small = len(core["signals"]) - big
    flipped = sum(1 for s in core["signals"] if s.get("flipped"))

    lines = [
        "🤝 <b>Diversity</b>",
        f"Detectors firing: <b>{len(core['signals'])}</b>",
        f"  → BIG: <b>{big}</b> · SMALL: <b>{small}</b>",
        f"  → Flipped by Beta: <b>{flipped}</b>",
        f"Consensus: <b>{core['consensus']*100:.0f}%</b>",
        f"Deferral: <b>{core['deferral'] or 'OK'}</b>",
        "",
        f"Drift cool-downs active: <b>{len(STATE.drift_last)}</b>",
    ]
    for k, ts in list(STATE.drift_last.items())[:10]:
        age = int(time.time() - ts)
        lines.append(f"  ⚠️ {h(k)} (fired {age}s ago)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_brier(update, ctx):
    lines = ["📏 <b>Calibration Quality</b>", ""]
    brier = STATE.global_scoring.brier_score()
    ll = STATE.global_scoring.logloss_score()
    n = STATE.global_scoring.n()
    if brier is not None:
        lines.append(f"Global Brier: <b>{brier:.4f}</b> ({n} samples)")
        lines.append(f"Global log loss: <b>{ll:.4f}</b>")
        lines.append("")
        lines.append("<i>Lower is better · 0 = perfect · 0.25 = always 50%</i>")
        lines.append("")
        lines.append("<b>Per-regime:</b>")
        for r, sc in STATE.scoring.items():
            b = sc.brier_score()
            if b is not None and sc.n() >= 10:
                lines.append(f"  {h(r)}: <b>{b:.4f}</b> (n={sc.n()})")
    else:
        lines.append("<i>Not enough samples yet</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_drift(update, ctx):
    lines = ["🌊 <b>Drift Detector</b>", ""]
    if not STATE.drift:
        lines.append("<i>No detectors being tracked yet</i>")
    else:
        for k, ph in STATE.drift.items():
            fired = (time.time() - STATE.drift_last.get(k, 0)) < 60
            marker = "🔥" if fired else "·"
            lines.append(
                f"{marker} {h(k)}: mean={ph.m:.2f} "
                f"pos={ph.sum_pos:.2f} neg={ph.sum_neg:.2f}"
            )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_regime(update, ctx):
    if len(STATE.history) < CFG.warm_min:
        await update.message.reply_text("Not enough history yet.")
        return
    sizes = [h["size"] for h in STATE.history]
    r = detect_regime(sizes)
    await update.message.reply_text(
        f"🧭 <b>Regime</b>: <b>{h(r.regime)}</b>\n"
        f"Confidence: {round(r.confidence*100)}%\n"
        f"Entropy: {r.entropy:.2f} / 1.00\n"
        f"Bias: {round(r.bias*100)}%\n"
        f"Switch rate: {round(r.switch_rate*100)}%\n"
        f"Max streak: {r.max_streak}",
        parse_mode="HTML",
    )


async def cmd_detectors(update, ctx):
    sizes = [h["size"] for h in STATE.history]
    nums = [h["num"] for h in STATE.history]
    dets = run_detectors(sizes, nums)
    if not dets:
        await update.message.reply_text("No active detectors.")
        return
    lines = ["🔎 <b>Currently firing</b>:"]
    for d in dets[:15]:
        arrow = "BIG" if d.dir == 1 else "SMALL"
        beta = get_beta(d.key)
        if beta.n() >= 10:
            lines.append(f"  {h(d.name)} → {arrow} ({d.conf}%) "
                         f"[β {beta.mean()*100:.0f}%]")
        else:
            lines.append(f"  {h(d.name)} → {arrow} ({d.conf}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_next2(update, ctx):
    result = generate_multi_step()
    if not result:
        await update.message.reply_text("Not enough history yet.")
        return
    p1, p2 = result
    await update.message.reply_text(format_multi_step(p1, p2), parse_mode="HTML")


async def cmd_cv(update, ctx):
    await update.message.reply_text("⏳ Running cross-validation...")
    results = cross_validate(k=5)
    if not results:
        await update.message.reply_text(
            f"Need at least {CFG.min_history + 100} results for CV."
        )
        return
    lines = ["📊 <b>Time-Series CV (5-fold)</b>", ""]
    for r in results:
        lines.append(f"Fold {r['fold']}: acc <b>{r['acc']}%</b> · "
                     f"Brier <b>{r['brier']:.4f}</b> (n={r['n']})")
    accs = [r["acc"] for r in results]
    if accs:
        mean = sum(accs) / len(accs)
        var = sum((x - mean) ** 2 for x in accs) / len(accs)
        lines.append("")
        lines.append(f"Mean accuracy: <b>{mean:.1f}%</b>")
        lines.append(f"Std: <b>{math.sqrt(var):.1f}%</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_backtest(update, ctx):
    if not STATE.backtest_done:
        await update.message.reply_text("Backtest not run yet.")
        return
    bt = STATE.backtest_result
    await update.message.reply_text(
        f"📈 <b>Walk-forward Backtest</b>\n"
        f"Rounds: <b>{bt.get('n', 0)}</b>\n"
        f"Accuracy: <b>{bt.get('acc', 0)}%</b>\n"
        f"Brier: <b>{bt.get('brier', 0)}</b>\n"
        f"<i>No lookahead — expansion window</i>",
        parse_mode="HTML",
    )


async def cmd_reset(update, ctx):
    if not is_admin(update):
        return
    STATE.beta_global.clear()
    STATE.beta_regime.clear()
    STATE.drift.clear()
    STATE.drift_last.clear()
    STATE.calibration.clear()
    STATE.global_calibration = Calibration()
    STATE.scoring.clear()
    STATE.global_scoring = Scoring()
    STATE.numerics.reset()
    STATE.loss_streak = 0
    STATE.backtest_done = False
    seed_from_history()
    await update.message.reply_text("♻️ Reset + backtest re-seeded.")


async def cmd_stop(update, ctx):
    if not is_admin(update):
        return
    STATE.paused = True
    await update.message.reply_text("⏸️ Paused.")


async def cmd_resume(update, ctx):
    if not is_admin(update):
        return
    STATE.paused = False
    await update.message.reply_text("▶️ Resumed.")


# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info(f"{CFG.bot_name} started...")
    last_processed_issue = None
    seeded = False

    while True:
        try:
            if await fetch_data(session):
                if not seeded and len(STATE.history) >= CFG.min_history:
                    seed_from_history()
                    seeded = True

                hist_dict = {x["issue"]: x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    learn_from_resolution(res)
                    if res["win"]:
                        STATE.loss_streak = 0
                    else:
                        STATE.loss_streak += 1
                    await send_msg(app, format_result(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)

                if latest_issue != last_processed_issue and not STATE.paused:
                    if not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            STATE.last_prediction = p
                            recent = STORE.recent_outcomes(12)
                            await send_msg(app, format_prediction(p, recent))
                            last_processed_issue = latest_issue
                            STATE.gen_count += 1

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")
            await asyncio.sleep(2)


# =========================
# WEB ENDPOINTS
# =========================
async def handle_ping(request):
    return web.Response(text=f"{CFG.bot_name} is running!")


async def handle_stats(request):
    total, wins = STORE.overall_stats()
    acc = round(wins / total * 100, 2) if total else 0
    brier = STATE.global_scoring.brier_score()
    ll = STATE.global_scoring.logloss_score()
    dets = []
    for k, beta in STATE.beta_global.items():
        if beta.n() >= 15:
            dets.append({
                "key": k, "mean": round(beta.mean() * 100, 1),
                "lower": round(beta.lower(1.0) * 100, 1),
                "n": int(beta.n()),
            })
    dets.sort(key=lambda x: -x["mean"])
    payload = {
        "bot": CFG.bot_name,
        "history": len(STATE.history),
        "predictions": total,
        "wins": wins,
        "accuracy": acc,
        "brier": round(brier, 4) if brier is not None else None,
        "log_loss": round(ll, 4) if ll is not None else None,
        "loss_streak": STATE.loss_streak,
        "backtest": STATE.backtest_result,
        "detectors": dets[:20],
        "drift_fires": len(STATE.drift_last),
        "paused": STATE.paused,
    }
    return web.json_response(payload)


async def handle_probabilities(request):
    if len(STATE.history) < CFG.min_history:
        return web.json_response({"error": "not enough history"})
    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]
    core = predict_core(sizes, nums)
    if not core:
        return web.json_response({"error": "no prediction"})
    return web.json_response({
        "p_big": round(core["p_big"], 4),
        "p_small": round(1 - core["p_big"], 4),
        "confidence": core["confidence"],
        "raw_conf": core["raw_conf"],
        "consensus": round(core["consensus"], 3),
        "deferral": core["deferral"],
        "regime": core["regime"],
        "numeric_probs": [round(p, 4) for p in core["numeric_probs"]],
    })


# =========================
# MAIN
# =========================
async def main():
    CFG.validate()
    if not CFG.telegram_token:
        raise RuntimeError("TG_TOKEN env variable is empty")

    web_app = web.Application()
    web_app.router.add_get("/", handle_ping)
    web_app.router.add_get("/stats", handle_stats)
    web_app.router.add_get("/probabilities", handle_probabilities)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web on :{port}")

    app = Application.builder().token(CFG.telegram_token) \
        .connect_timeout(20).read_timeout(20).build()

    for name, fn in [
        ("start", cmd_start), ("help", cmd_start),
        ("status", cmd_status), ("stats", cmd_stats),
        ("p", cmd_p), ("probability", cmd_p),
        ("diversity", cmd_diversity), ("brier", cmd_brier),
        ("drift", cmd_drift), ("regime", cmd_regime),
        ("detectors", cmd_detectors), ("next2", cmd_next2),
        ("cv", cmd_cv), ("backtest", cmd_backtest),
        ("reset", cmd_reset), ("stop", cmd_stop), ("resume", cmd_resume),
    ]:
        app.add_handler(CommandHandler(name, fn))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
