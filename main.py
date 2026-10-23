"""
V180 HYPER-ADAPTIVE — Deep upgrade of V172.

Adds:
  - 14 detectors (Markov, Bayes, Freq, Streak, Dragon, Reversal, Zigzag,
    Cycle, ACF1/2, TrendSlope, Drift, Hurst, Bollinger, ZScore)
  - Adaptive per-detector state (FLIP / MUTE / ELITE)
  - Regime detection (TRENDING / CHOPPY / BIASED / BALANCED / WARMING)
  - Correlation penalty on same-family signals
  - Confidence calibration (raw -> calibrated)
  - Walk-forward backtest at startup (no lookahead)
  - Improved number picker (Markov transition + gap + neighbors)
  - Commands + JSON health endpoint

⚠️ SECURITY: The Telegram token is exposed in this file.
   Rotate it before sharing, or set TG_TOKEN env variable.
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
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TimedOut, NetworkError


# =========================
# CONFIG (env overrides supported)
# =========================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "predictor.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V180 HYPER-ADAPTIVE"
    backtest_steps: int = 200      # rounds used to seed adaptive states
    min_history: int = 60          # minimum rounds before predicting
    warm_min: int = 25             # below this: no detectors fire
    admin_ids: tuple = ()          # empty = any user can run admin commands

    def parse_chat_id(self):
        return int(self.chat_id_raw) if self.chat_id_raw.isdigit() else self.chat_id_raw

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
    fh = RotatingFileHandler(Path(CFG.log_dir) / "bot.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)


setup_logging()
log = logging.getLogger("v180")


def h(x: Any) -> str:
    return html.escape(str(x))


# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT,
                    mode TEXT, logic TEXT, sigs TEXT, created_at REAL
                )
            """)
            # additive migrations (safe on repeated startup)
            for col, typ in [
                ("sigs", "TEXT"), ("raw_conf", "INTEGER"),
                ("regime", "TEXT"), ("details", "TEXT"),
            ]:
                try:
                    c.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM predictions WHERE issue=?", (issue,)
            ).fetchone() is not None

    def save_prediction(self, issue, size, num, mode, logic, sigs, raw_conf, regime, details):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO predictions "
                "(issue, size, num, mode, logic, sigs, raw_conf, regime, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (issue, size, num, mode, logic, json.dumps(sigs), raw_conf,
                 regime, json.dumps(details), time.time()),
            )

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, size, num, mode, sigs, raw_conf, regime, details "
                "FROM predictions WHERE status='PENDING'"
            ).fetchall()
        for issue, p_size, p_num, mode, sigs_json, raw_conf, regime, details_json in rows:
            if issue not in history_dict:
                continue
            act = history_dict[issue]
            act_size_str = "BIG" if act['size'] == 1 else "SMALL"
            win = (p_size == act_size_str)
            status = ("JACK 🏆" if p_num == act['num'] else "WIN ✅") if win else "LOSS ❌"
            with self._conn() as c:
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                    (status, act['num'], act_size_str, issue),
                )
            resolved.append({
                "issue": issue, "size": p_size, "num": p_num,
                "actual_num": act['num'], "actual_size": act_size_str,
                "status": status, "mode": mode, "win": win,
                "raw_conf": raw_conf, "regime": regime,
                "details": json.loads(details_json) if details_json else {},
                "sigs": json.loads(sigs_json) if sigs_json else [],
            })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, actual_size, actual_num, status FROM predictions "
                "WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows[::-1]

    def stats(self):
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status LIKE 'WIN%' OR status LIKE 'JACK%' "
                "THEN 1 ELSE 0 END) FROM predictions WHERE status!='PENDING'"
            ).fetchone()
        return (row[0] or 0), (row[1] or 0)


STORE = Storage(CFG.db_path)


# =========================
# ADAPTIVE STATE + CALIBRATION
# =========================
class AdaptiveState:
    """Tracks recent hit/miss history of one detector."""
    def __init__(self, maxlen=30):
        self.recent = deque(maxlen=maxlen)

    def record(self, correct: bool):
        self.recent.append(1 if correct else 0)

    def stats(self):
        n = len(self.recent)
        if n < 8:
            return {'mult': 1.0, 'flip': False, 'muted': False, 'badge': '', 'acc': 0.5, 'n': n}
        w = sum(self.recent)
        raw = w / n
        shrunk = (w + 3) / (n + 6)
        if n >= 20 and raw <= 0.35:
            return {'mult': 1.0, 'flip': True, 'muted': False, 'badge': 'FLIP', 'acc': raw, 'n': n}
        if n >= 20 and raw < 0.43:
            return {'mult': 0.25, 'flip': False, 'muted': True, 'badge': 'MUTE', 'acc': raw, 'n': n}
        if n >= 20 and raw >= 0.62:
            return {'mult': 1.6, 'flip': False, 'muted': False, 'badge': 'ELITE', 'acc': raw, 'n': n}
        return {
            'mult': max(0.35, min(1.6, 1 + (shrunk - 0.5) * 5)),
            'flip': False, 'muted': False, 'badge': '', 'acc': raw, 'n': n,
        }


class Calibration:
    """Maps raw confidence -> actual hit rate for similar-confidence predictions."""
    def __init__(self):
        self.samples = deque(maxlen=400)

    def add(self, conf, correct):
        self.samples.append((conf, 1 if correct else 0))

    def calibrate(self, raw_conf):
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
    c0, c1 = w - big_count, big_count
    H = 0.0
    for c in (c0, c1):
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
# DETECTORS (14 total)
# =========================
@dataclass
class Detector:
    key: str
    name: str
    dir: int          # 1 = BIG, 0 = SMALL
    conf: int
    weight: float
    family: str = "generic"
    is_reversal: bool = False


def run_detectors(sizes, nums):
    out = []
    n = len(sizes)
    if n < CFG.warm_min:
        return out

    # 1) MARKOV BACKOFF — highest order with enough samples wins
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
                weight=0.6 + strength * 1.2,
                family="markov",
            ))
        break

    # 2) BAYES — sum-aware, weights borderline numbers more
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

    # 3) FREQUENCY LEAN
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

    # 4) STREAK FOLLOW (2-4)
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

    # 5) DRAGON (5+)
    if run >= 5:
        out.append(Detector(
            key="Dragon", name=f"Dragon×{run}",
            dir=last, conf=min(94, 68 + run * 4),
            weight=1.9 + min(run, 6) * 0.15, family="dragon",
        ))

    # 6) REVERSAL — prior bias + breakout
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

    # 7) ZIGZAG
    switches = sum(1 for i in range(min(6, n - 1)) if sizes[-1 - i] != sizes[-2 - i])
    if switches >= 5:
        out.append(Detector(
            key="Zigzag", name="Zigzag",
            dir=1 - last, conf=62 + switches * 3, weight=1.4, family="zigzag",
        ))

    # 8) CYCLE
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

    # 9) ACF LAG 1 / 2
    for lag in (1, 2):
        N = min(30, n)
        if N < lag + 5:
            continue
        arr = sizes[-N:]
        mean = sum(arr) / N
        num = den = 0.0
        for i in range(N - lag):
            num += (arr[i] - mean) * (arr[i + lag] - mean)
        for i in range(N):
            den += (arr[i] - mean) ** 2
        if den <= 0:
            continue
        acf = num / den
        if abs(acf) >= 0.10:
            out.append(Detector(
                key=f"ACF{lag}", name=f"ACF-L{lag}({acf:+.2f})",
                dir=last if acf > 0 else 1 - last,
                conf=int(min(68, 55 + abs(acf) * 40)),
                weight=0.9 + abs(acf), family=f"acf{lag}",
            ))

    # 10) TREND SLOPE (on numbers, not sizes)
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

    # 11) DRIFT (recent mean vs older mean of numbers)
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

    # 12) HURST EXPONENT (trend only; fractal)
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

    # 13) BOLLINGER REVERSION
    if n >= 20:
        w20 = sizes[-20:]
        ma = sum(w20) / 20
        std = math.sqrt(sum((x - ma) ** 2 for x in w20) / 20)
        if std >= 0.15:
            if last >= ma + 2 * std:
                out.append(Detector(key="BollingerRev", name="Bollinger-Rev",
                                    dir=0, conf=78, weight=1.5, family="bollinger"))
            elif last <= ma - 2 * std:
                out.append(Detector(key="BollingerRev", name="Bollinger-Rev",
                                    dir=1, conf=78, weight=1.5, family="bollinger"))

    # 14) Z-SCORE (30-window)
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
# MERGE with correlation penalty + adaptive weights
# =========================
def merge_detectors(detectors, regime_name, adaptive, regime_adaptive):
    if not detectors:
        return None
    fam_count = Counter(d.family for d in detectors)
    bW = sW = tW = 0.0
    used = []

    for d in detectors:
        rk = (regime_name, d.key)
        st_obj = None
        if rk in regime_adaptive and len(regime_adaptive[rk].recent) >= 8:
            st_obj = regime_adaptive[rk]
        elif d.key in adaptive:
            st_obj = adaptive[d.key]
        st = st_obj.stats() if st_obj else {
            'mult': 1.0, 'flip': False, 'muted': False, 'badge': '', 'acc': 0.5, 'n': 0,
        }

        if st['muted']:
            used.append({'name': d.name, 'dir': d.dir, 'badge': 'MUTE', 'key': d.key})
            continue

        direction = 1 - d.dir if st['flip'] else d.dir
        fc = fam_count[d.family]
        fam_mult = 0.55 if fc >= 3 else (0.75 if fc == 2 else 1.0)
        w = (d.conf / 100.0) * d.weight * st['mult'] * fam_mult
        if direction == 1:
            bW += w
        else:
            sW += w
        tW += w
        used.append({'name': d.name, 'dir': direction, 'badge': st['badge'], 'key': d.key})

    if tW < 0.01:
        return None
    agree = max(bW, sW) / tW
    return {
        'dir': 1 if bW >= sW else 0,
        'strength': agree,
        'conf': min(96, int(agree * 100)),
        'used': used,
    }


# =========================
# NUMBER PICKER
# =========================
def pick_number(history, side):
    nums = [h['num'] for h in history]
    if not nums:
        return 0
    pool = [5, 6, 7, 8, 9] if side == 1 else [0, 1, 2, 3, 4]
    last = nums[-1]

    trans = Counter()
    for i in range(1, len(nums)):
        trans[(nums[i - 1], nums[i])] += 1
    freq30 = Counter(nums[-30:])

    scored = []
    for num in pool:
        gap = 60
        for i in range(len(nums) - 1, -1, -1):
            if nums[i] == num:
                gap = len(nums) - 1 - i
                break
        gap = min(gap, 30)
        tr = trans.get((last, num), 0)
        fr = freq30.get(num, 0)
        neigh = 1.5 if num in ((last + 1) % 10, (last + 9) % 10) else 0
        parity = 0.5 if (num % 2) == ((last + 1) % 2) else 0
        score = gap * 1.2 + fr * 1.5 + tr * 3 + neigh + parity
        scored.append((score, num))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][1] if scored else pool[0]


# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.adaptive = {}
        self.regime_adaptive = {}
        self.calibration = Calibration()
        self.loss_streak = 0
        self.backtest_done = False
        self.backtest_result = {}
        self.backtest_samples = 0
        self.last_prediction = None
        self.gen_count = 0
        self.paused = False


STATE = BotState()


def get_adaptive(key):
    if key not in STATE.adaptive:
        STATE.adaptive[key] = AdaptiveState()
    return STATE.adaptive[key]


def get_regime_adaptive(regime, key):
    rk = (regime, key)
    if rk not in STATE.regime_adaptive:
        STATE.regime_adaptive[rk] = AdaptiveState()
    return STATE.regime_adaptive[rk]


# =========================
# PREDICTION ENGINE
# =========================
def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        return None

    sizes = [h['size'] for h in STATE.history]
    nums = [h['num'] for h in STATE.history]

    regime = detect_regime(sizes)
    detectors = run_detectors(sizes, nums)
    merged = merge_detectors(detectors, regime.regime, STATE.adaptive, STATE.regime_adaptive)

    if merged is None:
        pred_dir = sizes[-1]
        raw_conf = 50
        used = []
        mode = "FALLBACK"
    else:
        pred_dir = merged['dir']
        raw_conf = merged['conf']
        used = merged['used']
        mode = regime.regime

    # Mild recovery dampening after 3+ losses (NOT aggressive flip)
    if STATE.loss_streak >= 3:
        raw_conf = max(52, raw_conf - 8)
        used.append({'name': 'Recovery-LowConf', 'dir': pred_dir, 'badge': 'REC', 'key': 'recovery'})

    calib_conf = STATE.calibration.calibrate(raw_conf)
    num = pick_number(STATE.history, pred_dir)
    next_issue = str(int(STATE.history[-1]['issue']) + 1)

    details = {
        'detectors': [
            {'key': u['key'], 'dir': u['dir'], 'name': u['name']}
            for u in used
            if u.get('key') and u['key'] != 'recovery'
        ],
        'regime': regime.regime,
        'regime_conf': regime.confidence,
        'entropy': regime.entropy,
        'switch_rate': regime.switch_rate,
        'max_streak': regime.max_streak,
        'loss_streak_at_pred': STATE.loss_streak,
    }

    return {
        'issue': next_issue,
        'size': "BIG" if pred_dir == 1 else "SMALL",
        'num': num,
        'confidence': calib_conf,
        'raw_conf': raw_conf,
        'mode': mode,
        'regime': regime.regime,
        'logic': ", ".join(u['name'] for u in used[:6]) or "no signals",
        'sigs': used,
        'details': details,
    }


def learn_from_resolution(res):
    """Update adaptive states + calibration after a result lands."""
    actual_size = 1 if res['actual_size'] == 'BIG' else 0
    details = res.get('details') or {}
    detectors = details.get('detectors') or []
    regime = details.get('regime') or 'BALANCED'

    for d in detectors:
        correct = (d['dir'] == actual_size)
        get_adaptive(d['key']).record(correct)
        get_regime_adaptive(regime, d['key']).record(correct)

    rc = res.get('raw_conf')
    if isinstance(rc, int):
        STATE.calibration.add(rc, res['win'])


def seed_from_history():
    """Walk-forward backtest — seeds adaptive states + calibration."""
    if len(STATE.history) < CFG.min_history + 30:
        return
    steps = min(CFG.backtest_steps, len(STATE.history) - CFG.min_history - 5)
    if steps < 20:
        return

    STATE.adaptive.clear()
    STATE.regime_adaptive.clear()
    STATE.calibration = Calibration()

    log.info(f"Running backtest over {steps} rounds...")
    wins = total = 0
    for t in range(len(STATE.history) - steps, len(STATE.history)):
        before = STATE.history[:t]
        actual = STATE.history[t]['size']
        sizes = [h['size'] for h in before]
        nums = [h['num'] for h in before]
        regime = detect_regime(sizes)
        detectors = run_detectors(sizes, nums)
        if not detectors:
            continue
        merged = merge_detectors(detectors, regime.regime, STATE.adaptive, STATE.regime_adaptive)
        if not merged:
            continue
        for d in detectors:
            get_adaptive(d.key).record(d.dir == actual)
            get_regime_adaptive(regime.regime, d.key).record(d.dir == actual)
        STATE.calibration.add(merged['conf'], merged['dir'] == actual)
        total += 1
        if merged['dir'] == actual:
            wins += 1

    STATE.backtest_done = True
    STATE.backtest_samples = total
    STATE.backtest_result = {'acc': round(wins / total * 100, 1) if total else 0, 'n': total}
    log.info(f"Backtest done: {wins}/{total} = {STATE.backtest_result['acc']}%")


# =========================
# FETCH
# =========================
async def fetch_data(session):
    try:
        async with session.get(
            CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)
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
                    out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
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
SIZE_EMOJI = {1: "📈", 0: "📉"}
REGIME_EMOJI = {"TRENDING": "📈", "CHOPPY": "〰️", "BIASED": "🎯",
                "BALANCED": "⚖️", "WARMING": "⏳"}


def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows[-12:]:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    sig_lines = []
    for s in p['sigs'][:6]:
        arrow = "▲ BIG" if s['dir'] == 1 else "▼ SMALL"
        badge = f" · {s['badge']}" if s['badge'] else ""
        sig_lines.append(f"  {h(s['name'])} → {arrow}{badge}")
    sig_text = "\n".join(sig_lines) if sig_lines else "  <i>no active signals</i>"

    size_side = 1 if p['size'] == 'BIG' else 0

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{SIZE_EMOJI[size_side]} {h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}% <i>(raw {p['raw_conf']}%)</i>
🧠 <b>Regime</b>: {REGIME_EMOJI.get(p['regime'], "⚖️")} <b>{h(p['regime'])}</b>
⚖️ <b>Mode</b>: <b>{h(p['mode'])}</b>

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
        f"Pred: {h(res['size'])} {h(res['num'])}\n"
        f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
        f"<b>{h(res['status'])}</b>"
    )


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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🤖 <b>{h(CFG.bot_name)}</b>\n\n"
        "Commands:\n"
        "/status — engine state\n"
        "/stats — accuracy + detector scoreboard\n"
        "/backtest — walk-forward summary\n"
        "/regime — market regime analysis\n"
        "/detectors — currently firing detectors\n"
        "/reset — reset adaptive memory (admin)\n"
        "/stop — pause predictions (admin)\n"
        "/resume — resume predictions (admin)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total, wins = STORE.stats()
    acc = round(wins / total * 100, 1) if total else 0
    bt_line = (
        f"Backtest: <b>{STATE.backtest_result.get('acc','-')}%</b> on {STATE.backtest_result.get('n',0)} rounds"
        if STATE.backtest_done else "Backtest: <i>pending</i>"
    )
    lines = [
        "📊 <b>Engine Status</b>",
        f"History loaded: <b>{len(STATE.history)}</b>",
        f"Predictions: <b>{total}</b> · wins <b>{wins}</b> · acc <b>{acc}%</b>",
        f"Loss streak: <b>{STATE.loss_streak}</b>",
        bt_line,
        f"Calibration samples: <b>{len(STATE.calibration.samples)}</b>",
        f"Detectors tracked: <b>{len(STATE.adaptive)}</b>",
        f"Paused: <b>{STATE.paused}</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total, wins = STORE.stats()
    acc = round(wins / total * 100, 1) if total else 0
    lines = [f"📊 <b>Live Stats</b>", f"Overall accuracy: <b>{acc}%</b> ({wins}/{total})", "",
             "🧠 <b>Top Detectors</b>"]
    rows = []
    for k, st in STATE.adaptive.items():
        if len(st.recent) >= 15:
            s = st.stats()
            rows.append((k, s['acc'], s['n'], s['badge']))
    rows.sort(key=lambda x: -x[1])
    for k, a, n, badge in rows[:12]:
        b = f" · {badge}" if badge else ""
        lines.append(f"  {h(k)}: {round(a*100)}% ({n}){b}")
    if not rows:
        lines.append("  <i>not enough samples yet</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not STATE.backtest_done:
        await update.message.reply_text("⏳ Backtest has not run yet (need more history).")
        return
    bt = STATE.backtest_result
    await update.message.reply_text(
        f"📈 <b>Backtest</b>\n"
        f"Rounds: <b>{bt.get('n', 0)}</b>\n"
        f"Accuracy: <b>{bt.get('acc', 0)}%</b>\n"
        f"<i>Walk-forward, no lookahead</i>",
        parse_mode="HTML",
    )


async def cmd_regime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(STATE.history) < CFG.warm_min:
        await update.message.reply_text("Not enough history yet.")
        return
    sizes = [h['size'] for h in STATE.history]
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


async def cmd_detectors(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sizes = [h['size'] for h in STATE.history]
    nums = [h['num'] for h in STATE.history]
    dets = run_detectors(sizes, nums)
    if not dets:
        await update.message.reply_text("No active detectors right now.")
        return
    lines = ["🔎 <b>Currently firing</b>:"]
    for d in dets[:15]:
        arrow = "BIG" if d.dir == 1 else "SMALL"
        st = get_adaptive(d.key).stats()
        badge = f" · {st['badge']}" if st['badge'] else ""
        lines.append(f"  {h(d.name)} → {arrow} ({d.conf}%){badge}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    STATE.adaptive.clear()
    STATE.regime_adaptive.clear()
    STATE.calibration = Calibration()
    STATE.loss_streak = 0
    STATE.backtest_done = False
    seed_from_history()
    await update.message.reply_text("♻️ Adaptive memory reset + backtest re-seeded.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    STATE.paused = True
    await update.message.reply_text("⏸️ Predictions paused.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    STATE.paused = False
    await update.message.reply_text("▶️ Predictions resumed.")


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
                    if res['win']:
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
                            STORE.save_prediction(
                                p['issue'], p['size'], p['num'], p['mode'],
                                p['logic'], p['sigs'], p['raw_conf'],
                                p['regime'], p['details'],
                            )
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
    total, wins = STORE.stats()
    acc = round(wins / total * 100, 2) if total else 0
    dets = []
    for k, st in STATE.adaptive.items():
        if len(st.recent) >= 15:
            s = st.stats()
            dets.append({'key': k, 'acc': round(s['acc'] * 100, 1),
                         'n': s['n'], 'badge': s['badge']})
    dets.sort(key=lambda x: -x['acc'])
    payload = {
        'bot': CFG.bot_name,
        'history': len(STATE.history),
        'predictions': total,
        'wins': wins,
        'accuracy': acc,
        'loss_streak': STATE.loss_streak,
        'backtest': STATE.backtest_result,
        'calibration_samples': len(STATE.calibration.samples),
        'detectors': dets[:20],
        'paused': STATE.paused,
    }
    return web.json_response(payload)


# =========================
# MAIN
# =========================
async def main():
    CFG.validate()

    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    web_app.router.add_get('/stats', handle_stats)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server on :{port}")

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("regime", cmd_regime))
    app.add_handler(CommandHandler("detectors", cmd_detectors))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("resume", cmd_resume))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
