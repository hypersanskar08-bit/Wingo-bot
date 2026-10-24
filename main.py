"""
V900 DEEP MASTER
================
- Last 1000 result memory
- Multi-window pattern tracking
- Similar-pattern memory scoring
- Sequence-based trend and trap detection
- Adaptive probability calibration
- Better number selection
- Dynamic risk sizing
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
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from telegram.error import TimedOut, NetworkError
from telegram.ext import Application

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAH_GCZP4vLI4M-rF8knnPAartqucT51X7U")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    webhook_url: str = os.environ.get("WEBHOOK_URL", "https://wingo-bot-mcs3.onrender.com")
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "v900_deep_master.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V900 DEEP MASTER"
    min_history: int = 50

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
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900_deep_master.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
log = logging.getLogger("v900")


def h(x: Any) -> str:
    return html.escape(str(x))


# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.execute("PRAGMA journal_mode=TRUNCATE")
        return conn

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
                p.get("raw_conf", 50), p.get("regime", "UNKNOWN"), json.dumps(p.get("details", {})),
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
            if issue not in history_dict:
                continue

            act = history_dict[issue]
            act_size = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size)

            if win and p_num == act["num"]:
                status = "JACK 🏆"
            elif win:
                status = "WIN ✅"
            else:
                status = "LOSS ❌"

            with self._conn() as c:
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                    (status, act["num"], act_size, issue)
                )

            resolved.append({
                "issue": issue,
                "size": p_size,
                "num": p_num,
                "actual_num": act["num"],
                "actual_size": act_size,
                "status": status,
                "mode": mode,
                "win": win,
                "raw_conf": raw_conf,
                "regime": regime,
                "details": json.loads(details_json) if details_json else {},
                "sigs": json.loads(sigs_json) if sigs_json else [],
                "p_big": p_big if p_big is not None else 0.5,
                "calibrated_conf": cal_conf or 50,
                "consensus": consensus or 0.5,
                "stake": stake or "1x"
            })

        return resolved

    def recent_outcomes(self, limit: int = 12):
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return rows[::-1]


STORE = Storage(CFG.db_path)


# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.win_streak = 0


STATE = BotState()


# =========================
# HELPERS
# =========================
def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def trailing_run(values: List[int], target: Optional[int] = None) -> int:
    if not values:
        return 0
    last = values[-1] if target is None else target
    run = 1
    for i in range(len(values) - 2, -1, -1):
        if values[i] == last:
            run += 1
        else:
            break
    return run


def ratio_big(values: List[int], n: int = 20) -> float:
    if not values:
        return 0.5
    arr = values[-n:]
    return sum(arr) / len(arr)


def majority_dir(values: List[int], window: int = 6) -> int:
    if not values:
        return 1
    chunk = values[-window:]
    return 1 if sum(chunk) >= (len(chunk) / 2) else 0


def alternating_score(values: List[int], window: int = 12) -> float:
    if len(values) < 2:
        return 0.0
    arr = values[-window:]
    changes = 0
    for i in range(1, len(arr)):
        if arr[i] != arr[i - 1]:
            changes += 1
    return changes / max(1, len(arr) - 1)


def block_pattern(values: List[int], window: int = 10) -> bool:
    if len(values) < 4:
        return False
    arr = values[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i+4]
        if a == b and c == d and a != c:
            return True
    return False


def volatility(values: List[int], window: int = 20) -> float:
    if len(values) < 2:
        return 0.0
    arr = values[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))


# =========================
# DEEP PATTERN ANALYSIS
# =========================
def detect_regime(sizes: List[int]) -> str:
    if len(sizes) < 8:
        return "COLD"

    last_run = trailing_run(sizes)
    alt = alternating_score(sizes, 10)
    block = block_pattern(sizes, 10)
    big_ratio = ratio_big(sizes, 10)

    if alt >= 0.65:
        return "ALTERNATING"
    if block:
        return "BLOCK"
    if last_run >= 3:
        return "TRAP"
    if last_run >= 5:
        return "TREND"
    if abs(big_ratio - 0.5) <= 0.12:
        return "CHOPPY"
    return "CYCLE"


def build_pattern_memory(history: List[Dict[str, Any]], window: int = 12) -> Dict[str, float]:
    """
    Compare current recent pattern against earlier similar patterns.

    The idea:
      - If similar pattern structure appeared before and it led to BIG majority,
        increase BIG probability.
      - Same for SMALL.
    """
    if len(history) < window + 4:
        return {"big_like": 0.5, "small_like": 0.5, "strength": 0.0}

    sizes = [x["size"] for x in history]
    recent = sizes[-window:]
    recent_last_run = trailing_run(recent)

    big_hits = 0
    small_hits = 0
    total_matches = 0

    for start in range(0, len(sizes) - window - 1):
        slice_seq = sizes[start:start + window]
        if len(slice_seq) < window:
            continue

        # pattern similarity:
        sim = 0.0
        # same last run intensity
        if trailing_run(slice_seq) == recent_last_run:
            sim += 0.4
        # same sign distribution
        if abs(ratio_big(slice_seq, window) - ratio_big(recent, window)) <= 0.2:
            sim += 0.3
        # same alternation behavior
        if abs(alternating_score(slice_seq, window) - alternating_score(recent, window)) <= 0.25:
            sim += 0.2
        # same block tendency
        if block_pattern(slice_seq, window) == block_pattern(recent, window):
            sim += 0.1

        if sim >= 0.7:
            total_matches += 1
            outcome = sizes[start + window]
            if outcome == 1:
                big_hits += 1
            else:
                small_hits += 1

    total = big_hits + small_hits
    if total == 0:
        return {"big_like": 0.5, "small_like": 0.5, "strength": 0.0}

    return {
        "big_like": big_hits / total,
        "small_like": small_hits / total,
        "strength": clamp(total_matches / max(1, len(history) - window), 0.0, 1.0),
    }


def number_frequency_weights(history: List[Dict[str, Any]], pred_dir: int) -> Dict[int, float]:
    nums = [x["num"] for x in history]
    recent = nums[-200:]
    freq = Counter(recent)

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    if not pool:
        pool = list(range(10))

    weights = {}
    for n in pool:
        w = 1.0
        w += freq.get(n, 0) * 3.0

        # prefer central cluster near 5-9 for BIG, 0-4 for SMALL
        if pred_dir == 1:
            if n in (6, 7, 8):
                w += 2.5
            if n in (5, 9):
                w += 1.0
        else:
            if n in (1, 2, 3):
                w += 2.5
            if n in (0, 4):
                w += 1.0

        # avoid stale repeated numbers
        if recent.count(n) >= 4:
            w *= 0.35

        # after consecutive losses, avoid obvious trap numbers
        if STATE.loss_streak >= 2:
            if pred_dir == 1 and n <= 4:
                w *= 0.2
            if pred_dir == 0 and n >= 5:
                w *= 0.2

        weights[n] = w

    return weights


def choose_number(pred_dir: int, history: List[Dict[str, Any]], loss_streak: int) -> int:
    weights = number_frequency_weights(history, pred_dir)
    if not weights:
        return random.choice(list(range(10)))

    return max(weights, key=weights.get)


# =========================
# MAIN PREDICTOR
# =========================
def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        return None

    history = STATE.history[-1000:]
    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]

    # --------- regime-based signals ----------
    regime = detect_regime(sizes)
    trend_run = trailing_run(sizes)
    recent_ratio = ratio_big(sizes, 20)
    alt = alternating_score(sizes, 12)
    block = block_pattern(sizes, 10)
    vol = volatility(sizes, 20)

    # ---------- layer 1: trend / reversal ----------
    p_big = recent_ratio

    if regime == "ALTERNATING":
        p_big = 1.0 - sizes[-1]
    elif regime == "TRAP":
        p_big = 1.0 - sizes[-1]
    elif regime == "BLOCK":
        p_big = 1.0 - sizes[-1]
    elif regime == "TREND":
        p_big = sizes[-1]
    elif regime == "CHOPPY":
        p_big = 0.5 + (recent_ratio - 0.5) * 0.8

    # ---------- layer 2: recent memory ----------
    memory = build_pattern_memory(history, window=12)
    if memory["strength"] > 0.0:
        # if similar historical patterns favored BIG
        if memory["big_like"] > memory["small_like"]:
            p_big = 0.5 + (p_big - 0.5) * 0.75 + (memory["big_like"] - 0.5) * 0.75
        else:
            p_big = 0.5 + (p_big - 0.5) * 0.75 + (memory["small_like"] - 0.5) * 0.75

    # ---------- layer 3: state / rebound ----------
    if STATE.loss_streak == 1:
        p_big = 1.0 - sizes[-1]
    elif STATE.loss_streak >= 2:
        p_big = 1.0 - majority_dir(sizes, 6)

    # ---------- layer 4: momentum from streaks ----------
    if trend_run >= 5:
        p_big = sizes[-1]
    elif trend_run >= 3:
        p_big = 1.0 - sizes[-1]  # trap reversal

    # ---------- layer 5: volatility smoothing ----------
    if vol > 0.55:
        p_big = 0.5 + (p_big - 0.5) * 0.75

    p_big = clamp(p_big, 0.05, 0.95)
    pred_dir = 1 if p_big >= 0.5 else 0

    # ---------- risk sizing ----------
    stake = "1x"
    if STATE.loss_streak >= 2:
        stake = "0.5x"
    elif STATE.win_streak >= 2:
        stake = "2x"

    # ---------- mode ----------
    if STATE.loss_streak >= 2:
        mode = "CONTRARIAN"
    elif STATE.loss_streak == 1:
        mode = "REBOUND"
    elif regime == "TRAP":
        mode = "ANTI-TRAP"
    elif regime == "TREND":
        mode = "TREND"
    elif regime == "ALTERNATING":
        mode = "REVERSAL"
    else:
        mode = "DYNAMIC"

    # ---------- choose number ----------
    num = choose_number(pred_dir, history, STATE.loss_streak)
    issue = str(int(STATE.history[-1]["issue"]) + 1)

    # signal names
    sigs = []
    if regime == "ALTERNATING":
        sigs.append("Alternating")
    if regime == "BLOCK":
        sigs.append("Block")
    if regime == "TRAP":
        sigs.append("Trap")
    if vol > 0.5:
        sigs.append("Volatile")
    if memory["strength"] > 0.0:
        sigs.append("Pattern-Memory")
    if not sigs:
        sigs.append("Baseline")

    confidence = int(50 + abs(p_big - 0.5) * 100)
    confidence = max(55, min(99, confidence))

    return {
        "issue": issue,
        "size": "BIG" if pred_dir == 1 else "SMALL",
        "num": num,
        "p_big": round(p_big, 4),
        "confidence": confidence,
        "raw_conf": confidence,
        "consensus": max(p_big, 1 - p_big),
        "mode": mode,
        "regime": regime,
        "logic": ", ".join(sigs),
        "sigs": [{"name": s, "dir": pred_dir} for s in sigs],
        "stake": stake,
        "details": {
            "regime": regime,
            "recent_ratio": recent_ratio,
            "alternating_score": alt,
            "trend_run": trend_run,
            "volatility": vol,
            "memory": memory,
            "loss_streak": STATE.loss_streak,
            "win_streak": STATE.win_streak
        }
    }


def learn_from_resolution(res: dict):
    if res["win"]:
        STATE.loss_streak = 0
        STATE.win_streak += 1
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0


# =========================
# API FETCH
# =========================
async def fetch_data(session):
    try:
        async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
            if r.status != 200:
                return False

            payload = await r.json()
            if not isinstance(payload, dict):
                return False

            data_list = payload.get("data", {}).get("list", [])
            if not data_list:
                return False

            data_list.sort(key=lambda x: int(x.get("issueNumber", x.get("issue", 0))))

            out = []
            seen = set()
            for item in data_list:
                try:
                    issue = str(item.get("issueNumber", item.get("issue", "")))
                    if not issue or issue in seen:
                        continue
                    seen.add(issue)

                    raw_num = item.get("number", item.get("value", 0))
                    num = safe_int(raw_num, 0)
                    if num < 0 or num > 9:
                        continue

                    out.append({
                        "issue": issue,
                        "num": num,
                        "size": 1 if num >= 5 else 0
                    })
                except Exception:
                    continue

            if out:
                STATE.history = out[-1000:]
                return True
            return False

    except Exception as e:
        log.warning(f"Fetch failed: {e}")
        return False


# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = "".join(
        f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"
        for r in recent_rows[-12:]
    )
    sig_text = "\n".join(f"  🕵️ {h(s['name'])}" for s in p["sigs"])
    stake_emoji = {"2x": "🔥 2x", "1x": "🟡 1x", "0.5x": "🛡️ 0.5x"}.get(p['stake'], "1x")

    return f"""
📌 <b>Period</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

🧠 <b>Mode</b>: <b>{h(p['mode'])}</b>
🎲 <b>P(BIG)</b>: <b>{p['p_big']*100:.1f}%</b>
💰 <b>Risk Stake</b>: <b>{stake_emoji}</b>

🔎 <b>Active Agents</b>:
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


# =========================
# TELEGRAM SEND
# =========================
async def send_msg(app, text, retries=3):
    for _ in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return True
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Send error: {e}")
            break
    return False


# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    last_processed_issue = None
    while True:
        try:
            if await fetch_data(session):
                hist_dict = {x["issue"]: x for x in STATE.history}
                resolved = STORE.resolve_pending(hist_dict)

                for res in resolved:
                    learn_from_resolution(res)
                    await send_msg(app, format_result(res))

                latest_issue = STATE.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)

                if latest_issue != last_processed_issue:
                    if not STORE.prediction_exists(next_issue):
                        p = generate_prediction()
                        if p:
                            STORE.save_prediction(p)
                            await send_msg(app, format_prediction(p, STORE.recent_outcomes(12)))
                            last_processed_issue = latest_issue

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except Exception as e:
            log.exception(f"Loop error: {e}")
            await asyncio.sleep(2)


async def handle_ping(request):
    return web.Response(text="V900 is running!")


# =========================
# MAIN
# =========================
async def main():
    CFG.validate()

    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()

    async with app:
        await app.start()
        port = int(os.environ.get("PORT", 8080))

        if CFG.webhook_url:
            webhook_path = f"/webhook/{CFG.telegram_token}"
            await app.bot.set_webhook(url=f"{CFG.webhook_url}{webhook_path}", drop_pending_updates=True)
            web_app = web.Application()
            web_app.router.add_get("/", handle_ping)
            web_app.router.add_post(webhook_path, app.update_queue.put)
            runner = web.AppRunner(web_app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
        else:
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.updater.start_polling(drop_pending_updates=True)

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
