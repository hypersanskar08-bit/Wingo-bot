import asyncio
import contextlib
import gc
import html
import logging
import math
import os
import signal
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA"
    chat_id_raw: str = "1264164655"
    admin_ids_raw: str = "1264164655"
    source_api_url: str = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"
    
    db_path: str = os.getenv("DB_PATH", "monitor.db").strip() or "monitor.db"
    log_dir: str = os.getenv("LOG_DIR", "logs").strip() or "logs"
    fetch_every: float = float(os.getenv("FETCH_EVERY", "5"))
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "10"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    bot_name: str = os.getenv("BOT_NAME", "Sequence Monitor").strip() or "Sequence Monitor"
    send_cooldown: float = float(os.getenv("SEND_COOLDOWN", "1.0"))
    analysis_window: int = int(os.getenv("ANALYSIS_WINDOW", "120"))
    binary_short_window: int = int(os.getenv("BINARY_SHORT_WINDOW", "12"))
    binary_long_window: int = int(os.getenv("BINARY_LONG_WINDOW", "48"))
    categorical_short_window: int = int(os.getenv("CAT_SHORT_WINDOW", "15"))
    categorical_long_window: int = int(os.getenv("CAT_LONG_WINDOW", "60"))
    anomaly_z_threshold: float = float(os.getenv("ANOMALY_Z_THRESHOLD", "2.2"))
    alert_cooldown_sec: int = int(os.getenv("ALERT_COOLDOWN_SEC", "90"))
    paused_default: bool = os.getenv("PAUSED_DEFAULT", "0") == "1"

    def parse_chat_id(self):
        if not self.chat_id_raw:
            raise RuntimeError("CHAT_ID missing")
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
            with contextlib.suppress(ValueError):
                ids.add(int(x))
        return ids

    def validate(self):
        if not self.telegram_token:
            raise RuntimeError("TELEGRAM_TOKEN missing")
        if not self.chat_id_raw:
            raise RuntimeError("CHAT_ID missing")
        if not self.source_api_url:
            raise RuntimeError("SOURCE_API_URL missing")
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        self.parse_chat_id()

CFG = Config()

# =========================
# LOGGING
# =========================
def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, CFG.log_level, logging.INFO))
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

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
log = logging.getLogger("sequence_monitor")

# =========================
# HELPERS
# =========================
def h(x: Any) -> str:
    return html.escape(str(x))

def safe_mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

def stddev(xs):
    if len(xs) < 2:
        return 0.0
    return statistics.pstdev(xs)

def zscore(value, xs):
    if len(xs) < 5:
        return 0.0
    s = stddev(xs)
    if s == 0:
        return 0.0
    return (value - safe_mean(xs)) / s

# =========================
# RUNTIME STATE
# =========================
@dataclass
class RuntimeState:
    started_at: float = field(default_factory=time.time)
    paused: bool = CFG.paused_default
    fetch_ok: int = 0
    fetch_fail: int = 0
    send_ok: int = 0
    send_fail: int = 0
    loop_iterations: int = 0
    last_fetch_at: Optional[float] = None
    last_error: str = ""
    last_alert_at_by_key: dict = field(default_factory=dict)
    last_seen_item_id: Optional[str] = None
    last_summary: str = ""
    shutdown_requested: bool = False

    def uptime(self):
        sec = int(time.time() - self.started_at)
        hh = sec // 3600
        mm = (sec % 3600) // 60
        ss = sec % 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

STATE = RuntimeState()

# =========================
# STORAGE (Memory Optimized)
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        # RAM bachane ke liye WAL mode hata kar TRUNCATE use kiya hai
        c.execute("PRAGMA journal_mode=TRUNCATE")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    item_id TEXT PRIMARY KEY,
                    ts REAL,
                    binary_value INTEGER,
                    category_value TEXT,
                    numeric_value REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_key TEXT,
                    message TEXT,
                    created_at REAL
                )
            """)

    def save_observation(self, item_id: str, ts: float, binary_value: int, category_value: str, numeric_value: float):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO observations
                (item_id, ts, binary_value, category_value, numeric_value)
                VALUES (?, ?, ?, ?, ?)
            """, (item_id, ts, binary_value, category_value, numeric_value))

    def latest_item_id(self) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT item_id FROM observations ORDER BY ts DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def recent(self, limit=200):
        with self._conn() as c:
            rows = c.execute("""
                SELECT item_id, ts, binary_value, category_value, numeric_value
                FROM observations
                ORDER BY ts DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

    def counts(self):
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            alerts = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        return total, alerts

    def save_alert(self, key: str, message: str):
        with self._conn() as c:
            c.execute("INSERT INTO alerts(alert_key, message, created_at) VALUES (?, ?, ?)", (key, message, time.time()))

    def recent_alerts(self, limit=10):
        with self._conn() as c:
            return c.execute("SELECT alert_key, message, created_at FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

STORE = Storage(CFG.db_path)

# =========================
# GENERIC ANALYTICS
# =========================
class SequenceAnalytics:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def analyze(self, rows):
        if not rows: return None
        binary_series = [int(r[2]) for r in rows if r[2] is not None]
        cat_series = [str(r[3]) for r in rows if r[3] is not None]
        num_series = [float(r[4]) for r in rows if r[4] is not None]
        return {
            "binary": self._analyze_binary(binary_series),
            "categorical": self._analyze_categorical(cat_series),
            "numeric": self._analyze_numeric(num_series),
        }

    def _analyze_binary(self, xs):
        if len(xs) < max(self.cfg.binary_short_window, self.cfg.binary_long_window):
            return {"enough": False}
        short = xs[-self.cfg.binary_short_window:]
        long = xs[-self.cfg.binary_long_window:]
        short_rate, long_rate = safe_mean(short), safe_mean(long)
        delta = short_rate - long_rate
        streak = 1
        for i in range(len(xs) - 2, -1, -1):
            if xs[i] == xs[-1]: streak += 1
            else: break
        recent_blocks = [safe_mean(xs[max(0, i-10):i]) for i in range(10, len(xs)+1, 5)]
        drift_z = zscore(short_rate, recent_blocks[:-1] if len(recent_blocks) > 1 else recent_blocks)
        regime = "stable"
        if abs(delta) >= 0.20: regime = "strong_shift"
        elif abs(delta) >= 0.10: regime = "moderate_shift"
        return {
            "enough": True, "short_rate": round(short_rate, 3), "long_rate": round(long_rate, 3),
            "delta": round(delta, 3), "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
            "regime": regime, "streak_value": xs[-1], "streak_len": streak, "drift_z": round(drift_z, 3)
        }

    def _analyze_categorical(self, xs):
        if len(xs) < max(self.cfg.categorical_short_window, self.cfg.categorical_long_window):
            return {"enough": False}
        short, long = xs[-self.cfg.categorical_short_window:], xs[-self.cfg.categorical_long_window:]
        short_freq, long_freq = Counter(short), Counter(long)
        s_total, l_total = sum(short_freq.values()) or 1, sum(long_freq.values()) or 1
        drift = {k: round(short_freq.get(k, 0)/s_total - long_freq.get(k, 0)/l_total, 3) for k in set(short_freq)|set(long_freq)}
        dominant_short = max(short_freq, key=short_freq.get) if short_freq else None
        dominant_long = max(long_freq, key=long_freq.get) if long_freq else None
        shift_score = sum(abs(v) for v in drift.values()) / max(1, len(drift))
        regime = "stable"
        if shift_score >= 0.20: regime = "strong_shift"
        elif shift_score >= 0.10: regime = "moderate_shift"
        return {"enough": True, "dominant_short": dominant_short, "dominant_long": dominant_long, "regime": regime}

    def _analyze_numeric(self, xs):
        if len(xs) < 20: return {"enough": False}
        short, long = xs[-10:], xs[-40:] if len(xs) >= 40 else xs
        short_mean, long_mean = safe_mean(short), safe_mean(long)
        delta = short_mean - long_mean
        z = zscore(xs[-1], long)
        anomaly = abs(z) >= self.cfg.anomaly_z_threshold
        return {
            "enough": True, "short_mean": round(short_mean, 3), "long_mean": round(long_mean, 3),
            "delta": round(delta, 3), "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
            "last_value": round(xs[-1], 3), "zscore": round(z, 3), "anomaly": anomaly
        }

ANALYTICS = SequenceAnalytics(CFG)

# =========================
# TELEGRAM ACCESS CONTROL
# =========================
def is_admin(update: Update):
    admins = CFG.admin_ids()
    if not admins: return True
    return update.effective_user.id in admins if update.effective_user else False

def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await reply(update, "⛔ Admin only.")
            return
        return await fn(update, context)
    return wrapper

async def reply(update: Update, text: str, **kwargs):
    if update.message: await update.message.reply_text(text, **kwargs)
    elif update.effective_chat: await update.get_bot().send_message(chat_id=update.effective_chat.id, text=text, **kwargs)

class SendLimiter:
    def __init__(self, cooldown: float):
        self.cooldown, self._last, self._lock = cooldown, 0.0, asyncio.Lock()
    async def wait_turn(self):
        async with self._lock:
            diff = time.time() - self._last
            if diff < self.cooldown: await asyncio.sleep(self.cooldown - diff)
            self._last = time.time()

SEND_LIMITER = SendLimiter(CFG.send_cooldown)

async def safe_send(app: Application, text: str):
    for attempt in range(3):
        try:
            await SEND_LIMITER.wait_turn()
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML", disable_web_page_preview=True)
            STATE.send_ok += 1
            return True
        except Exception as e:
            STATE.send_fail += 1
            STATE.last_error = f"send fail: {e}"
            await asyncio.sleep(1.5 + attempt)
    return False

# =========================
# SOURCE FETCHER (Memory Safe)
# =========================
async def fetch_sequence_data(session: aiohttp.ClientSession):
    async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
        if r.status != 200: raise RuntimeError(f"bad status {r.status}")
        data = await r.json()
        items = data.get("data", {}).get("list", [])
        
        # 🔥 MEMORY FIX: Sirf last 200 items process karo, 1000 nahi
        items = items[-200:] 
        out = []

        for item in items:
            try:
                item_id = str(item.get("issueNumber") or item.get("issue") or "")
                if not item_id: continue
                raw_num = item.get("number", 0)
                numeric_value = int(raw_num)
                binary_value = 1 if numeric_value >= 5 else 0
                category_value = str(raw_num)
                ts = float(item.get("ts") or time.time())

                out.append({
                    "item_id": item_id, "ts": ts, "binary_value": binary_value,
                    "category_value": category_value, "numeric_value": float(numeric_value)
                })
            except Exception:
                continue

        out.sort(key=lambda x: int(x["item_id"]) if x["item_id"].isdigit() else x["item_id"])
        return out

# =========================
# ALERT ENGINE
# =========================
class AlertEngine:
    def __init__(self, cfg: Config): self.cfg = cfg

    def should_emit(self, key: str):
        last = STATE.last_alert_at_by_key.get(key, 0)
        if time.time() - last < self.cfg.alert_cooldown_sec: return False
        STATE.last_alert_at_by_key[key] = time.time()
        return True

    def build_alerts(self, analysis: dict):
        alerts = []
        b, n = analysis.get("binary") or {}, analysis.get("numeric") or {}
        if b.get("enough") and b.get("regime") == "strong_shift":
            alerts.append((f"bin_{b['direction']}", f"🔄 <b>Binary shift</b>\nDir: <b>{h(b['direction'])}</b>\nStreak: <code>{h(b['streak_value'])}×{h(b['streak_len'])}</code>"))
        if n.get("enough") and n.get("anomaly"):
            alerts.append(("num_anom", f"📈 <b>Numeric anomaly</b>\nLast: <code>{h(n['last_value'])}</code>\nz: <code>{h(n['zscore'])}</code>"))
        return alerts

ALERTS = AlertEngine(CFG)

# =========================
# FORMATTERS & COMMANDS
# =========================
def format_analysis(analysis: dict):
    if not analysis: return "No analysis yet."
    b, c, n = analysis.get("binary") or {}, analysis.get("categorical") or {}, analysis.get("numeric") or {}
    lines = ["📊 <b>Sequence Analysis</b>"]
    if b.get("enough"):
        lines += ["", "• <b>Binary</b>", f"short={h(b['short_rate'])}, long={h(b['long_rate'])}, delta={h(b['delta'])}", f"dir={h(b['direction'])}, regime={h(b['regime'])}, streak={h(b['streak_value'])}×{h(b['streak_len'])}"]
    if c.get("enough"):
        lines += ["", "• <b>Categorical</b>", f"dom={h(c['dominant_short'])}/{h(c['dominant_long'])}", f"regime={h(c['regime'])}"]
    if n.get("enough"):
        lines += ["", "• <b>Numeric</b>", f"last={h(n['last_value'])}, z={h(n['zscore'])}, anom={h(n['anomaly'])}"]
    return "\n".join(lines)

@admin_only
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(u, f"🤖 <b>{h(CFG.bot_name)}</b> online\n/health /analyze /recent /alerts /pause /resume /config", parse_mode="HTML")

@admin_only
async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE):
    total, alerts = STORE.counts()
    await reply(u, f"🩺 <b>Health</b>\nUptime: <code>{h(STATE.uptime())}</code>\nFetch OK/Fail: <code>{h(STATE.fetch_ok)}/{h(STATE.fetch_fail)}</code>\nObs: <code>{h(total)}</code> | Alerts: <code>{h(alerts)}</code>\nErr: <code>{h(STATE.last_error or 'none')[:200]}</code>", parse_mode="HTML")

@admin_only
async def cmd_analyze(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent(CFG.analysis_window)
    await reply(u, format_analysis(ANALYTICS.analyze(rows)), parse_mode="HTML")

@admin_only
async def cmd_recent(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent(10)
    if not rows: return await reply(u, "No observations.")
    lines = ["🧾 <b>Recent</b>"] + [f"<code>{h(r[0][-4:])}</code> | bin={h(r[2])} | num={h(r[4])}" for r in rows[-10:]]
    await reply(u, "\n".join(lines), parse_mode="HTML")

@admin_only
async def cmd_alerts(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent_alerts(10)
    if not rows: return await reply(u, "No alerts.")
    lines = ["🚨 <b>Alerts</b>"] + [f"• <code>{h(k)}</code> | {time.strftime('%H:%M:%S', time.localtime(ts))}" for k, m, ts in rows]
    await reply(u, "\n".join(lines), parse_mode="HTML")

@admin_only
async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    STATE.paused = True; await reply(u, "⏸ Paused.")

@admin_only
async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    STATE.paused = False; await reply(u, "▶️ Resumed.")

@admin_only
async def cmd_config(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(u, f"⚙️ <b>Config</b>\nFETCH_EVERY: <code>{h(CFG.fetch_every)}</code>\nWINDOW: <code>{h(CFG.analysis_window)}</code>", parse_mode="HTML")

# =========================
# WORKER LOOP
# =========================
async def process_cycle(app: Application, session: aiohttp.ClientSession):
    items = await fetch_sequence_data(session)
    if not items: return

    new_count = 0
    latest_known = STORE.latest_item_id()

    for item in items:
        item_id = item["item_id"]
        if latest_known is not None:
            try:
                if int(item_id) <= int(latest_known): continue
            except ValueError:
                if str(item_id) <= str(latest_known): continue

        STORE.save_observation(
            item_id=item_id, ts=item["ts"], binary_value=item["binary_value"],
            category_value=item["category_value"], numeric_value=item["numeric_value"]
        )
        STATE.last_seen_item_id = item_id
        new_count += 1

    if new_count <= 0: return

    rows = STORE.recent(CFG.analysis_window)
    analysis = ANALYTICS.analyze(rows)
    STATE.last_summary = format_analysis(analysis)

    if not STATE.paused:
        for key, message in ALERTS.build_alerts(analysis):
            if ALERTS.should_emit(key):
                STORE.save_alert(key, message)
                await safe_send(app, message)

async def worker_loop(app: Application):
    log.info("worker loop started")
    backoff = 2.0

    async with aiohttp.ClientSession() as session:
        while not STATE.shutdown_requested:
            try:
                STATE.loop_iterations += 1
                await process_cycle(app, session)
                STATE.fetch_ok += 1
                STATE.last_fetch_at = time.time()
                backoff = 2.0
                
                # 🔥 MEMORY FIX: Garbage Collector chalao taaki RAM free ho
                gc.collect()
                
                await asyncio.sleep(CFG.fetch_every)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                STATE.fetch_fail += 1
                STATE.last_error = f"{type(e).__name__}: {e}"
                log.exception("worker error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 30.0)

# =========================
# MAIN
# =========================
async def main():
    CFG.validate()
    app = Application.builder().token(CFG.telegram_token).build()

    for cmd, fn in [("start", cmd_start), ("health", cmd_health), ("analyze", cmd_analyze), 
                    ("recent", cmd_recent), ("alerts", cmd_alerts), ("pause", cmd_pause), 
                    ("resume", cmd_resume), ("config", cmd_config)]:
        app.add_handler(CommandHandler(cmd, fn))

    stop_event = asyncio.Event()
    def _stop_signal(*_):
        STATE.shutdown_requested = True
        stop_event.set()

    with contextlib.suppress(NotImplementedError):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _stop_signal)
        loop.add_signal_handler(signal.SIGTERM, _stop_signal)

    async with app:
        await app.start()
        if app.updater is None: raise RuntimeError("Updater unavailable")
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)

        bg_task = asyncio.create_task(worker_loop(app))
        try:
            await stop_event.wait()
        finally:
            bg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await bg_task
            await app.updater.stop()
            await app.stop()

if __name__ == "__main__":
    asyncio.run(main())








