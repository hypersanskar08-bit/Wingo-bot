"""
V900 SEQUENCE CALIBRATION MASTER
================================
- Uses last 1000 API results
- Sequence fingerprint memory
- Similar-pattern historical scoring
- Signal calibration memory
- Anti-trap / trend / reversal detection
- Dynamic risk control
- Better number selection from same-pattern outcomes
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
    db_path: str = os.environ.get("DB_PATH", "v900_seq_calibrated.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V900 SEQ CALIBRATION MASTER"
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
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900_seq_calibration.log", maxBytes=1_000_000, backupCount=2)
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS signal_weights (
                    name TEXT PRIMARY KEY,
                    weight REAL DEFAULT 1.0,
                    hits INTEGER DEFAULT 0,
                    misses INTEGER DEFAULT 0,
                    updated_at REAL DEFAULT 0
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

    def get_signal_weight(self, name: str) -> float:
        with self._conn() as c:
            row = c.execute("SELECT weight FROM signal_weights WHERE name=?", (name,)).fetchone()
            return float(row[0]) if row else 1.0

    def set_signal_weight(self, name: str, value: float, hit: int = 0, miss: int = 0):
        with self._conn() as c:
            existing = c.execute("SELECT hits, misses FROM signal_weights WHERE name=?", (name,)).fetchone()
            if existing:
                hits, misses = existing
                c.execute(
                    "UPDATE signal_weights SET weight=?, hits=?, misses=?, updated_at=? WHERE name=?",
                    (value, hits + hit, misses + miss, time.time(), name)
                )
            else:
                c.execute(
                    "INSERT INTO signal_weights (name, weight, hits, misses, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (name, value, hit, miss, time.time())
                )

    def update_signal_calibration(self, signal_name: str, outcome_was_correct: bool):
        current = self.get_signal_weight(signal_name)
        if outcome_was_correct:
            new_value = clamp(current * 1.12, 0.4, 4.5)
            self.set_signal_weight(signal_name, new_value, hit=1, miss=0)
        else:
            new_value = clamp(current * 0.80, 0.2, 4.5)
            self.set_signal_weight(signal_name, new_value, hit=0, miss=1)


STORE = Storage(CFG.db_path)


# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.win_streak = 0
        self.signal_weights = {}


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


def ratio_big(values: List[int], window: int = 20) -> float:
    if not values:
        return 0.5
    arr = values[-window:]
    return sum(arr) / len(arr)


def majority_dir(values: List[int], window: int = 6) -> int:
    if not values:
        return 1
    arr = values[-window:]
    return 1 if sum(arr) >= (len(arr) / 2) else 0


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
# SEQUENCE FINGERPRINT
# =========================
def sequence_fingerprint(sizes: List[int], windows: Optional[List[int]] = None) -> List[Tuple[int, Tuple[int, ...]]]:
    if windows is None:
        windows = [3, 5, 8, 12]
    out = []
    for w in windows:
        if len(sizes) >= w:
            chunk = tuple(sizes[-w:])
            out.append((w, chunk))
    return out


def similar_pattern_memory(history: List[Dict[str, Any]], current_sizes: List[int]) -> Dict[str, float]:
    """
    Finds how often similar sequence fingerprints historically ended as BIG or SMALL.
    """
    if len(history) < 10:
        return {"big_like": 0.5, "small_like": 0.5, "strength": 0.0}

    cur_fp = sequence_fingerprint(current_sizes)

    big_hits = 0
    small_hits = 0
    total = 0

    for idx in range(len(history) - 1):
        s = [x["size"] for x in history[max(0, idx - 60):idx + 1]]
        if len(s) < 5:
            continue

        fp = sequence_fingerprint(s)
        if not fp:
            continue

        score = 0.0
        for w, chunk in fp:
            for cw, cchunk in cur_fp:
                if w == cw and chunk == cchunk:
                    score += 1.0

        if score >= 1.0:
            total += 1
            next_size = history[idx + 1].get("size", 0)
            if next_size == 1:
                big_hits += 1
            else:
                small_hits += 1

    if total == 0:
        return {"big_like": 0.5, "small_like": 0.5, "strength": 0.0}

    return {
        "big_like": big_hits / total,
        "small_like": small_hits / total,
        "strength": clamp(total / max(1, len(history)), 0.0, 1.0),
    }


# =========================
# REGIME DETECTION
# =========================
def detect_regime(sizes: List[int]) -> str:
    if len(sizes) < 8:
        return "COLD"

    run = trailing_run(sizes)
    alt = alternating_score(sizes, 10)
    block = block_pattern(sizes, 10)
    ratio = ratio_big(sizes, 10)

    if alt >= 0.65:
        return "ALTERNATING"
    if block:
        return "BLOCK"
    if run >= 3:
        return "TRAP"
    if run >= 5:
        return "TREND"
    if abs(ratio - 0.5) <= 0.12:
        return "CHOPPY"
    return "CYCLE"


# =========================
# SIGNAL BUILDING
# =========================
def candidate_signals(history: List[Dict[str, Any]]) -> List[Tuple[str, int, float, str]]:
    if not history:
        return []

    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]
    regime = detect_regime(sizes)
    trend_run = trailing_run(sizes)
    alt = alternating_score(sizes, 12)
    block = block_pattern(sizes, 10)
    recent_ratio = ratio_big(sizes, 20)
    vol = volatility(sizes, 20)

    signals: List[Tuple[str, int, float, str]] = []

    # 1. trap
    if trend_run >= 3:
        signals.append(("Trap", 1 - sizes[-1], 1.6, "Streak is likely a trap and should reverse."))

    # 2. trend continuation
    if trend_run >= 5:
        signals.append(("Trend", sizes[-1], 1.4, "Large momentum run suggests continuation."))

    # 3. alternating / reversal
    if alt >= 0.6:
        signals.append(("Alternating", 1 - sizes[-1], 1.5, "Alternation suggests a flip against last side."))

    # 4. block break
    if block:
        signals.append(("Block", 1 - sizes[-1], 1.4, "Block pattern often breaks into reversal."))

    # 5. dominant-side bias
    if abs(recent_ratio - 0.5) > 0.2:
        signals.append(("Bias", 1 if recent_ratio >= 0.5 else 0, 1.1, "Strong side bias is in market."))

    # 6. volatility
    if vol > 0.5:
        signals.append(("Volatile", 1 - sizes[-1], 0.9, "Volatility suggests unpredictable reversal."))

    # 7. pattern memory
    pattern_mem = similar_pattern_memory(history, sizes)
    if pattern_mem["strength"] > 0.0:
        d = 1 if pattern_mem["big_like"] >= pattern_mem["small_like"] else 0
        signals.append(("Pattern-Memory", d, 1.8, "Similar historical sequence points to this side."))

    return signals


# =========================
# CALIBRATION
# =========================
def get_weighted_signal(name: str) -> float:
    return STORE.get_signal_weight(name)


def apply_signal_calibration(name: str, was_correct: bool):
    STORE.update_signal_calibration(name, was_correct)


# =========================
# NUMBER SELECTION FROM SAME-PATTERN HISTORICAL OUTCOMES
# =========================
def choose_number(pred_dir: int, history: List[Dict[str, Any]], state_loss_streak: int) -> int:
    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]
    recent_nums = nums[-200:]
    freq = Counter(recent_nums)

    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    if not pool:
        pool = list(range(10))

    # Search similar recent patterns and see which numbers followed them
    score_map: Dict[int, float] = defaultdict(float)

    for idx in range(1, len(history) - 1):
        seg = [x["size"] for x in history[max(0, idx - 12):idx + 1]]
        if len(seg) < 6:
            continue

        cur_fp = sequence_fingerprint(seg)
        current_fp = sequence_fingerprint(sizes[-12:])
        if not cur_fp or not current_fp:
            continue

        match = False
        for a in cur_fp:
            if a in current_fp:
                match = True
                break

        if match:
            num = history[idx]["num"]
            dir_out = 1 if history[idx]["size"] == 1 else 0
            # reward numbers following similar patterns
            if dir_out == pred_dir:
                score_map[num] += 3.0
            else:
                score_map[num] -= 1.5

    for n in pool:
        score_map[n] += freq.get(n, 0) * 2.5

        if pred_dir == 1:
            if n in (6, 7, 8):
                score_map[n] += 2.0
            if n in (5, 9):
                score_map[n] += 1.0
        else:
            if n in (1, 2, 3):
                score_map[n] += 2.0
            if n in (0, 4):
                score_map[n] += 1.0

        if state_loss_streak >= 2:
            if pred_dir == 1 and n <= 4:
                score_map[n] *= 0.2
            if pred_dir == 0 and n >= 5:
                score_map[n] *= 0.2

        if recent_nums.count(n) >= 4:
            score_map[n] *= 0.25

    if not score_map:
        return random.choice(pool)

    best_num = max(score_map, key=score_map.get)
    return best_num


# =========================
# PREDICTION GENERATION
# =========================
def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        return None

    history = STATE.history[-1000:]
    sizes = [x["size"] for x in history]
    nums = [x["num"] for x in history]
    regime = detect_regime(sizes)

    signals = candidate_signals(history)

    # Weighted vote
    total_weight = 0.0
    big_weight = 0.0
    small_weight = 0.0

    for name, dir_val, raw_weight, reason in signals:
        weight = get_weighted_signal(name) * raw_weight
        if dir_val == 1:
            big_weight += weight
        else:
            small_weight += weight
        total_weight += weight

    if total_weight <= 0:
        # fallback from recent ratio
        p_big = ratio_big(sizes, 15)
    else:
        p_big = big_weight / total_weight

    # State-driven adjustments
    if STATE.loss_streak == 1:
        p_big = 1.0 - sizes[-1]
    elif STATE.loss_streak >= 2:
        p_big = 1.0 - majority_dir(sizes, 6)

    # strong trap override
    if trailing_run(sizes) >= 3:
        p_big = 1.0 - sizes[-1]

    # momentum override
    if trailing_run(sizes) >= 5:
        p_big = sizes[-1]

    p_big = clamp(p_big, 0.05, 0.95)
    pred_dir = 1 if p_big >= 0.5 else 0

    # stake logic
    stake = "1x"
    if STATE.loss_streak >= 2:
        stake = "0.5x"
    elif STATE.win_streak >= 2:
        stake = "2x"

    # mode
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

    num = choose_number(pred_dir, history, STATE.loss_streak)
    issue = str(int(STATE.history[-1]["issue"]) + 1)

    signal_entries = []
    for name, dir_val, raw_weight, reason in signals:
        signal_entries.append({
            "name": name,
            "dir": "BIG" if dir_val == 1 else "SMALL",
            "weight": round(get_weighted_signal(name) * raw_weight, 3),
            "reason": reason
        })

    if not signal_entries:
        signal_entries.append({"name": "Baseline", "dir": "BIG" if pred_dir == 1 else "SMALL", "weight": 1.0, "reason": "Fallback value"})

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
        "logic": ", ".join(s["name"] for s in signal_entries),
        "sigs": [{"name": s["name"], "dir": pred_dir} for s in signal_entries],
        "stake": stake,
        "details": {
            "signals": signal_entries,
            "history_length": len(history),
            "loss_streak": STATE.loss_streak,
            "win_streak": STATE.win_streak,
            "regime": regime
        }
    }


def learn_from_resolution(res: dict):
    global STATE
    if res["win"]:
        STATE.loss_streak = 0
        STATE.win_streak += 1
    else:
        STATE.loss_streak += 1
        STATE.win_streak = 0

    # Update signal calibration using actual result
    for sig in res.get("sigs", []):
        name = sig.get("name")
        if name:
            if res["win"]:
                apply_signal_calibration(name, True)
            else:
                apply_signal_calibration(name, False)

    # initialize some default weights if missing
    for name in ["Trap", "Trend", "Alternating", "Block", "Bias", "Volatile", "Pattern-Memory"]:
        STORE.get_signal_weight(name)  # creates nothing, but not a mutation
        # no-op if absent; DB insertion is handled inside set_signal_weight on update
        # we do not auto-init here to avoid extra writes, but weights will be set on first update


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
