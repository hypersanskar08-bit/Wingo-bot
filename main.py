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
    db_path: str = os.environ.get("DB_PATH", "ftw_final_pattern.db")
    cache_file: str = "ftw_final_pattern_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW FINAL PATTERN V2"
    min_history: int = 8
    recent_limit: int = 12
    max_history: int = 1000
    min_repeat: int = 3
    pattern_stats_cap: int = 2500

    def parse_chat_id(self):
        raw = self.chat_id_raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def api_urls(self) -> List[str]:
        urls = [u.strip() for u in self.source_api_urls.split(",") if u.strip()]
        return urls or [
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
    if value is None: return None
    if isinstance(value, str):
        value = value.strip()
        if value == "": return None
        if value.isdigit(): return int(value)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits: return int(digits)
    try: return int(value)
    except Exception: return None

def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))

def trailing_run(seq: List[int]) -> int:
    if not seq: return 0
    last = seq[-1]
    count = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last: count += 1
        else: break
    return count

def volatility(seq: List[int], window: int = 20) -> float:
    if len(seq) < 2: return 0.0
    arr = seq[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))

def ratio_big(seq: List[int], window: int = 20) -> float:
    if not seq: return 0.5
    arr = seq[-window:]
    return sum(arr) / max(1, len(arr))

def detect_regime(seq: List[int]) -> str:
    if len(seq) < 8: return "COLD"
    run = trailing_run(seq)
    alt = sum(1 for i in range(1, len(seq[-10:])) if seq[-10:][i] != seq[-10:][i - 1]) / max(1, len(seq[-10:]) - 1)
    ratio = ratio_big(seq, 10)
    if alt >= 0.65: return "ALTERNATING"
    if run >= 5: return "TREND"
    if run >= 3: return "TRAP"
    if abs(ratio - 0.5) <= 0.12: return "CHOPPY"
    return "CYCLE"

def signal_strength(history: List[Dict[str, Any]]) -> float:
    if len(history) < 8: return 0.2
    seq = [x["size"] for x in history]
    regime = detect_regime(seq)
    run = trailing_run(seq)
    alt = sum(1 for i in range(1, len(seq[-12:])) if seq[-12:][i] != seq[-12:][i - 1]) / max(1, len(seq[-12:]) - 1)
    s = 0.0
    if regime == "TRAP": s += 0.35
    if regime == "TREND": s += 0.25
    if regime == "ALTERNATING": s += 0.30
    if run >= 3: s += 0.25
    if alt >= 0.6: s += 0.15
    return clamp(min(1.0, s), 0.0, 1.0)

# ============================================================
# STATISTICS CORE (shared by every detector below)
# Wilson lower bound = how confident we can be a win-rate is really above
# 50% given how much live evidence actually exists — a pattern seen 3
# times at 67% and one seen 80 times at 67% must NOT be trusted the same
# amount, and this is what makes that honest instead of a flat threshold.
# ============================================================
def wilson_lb(w: float, n: float, z: float = 1.645) -> float:
    if n <= 0: return 0.5
    p = w / n
    z2 = z * z
    denom = 1 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * math.sqrt(max(0.0, (p * (1 - p) + z2 / (4 * n)) / n))
    return max(0.0, min(1.0, (center - margin) / denom))

def logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))

def sigmoid(x: float) -> float:
    x = max(-15.0, min(15.0, x))
    return 1.0 / (1.0 + math.exp(-x))

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
        except Exception: pass

    def load_cache(self):
        try:
            if os.path.exists(CFG.cache_file):
                with open(CFG.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data[-CFG.max_history:]
        except Exception: pass

STATE = BotState()

# ============================================================
# STORAGE
# One unified, honest tracking system (replaces the old pair of
# pattern_penalties + pattern_memory tables, which duplicated state and
# could disagree with each other) plus a new engine_stats table that
# shadow-tracks EVERY sub-engine's own live accuracy every round, whether
# it was the one actually used or not — that's what lets the fusion step
# below trust whichever engines are genuinely working right now instead
# of a fixed hard-coded preference order.
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
                    issue TEXT PRIMARY KEY, pred_type TEXT, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT,
                    logic TEXT, details TEXT, stake TEXT, created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pattern_stats (
                    pattern_key TEXT PRIMARY KEY,
                    w_hits REAL DEFAULT 0, w_total REAL DEFAULT 0,
                    total_preds INTEGER DEFAULT 0, total_wins INTEGER DEFAULT 0,
                    strikes INTEGER DEFAULT 0, blacklisted INTEGER DEFAULT 0,
                    recent TEXT DEFAULT '[]', last_seen REAL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS engine_stats (
                    engine_name TEXT PRIMARY KEY,
                    w_hits REAL DEFAULT 0, w_total REAL DEFAULT 0,
                    total_preds INTEGER DEFAULT 0, total_wins INTEGER DEFAULT 0,
                    last_seen REAL DEFAULT 0
                )
            """)

    # ---- predictions (unchanged shape) ----
    def save_prediction(self, p: Dict[str, Any]):
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO predictions
                (issue, pred_type, size, num, logic, details, stake, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["issue"], p["type"], p.get("size", ""), p.get("num", 0), p.get("logic", "N/A"),
                  json.dumps(p.get("details", {})), p.get("stake", "1x"), time.time()))

    def prediction_exists(self, issue: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def resolve_pending(self, history_by_issue):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, pred_type, size, num, details, stake FROM predictions WHERE status='PENDING'").fetchall()

        for issue, pred_type, p_size, p_num, details_json, stake in rows:
            act = history_by_issue.get(str(issue))
            if not act: continue

            actual_size = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == actual_size) if p_size else False
            status = "JACK 🏆" if (win and p_num == act["num"]) else "WIN ✅" if win else "LOSS ❌"

            with self._conn() as c2:
                c2.execute("UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                           (status, act["num"], actual_size, issue))

            resolved.append({
                "issue": issue, "type": pred_type or "SIZE", "size": p_size, "num": p_num, "stake": stake,
                "actual_num": act["num"], "actual_size": actual_size, "status": status, "win": win,
                "details": json.loads(details_json) if details_json else {}
            })
        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, size, status FROM predictions WHERE status != 'PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,)).fetchall()
        return rows[::-1]

    # ---- unified pattern tracking ----
    def get_pattern_stats(self, key: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT w_hits, w_total, total_preds, total_wins, strikes, blacklisted, recent FROM pattern_stats WHERE pattern_key=?",
                (key,)).fetchone()
        if not row:
            return {"w_hits": 0.0, "w_total": 0.0, "total_preds": 0, "total_wins": 0,
                     "strikes": 0, "blacklisted": False, "recent": []}
        return {"w_hits": row[0], "w_total": row[1], "total_preds": row[2], "total_wins": row[3],
                "strikes": row[4], "blacklisted": bool(row[5]), "recent": json.loads(row[6] or "[]")}

    def record_pattern_result(self, key: str, won: bool):
        """
        Replaces the old binary is_flipped-after-2-losses rule (which
        flip-flops on pure noise — a genuinely 50-55% pattern hits 2
        losses in a row roughly 1 round in 4-5, so the old logic was
        inverting itself constantly, then inverting back, etc.) with:
          - decayed Bayesian hit/total counts, so recent rounds matter
            most but nothing is thrown away after one bad round
          - a 3-STRIKES BLACKLIST instead of a flip: a pattern that keeps
            losing sits OUT of the vote rather than being inverted and
            instantly trusted in the opposite direction with zero
            evidence that direction is any better
          - 2 consecutive wins (while still being graded, even while
            blacklisted) lift the blacklist
        """
        st = self.get_pattern_stats(key)
        decay = 0.94
        w_hits = st["w_hits"] * decay + (1.0 if won else 0.0)
        w_total = st["w_total"] * decay + 1.0
        total_preds = st["total_preds"] + 1
        total_wins = st["total_wins"] + (1 if won else 0)
        recent = (st["recent"] + [1 if won else 0])[-12:]

        strikes = st["strikes"]
        blacklisted = st["blacklisted"]
        if won:
            strikes = 0
            if blacklisted and len(recent) >= 2 and recent[-2:] == [1, 1]:
                blacklisted = False
        else:
            strikes += 1
            if strikes >= 3:
                blacklisted = True

        with self._conn() as c:
            c.execute("""
                INSERT INTO pattern_stats (pattern_key, w_hits, w_total, total_preds, total_wins, strikes, blacklisted, recent, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    w_hits=excluded.w_hits, w_total=excluded.w_total,
                    total_preds=excluded.total_preds, total_wins=excluded.total_wins,
                    strikes=excluded.strikes, blacklisted=excluded.blacklisted,
                    recent=excluded.recent, last_seen=excluded.last_seen
            """, (key, w_hits, w_total, total_preds, total_wins, strikes, int(blacklisted), json.dumps(recent), time.time()))

    def pattern_lower_bound(self, key: str) -> Tuple[float, int]:
        st = self.get_pattern_stats(key)
        if st["blacklisted"]:
            return 0.0, st["total_preds"]
        return wilson_lb(st["w_hits"], st["w_total"]), st["total_preds"]

    def cleanup_pattern_stats(self, keep: Optional[int] = None):
        keep = keep or CFG.pattern_stats_cap
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM pattern_stats").fetchone()[0]
            if n > keep:
                c.execute("""
                    DELETE FROM pattern_stats WHERE pattern_key IN (
                        SELECT pattern_key FROM pattern_stats ORDER BY last_seen ASC LIMIT ?
                    )
                """, (n - keep,))

    # ---- per sub-engine live accuracy (shadow-tracked every round) ----
    def record_engine_result(self, name: str, won: bool):
        with self._conn() as c:
            row = c.execute("SELECT w_hits, w_total, total_preds, total_wins FROM engine_stats WHERE engine_name=?",
                            (name,)).fetchone()
        decay = 0.96
        w_hits0, w_total0, tp0, tw0 = (0.0, 0.0, 0, 0) if not row else row
        w_hits = w_hits0 * decay + (1.0 if won else 0.0)
        w_total = w_total0 * decay + 1.0
        with self._conn() as c:
            c.execute("""
                INSERT INTO engine_stats (engine_name, w_hits, w_total, total_preds, total_wins, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(engine_name) DO UPDATE SET
                    w_hits=excluded.w_hits, w_total=excluded.w_total,
                    total_preds=excluded.total_preds, total_wins=excluded.total_wins, last_seen=excluded.last_seen
            """, (name, w_hits, w_total, tp0 + 1, tw0 + (1 if won else 0), time.time()))

    def engine_weight(self, name: str) -> float:
        """Soft internal fusion weight — engines with little live data get a
        small flat vote; engines not beating chance get pushed near zero."""
        with self._conn() as c:
            row = c.execute("SELECT w_hits, w_total, total_preds FROM engine_stats WHERE engine_name=?",
                            (name,)).fetchone()
        if not row or row[2] < 8:
            return 0.35
        lb = wilson_lb(row[0], row[1], z=1.28)
        return 0.08 if lb <= 0.5 else (lb - 0.5) * 6.0

    def engine_lb(self, name: str) -> Tuple[float, int]:
        """Stricter, display/priority-grade lower bound (95% one-sided)."""
        with self._conn() as c:
            row = c.execute("SELECT w_hits, w_total, total_preds FROM engine_stats WHERE engine_name=?",
                            (name,)).fetchone()
        if not row or row[2] < 15:
            return 0.0, 0 if not row else row[2]
        return wilson_lb(row[0], row[1], z=1.645), row[2]

STORE = Storage(CFG.db_path)

# ============================================================
# PATTERN MINER
# Tests EVERY signature length 4-16 as a plain tail-substring of the
# actual B/S sequence (not just run-compressed block suffixes like
# before, which only ever produced a handful of candidates per round) —
# a much larger, more thorough candidate space.
# ============================================================
def mine_exact_patterns(history: List[Dict[str, Any]], store: "Storage") -> List[Dict[str, Any]]:
    seq = [1 if x["size"] == 1 else 0 for x in history]
    N = len(seq)
    out: List[Dict[str, Any]] = []
    if N < 20:
        return out
    for L in range(16, 3, -1):
        if N < L + CFG.min_repeat:
            continue
        tail = seq[-L:]
        matches = [i for i in range(N - L) if seq[i:i + L] == tail]
        if len(matches) < CFG.min_repeat:
            continue
        next_bits = [seq[i + L] for i in matches if i + L < N]
        if not next_bits:
            continue
        total = len(next_bits)
        decay = 0.90
        weights = [decay ** (total - 1 - k) for k in range(total)]
        w_big = sum(w for w, v in zip(weights, next_bits) if v == 1)
        w_small = sum(w for w, v in zip(weights, next_bits) if v == 0)
        w_tot = w_big + w_small or 1.0
        p_big = (w_big + 1) / (w_tot + 2)      # Laplace smoothing
        if abs(p_big - 0.5) < 0.06:
            continue
        key = "".join("B" if v else "S" for v in tail)
        lb, n_live = store.pattern_lower_bound(key)
        out.append({
            "key": key, "length": L, "n_hist": total, "p_big": p_big,
            "dir": 1 if p_big >= 0.5 else 0, "lb": lb, "n_live": n_live,
        })
    out.sort(key=lambda d: (d["lb"], d["length"]), reverse=True)
    return out

def fuzzy_match_signal(seq: List[int]) -> Optional[Tuple[int, float, float]]:
    """Approximate (Hamming-similarity ≥5/7) match — a genuinely different
    technique from exact matching, catches near-repeats exact matching
    would miss entirely."""
    N = len(seq)
    win = 7
    if N < win * 3:
        return None
    recent = seq[-win:]
    m_big = m_small = 0
    for i in range(0, N - win * 2):
        w = seq[i:i + win]
        sim = sum(1 for a, b in zip(w, recent) if a == b)
        if sim >= 5:
            nxt = seq[i + win] if i + win < N else None
            if nxt is not None:
                if nxt == 1: m_big += 1
                else: m_small += 1
    total = m_big + m_small
    if total < 3:
        return None
    p_big = (m_big + 1) / (total + 2)
    if abs(p_big - 0.5) < 0.06:
        return None
    return (1 if p_big >= 0.5 else 0, p_big, min(1.0, total / 10.0))

def gap_theory_signal(seq: List[int]) -> Optional[Tuple[int, float]]:
    """How long since the OPPOSITE side last appeared — the longer the
    gap, the more "due" that side is considered."""
    if len(seq) < 10:
        return None
    last = seq[-1]
    opp = 1 - last
    gap = None
    for i, v in enumerate(reversed(seq[:-1])):
        if v == opp:
            gap = i + 1
            break
    if gap is None or gap < 5:
        return None
    strength = min(1.0, (gap - 4) / 8.0) * 0.9
    return (opp, strength)

def alternation_break_signal(seq: List[int]) -> Optional[Tuple[int, float]]:
    """6 perfectly alternating results in a row is itself an unusual event
    — bet on the alternation finally breaking."""
    if len(seq) < 7:
        return None
    tail = seq[-6:]
    strict = all(tail[i] != tail[i + 1] for i in range(len(tail) - 1))
    if not strict:
        return None
    return (1 - seq[-1], 0.7)

def advanced_markov(seq: List[int], max_n: int = 4) -> Tuple[float, float]:
    if len(seq) < 5: return 0.5, 0.0
    total_score, total_weight = 0.0, 0.0
    for n in range(2, max_n + 1):
        if len(seq) <= n: continue
        pattern = tuple(seq[-n:])
        match_count, big_count = 0, 0
        for i in range(len(seq) - n):
            if tuple(seq[i:i+n]) == pattern:
                match_count += 1
                if seq[i+n] == 1: big_count += 1
        if match_count >= 2:
            prob = big_count / match_count
            weight = match_count * (n ** 1.5)
            total_score += prob * weight
            total_weight += weight
    return (total_score / total_weight, total_weight) if total_weight > 0 else (0.5, 0.0)

# ============================================================
# SIGNAL GATHERING + FUSION
# Every detector returns its own raw directional call + raw evidence
# strength; the fusion step below is what decides how much to actually
# trust each one, based on that engine's OWN live track record — nothing
# is hard-coded as "the preferred" engine the way the old pattern-first
# logic was.
# ============================================================
def gather_signals(history: List[Dict[str, Any]], store: "Storage") -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    seq = [x["size"] for x in history]
    N = len(seq)
    signals: List[Dict[str, Any]] = []

    exact_candidates = mine_exact_patterns(history, store)
    top_pattern = exact_candidates[0] if exact_candidates else None
    if top_pattern and not store.get_pattern_stats(top_pattern["key"])["blacklisted"]:
        conf = 0.5 + abs(top_pattern["p_big"] - 0.5) * min(1.0, math.log(top_pattern["n_hist"] + 1) / math.log(15))
        signals.append({"name": "PatternExact", "dir": top_pattern["dir"], "conf": conf})

    fz = fuzzy_match_signal(seq)
    if fz:
        d, p, ev = fz
        signals.append({"name": "PatternFuzzy", "dir": d, "conf": 0.5 + abs(p - 0.5) * ev})

    gt = gap_theory_signal(seq)
    if gt:
        d, strength = gt
        signals.append({"name": "GapTheory", "dir": d, "conf": 0.5 + strength * 0.4})

    ab = alternation_break_signal(seq)
    if ab:
        d, strength = ab
        signals.append({"name": "AltBreak", "dir": d, "conf": 0.5 + strength * 0.4})

    mk_prob, mk_weight = advanced_markov(seq)
    if mk_weight > 0 and abs(mk_prob - 0.5) >= 0.05:
        signals.append({"name": "Markov", "dir": 1 if mk_prob >= 0.5 else 0, "conf": max(mk_prob, 1 - mk_prob)})

    run = trailing_run(seq)
    if run >= 3:
        signals.append({"name": "TrendFollow", "dir": seq[-1], "conf": 0.5 + min(0.25, run * 0.04)})

    if N >= 20:
        sma5, sma20 = sum(seq[-5:]) / 5, sum(seq[-20:]) / 20
        if abs(sma5 - sma20) > 0.12:
            d = 1 if sma5 > sma20 else 0
            signals.append({"name": "SmaCross", "dir": d, "conf": 0.5 + min(0.2, abs(sma5 - sma20))})

    return signals, top_pattern

ALL_ENGINE_NAMES = ["PatternExact", "PatternFuzzy", "GapTheory", "AltBreak", "Markov", "TrendFollow", "SmaCross"]

def fuse_signals(signals: List[Dict[str, Any]], store: "Storage") -> Tuple[int, float, List[str]]:
    """Log-odds fusion weighted by each sub-engine's OWN live (Wilson lower
    bound) accuracy — an engine that isn't beating chance gets pushed
    toward a near-zero vote automatically, every round, forever; nothing
    is assumed good just because it sounds sophisticated."""
    lo_sum, wt_sum = 0.0, 0.0
    used = []
    for s in signals:
        ew = store.engine_weight(s["name"])
        cw = max(0.0, s["conf"] - 0.5) * 2.0 * ew
        if cw <= 1e-6:
            continue
        p = s["conf"] if s["dir"] == 1 else 1 - s["conf"]
        lo_sum += logit(p) * cw
        wt_sum += cw
        used.append(s["name"])
    if wt_sum < 1e-6:
        return (1 if random.random() > 0.5 else 0), 0.5, []
    p_final = sigmoid(lo_sum / wt_sum)
    return (1 if p_final >= 0.5 else 0), max(p_final, 1 - p_final), used

def apply_pre_pattern_priority(final_dir: int, final_conf: float, top_pattern: Optional[Dict[str, Any]],
                                store: "Storage") -> Tuple[int, float, str]:
    """A specific pattern with ≥65% of its OWN live, round-by-round graded
    calls correct (Wilson lower bound, ≥8 graded rounds, not blacklisted)
    outranks the weighted vote outright — UNLESS some engine's own proven
    live accuracy is even higher than that."""
    if not top_pattern:
        return final_dir, final_conf, ""
    lb, n_live = store.pattern_lower_bound(top_pattern["key"])
    if n_live < 8 or lb < 0.65:
        return final_dir, final_conf, ""
    best_engine_lb = 0.0
    for name in ALL_ENGINE_NAMES:
        elb, en = store.engine_lb(name)
        if en >= 15:
            best_engine_lb = max(best_engine_lb, elb)
    if best_engine_lb > lb:
        return final_dir, final_conf, ""
    return top_pattern["dir"], max(final_conf, lb), " +PRE65"

# ============================================================
# NUMBER PREDICTOR — deterministic (the old version used
# random.randint() inside the "prediction", so the exact same history
# could produce a different number guess every time it was asked; that's
# not a prediction, it's noise wearing a label. Gap+frequency scoring is
# stable: same history in, same number out.)
# ============================================================
def predict_number(history: List[Dict[str, Any]], direction: int) -> int:
    cands = [5, 6, 7, 8, 9] if direction == 1 else [0, 1, 2, 3, 4]
    nums = [x["num"] for x in history[-120:]]
    if len(nums) < 8:
        return cands[0]
    n = len(nums)
    gap = {}
    for v in cands:
        g = n
        for i in range(n - 1, -1, -1):
            if nums[i] == v:
                g = n - 1 - i
                break
        gap[v] = g
    freq = {v: 0.0 for v in cands}
    for i, v in enumerate(nums):
        if v in freq:
            freq[v] += 1.0 / ((n - i) ** 0.4)
    scored = []
    for v in cands:
        s = gap[v] * 0.8 + freq[v] * 1.0 + (9 - v) * 0.001   # tiny deterministic tie-break, not random
        scored.append((s, v))
    scored.sort(reverse=True)
    return scored[0][1]

# ============================================================
# PREDICTION BUILD
# ============================================================
def build_prediction(history: List[Dict[str, Any]], issue: Any, store: "Storage") -> Dict[str, Any]:
    if len(history) < CFG.min_history:
        n = random.randint(0, 9)   # genuinely no data yet — an honest coin flip, not a fabricated "signal"
        return {"issue": str(issue), "type": "SIZE", "size": "BIG" if n >= 5 else "SMALL", "num": n,
                "raw_conf": 50, "logic": "WARMING UP", "stake": "0.5x", "details": {}}

    seq_full = [x["size"] for x in history]
    signals, top_pattern = gather_signals(history, store)
    final_dir, final_conf, used = fuse_signals(signals, store)
    final_dir, final_conf, pre_tag = apply_pre_pattern_priority(final_dir, final_conf, top_pattern, store)

    # Cooldown now looks at the ENSEMBLE's own recent live record (Wilson
    # lower bound over the last 10 resolved rounds) instead of a raw
    # loss-streak trigger. 2-in-a-row is common even for a genuinely good
    # predictor, and the old code both skipped AND flipped a pattern on
    # that same noisy event — compounding into exactly the oscillation
    # this rewrite is meant to fix.
    recent_rows = store.recent_outcomes(10)
    recent_results = [1 if ("WIN" in str(s) or "JACK" in str(s)) else 0 for _, _, s in recent_rows]
    ens_lb = wilson_lb(sum(recent_results), len(recent_results), z=1.28) if len(recent_results) >= 8 else 0.55

    if STATE.loss_streak >= 4 and ens_lb < 0.45:
        return {"issue": str(issue), "type": "SIZE", "size": "SKIP", "num": 0, "raw_conf": 0,
                "logic": f"COOLDOWN (streak={STATE.loss_streak}, live={ens_lb:.0%})", "stake": "0x", "details": {}}

    pred_size = "BIG" if final_dir == 1 else "SMALL"
    final_num = predict_number(history, final_dir)

    logic_bits = []
    if top_pattern:
        logic_bits.append(f"PAT L{top_pattern['length']} n={top_pattern['n_hist']}")
    if used:
        logic_bits.append("+".join(used))
    if pre_tag:
        logic_bits.append(pre_tag.strip())
    logic = " | ".join(logic_bits) if logic_bits else "ENSEMBLE"

    conf_pct = int(clamp(final_conf, 0.50, 0.85) * 100)
    stake = "2x" if conf_pct >= 72 else "1x" if conf_pct >= 60 else "0.5x"

    shadow = {s["name"]: s["dir"] for s in signals}   # every engine's own call, graded live next round regardless of use

    return {
        "issue": str(issue), "type": "SIZE", "size": pred_size, "num": final_num,
        "raw_conf": conf_pct, "logic": logic, "stake": stake,
        "details": {
            "signal_strength": signal_strength(history),
            "regime": detect_regime(seq_full),
            "pattern_key": top_pattern["key"] if top_pattern else None,
            "shadow": shadow,
        }
    }

# ============================================================
# PARSE & FETCH
# ============================================================
def parse_history_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None: return []
    if isinstance(payload, list): items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list): items = payload["data"]
        elif isinstance(payload.get("data"), dict): items = payload["data"].get("list", []) if isinstance(payload["data"].get("list"), list) else []
        else: items = payload.get("result", payload.get("items", payload.get("history", [])))
    else: items = []

    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        issue = normalize_issue(item.get("issueNumber", item.get("issue", "")))
        if issue is None: continue
        num = safe_int(item.get("number", item.get("value", 0)), 0)
        if not (0 <= num <= 9): continue
        key = str(issue)
        if key in seen: continue
        seen.add(key)
        out.append({"issue": key, "num": num, "size": 1 if num >= 5 else 0})
    out.sort(key=lambda x: int(x["issue"]))
    return out

async def fetch_history(session):
    for url in CFG.api_urls():
        try:
            async with session.get(url, timeout=CFG.request_timeout) as response:
                if response.status != 200: continue
                parsed = parse_history_payload(await response.json(content_type=None))
                if parsed:
                    STATE.history = parsed[-CFG.max_history:]
                    STATE.save_cache()
                    return True
        except Exception: pass
    return False

# ============================================================
# FORMAT / SEND
# ============================================================
def normalize_recent_row(row: Any):
    if not isinstance(row, (tuple, list)): return "", "", "", ""
    if len(row) >= 3: return str(row[0]), str(row[1]), str(row[2]), ""
    return "", "", "", ""

def format_prediction(pred: Dict[str, Any], recent_rows):
    recent_lines = []
    for r in recent_rows[-CFG.recent_limit:]:
        issue, size, status, _ = normalize_recent_row(r)
        if not issue: continue
        recent_lines.append(f"➜ <code>{h(issue[-4:])}</code> {h(size)} → <b>{h(status)}</b>" if status else f"➜ <code>{h(issue[-4:])}</code> {h(size)}")
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
    return f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'])}</code>\nPred: <b>{h(res['size'])} {h(res['num'])}</b>\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>"

async def send_msg(bot, text: str, retries: int = 3):
    for attempt in range(retries):
        try:
            await asyncio.wait_for(bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML"), timeout=CFG.request_timeout)
            return True
        except Exception: await asyncio.sleep(1)
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
                        STATE.win_streak += 1; STATE.loss_streak = 0; STATE.recovery_mode = False
                    else:
                        STATE.loss_streak += 1; STATE.win_streak = 0; STATE.recovery_mode = STATE.loss_streak >= 4

                    details = res.get("details", {}) or {}
                    pattern_key = details.get("pattern_key")
                    if pattern_key:
                        STORE.record_pattern_result(str(pattern_key), bool(res["win"]))

                    shadow = details.get("shadow") or {}
                    actual_dir = 1 if res["actual_size"] == "BIG" else 0
                    for name, d in shadow.items():
                        STORE.record_engine_result(name, d == actual_dir)

                    STORE.cleanup_pattern_stats()
                    STATE.save_cache()
                    await send_msg(app.bot, format_result(res))

                if not STATE.history:
                    await asyncio.sleep(CFG.fetch_every)
                    continue

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(max([normalize_issue(x["issue"]) for x in STATE.history if normalize_issue(x["issue"]) is not None], default=0) + 1)

                if latest_issue != last_processed_issue and not STORE.prediction_exists(next_issue):
                    pred = build_prediction(STATE.history, next_issue, STORE)

                    if pred["size"] == "SKIP":
                        skip_msg = f"⚠️ <b>COOLDOWN ACTIVE</b>\n{h(pred['logic'])}\nPeriod: <code>{next_issue}</code>"
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

async def health_check(request): return web.Response(text="FTW FINAL PATTERN V2 is Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

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
    asyncio.run(main())
