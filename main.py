"""
FTW AI V34 FIX
====================
- exact pattern only when strong enough
- pattern length 3..13
- recent 1000 result matching
- trend shift detector with strict gate
- safe fallback when signal weak
- number prediction via exact next-value distribution
- Telegram + SQLite + API fetch
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
    db_path: str = os.environ.get("DB_PATH", "ftw_ai_v34_fix.db")
    cache_file: str = "ftw_ai_v34_fix_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW AI V34 FIX"
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

    fh = RotatingFileHandler(Path(CFG.log_dir) / "ftw_ai_v34_fix.log", maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_ai_v34")

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
# EXACT PATTERN DETECTOR WITH STRICT GATE
# ============================================================
class ExactPatternV34:
    def __init__(self):
        self.min_window = 3
        self.max_window = 13

    def _pattern_match(self, seq: List[int], window: int) -> Dict[str, float]:
        if len(seq) < window + 2:
            return {"BIG": 0.5, "SMALL": 0.5, "strength": 0.0, "matches": 0}

        suffix = tuple(seq[-window:])
        big_total = 0.0
        small_total = 0.0
        total_weight = 0.0
        matches = 0

        for i in range(len(seq) - window - 1):
            candidate = tuple(seq[i:i + window])
            if candidate != suffix:
                continue

            matches += 1
            next_val = seq[i + window]
            distance = len(seq) - (i + window)
            weight = max(0.10, 1.0 - (distance / 1000.0))
            total_weight += weight
            if next_val == 1:
                big_total += weight
            else:
                small_total += weight

        if total_weight == 0 or matches == 0:
            return {"BIG": 0.5, "SMALL": 0.5, "strength": 0.0, "matches": 0}

        p_big = (big_total + 1.0) / (total_weight + 2.0)
        p_small = (small_total + 1.0) / (total_weight + 2.0)

        return {
            "BIG": p_big,
            "SMALL": p_small,
            "strength": abs(p_big - p_small),
            "matches": matches,
        }

    def detect(self, seq: List[int]) -> Dict[str, Any]:
        if not seq:
            return {
                "prediction": "BIG",
                "confidence": 0.50,
                "regime": "neutral",
                "top_mode": None,
                "strong": False,
            }

        best = None
        best_score = -1.0

        for window in range(self.min_window, self.max_window + 1):
            result = self._pattern_match(seq, window)
            strength = result["strength"]
            matches = result["matches"]
            if matches == 0:
                continue

            # strict gate: only accept if pattern is actually strong enough
            if strength < 0.12:
                continue

            pred = "BIG" if result["BIG"] >= result["SMALL"] else "SMALL"
            score = strength * 100.0 + (matches * 3.0) + (window * 2.5)

            if score > best_score:
                best_score = score
                best = {
                    "window": window,
                    "pred": pred,
                    "big_prob": result["BIG"],
                    "small_prob": result["SMALL"],
                    "strength": strength,
                    "matches": matches,
                    "confidence": max(result["BIG"], result["SMALL"]),
                }

        if best is None:
            # safe fallback
            last = seq[-1]
            return {
                "prediction": "BIG" if last == 1 else "SMALL",
                "confidence": 0.55,
                "regime": "neutral",
                "top_mode": None,
                "strong": False,
            }

        # trend shift check
        if len(seq) >= 6:
            recent3 = seq[-3:]
            prev3 = seq[-6:-3]
            recent_big = sum(recent3)
            prev_big = sum(prev3)

            if recent_big > prev_big + 1:
                regime = "trend_up"
            elif recent_big < prev_big - 1:
                regime = "trend_down"
            else:
                regime = "balanced"
        else:
            regime = "balanced"

        # anti-chase rule: if exact pattern is strong enough, trust it
        # otherwise don't force a prediction
        return {
            "prediction": best["pred"],
            "confidence": clamp(best["confidence"], 0.50, 0.90),
            "regime": regime,
            "top_mode": best,
            "strong": True,
        }

PATTERN_V34 = ExactPatternV34()

# ============================================================
# 12-FEATURE SIZE SCORER
# ============================================================
class SizeScorerV34:
    def __init__(self):
        self.weights = {
            "recent_size": 1.20,
            "run_length": 1.30,
            "last3_big_ratio": 0.90,
            "last5_big_ratio": 0.85,
            "alternation": 0.75,
            "pattern_repeat": 1.00,
            "trend_velocity": 0.90,
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
        r = 1
        for i in range(len(seq)-2, -1, -1):
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
            if seq[i] != seq[i-1]:
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

        features = self.compute_features(seq)
        logit = 0.0
        for name, value in features.items():
            logit += self.weights[name] * value

        prob = self._sigmoid(logit)
        label = "BIG" if prob >= 0.5 else "SMALL"

        # hard gate: if uncertainty is too weak, do not over-predict
        if abs(prob - 0.5) < 0.08:
            if seq[-1] == 1:
                label = "BIG"
                prob = 0.55
            else:
                label = "SMALL"
                prob = 0.45

        return label, clamp(prob, 0.50, 0.94), features

SIZE_SCORER = SizeScorerV34()

# ============================================================
# EXACT NEXT VALUE MATRIX
# ============================================================
class ExactNextNumberMatrix:
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

    def predict(self, seq: List[int], pred_size: str) -> int:
        if not seq:
            return random.choice(family_for_size(pred_size))

        self.update(seq[-2000:])

        last_digit = seq[-1]
        base = self.trans.get(last_digit, {d: 0.1 for d in range(10)})

        target_family = set(family_for_size(pred_size))
        score = {d: 0.0 for d in range(10)}

        for d in range(10):
            score[d] = base.get(d, 0.0) * 0.8
            if d in target_family:
                score[d] += 0.35
            else:
                score[d] *= 0.18

        if pred_size == "SMALL":
            center = {0: 1.12, 1: 1.18, 2: 1.10, 3: 1.06, 4: 1.00}
            for d, w in center.items():
                score[d] *= w
        else:
            center = {5: 1.00, 6: 1.18, 7: 1.12, 8: 1.08, 9: 1.00}
            for d, w in center.items():
                score[d] *= w

        total = sum(score.values())
        if total == 0:
            return random.choice(family_for_size(pred_size))

        for d in score:
            score[d] /= total

        return int(max(score, key=score.get))

NUMBER_MATRIX = ExactNextNumberMatrix()

# ============================================================
# STATE
# ============================================================
class BotState:
    def __init__(self):
        self.history: List[Dict[str, Any]] = []

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
    pattern = PATTERN_V34.detect(seq[-1000:])
    ai_size, ai_prob, ai_features = SIZE_SCORER.score(seq)

    # Strong gate
    if pattern["strong"] and pattern["prediction"] == ai_size:
        final_size = ai_size
        confidence = clamp(ai_prob + pattern["confidence"] * 0.20, 0.50, 0.94)
    elif pattern["strong"] and pattern["prediction"] != ai_size:
        final_size = pattern["prediction"]
        confidence = pattern["confidence"]
    else:
        # safe fallback
        final_size = ai_size
        confidence = clamp(ai_prob, 0.50, 0.75)

    num_seq = [x["num"] for x in history if x.get("num") is not None]
    target_num = NUMBER_MATRIX.predict(num_seq, final_size)

    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)
    stake = "3x" if confidence >= 0.85 else "2x" if confidence >= 0.72 else "1x" if confidence >= 0.60 else "0.5x"

    return {
        "issue": issue,
        "type": "SIZE",
        "size": final_size,
        "num": target_num,
        "raw_conf": int(confidence * 100),
        "logic": f"STRICT PATTERN + AI | regime={pattern['regime']} | strong={pattern['strong']}",
        "stake": stake,
        "details": {
            "pattern": pattern,
            "size_probability": ai_prob,
            "size_features": ai_features
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
    return web.Response(text="FTW AI V34 FIX is Running")

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
