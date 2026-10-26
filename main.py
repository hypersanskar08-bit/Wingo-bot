"""
FTW V28 — SUPER ADVANCED PREDICTOR
==================================
- Multi-model regime-aware ensemble
- Pattern priority with anti-trap consensus logic
- Better number prediction using distribution + sequence memory
- Dynamic confidence and stake
- Telegram-ready output
- Safer and cleaner than V27
"""

import asyncio
import gc
import html
import json
import logging
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
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
    db_path: str = os.environ.get("DB_PATH", "ftw_v28_super_advanced.db")
    cache_file: str = "ftw_v28_cache.json"
    log_dir: str = "logs"
    fetch_every: float = 1.0
    request_timeout: float = 15.0
    bot_name: str = "FTW V28 SUPER ADVANCED"
    min_history: int = 8
    recent_limit: int = 12
    max_history_window: int = 2000

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

    fh = RotatingFileHandler(
        Path(CFG.log_dir) / "ftw_v28_super_advanced.log",
        maxBytes=1_000_000,
        backupCount=3,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("ftw_v28")

# ============================================================
# UTILITIES
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
    c = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            c += 1
        else:
            break
    return c

def sequence_similarity(a: List[int], b: List[int]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    same = sum(1 for i in range(n) if a[i] == b[i])
    return same / n

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
# SQLite STORE
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
# PREDICTION ENGINE
# ============================================================
def sequence_based_pattern(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 12:
        return None

    seq = [x["size"] for x in history]
    total = len(seq)
    current_live = "BIG" if seq[-1] == 1 else "SMALL"

    best = None
    best_score = -1.0

    for window in range(4, 14):
        if total < window + 4:
            continue

        suffix = tuple(seq[-window:])
        positive_weight = 0.0
        negative_weight = 0.0
        total_match_weight = 0.0

        lookback = min(total, 300)
        for i in range(total - window - 1, total - lookback, -1):
            candidate = tuple(seq[i:i + window])
            if candidate != suffix:
                continue

            next_val = seq[i + window]
            recency = max(0.1, 1.0 - ((total - (i + window)) / 100.0))
            total_match_weight += recency
            if next_val == 1:
                positive_weight += recency
            else:
                negative_weight += recency

        if total_match_weight < 2.0:
            continue

        p_big = (positive_weight + 1.0) / (total_match_weight + 2.0)
        p_small = (negative_weight + 1.0) / (total_match_weight + 2.0)

        pred = "BIG" if p_big >= p_small else "SMALL"
        edge = abs(p_big - p_small) * 100.0
        confidence = max(0.55, min(0.96, max(p_big, p_small)))

        # Anti-trap guard: if current run is strong and pattern points opposite
        if trailing_run(seq) >= 2 and pred != current_live and edge < 18.0:
            continue

        score = edge + (window * 2.5) + (total_match_weight * 3.0)
        if score > best_score:
            best_score = score
            best = {
                "size": pred,
                "logic": f"PATTERN {window}-SEQ (edge {edge:.1f}%)",
                "confidence": confidence,
                "stake": "2x" if edge > 25 else "1x" if edge > 15 else "0.5x",
            }

    return best

def momentum_model(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(history) < 6:
        return None

    seq = [x["size"] for x in history]
    current = seq[-1]
    run = trailing_run(seq)
    last3 = seq[-3:]
    last5 = seq[-5:]

    big_count = sum(1 for v in last3 if v == 1)
    small_count = sum(1 for v in last3 if v == 0)

    if run >= 4:
        pred = "BIG" if current == 1 else "SMALL"
        return {
            "size": pred,
            "logic": f"RUN MOMENTUM (run={run})",
            "confidence": 0.82 if run >= 5 else 0.74,
            "stake": "2x" if run >= 5 else "1x",
        }

    # alternation bias
    alt = abs(big_count - small_count)
    if alt == 0 and len(last5) == 5:
        pred = "BIG" if sum(last5) >= 3 else "SMALL"
        return {
            "size": pred,
            "logic": "REGIME BIAS",
            "confidence": 0.68,
            "stake": "1x",
        }

    return None

def voting_consensus(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    seq = [x["size"] for x in history]
    total = len(seq)
    if total < 12:
        return None

    big_vote = 0.0
    small_vote = 0.0

    # check many windows
    for window in range(4, 14):
        if total < window + 4:
            continue
        suffix = tuple(seq[-window:])
        pos, neg, wt = 0.0, 0.0, 0.0

        lookback = min(total, 300)
        for i in range(total - window - 1, total - lookback, -1):
            candidate = tuple(seq[i:i + window])
            if candidate != suffix:
                continue
            next_val = seq[i + window]
            recency = max(0.1, 1.0 - ((total - (i + window)) / 100.0))
            wt += recency
            if next_val == 1:
                pos += recency
            else:
                neg += recency

        if wt < 2.0:
            continue

        p_big = (pos + 1.0) / (wt + 2.0)
        p_small = (neg + 1.0) / (wt + 2.0)

        edge = abs(p_big - p_small) * 100.0
        if p_big >= p_small:
            big_vote += edge
        else:
            small_vote += edge

    if big_vote + small_vote == 0:
        return None

    if big_vote > small_vote:
        final_pred = "BIG"
        final_vote = (big_vote / (big_vote + small_vote)) * 100.0
    else:
        final_pred = "SMALL"
        final_vote = (small_vote / (big_vote + small_vote)) * 100.0

    # anti-trap inversion when consensus is too high
    if final_vote >= 75.0:
        inverted = "SMALL" if final_pred == "BIG" else "BIG"
        return {
            "size": inverted,
            "logic": f"TRAP INVERTED ({final_vote:.1f}% consensus)",
            "confidence": 0.80,
            "stake": "1x",
        }

    if final_vote >= 60.0:
        return {
            "size": final_pred,
            "logic": f"CONSENSUS VOTE ({final_vote:.1f}% consensus)",
            "confidence": 0.72,
            "stake": "1x",
        }

    return None

def smart_fallback(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    seq = [x["size"] for x in history]
    if len(seq) < 3:
        return {"size": "BIG", "logic": "SEED FALLBACK", "confidence": 0.55, "stake": "0.5x"}

    last3 = seq[-3:]
    ones = sum(last3)

    if ones >= 2:
        return {"size": "BIG", "logic": "SMART FALLBACK (recent BIG bias)", "confidence": 0.65, "stake": "0.5x"}
    return {"size": "SMALL", "logic": "SMART FALLBACK (recent SMALL bias)", "confidence": 0.65, "stake": "0.5x"}

def predict_size(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    models = [
        sequence_based_pattern(history),
        momentum_model(history),
        voting_consensus(history),
    ]

    valid = [m for m in models if m is not None]
    if not valid:
        return smart_fallback(history)

    # choose highest confidence model
    best = max(valid, key=lambda x: x["confidence"])
    # if multiple strong models agree, add bonus
    agreement = sum(1 for m in valid if m["size"] == best["size"])
    if agreement >= 2:
        best["confidence"] = clamp(best["confidence"] + 0.07, 0.55, 0.97)
        best["stake"] = "2x" if best["stake"] == "1x" else "2x" if best["stake"] == "0.5x" else "3x"

    return best

def choose_number_for_size(size: str, history: List[Dict[str, Any]], target_issue: Optional[str] = None) -> int:
    valid = family_for_size(size)
    nums = [x["num"] for x in history if x.get("num") is not None]

    # 1) exact number frequency in recent window
    recent = nums[-60:]
    recent_valid = [n for n in recent if n in valid]
    if recent_valid:
        c = Counter(recent_valid)
        best_num, best_count = c.most_common(1)[0]
        if best_count >= 2:
            return best_num

    # 2) repeated sequence continuation
    for window in range(3, 8):
        if len(nums) < window + 2:
            continue
        suffix = tuple(nums[-window:])
        counts = defaultdict(int)
        for i in range(len(nums) - window):
            if tuple(nums[i:i + window]) == suffix:
                counts[nums[i + window]] += 1
        if counts:
            chosen, freq = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
            if chosen in valid and freq >= 1:
                return chosen

    # 3) weighted family pattern by frequency
    family_counts = Counter([n for n in nums if n in valid])
    if family_counts:
        chosen, _ = family_counts.most_common(1)[0]
        return chosen

    # 4) smart random family choice with bias to center numbers
    center_bias = [5, 6, 7, 8, 9] if size == "BIG" else [1, 2, 3, 4, 0]
    return random.choice(center_bias)

def build_prediction(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < CFG.min_history:
        issue = "1"
        size = "BIG" if random.random() >= 0.5 else "SMALL"
        num = choose_number_for_size(size, history, issue)
        return {
            "issue": issue,
            "type": "SIZE",
            "size": size,
            "num": num,
            "raw_conf": 45,
            "logic": "WARMING UP",
            "stake": "0.5x",
            "details": {}
        }

    size_signal = predict_size(history)
    issue = str(max([normalize_issue(x["issue"]) for x in history if normalize_issue(x["issue"]) is not None], default=0) + 1)
    target_size = size_signal["size"]
    target_num = choose_number_for_size(target_size, history, issue)

    return {
        "issue": issue,
        "type": "SIZE",
        "size": target_size,
        "num": target_num,
        "raw_conf": int(size_signal["confidence"] * 100),
        "logic": size_signal["logic"],
        "stake": size_signal["stake"],
        "details": {"model": size_signal["logic"], "confidence": size_signal["confidence"]}
    }

# ============================================================
# HISTORY PARSING
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

    out = []
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
            "size": 1 if num >= 5 else 0,
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
                resolved = STORE.resolve_pending(history_by_issue)

                for res in resolved:
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
    return web.Response(text="FTW V28 SUPER ADVANCED is Running")

async def main():
    CFG.validate()
    STATE.load_cache()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    await app.initialize()
    await app.start()
    bot_info = await app.bot.get_me()
    log.info("Bot started: %s", bot_info.username)

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
