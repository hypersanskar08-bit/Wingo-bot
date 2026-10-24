"""
V900 DEEP PATTERN MASTER
=======================
- Uses last 1000 results from API
- Deep pattern memory + historical signal bank
- Alternation / block / streak / trap / reversal / trend detection
- Volatility and frequency scoring
- Rebound logic after losses
- Smart stake sizing
- Number selection based on real pattern frequency
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
from typing import Any, Dict, List, Optional

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
    telegram_token: str = os.environ.get("TG_TOKEN", "8611789455:AAH_GCZP4vLI4M-rF8knnPAartqucT51X7U")
    chat_id_raw: str = os.environ.get("TG_CHAT", "1264164655")
    webhook_url: str = os.environ.get("WEBHOOK_URL", "https://wingo-bot-mcs3.onrender.com")
    source_api_url: str = os.environ.get(
        "API_URL",
        "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",
    )
    db_path: str = os.environ.get("DB_PATH", "predictor_v900_deep.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V900 DEEP PATTERN MASTER"
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
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900_deep.log", maxBytes=1_000_000, backupCount=2)
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
            act_size_str = "BIG" if act["size"] == 1 else "SMALL"
            win = (p_size == act_size_str)

            if win and p_num == act["num"]:
                status = "JACK 🏆"
            elif win:
                status = "WIN ✅"
            else:
                status = "LOSS ❌"

            with self._conn() as c:
                c.execute(
                    "UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                    (status, act["num"], act_size_str, issue)
                )

            resolved.append({
                "issue": issue,
                "size": p_size,
                "num": p_num,
                "actual_num": act["num"],
                "actual_size": act_size_str,
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
# HELPER FUNCTIONS
# =========================
def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def safe_int(v, default=0):
    try:
        return int(v)
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


def majority_dir(values: List[int], window: int = 6) -> int:
    if not values:
        return 1
    chunk = values[-window:]
    big_count = sum(chunk)
    return 1 if big_count >= (len(chunk) / 2) else 0


def alternating_score(sizes: List[int], window: int = 12) -> float:
    if len(sizes) < 2:
        return 0.0
    arr = sizes[-window:]
    changes = 0
    for i in range(1, len(arr)):
        if arr[i] != arr[i - 1]:
            changes += 1
    return changes / max(1, len(arr) - 1)


def block_score(sizes: List[int], window: int = 10) -> bool:
    if len(sizes) < 4:
        return False
    arr = sizes[-window:]
    for i in range(len(arr) - 3):
        a, b, c, d = arr[i:i+4]
        if a == b and c == d and a != c:
            return True
    return False


def recent_big_ratio(sizes: List[int], window: int = 20) -> float:
    if len(sizes) < 1:
        return 0.5
    arr = sizes[-window:]
    return sum(arr) / len(arr)


def volatility_score(sizes: List[int], window: int = 20) -> float:
    if len(sizes) < 2:
        return 0.0
    arr = sizes[-window:]
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    return sum(diffs) / max(1, len(diffs))


# =========================
# DEEP MARKET ANALYSIS
# =========================
def analyze_history(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {
            "side_bias": 0.5,
            "regime": "EMPTY",
            "patterns": [],
            "signal_strength": 0.0,
            "volatility": 0.0,
            "trend_run": 0,
            "last_size": 1,
        }

    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]

    runs = {
        "last_run": trailing_run(sizes),
        "last_big_run": trailing_run([1 if s == 1 else 0 for s in sizes]),
        "last_small_run": trailing_run([1 if s == 0 else 0 for s in sizes]),
    }

    recent_ratio = recent_big_ratio(sizes, 20)
    alt = alternating_score(sizes, 12)
    block = block_score(sizes, 10)
    last = sizes[-1]
    last_dir = 1 if last == 1 else 0

    # find dominant recent number group
    freq = Counter(nums[-200:])
    most_common = freq.most_common(5)
    number_bias = {
        "most_common": most_common[:3],
        "big_group_score": sum(freq[n] for n in range(5, 10)),
        "small_group_score": sum(freq[n] for n in range(0, 5)),
    }

    # side bias / confidence
    side_bias = 0.5
    if len(sizes) >= 10:
        side_bias = recent_big_ratio(sizes, 10)

    # regime detection
    regime = "DYNAMIC"
    if alt >= 0.65:
        regime = "ALTERNATING"
    elif block:
        regime = "BLOCK"
    elif runs["last_run"] >= 3:
        regime = "TRAP"
    elif runs["last_run"] >= 5:
        regime = "TREND"
    elif abs(recent_ratio - 0.5) <= 0.15:
        regime = "CHOPPY"
    else:
        regime = "CYCLE"

    # signal strength
    signal_strength = 0.0
    if alt >= 0.5:
        signal_strength += 0.25
    if block:
        signal_strength += 0.20
    if runs["last_run"] >= 3:
        signal_strength += 0.30
    if abs(recent_ratio - 0.5) > 0.25:
        signal_strength += 0.15
    signal_strength = clamp(signal_strength, 0.0, 1.0)

    # direct pattern features
    pattern_info = {
        "alternating": alt,
        "block_pattern": block,
        "streak": runs["last_run"],
        "recent_big_ratio": recent_ratio,
        "last_side": last_dir,
        "volatility": volatility_score(sizes, 20),
        "number_bias": number_bias,
    }

    return {
        "side_bias": side_bias,
        "regime": regime,
        "patterns": pattern_info,
        "signal_strength": signal_strength,
        "volatility": volatility_score(sizes, 20),
        "trend_run": runs["last_run"],
        "last_size": last,
    }


# =========================
# DEEP HISTORICAL MEMORY SCORING
# =========================
def build_pattern_memory(history: List[Dict[str, Any]], window: int = 12):
    """
    Builds a memory bank of pattern occurrences from the recent 1000 results.
    Score is based on how often a similar pattern ended as BIG or SMALL.
    """
    if len(history) < window + 5:
        return {
            "big_score": 0.5,
            "small_score": 0.5,
            "pattern_count": 0
        }

    sizes = [x["size"] for x in history]
    scores = {"BIG": 0.0, "SMALL": 0.0}
    seen = 0

    for i in range(window, len(sizes) - 1):
        seq = sizes[i-window:i]
        next_size = sizes[i]

        # pattern templates
        last_run = trailing_run(seq)
        alt = alternating_score(seq, min(len(seq), 10))
        block = block_score(seq, min(len(seq), 10))
        ratio = recent_big_ratio(seq, min(len(seq), 20))

        match = False
        if last_run >= 3:
            match = True
        if alt >= 0.6:
            match = True
        if block:
            match = True
        if abs(ratio - 0.5) > 0.2:
            match = True

        if match:
            seen += 1
            if next_size == 1:
                scores["BIG"] += 1.0
            else:
                scores["SMALL"] += 1.0

    total = scores["BIG"] + scores["SMALL"]
    if total <= 0:
        return {"big_score": 0.5, "small_score": 0.5, "pattern_count": 0}
    return {
        "big_score": scores["BIG"] / total,
        "small_score": scores["SMALL"] / total,
        "pattern_count": seen
    }


# =========================
# PREDICTOR
# =========================
def choose_number(pred_dir: int, history: List[Dict[str, Any]], state_loss_streak: int) -> int:
    nums = [x["num"] for x in history]
    recent = nums[-200:]
    freq = Counter(recent)

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    if not pool:
        pool = list(range(10))

    weights = {}
    for n in pool:
        w = 1.0

        # frequency importance
        w += freq.get(n, 0) * 3.0

        # number cluster preference
        if pred_dir == 1:
            if n in (6, 7, 8):
                w += 2.0
            if n in (5, 9):
                w += 1.0
        else:
            if n in (1, 2, 3):
                w += 2.0
            if n in (0, 4):
                w += 1.0

        # avoid repeated stale numbers
        recent_window = recent[-20:]
        if recent_window.count(n) >= 4:
            w *= 0.35

        # after heavy loss streak, avoid repeating obvious trap numbers
        if state_loss_streak >= 2:
            if pred_dir == 1 and n <= 4:
                w *= 0.2
            if pred_dir == 0 and n >= 5:
                w *= 0.2

        weights[n] = w

    if not weights:
        return random.choice(pool)

    return max(weights, key=weights.get)


def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        return None

    history = STATE.history[-1000:]
    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]
    market = analyze_history(history)
    memory = build_pattern_memory(history, window=12)

    # --- base side probability ---
    p_big = market["side_bias"]

    # 1. alternation: reversal bias
    if market["regime"] == "ALTERNATING":
        p_big = 1.0 - sizes[-1]
    # 2. trap pattern: reverse streak
    elif market["regime"] == "TRAP":
        p_big = 1.0 - sizes[-1]
    # 3. trend pattern: continue with momentum
    elif market["regime"] == "TREND":
        p_big = sizes[-1]
    # 4. block pattern: likely break
    elif market["regime"] == "BLOCK":
        p_big = 1.0 - sizes[-1]
    # 5. choppy: use recent ratio more gently
    elif market["regime"] == "CHOPPY":
        p_big = 0.5 + (market["side_bias"] - 0.5) * 0.8

    # 6. memory bank adjustment
    if memory["pattern_count"] > 0:
        if memory["big_score"] > memory["small_score"]:
            p_big = 0.5 + (p_big - 0.5) * 0.7 + (memory["big_score"] - 0.5) * 0.6
        else:
            p_big = 0.5 + (p_big - 0.5) * 0.7 + (memory["small_score"] - 0.5) * 0.6

    # 7. state-driven adaptive adjustment
    if STATE.loss_streak == 1:
        p_big = 1.0 - sizes[-1]
    elif STATE.loss_streak >= 2:
        p_big = 1.0 - majority_dir(sizes, 6)

    p_big = clamp(p_big, 0.05, 0.95)

    pred_dir = 1 if p_big >= 0.5 else 0

    # Risk stake
    stake = "1x"
    if STATE.loss_streak >= 2:
        stake = "0.5x"
    elif STATE.win_streak >= 2:
        stake = "2x"

    # Mode
    if STATE.loss_streak >= 2:
        mode = "CONTRARIAN"
    elif STATE.loss_streak == 1:
        mode = "REBOUND"
    elif market["regime"] == "TRAP":
        mode = "ANTI-TRAP"
    elif market["regime"] == "TREND":
        mode = "TREND"
    elif market["regime"] == "ALTERNATING":
        mode = "REVERSAL"
    else:
        mode = "DYNAMIC"

    # Target number
    num = choose_number(pred_dir, history, STATE.loss_streak)
    issue = str(int(STATE.history[-1]["issue"]) + 1)

    # Build signal metadata
    signal_names = []
    if market["patterns"]["alternating"] >= 0.5:
        signal_names.append("Alternating")
    if market["patterns"]["block_pattern"]:
        signal_names.append("Block")
    if market["trend_run"] >= 3:
        signal_names.append("Trap")
    if market["patterns"]["volatility"] > 0.5:
        signal_names.append("Volatile")
    if memory["pattern_count"] > 0:
        signal_names.append("Pattern-Memory")

    # Confidence
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
        "regime": market["regime"],
        "logic": ", ".join(signal_names) if signal_names else "Baseline",
        "sigs": [{"name": s, "dir": pred_dir} for s in signal_names],
        "stake": stake,
        "details": {
            "market": market,
            "memory": memory,
            "loss_streak": STATE.loss_streak,
            "win_streak": STATE.win_streak,
            "pred_dir": pred_dir,
            "issue": issue
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

            items = payload.get("data", {}).get("list", [])
            if not items:
                return False

            # sort by issue number
            items.sort(key=lambda x: int(x.get("issueNumber", x.get("issue", 0))))

            out = []
            seen = set()
            for item in items:
                try:
                    issue = str(item.get("issueNumber", item.get("issue", "")))
                    if not issue or issue in seen:
                        continue
                    seen.add(issue)

                    num = safe_int(item.get("number", item.get("value", 0)), 0)

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
# FORMAT
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
