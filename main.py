import asyncio
import contextlib
import csv
import html
import json
import logging
import math
import os
import random
import signal
import sqlite3
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Any

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    telegram_token: str = "8611789455:AAFexWc48dhJKgcUK-hkCpRQowbGTm_Bwx0"
    chat_id_raw: str = "1264164655"
    admin_ids_raw: str = "1264164655"
    api_url: str = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"
    

    db_path: str = os.getenv("DB_PATH", "predictor.db").strip() or "predictor.db"
    export_dir: str = os.getenv("EXPORT_DIR", "exports").strip() or "exports"
    log_dir: str = os.getenv("LOG_DIR", "logs").strip() or "logs"
    fetch_every: float = float(os.getenv("FETCH_EVERY", "2"))
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "8"))
    bot_name: str = os.getenv("BOT_NAME", "V98 FIXED").strip() or "V98 FIXED"
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    send_cooldown: float = float(os.getenv("SEND_COOLDOWN", "1.0"))
    backtest_min_history: int = int(os.getenv("BACKTEST_MIN_HISTORY", "100"))
    min_size_history: int = int(os.getenv("MIN_SIZE_HISTORY", "40"))
    resolve_limit: int = int(os.getenv("RESOLVE_LIMIT", "250"))
    max_recent_lines: int = int(os.getenv("MAX_RECENT_LINES", "10"))
    paused_default: bool = os.getenv("PAUSED_DEFAULT", "0") == "1"
    high_conf_only: bool = os.getenv("HIGH_CONF_ONLY", "0") == "1"
    high_conf_threshold: int = int(os.getenv("HIGH_CONF_THRESHOLD", "64"))
    recalibrate_every: int = int(os.getenv("RECALIBRATE_EVERY", "8"))
    adaptive_decay: float = float(os.getenv("ADAPTIVE_DECAY", "0.94"))
    rolling_eval_window: int = int(os.getenv("ROLLING_EVAL_WINDOW", "80"))
    max_confidence_cap: int = int(os.getenv("MAX_CONFIDENCE_CAP", "88"))
    min_confidence_floor: int = int(os.getenv("MIN_CONFIDENCE_FLOOR", "50"))

    def parse_chat_id(self):
        if not self.chat_id_raw:
            raise RuntimeError("CHAT_ID missing.")
        try:
            return int(self.chat_id_raw)
        except ValueError:
            return self.chat_id_raw

    def admin_ids(self):
        ids = set()
        for x in self.admin_ids_raw.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                ids.add(int(x))
            except ValueError:
                pass
        return ids

    def ensure_dirs(self):
        Path(self.export_dir).mkdir(parents=True, exist_ok=True)
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

    def validate(self):
        if not self.telegram_token:
            raise RuntimeError("TELEGRAM_TOKEN missing.")
        self.ensure_dirs()
        self.parse_chat_id()


CFG = Config()


def setup_logging():
    CFG.ensure_dirs()

    root = logging.getLogger()
    root.setLevel(getattr(logging, CFG.log_level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    root.handlers.clear()

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(
        Path(CFG.log_dir) / "bot.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v98_fixed")


STATUS_EMOJI = {"WIN": "✅", "LOSS": "❌", "JACK": "🏆"}


def h(x: Any) -> str:
    return html.escape(str(x))


def size_label(v):
    if isinstance(v, str):
        u = v.upper()
        if u in ("BIG", "SMALL"):
            return u
        return "BIG" if u in ("1", "BIG") else "SMALL"
    return "BIG" if int(v) == 1 else "SMALL"


def status_emoji(s: str) -> str:
    s = (s or "").upper()
    return STATUS_EMOJI.get(s, s or "?")


def confidence_tier(conf: int) -> str:
    if conf >= 70:
        return "HIGH"
    if conf >= 60:
        return "MID"
    return "LOW"


def binomial_z(hits, total, p=0.5):
    if total <= 0:
        return 0.0
    mu = total * p
    sd = math.sqrt(total * p * (1 - p)) or 1e-9
    return (hits - mu) / sd


def two_sided_p(z):
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


@dataclass
class RuntimeState:
    started_at: float = field(default_factory=time.time)
    paused: bool = CFG.paused_default
    fetch_ok: int = 0
    fetch_fail: int = 0
    send_ok: int = 0
    send_fail: int = 0
    last_fetch_at: Optional[float] = None
    last_predict_at: Optional[float] = None
    last_resolve_at: Optional[float] = None
    last_error: str = ""
    loop_iterations: int = 0
    sent_messages: int = 0
    dedupe_issue: Optional[str] = None
    recalibration_runs: int = 0
    resolved_since_recalibration: int = 0

    def uptime(self):
        sec = int(time.time() - self.started_at)
        hh = sec // 3600
        mm = (sec % 3600) // 60
        ss = sec % 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}"


STATE = RuntimeState()


class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY,
                    size TEXT,
                    num INTEGER,
                    confidence INTEGER,
                    calibrated INTEGER,
                    tier TEXT,
                    logic TEXT,
                    details TEXT,
                    signals TEXT,
                    status TEXT DEFAULT 'PENDING',
                    actual_num INTEGER,
                    actual_size TEXT,
                    created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS signal_weights (
                    name TEXT PRIMARY KEY,
                    weight REAL,
                    updated_at REAL
                )
            """)

    def save(self, p: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO predictions
                (issue,size,num,confidence,calibrated,tier,logic,details,signals,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                p["issue"], p["size"], p["num"], p["confidence"], p["calibrated"],
                p["tier"], p.get("logic", ""), p.get("details", ""),
                json.dumps(p.get("signals", {})), p.get("status", "PENDING"), time.time()
            ))

    def exists(self, issue: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM predictions WHERE issue=? LIMIT 1", (issue,)).fetchone()
        return row is not None

    def latest_prediction(self):
        with self._conn() as c:
            return c.execute("""
                SELECT issue,size,num,confidence,calibrated,tier,logic,details,status,actual_num,actual_size,created_at
                FROM predictions ORDER BY created_at DESC LIMIT 1
            """).fetchone()

    def resolve(self, hist: dict, limit=250):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue,size,num FROM predictions
                WHERE status='PENDING'
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()

        resolved = []
        for issue, size, num in rows:
            if issue not in hist:
                continue
            r = hist[issue]
            actual_size = "BIG" if r["size"] else "SMALL"
            win = size == actual_size
            status = ("JACK" if r["num"] == num else "WIN") if win else "LOSS"

            with self._conn() as c:
                c.execute("""
                    UPDATE predictions
                    SET status=?, actual_num=?, actual_size=?
                    WHERE issue=?
                """, (status, r["num"], actual_size, issue))

            resolved.append({
                "issue": issue,
                "size": size,
                "num": num,
                "actual_num": r["num"],
                "actual_size": actual_size,
                "status": status,
            })
        return resolved

    def stats(self):
        with self._conn() as c:
            total, wins, jacks = c.execute("""
                SELECT COUNT(*),
                       SUM(CASE WHEN status IN ('WIN','JACK') THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='JACK' THEN 1 ELSE 0 END)
                FROM predictions
                WHERE status != 'PENDING'
            """).fetchone()

        if not total:
            return None
        wins = wins or 0
        jacks = jacks or 0
        return {
            "total": total,
            "wins": wins,
            "loss": total - wins,
            "jacks": jacks,
            "rate": round(100 * wins / total, 1),
        }

    def counts(self):
        with self._conn() as c:
            pending = c.execute("SELECT COUNT(*) FROM predictions WHERE status='PENDING'").fetchone()[0]
            resolved = c.execute("SELECT COUNT(*) FROM predictions WHERE status!='PENDING'").fetchone()[0]
        return pending, resolved

    def recent(self, n=12):
        with self._conn() as c:
            return c.execute("""
                SELECT issue,size,num,actual_num,actual_size,status,confidence,calibrated,tier
                FROM predictions
                WHERE status != 'PENDING'
                ORDER BY created_at DESC LIMIT ?
            """, (n,)).fetchall()

    def recent_pending(self, n=5):
        with self._conn() as c:
            return c.execute("""
                SELECT issue,size,num,confidence,calibrated,tier,logic
                FROM predictions
                WHERE status='PENDING'
                ORDER BY created_at DESC LIMIT ?
            """, (n,)).fetchall()

    def recent_pred_nums(self, n=6):
        with self._conn() as c:
            rows = c.execute("""
                SELECT num FROM predictions
                WHERE num IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
            """, (n,)).fetchall()
        return [r[0] for r in rows][::-1]

    def rolling_outcomes(self, limit=80):
        with self._conn() as c:
            rows = c.execute("""
                SELECT status, confidence, tier
                FROM predictions
                WHERE status != 'PENDING'
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows

    def recent_pred_bias(self, n=20):
        with self._conn() as c:
            rows = c.execute("""
                SELECT size FROM predictions
                ORDER BY created_at DESC LIMIT ?
            """, (n,)).fetchall()
        big = sum(1 for r in rows if size_label(r[0]) == "BIG")
        return big, len(rows) - big

    def signal_accuracy(self):
        acc = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""
                SELECT signals, actual_size FROM predictions
                WHERE status != 'PENDING' AND signals IS NOT NULL
            """).fetchall()

        for sig_json, actual in rows:
            if not actual:
                continue
            act = 1 if size_label(actual) == "BIG" else 0
            try:
                sig = json.loads(sig_json)
            except Exception:
                continue
            for name, p in sig.items():
                acc[name][1] += 1
                if (1 if p >= 0.5 else 0) == act:
                    acc[name][0] += 1
        return {k: tuple(v) for k, v in acc.items()}

    def recent_signal_accuracy(self, limit=60):
        acc = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""
                SELECT signals, actual_size FROM predictions
                WHERE status != 'PENDING' AND signals IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()

        for sig_json, actual in rows:
            if not actual:
                continue
            act = 1 if size_label(actual) == "BIG" else 0
            try:
                sig = json.loads(sig_json)
            except Exception:
                continue
            for name, p in sig.items():
                acc[name][1] += 1
                if (1 if p >= 0.5 else 0) == act:
                    acc[name][0] += 1
        return {k: tuple(v) for k, v in acc.items()}

    def save_signal_weights(self, weights: dict):
        with self._conn() as c:
            now = time.time()
            for name, weight in weights.items():
                c.execute("""
                    INSERT INTO signal_weights(name, weight, updated_at)
                    VALUES(?,?,?)
                    ON CONFLICT(name) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at
                """, (name, float(weight), now))

    def load_signal_weights(self):
        with self._conn() as c:
            rows = c.execute("SELECT name, weight FROM signal_weights").fetchall()
        return {name: weight for name, weight in rows}

    def calibration_buckets(self):
        buckets = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""
                SELECT confidence, status FROM predictions
                WHERE status != 'PENDING' AND confidence IS NOT NULL
            """).fetchall()

        for conf, status in rows:
            b = (conf // 10) * 10
            buckets[b][1] += 1
            if status in ("WIN", "JACK"):
                buckets[b][0] += 1
        return {k: tuple(v) for k, v in buckets.items()}

    def resolved_predictions_with_signals(self, limit=250):
        with self._conn() as c:
            rows = c.execute("""
                SELECT signals, actual_size, status, confidence
                FROM predictions
                WHERE status != 'PENDING' AND signals IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows

    def export_csv(self, export_path: str):
        with self._conn() as c:
            rows = c.execute("""
                SELECT issue,size,num,confidence,calibrated,tier,logic,details,status,actual_num,actual_size,created_at
                FROM predictions
                ORDER BY created_at DESC
            """).fetchall()

        with open(export_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "issue", "size", "num", "confidence", "calibrated", "tier",
                "logic", "details", "status", "actual_num", "actual_size", "created_at"
            ])
            w.writerows(rows)


STORE = Storage(CFG.db_path)


from dataclasses import dataclass

@dataclass
class SignalResult:
    name: str
    p_big: float
    strength: float
    detail: str = ""


class SignalBase:
    name = "base"
    def compute(self, sizes, nums):
        raise NotImplementedError


class PatternSignal(SignalBase):
    name = "pattern"
    def compute(self, sizes, nums):
        for k in range(8, 3, -1):
            if len(sizes) < k + 1:
                continue
            pat = tuple(sizes[-k:])
            b = s = 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pat:
                    if sizes[i+k]:
                        b += 1
                    else:
                        s += 1
            t = b + s
            if t >= 2:
                p = (b + 1) / (t + 2)
                return SignalResult(self.name, p, min(0.7, 0.15 + t * 0.08), f"k={k}")
        return None


class WeightedMarkovSignal(SignalBase):
    def __init__(self, order=2, decay=0.96):
        self.order = order
        self.decay = decay
        self.name = f"wmarkov{order}"

    def compute(self, sizes, nums):
        if len(sizes) <= self.order + 5:
            return None
        key = tuple(sizes[-self.order:])
        b = s = 0.0
        m = 0
        horizon = len(sizes) - self.order
        for i in range(horizon):
            if tuple(sizes[i:i+self.order]) == key:
                age = horizon - i
                w = self.decay ** age
                if sizes[i+self.order]:
                    b += w
                else:
                    s += w
                m += 1
        total = b + s
        if total <= 0:
            return None
        p = (b + 1.0) / (total + 2.0)
        return SignalResult(self.name, p, min(0.75, 0.10 * m + 0.2 * abs(p - 0.5)), f"m={m}")


class StreakSignal(SignalBase):
    name = "streak"
    def compute(self, sizes, nums):
        if len(sizes) < 2:
            return None
        last = sizes[-1]
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == last:
                streak += 1
            else:
                break
        if streak < 2:
            return SignalResult(self.name, 0.5, 0.0, "none")
        d = 1 if last else -1
        p = max(0.05, min(0.95, 0.5 + d * min(0.25, 0.06 * streak)))
        return SignalResult(self.name, p, min(0.6, 0.12 * streak), f"streak={streak}")


class MeanRevSignal(SignalBase):
    name = "meanrev"
    def __init__(self, window=20):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        bc = sum(sizes[-self.window:])
        ratio = bc / self.window
        return SignalResult(self.name, 1 - ratio, min(0.7, abs(ratio - 0.5) * 2), f"{bc}/{self.window}")


class EWMASignal(SignalBase):
    name = "ewma"
    def __init__(self, alpha=0.25):
        self.alpha = alpha
    def compute(self, sizes, nums):
        if len(sizes) < 20:
            return None
        ew = sizes[0]
        for x in sizes[1:]:
            ew = self.alpha * x + (1 - self.alpha) * ew
        p = max(0.05, min(0.95, ew))
        return SignalResult(self.name, p, min(0.45, abs(ew - 0.5) * 1.8), f"ew={ew:.2f}")


class FrequencySignal(SignalBase):
    name = "freq"
    def __init__(self, window=50):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        r = sum(sizes[-self.window:]) / self.window
        p = 0.5 + max(-0.15, min(0.15, (0.5 - r) * 0.5))
        return SignalResult(self.name, p, min(0.4, abs(r - 0.5)), f"r={r:.2f}")


class RunLengthSignal(SignalBase):
    name = "runlength"
    def compute(self, sizes, nums):
        if len(sizes) < 20:
            return None
        runs = []
        cur = 1
        for i in range(1, len(sizes)):
            if sizes[i] == sizes[i-1]:
                cur += 1
            else:
                runs.append(cur)
                cur = 1
        runs.append(cur)
        avg = sum(runs) / len(runs)
        last = sizes[-1]
        if runs[-1] > avg:
            p = 0.0 if last else 1.0
            strength = min(0.4, (runs[-1] - avg) / max(avg, 1))
        else:
            p = 1.0 if last else 0.0
            strength = 0.15
        return SignalResult(self.name, p, strength, f"{runs[-1]}/{avg:.1f}")


class SelfCorrectionSignal(SignalBase):
    name = "selfcorrect"
    def __init__(self, store: Storage):
        self.store = store

    def compute(self, sizes, nums):
        rows = self.store.recent(6)
        if len(rows) < 3:
            return None
        wrong_streak = 0
        for r in rows:
            if r[5] == "LOSS":
                wrong_streak += 1
            else:
                break
        if wrong_streak < 2:
            return SignalResult(self.name, 0.5, 0.0, "ok")
        last_pred_big = 1 if size_label(rows[0][1]) == "BIG" else 0
        return SignalResult(self.name, 1 - last_pred_big, min(0.5, 0.12 * wrong_streak), f"loss={wrong_streak}")


class Predictor:
    def __init__(self, store: Storage, cfg: Config):
        self.store = store
        self.cfg = cfg
        self.history = []
        self.last_fetch_meta = {}

        self.signals = [
            PatternSignal(),
            WeightedMarkovSignal(2, 0.96),
            WeightedMarkovSignal(3, 0.97),
            StreakSignal(),
            MeanRevSignal(20),
            EWMASignal(0.25),
            FrequencySignal(50),
            RunLengthSignal(),
            SelfCorrectionSignal(store),
        ]

        self.acc_weights = self._acc_weights()
        persisted = self.store.load_signal_weights()
        if persisted:
            self.acc_weights.update(persisted)
        self.recent_pred_nums = deque(self.store.recent_pred_nums(6), maxlen=6)

    def _acc_weights(self):
        long_acc = self.store.signal_accuracy()
        short_acc = self.store.recent_signal_accuracy(60)
        w = {}
        for s in self.signals:
            hl, tl = long_acc.get(s.name, (0, 0))
            hs, ts = short_acc.get(s.name, (0, 0))
            long_rate = (hl + 5) / (tl + 10)
            short_rate = (hs + 3) / (ts + 6)
            blended = 0.4 * long_rate + 0.6 * short_rate
            w[s.name] = 0.55 + blended
        return w

    def refresh_weights(self):
        self.acc_weights = self._acc_weights()

    def adaptive_reweight(self):
        rows = self.store.resolved_predictions_with_signals(limit=250)
        if len(rows) < 25:
            return self.acc_weights

        decay = self.cfg.adaptive_decay
        perf = defaultdict(lambda: [0.0, 0.0])

        for idx, (sig_json, actual_size, status, confidence) in enumerate(rows):
            time_w = decay ** idx
            actual = 1 if size_label(actual_size) == "BIG" else 0
            try:
                sig = json.loads(sig_json)
            except Exception:
                continue

            for name, p in sig.items():
                perf[name][1] += time_w
                pred = 1 if p >= 0.5 else 0
                if pred == actual:
                    perf[name][0] += time_w

        new_w = {}
        for s in self.signals:
            hit, tot = perf.get(s.name, (0.0, 0.0))
            rate = (hit + 1.5) / (tot + 3.0)
            old = self.acc_weights.get(s.name, 1.0)
            proposed = 0.65 + rate
            new_w[s.name] = 0.75 * old + 0.25 * proposed

        self.acc_weights = new_w
        self.store.save_signal_weights(self.acc_weights)
        STATE.recalibration_runs += 1
        return self.acc_weights

    async def fetch(self, session: aiohttp.ClientSession):
        try:
            async with session.get(
                self.cfg.api_url,
                timeout=aiohttp.ClientTimeout(total=self.cfg.request_timeout)
            ) as r:
                if r.status != 200:
                    STATE.fetch_fail += 1
                    STATE.last_error = f"fetch bad status {r.status}"
                    return False

                try:
                    data = await r.json()
                except Exception:
                    txt = await r.text()
                    STATE.fetch_fail += 1
                    STATE.last_error = "invalid json"
                    log.warning("invalid json: %s", txt[:300])
                    return False

                raw = data.get("data", {}).get("list", [])
                out = []
                for item in raw:
                    try:
                        n = int(item["number"])
                        issue = str(item["issueNumber"])
                        int(issue)
                        out.append({
                            "issue": issue,
                            "num": n,
                            "size": 1 if n >= 5 else 0
                        })
                    except Exception:
                        continue

                if not out:
                    STATE.fetch_fail += 1
                    STATE.last_error = "empty parsed history"
                    return False

                out.sort(key=lambda x: int(x["issue"]))
                self.history = out
                self.last_fetch_meta = {
                    "count": len(out),
                    "first_issue": out[0]["issue"],
                    "last_issue": out[-1]["issue"],
                    "last_num": out[-1]["num"],
                    "last_size": size_label(out[-1]["size"]),
                }
                STATE.fetch_ok += 1
                STATE.last_fetch_at = time.time()
                return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.fetch_fail += 1
            STATE.last_error = f"fetch exception: {e}"
            log.exception("fetch exception: %s", e)
            return False

    def _core(self, sizes, nums):
        if len(sizes) < self.cfg.min_size_history:
            return None

        results = {}
        for sig in self.signals:
            try:
                r = sig.compute(sizes, nums)
            except Exception:
                continue
            if r and r.strength > 0:
                results[r.name] = r

        if not results:
            return None

        weighted_sum = 0.0
        total_weight = 0.0
        ranked = []
        raw_votes = {}

        for name, r in results.items():
            acc_w = self.acc_weights.get(name, 1.0)
            w = max(0.01, r.strength * acc_w)
            weighted_sum += r.p_big * w
            total_weight += w
            raw_votes[name] = round(r.p_big, 3)
            ranked.append((name, r, w))

        p_big = weighted_sum / total_weight if total_weight else 0.5
        pred_size = 1 if p_big >= 0.5 else 0
        edge = abs(p_big - 0.5)

        ranked.sort(key=lambda x: x[2], reverse=True)
        top_ranked = ranked[:5]

        agreement = 0.0
        if top_ranked:
            same_side = sum(1 for _, r, _ in top_ranked if (1 if r.p_big >= 0.5 else 0) == pred_size)
            agreement = same_side / len(top_ranked)

        big_pred, small_pred = self.store.recent_pred_bias(20)
        total_pred = big_pred + small_pred
        if total_pred >= 10:
            bias_ratio = big_pred / total_pred
            if bias_ratio > 0.72 and pred_size == 1:
                edge *= 0.62
            elif bias_ratio < 0.28 and pred_size == 0:
                edge *= 0.62

        recent = self.store.rolling_outcomes(self.cfg.rolling_eval_window)
        rolling_rate = None
        if recent:
            wins = sum(1 for st, _, _ in recent if st in ("WIN", "JACK"))
            rolling_rate = wins / len(recent)
            if rolling_rate < 0.50:
                edge *= 0.88
            elif rolling_rate > 0.58:
                edge *= 1.05

        confidence = self.cfg.min_confidence_floor + round(edge * 50 + agreement * 10)

        if rolling_rate is not None:
            if rolling_rate < 0.50:
                confidence -= 4
            elif rolling_rate > 0.58:
                confidence += 3

        confidence = max(self.cfg.min_confidence_floor, min(self.cfg.max_confidence_cap, confidence))
        logic = " + ".join(name for name, _, _ in top_ranked[:4])

        return {
            "p_big": p_big,
            "pred_size": pred_size,
            "confidence": confidence,
            "tier": confidence_tier(confidence),
            "signals": raw_votes,
            "logic": logic,
            "details": "hidden",
        }

    def _number_scores(self, nums, pool):
        if not nums:
            return {n: 1.0 for n in pool}

        trans = defaultdict(Counter)
        for i in range(len(nums) - 1):
            trans[nums[i]][nums[i+1]] += 1

        last_num = nums[-1]
        row = trans[last_num]
        row_total = sum(row.values())

        freq = Counter(nums[-120:])
        freq_total = sum(freq.values()) or 1

        gaps = {}
        for n in pool:
            gaps[n] = len(nums)
            for i in range(len(nums)-1, -1, -1):
                if nums[i] == n:
                    gaps[n] = len(nums) - i
                    break

        scores = {}
        for n in pool:
            p_trans = (row.get(n, 0) + 0.5) / (row_total + 5.0)
            p_freq = (freq.get(n, 0) + 0.5) / (freq_total + 5.0)
            gap_norm = min(1.0, gaps[n] / 20.0)
            anti_hot = 1.0 - min(1.0, p_freq * 10)
            score = 0.42 * p_trans * 10 + 0.18 * p_freq * 10 + 0.24 * gap_norm + 0.10 * anti_hot
            if n in self.recent_pred_nums:
                score *= 0.45
            scores[n] = score
        return scores

    def _pick_number(self, nums, pred_size, temperature=0.52):
        pool = list(range(5, 10)) if pred_size else list(range(0, 5))
        scores = self._number_scores(nums, pool)

        max_s = max(scores.values())
        exp_s = {n: math.exp((s - max_s) / temperature) for n, s in scores.items()}
        total = sum(exp_s.values()) or 1.0

        r = random.random() * total
        cum = 0.0
        chosen = max(scores, key=scores.get)

        for n in sorted(exp_s):
            cum += exp_s[n]
            if r < cum:
                chosen = n
                break

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        top = [n for n, _ in ranked[:3]]
        return chosen, top

    def _calibrate(self, raw_conf):
        buckets = self.store.calibration_buckets()
        b = (raw_conf // 10) * 10
        for delta in (0, 10, -10, 20, -20, 30, -30):
            probe = b + delta
            if probe in buckets and buckets[probe][1] >= 15:
                hit, tot = buckets[probe]
                return round(100 * hit / tot)
        return raw_conf

    def predict(self):
        if not self.history:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        core = self._core(sizes, nums)
        if not core:
            return None

        best_num, top = self._pick_number(nums, core["pred_size"])
        self.recent_pred_nums.append(best_num)
        calibrated = self._calibrate(core["confidence"])

        STATE.last_predict_at = time.time()

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if core["pred_size"] else "SMALL",
            "num": best_num,
            "top": top,
            "confidence": core["confidence"],
            "calibrated": calibrated,
            "tier": core["tier"],
            "logic": core["logic"],
            "details": core["details"],
            "signals": core["signals"],
            "status": "PENDING",
        }

    def backtest(self, min_history=None):
        min_history = min_history or self.cfg.backtest_min_history
        sizes = [h["size"] for h in self.history]
        nums = [h["num"] for h in self.history]

        if len(sizes) < min_history + 20:
            return None

        size_hits = 0
        size_tot = 0
        skipped = 0

        for i in range(min_history, len(sizes)):
            core = self._core(sizes[:i], nums[:i])
            if not core:
                skipped += 1
                continue
            actual = sizes[i]
            ok = core["pred_size"] == actual
            size_hits += 1 if ok else 0
            size_tot += 1

        tail = sizes[min_history:]
        big_base = max(sum(tail), len(tail) - sum(tail)) / max(1, len(tail))
        z = binomial_z(size_hits, size_tot)
        pval = two_sided_p(z)

        return {
            "total": size_tot,
            "hits": size_hits,
            "skipped": skipped,
            "acc": size_hits / size_tot if size_tot else 0,
            "big_baseline": big_base,
            "z": z,
            "p": pval,
        }


PREDICTOR = Predictor(STORE, CFG)


def is_admin(update: Update):
    admins = CFG.admin_ids()
    if not admins:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in admins


def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await reply(update, "⛔ Admin only.")
            return
        return await fn(update, context)
    return wrapper


async def reply(update: Update, text: str, **kwargs):
    if update.message:
        await update.message.reply_text(text, **kwargs)
    elif update.effective_chat:
        await update.get_bot().send_message(chat_id=update.effective_chat.id, text=text, **kwargs)


class SendLimiter:
    def __init__(self, cooldown: float):
        self.cooldown = cooldown
        self._lock = asyncio.Lock()
        self._last_sent = 0.0

    async def wait_turn(self):
        async with self._lock:
            now = time.time()
            diff = now - self._last_sent
            if diff < self.cooldown:
                await asyncio.sleep(self.cooldown - diff)
            self._last_sent = time.time()


SEND_LIMITER = SendLimiter(CFG.send_cooldown)


async def safe_send(app: Application, text: str):
    for attempt in range(3):
        try:
            await SEND_LIMITER.wait_turn()
            await app.bot.send_message(
                chat_id=CFG.parse_chat_id(),
                text=text,
                parse_mode="HTML"
            )
            STATE.send_ok += 1
            STATE.sent_messages += 1
            return True
        except Exception as e:
            STATE.send_fail += 1
            STATE.last_error = f"send fail: {e}"
            await asyncio.sleep(1.5 + attempt)
    return False


def fmt_prediction(p: dict):
    rows = STORE.recent(CFG.max_recent_lines)
    rows = rows[::-1]
    recent = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(size_label(r[4]))} {h(r[3])} → {h(status_emoji(r[5]))}\n"
        for r in rows
    )

    candidates = " ".join(str(n) for n in p.get("top", [p["num"]]))

    return (
        f"📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>\n"
        f"🎯 <b>Size</b>: <b>{h(p['size'])}</b> | 🔢 <b>Target</b>: <code>{h(p['num'])}</code>\n"
        f"🎲 <b>Candidates</b>: {h(candidates)}\n"
        f"📈 <b>Confidence</b>: {h(p['confidence'])}% <i>(cal: {h(p['calibrated'])}%)</i>\n"
        f"🏷 <b>Tier</b>: <b>{h(p['tier'])}</b>\n"
        f"━━━━━━━━━━━━━\n"
        f"🕒 <b>Recent</b>:\n{recent}"
        f"━━━━━━━━━━━━━\n"
        f"🧬 <i>{h(CFG.bot_name)}</i>"
    )


@admin_only
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(
        u,
        f"🧬 <b>{h(CFG.bot_name)}</b> online\n"
        f"/stats /history /signals /weights /backtest /health /predict /last /status /config /stop /resume /export /recalibrate",
        parse_mode="HTML",
    )


@admin_only
async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = STORE.stats()
    if not s:
        await reply(u, "📊 Koi resolved prediction nahi abhi.")
        return

    pending, resolved = STORE.counts()
    jack_rate = 100 * s["jacks"] / s["total"] if s["total"] else 0
    rows = STORE.rolling_outcomes(CFG.rolling_eval_window)
    rolling = (sum(1 for st, _, _ in rows if st in ("WIN", "JACK")) / len(rows)) if rows else 0

    await reply(
        u,
        f"📊 <b>Stats</b>\n"
        f"Resolved: {resolved} | Pending: {pending}\n"
        f"✅ Win: {s['wins']} | ❌ Loss: {s['loss']}\n"
        f"🏆 JACK: {s['jacks']} ({jack_rate:.1f}%)\n"
        f"Overall rate: <b>{s['rate']}%</b>\n"
        f"Rolling({CFG.rolling_eval_window}): <b>{100*rolling:.1f}%</b>",
        parse_mode="HTML",
    )


@admin_only
async def cmd_history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent(15)
    if not rows:
        await reply(u, "🗂 Empty.")
        return
    rows = rows[::-1]
    lines = [
        f"<code>{h(r[0][-4:])}</code> {h(size_label(r[4]))} {h(r[3])} → {h(status_emoji(r[5]))} <i>{h(r[8])}</i>"
        for r in rows
    ]
    await reply(u, "🗂 <b>Recent</b>\n" + "\n".join(lines), parse_mode="HTML")


@admin_only
async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    acc = STORE.signal_accuracy()
    if not acc:
        await reply(u, "Signals ko resolved samples chahiye.")
        return
    lines = [f"{n:<12} {100*h_/t_:5.1f}% ({h_}/{t_})" for n, (h_, t_) in sorted(acc.items(), key=lambda kv: -kv[1][1])]
    await reply(u, "🧪 <b>Per-signal</b>\n<pre>" + h("\n".join(lines)) + "</pre>", parse_mode="HTML")


@admin_only
async def cmd_weights(u: Update, c: ContextTypes.DEFAULT_TYPE):
    w = PREDICTOR.acc_weights
    lines = [f"{n:<12} {v:.3f}" for n, v in sorted(w.items(), key=lambda kv: -kv[1])]
    await reply(u, "⚖️ <b>Weights</b>\n<pre>" + h("\n".join(lines)) + "</pre>", parse_mode="HTML")


@admin_only
async def cmd_backtest(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(u, "⏳ Backtesting...")
    bt = await asyncio.to_thread(PREDICTOR.backtest)
    if not bt:
        await reply(u, "History kam hai.")
        return

    edge = "EDGE ✅" if bt["p"] < 0.05 else "NO EDGE ❌"
    await reply(
        u,
        f"📉 <b>Backtest</b>\n"
        f"Samples: {bt['total']} | Skipped: {bt['skipped']}\n"
        f"Accuracy: <b>{100*bt['acc']:.1f}%</b>\n"
        f"Baseline: {100*bt['big_baseline']:.1f}%\n"
        f"z={bt['z']:+.2f}, p={bt['p']:.3f} → <b>{edge}</b>",
        parse_mode="HTML",
    )


@admin_only
async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE):
    meta = PREDICTOR.last_fetch_meta or {}
    pending, resolved = STORE.counts()
    await reply(
        u,
        f"🩺 <b>Health</b>\n"
        f"Uptime: <code>{h(STATE.uptime())}</code>\n"
        f"Paused: <b>{h(STATE.paused)}</b>\n"
        f"Fetch OK/Fail: <code>{h(STATE.fetch_ok)}/{h(STATE.fetch_fail)}</code>\n"
        f"Send OK/Fail: <code>{h(STATE.send_ok)}/{h(STATE.send_fail)}</code>\n"
        f"Recalibrations: <code>{h(STATE.recalibration_runs)}</code>\n"
        f"Loop iterations: <code>{h(STATE.loop_iterations)}</code>\n"
        f"Last issue: <code>{h(meta.get('last_issue', 'n/a'))}</code>\n"
        f"Last draw: <code>{h(meta.get('last_size', 'n/a'))} {h(meta.get('last_num', 'n/a'))}</code>\n"
        f"Pending/Resolved: <code>{h(pending)}/{h(resolved)}</code>\n"
        f"Last error: <code>{h(STATE.last_error or 'none')[:400]}</code>",
        parse_mode="HTML",
    )


@admin_only
async def cmd_predict(u: Update, c: ContextTypes.DEFAULT_TYPE):
    p = PREDICTOR.predict()
    if not p:
        await reply(u, "Prediction unavailable.")
        return
    await reply(u, fmt_prediction(p), parse_mode="HTML")


@admin_only
async def cmd_last(u: Update, c: ContextTypes.DEFAULT_TYPE):
    row = STORE.latest_prediction()
    if not row:
        await reply(u, "No prediction found.")
        return

    issue, size, num, conf, calib, tier, logic, details, status, actual_num, actual_size, _ = row
    await reply(
        u,
        f"🧾 <b>Last</b>\n"
        f"Issue: <code>{h(issue)}</code>\n"
        f"Pred: <b>{h(size)}</b> {h(num)}\n"
        f"Conf: {h(conf)}% (cal: {h(calib)}%) | {h(tier)}\n"
        f"Status: {h(status)}\n"
        f"Actual: {h(actual_size)} {h(actual_num)}",
        parse_mode="HTML",
    )


@admin_only
async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent_pending(5)
    if not rows:
        await reply(u, "No pending predictions.")
        return

    lines = []
    for issue, size, num, conf, calib, tier, logic in rows:
        lines.append(
            f"<code>{h(issue[-4:])}</code> {h(size)} {h(num)} "
            f"<i>{h(conf)}%/{h(calib)}%</i> <b>{h(tier)}</b>"
        )

    await reply(u, "⏳ <b>Pending</b>\n" + "\n".join(lines), parse_mode="HTML")


@admin_only
async def cmd_config(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(
        u,
        f"⚙️ <b>Config</b>\n"
        f"BOT_NAME: <code>{h(CFG.bot_name)}</code>\n"
        f"FETCH_EVERY: <code>{h(CFG.fetch_every)}</code>\n"
        f"TIMEOUT: <code>{h(CFG.request_timeout)}</code>\n"
        f"HIGH_CONF_ONLY: <code>{h(CFG.high_conf_only)}</code>\n"
        f"HIGH_CONF_THRESHOLD: <code>{h(CFG.high_conf_threshold)}</code>\n"
        f"MIN_SIZE_HISTORY: <code>{h(CFG.min_size_history)}</code>\n"
        f"BACKTEST_MIN_HISTORY: <code>{h(CFG.backtest_min_history)}</code>\n"
        f"ROLLING_EVAL_WINDOW: <code>{h(CFG.rolling_eval_window)}</code>\n"
        f"DB_PATH: <code>{h(CFG.db_path)}</code>\n"
        f"API_URL: <code>{h(CFG.api_url)[:300]}</code>",
        parse_mode="HTML",
    )


@admin_only
async def cmd_stop(u: Update, c: ContextTypes.DEFAULT_TYPE):
    STATE.paused = True
    await reply(u, "⏸ Bot paused.")


@admin_only
async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    STATE.paused = False
    await reply(u, "▶️ Bot resumed.")


@admin_only
async def cmd_export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    ts = int(time.time())
    export_path = str(Path(CFG.export_dir) / f"predictions_{ts}.csv")
    await asyncio.to_thread(STORE.export_csv, export_path)
    await reply(u, f"📦 Exported: <code>{h(export_path)}</code>", parse_mode="HTML")


@admin_only
async def cmd_recalibrate(u: Update, c: ContextTypes.DEFAULT_TYPE):
    PREDICTOR.refresh_weights()
    PREDICTOR.adaptive_reweight()
    STATE.resolved_since_recalibration = 0
    await reply(u, "♻️ Recalibrated weights.")


async def worker_loop(app: Application):
    log.info("worker loop started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                STATE.loop_iterations += 1
                ok = await PREDICTOR.fetch(session)

                if ok and PREDICTOR.history:
                    hist = {x["issue"]: x for x in PREDICTOR.history}

                    resolved = STORE.resolve(hist, limit=CFG.resolve_limit)
                    if resolved:
                        STATE.last_resolve_at = time.time()

                    for res in resolved:
                        STATE.resolved_since_recalibration += 1
                        PREDICTOR.refresh_weights()

                        if STATE.resolved_since_recalibration >= max(1, CFG.recalibrate_every):
                            with contextlib.suppress(Exception):
                                PREDICTOR.adaptive_reweight()
                            STATE.resolved_since_recalibration = 0

                        if not STATE.paused:
                            await safe_send(
                                app,
                                f"🎯 <b>Result</b>\n"
                                f"Period <code>{h(res['issue'][-4:])}</code>\n"
                                f"Pred {h(res['size'])} {h(res['num'])}\n"
                                f"Actual {h(res['actual_size'])} {h(res['actual_num'])}\n"
                                f"{h(status_emoji(res['status']))}"
                            )

                    latest_issue = PREDICTOR.history[-1]["issue"]
                    next_issue = str(int(latest_issue) + 1)

                    if not STATE.paused:
                        if STATE.dedupe_issue != next_issue and not STORE.exists(next_issue):
                            p = PREDICTOR.predict()
                            if p:
                                should_send = True
                                if CFG.high_conf_only and p["calibrated"] < CFG.high_conf_threshold:
                                    should_send = False

                                STORE.save(p)
                                STATE.dedupe_issue = next_issue

                                if should_send:
                                    await safe_send(app, fmt_prediction(p))
                                    log.info("prediction saved/sent for issue %s", p["issue"])
                                else:
                                    log.info("prediction saved only for issue %s", p["issue"])

                await asyncio.sleep(CFG.fetch_every)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                STATE.last_error = f"loop exception: {e}"
                log.exception("loop exception: %s", e)
                await asyncio.sleep(3)


async def main():
    CFG.validate()

    app = Application.builder().token(CFG.telegram_token).build()

    handlers = [
        ("start", cmd_start),
        ("stats", cmd_stats),
        ("history", cmd_history),
        ("signals", cmd_signals),
        ("weights", cmd_weights),
        ("backtest", cmd_backtest),
        ("health", cmd_health),
        ("predict", cmd_predict),
        ("last", cmd_last),
        ("status", cmd_status),
        ("config", cmd_config),
        ("stop", cmd_stop),
        ("resume", cmd_resume),
        ("export", cmd_export),
        ("recalibrate", cmd_recalibrate),
    ]

    for name, fn in handlers:
        app.add_handler(CommandHandler(name, fn))

    stop_event = asyncio.Event()

    def _stop_signal(*_):
        log.info("shutdown signal received")
        stop_event.set()

    with contextlib.suppress(NotImplementedError):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _stop_signal)
        loop.add_signal_handler(signal.SIGTERM, _stop_signal)

    async with app:
        await app.start()

        if app.updater is None:
            raise RuntimeError("Updater unavailable.")

        await app.updater.start_polling()
        bg_task = asyncio.create_task(worker_loop(app))

        try:
            await stop_event.wait()
        finally:
            bg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bg_task
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())







