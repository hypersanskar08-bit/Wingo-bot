"""
V900 RHYTHM-MASTER - KING MODE
================================
- Multi-regime ensemble predictor
- Double-loss contrarian rebound
- Smart anti-trap and anti-chop logic
- Strong momentum and reversal detection
- Adaptive staking and number picking
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
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
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
    db_path: str = os.environ.get("DB_PATH", "predictor_v900.db")
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V900 RHYTHM-MASTER"
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
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir) / "v900.log", maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt); root.addHandler(fh)


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
                p.get("raw_conf", 50), p.get("regime", "CHOPPY"), json.dumps(p.get("details", {})),
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
            status = ("JACK 🏆" if p_num == act["num"] else "WIN ✅") if win else "LOSS ❌"

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
# ADVANCED PREDICTOR
# =========================
@dataclass
class Signal:
    name: str
    dir: int
    weight: float
    confidence: int
    rationale: str = ""


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


def has_alternating_pattern(values: List[int], window: int = 10) -> bool:
    if len(values) < window:
        return False
    arr = values[-window:]
    return all(arr[i] != arr[i - 1] for i in range(1, len(arr)))


def has_block_pattern(values: List[int], window: int = 10) -> bool:
    if len(values) < window:
        return False
    arr = values[-window:]
    if len(arr) >= 4:
        for i in range(len(arr) - 3):
            a, b, c, d = arr[i:i+4]
            if a == b and c == d and a != c:
                return True
    return False


def recent_majority(values: List[int], n: int = 5) -> int:
    if not values:
        return 1
    chunk = values[-n:]
    return 1 if sum(chunk) >= (len(chunk) / 2) else 0


def regime_of(sizes: List[int]) -> str:
    if len(sizes) < 8:
        return "COLD"

    last_8 = sizes[-8:]

    if has_alternating_pattern(sizes, 8):
        return "REVERSAL"
    if has_block_pattern(sizes, 10):
        return "BLOCK"
    if trailing_run(sizes, sizes[-1]) >= 3:
        return "TRAP"
    if recent_majority(sizes, 5) == sizes[-1] and len(sizes) >= 10:
        return "TREND"
    if abs(sum(last_8) - (len(last_8) / 2)) <= 1:
        return "CHOPPY"

    return "CYCLE"


def detect_rhythm_signal(sizes: List[int]) -> Optional[Signal]:
    if len(sizes) < 10:
        return None
    last_10 = sizes[-10:]
    if has_alternating_pattern(sizes, 10):
        dir_ = 1 - sizes[-1]
        return Signal(
            name="Rhythm-Alt",
            dir=dir_,
            weight=3.0,
            confidence=86,
            rationale="Alternating pattern suggests reversal against last side."
        )
    if last_10[-1] == last_10[-2] and last_10[-3] != last_10[-2]:
        dir_ = 1 - sizes[-1]
        return Signal(
            name="Block-Break",
            dir=dir_,
            weight=2.5,
            confidence=78,
            rationale="BBSS break pattern suggests a trap shift."
        )
    return None


def detect_trap_signal(sizes: List[int]) -> Optional[Signal]:
    if len(sizes) < 6:
        return None
    last = sizes[-1]
    run = trailing_run(sizes)
    if run >= 3:
        return Signal(
            name="Trap-Break",
            dir=1 - last,
            weight=4.2,
            confidence=92,
            rationale="3+ same-side streak is high-probability trap."
        )
    return None


def detect_trend_signal(sizes: List[int]) -> Optional[Signal]:
    if len(sizes) < 6:
        return None
    last = sizes[-1]
    run = trailing_run(sizes)
    if run >= 5:
        return Signal(
            name="Trend-Force",
            dir=last,
            weight=3.0,
            confidence=80,
            rationale="Strong trend continuation is alive."
        )
    if run in (1, 2):
        return Signal(
            name="Trend-Soft",
            dir=last,
            weight=1.8,
            confidence=66,
            rationale="Low-level continuation pressure."
        )
    return None


def detect_reversal_signal(sizes: List[int]) -> Optional[Signal]:
    if len(sizes) < 8:
        return None
    recent = sizes[-8:]
    if recent.count(1) >= 6 and recent[-1] == 1:
        return Signal(
            name="Reversal-Fade-Big",
            dir=0,
            weight=2.7,
            confidence=82,
            rationale="Extended BIG side is likely exhausted."
        )
    if recent.count(0) >= 6 and recent[-1] == 0:
        return Signal(
            name="Reversal-Fade-Small",
            dir=1,
            weight=2.7,
            confidence=82,
            rationale="Extended SMALL side is likely exhausted."
        )
    return None


def detect_cycle_signal(sizes: List[int]) -> Optional[Signal]:
    if len(sizes) < 12:
        return None
    if has_alternating_pattern(sizes, 12):
        return Signal(
            name="Cycle-Flip",
            dir=1 - sizes[-1],
            weight=2.2,
            confidence=72,
            rationale="Cycle alternation points to opposite-side move."
        )
    return None


def detect_frequency_signal(nums: List[int]) -> Optional[Signal]:
    if len(nums) < 10:
        return None
    recent = nums[-20:]
    big_numbers = [n for n in recent if n >= 5]
    small_numbers = [n for n in recent if n < 5]
    if len(big_numbers) > len(small_numbers):
        return Signal(
            name="Freq-Bias-Big",
            dir=1,
            weight=1.8,
            confidence=66,
            rationale="Recent number distribution prefers BIG side."
        )
    if len(small_numbers) > len(big_numbers):
        return Signal(
            name="Freq-Bias-Small",
            dir=0,
            weight=1.8,
            confidence=66,
            rationale="Recent number distribution prefers SMALL side."
        )
    return None


def detect_loss_rebound_signal(sizes: List[int], loss_streak: int) -> Optional[Signal]:
    if loss_streak == 0:
        return None
    if len(sizes) < 4:
        return None
    last = sizes[-1]

    if loss_streak == 1:
        return Signal(
            name="Rebound-After-Loss",
            dir=1 - last,
            weight=4.8,
            confidence=94,
            rationale="Immediate rebound logic: reverse the last losing side."
        )
    if loss_streak >= 2:
        dominant = recent_majority(sizes, 6)
        return Signal(
            name="Contrarian-Double-Loss",
            dir=1 - dominant,
            weight=5.8,
            confidence=98,
            rationale="Repeated losses suggest the market is likely trapping the previous pattern."
        )
    return None


def weighted_choice(weights: Dict[Any, float]) -> Any:
    total = sum(weights.values())
    if total <= 0:
        return random.choice(list(weights.keys()))
    r = random.random() * total
    acc = 0.0
    for key, val in weights.items():
        acc += val
        if r <= acc:
            return key
    return max(weights, key=weights.get)


def pick_number(pred_dir: int, nums: List[int], sizes: List[int], loss_streak: int) -> int:
    pool = list(range(5, 10)) if pred_dir == 1 else list(range(0, 5))
    if not pool:
        pool = list(range(10))

    recent = nums[-20:]
    freq = Counter(recent)
    weights: Dict[int, float] = {}

    for n in pool:
        w = 1.0

        w += freq.get(n, 0) * 2.5

        if recent:
            nearest = min(recent, key=lambda x: abs(x - n))
            w += max(0.0, 2.0 - abs(nearest - n)) * 1.2

        if pred_dir == 1:
            if n in (6, 7, 8):
                w += 1.5
            if loss_streak >= 2 and n <= 4:
                w *= 0.25
        else:
            if n in (1, 2, 3):
                w += 1.5
            if loss_streak >= 2 and n >= 5:
                w *= 0.25

        if recent and len(recent) >= 6:
            last_6 = recent[-6:]
            if n in last_6 and recent.count(n) >= 3:
                w *= 0.6

        weights[n] = w

    if not weights:
        return random.choice(pool)

    return weighted_choice(weights)


def generate_prediction():
    if len(STATE.history) < CFG.min_history:
        return None

    sizes = [x["size"] for x in STATE.history]
    nums = [x["num"] for x in STATE.history]

    signal_list: List[Signal] = []

    sig = detect_rhythm_signal(sizes)
    if sig:
        signal_list.append(sig)

    sig = detect_trap_signal(sizes)
    if sig:
        signal_list.append(sig)

    sig = detect_trend_signal(sizes)
    if sig:
        signal_list.append(sig)

    sig = detect_reversal_signal(sizes)
    if sig:
        signal_list.append(sig)

    sig = detect_cycle_signal(sizes)
    if sig:
        signal_list.append(sig)

    sig = detect_frequency_signal(nums)
    if sig:
        signal_list.append(sig)

    sig = detect_loss_rebound_signal(sizes, STATE.loss_streak)
    if sig:
        signal_list.append(sig)

    if not signal_list:
        last_3 = sizes[-3:]
        fallback_dir = 1 if sum(last_3) >= 2 else 0
        signal_list.append(
            Signal(
                name="Fallback-Majority",
                dir=fallback_dir,
                weight=1.2,
                confidence=57,
                rationale="No strong regime, using last-three majority."
            )
        )

    big_weight = sum(s.weight for s in signal_list if s.dir == 1)
    small_weight = sum(s.weight for s in signal_list if s.dir == 0)
    total_weight = big_weight + small_weight

    if total_weight <= 0:
        pred_dir = 1 if random.random() >= 0.5 else 0
        p_big = 0.5
    else:
        p_big = big_weight / total_weight
        pred_dir = 1 if p_big >= 0.5 else 0

    stake = "1x"
    if STATE.loss_streak >= 2:
        stake = "0.5x"
    elif STATE.win_streak >= 2:
        stake = "2x"

    mode = "DYNAMIC"
    if STATE.loss_streak == 1:
        mode = "REBOUND"
    elif STATE.loss_streak >= 2:
        mode = "CONTRARIAN"
    elif regime_of(sizes) == "TRAP":
        mode = "ANTI-TRAP"
    elif regime_of(sizes) == "TREND":
        mode = "TREND"
    elif regime_of(sizes) == "REVERSAL":
        mode = "REVERSAL"

    chosen_num = pick_number(pred_dir, nums, sizes, STATE.loss_streak)
    issue = str(int(STATE.history[-1]["issue"]) + 1)

    active_signals = [
        {
            "name": s.name,
            "dir": "BIG" if s.dir == 1 else "SMALL",
            "weight": round(s.weight, 2),
            "confidence": s.confidence,
            "rationale": s.rationale
        }
        for s in signal_list
    ]

    raw_conf = int(50 + abs(p_big - 0.5) * 100)
    conf = max(55, min(99, raw_conf))

    return {
        "issue": issue,
        "size": "BIG" if pred_dir == 1 else "SMALL",
        "num": chosen_num,
        "p_big": round(p_big, 4),
        "confidence": conf,
        "raw_conf": conf,
        "consensus": max(p_big, 1 - p_big),
        "mode": mode,
        "regime": regime_of(sizes),
        "logic": ", ".join(f"{s['name']}({s['dir']})" for s in active_signals),
        "sigs": [
            {"name": s["name"], "dir": pred_dir, "weight": s["weight"], "confidence": s["confidence"]}
            for s in active_signals
        ],
        "stake": stake,
        "details": {
            "agents": {s["name"]: s["dir"] for s in active_signals},
            "regime": regime_of(sizes),
            "loss_streak": STATE.loss_streak,
            "win_streak": STATE.win_streak,
            "signals": active_signals
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
# DATA FETCH
# =========================
async def fetch_data(session):
    try:
        async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
            if r.status != 200:
                return False

            data = await r.json()
            if isinstance(data, dict):
                items = data.get("data", {}).get("list", [])
            else:
                items = data if isinstance(data, list) else []

            if not items:
                return False

            items.sort(key=lambda x: int(x.get("issueNumber", x.get("issue", 0))))
            out = []
            seen = set()

            for item in items:
                try:
                    issue = str(item.get("issueNumber", item.get("issue", "")))
                    if not issue or issue in seen:
                        continue
                    seen.add(issue)

                    num = int(item.get("number", item.get("value", 0)))
                    out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                except Exception:
                    continue

            if out:
                STATE.history = out
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
