import asyncio
import contextlib
import html
import json
import logging
import math
import os
import signal
import sqlite3
import statistics
import time
from collections import Counter, defaultdict, deque
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
    telegram_token: str = "8611789455:AAFexWc48dhJKgcUK-hkCpRQowbGTm_Bwx0"
    chat_id_raw: str = "1264164655"
    admin_ids_raw: str = "1264164655"
    source_api_url: str = os.getenv("https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000", "").strip()
    

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


def now_ts() -> float:
    return time.time()


def sigmoid(x: float) -> float:
    try:
        return 1 / (1 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


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
# STORAGE
# =========================

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
                CREATE TABLE IF NOT EXISTS observations (
                    item_id TEXT PRIMARY KEY,
                    ts REAL,
                    binary_value INTEGER,
                    category_value TEXT,
                    numeric_value REAL,
                    raw_json TEXT
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

    def save_observation(self, item_id: str, ts: float, binary_value: int, category_value: str, numeric_value: float, raw_json: dict):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO observations
                (item_id, ts, binary_value, category_value, numeric_value, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (item_id, ts, binary_value, category_value, numeric_value, json.dumps(raw_json, ensure_ascii=False)))

    def latest_item_id(self) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT item_id FROM observations ORDER BY ts DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def recent(self, limit=200):
        with self._conn() as c:
            rows = c.execute("""
                SELECT item_id, ts, binary_value, category_value, numeric_value, raw_json
                FROM observations
                ORDER BY ts DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return rows[::-1]

    def counts(self):
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            alerts = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        return total, alerts

    def save_alert(self, key: str, message: str):
        with self._conn() as c:
            c.execute("""
                INSERT INTO alerts(alert_key, message, created_at)
                VALUES (?, ?, ?)
            """, (key, message, time.time()))

    def recent_alerts(self, limit=10):
        with self._conn() as c:
            rows = c.execute("""
                SELECT alert_key, message, created_at
                FROM alerts
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows


STORE = Storage(CFG.db_path)


# =========================
# GENERIC ANALYTICS
# =========================

class SequenceAnalytics:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def analyze(self, rows):
        if not rows:
            return None

        binary_series = [int(r[2]) for r in rows if r[2] is not None]
        cat_series = [str(r[3]) for r in rows if r[3] is not None]
        num_series = [float(r[4]) for r in rows if r[4] is not None]

        result = {
            "binary": self._analyze_binary(binary_series),
            "categorical": self._analyze_categorical(cat_series),
            "numeric": self._analyze_numeric(num_series),
        }
        return result

    def _analyze_binary(self, xs):
        if len(xs) < max(self.cfg.binary_short_window, self.cfg.binary_long_window):
            return {"enough": False}

        short = xs[-self.cfg.binary_short_window:]
        long = xs[-self.cfg.binary_long_window:]

        short_rate = safe_mean(short)
        long_rate = safe_mean(long)
        delta = short_rate - long_rate

        streak = 1
        for i in range(len(xs) - 2, -1, -1):
            if xs[i] == xs[-1]:
                streak += 1
            else:
                break

        recent_blocks = [safe_mean(xs[max(0, i-10):i]) for i in range(10, len(xs)+1, 5)]
        drift_z = zscore(short_rate, recent_blocks[:-1] if len(recent_blocks) > 1 else recent_blocks)

        transition = self._transition_matrix(xs, order=1, states=[0, 1])

        regime = "stable"
        if abs(delta) >= 0.20:
            regime = "strong_shift"
        elif abs(delta) >= 0.10:
            regime = "moderate_shift"

        direction = "up" if delta > 0 else "down" if delta < 0 else "flat"

        return {
            "enough": True,
            "short_rate": round(short_rate, 3),
            "long_rate": round(long_rate, 3),
            "delta": round(delta, 3),
            "direction": direction,
            "regime": regime,
            "streak_value": xs[-1],
            "streak_len": streak,
            "drift_z": round(drift_z, 3),
            "transition": transition,
        }

    def _analyze_categorical(self, xs):
        if len(xs) < max(self.cfg.categorical_short_window, self.cfg.categorical_long_window):
            return {"enough": False}

        short = xs[-self.cfg.categorical_short_window:]
        long = xs[-self.cfg.categorical_long_window:]

        short_freq = self._freq(short)
        long_freq = self._freq(long)

        drift = {}
        keys = sorted(set(short_freq) | set(long_freq))
        for k in keys:
            drift[k] = round(short_freq.get(k, 0.0) - long_freq.get(k, 0.0), 3)

        dominant_short = max(short_freq.items(), key=lambda kv: kv[1])[0] if short_freq else None
        dominant_long = max(long_freq.items(), key=lambda kv: kv[1])[0] if long_freq else None

        entropy_short = self._entropy(list(short_freq.values()))
        entropy_long = self._entropy(list(long_freq.values()))

        transitions = self._transition_matrix(xs, order=1, states=sorted(set(xs)))

        shift_score = sum(abs(v) for v in drift.values()) / max(1, len(drift))
        regime = "stable"
        if shift_score >= 0.20:
            regime = "strong_shift"
        elif shift_score >= 0.10:
            regime = "moderate_shift"

        patterns = self._top_repeated_patterns(xs, k_values=(2, 3, 4), topn=5)

        return {
            "enough": True,
            "dominant_short": dominant_short,
            "dominant_long": dominant_long,
            "drift": drift,
            "entropy_short": round(entropy_short, 3),
            "entropy_long": round(entropy_long, 3),
            "regime": regime,
            "transition": transitions,
            "patterns": patterns,
        }

    def _analyze_numeric(self, xs):
        if len(xs) < 20:
            return {"enough": False}

        short = xs[-10:]
        long = xs[-40:] if len(xs) >= 40 else xs

        short_mean = safe_mean(short)
        long_mean = safe_mean(long)
        delta = short_mean - long_mean
        z = zscore(xs[-1], long)

        direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        anomaly = abs(z) >= self.cfg.anomaly_z_threshold

        slope = self._linear_slope(long[-20:] if len(long) >= 20 else long)
        volatility = stddev(long)

        return {
            "enough": True,
            "short_mean": round(short_mean, 3),
            "long_mean": round(long_mean, 3),
            "delta": round(delta, 3),
            "direction": direction,
            "last_value": round(xs[-1], 3),
            "zscore": round(z, 3),
            "anomaly": anomaly,
            "slope": round(slope, 4),
            "volatility": round(volatility, 4),
        }

    def _freq(self, xs):
        c = Counter(xs)
        total = sum(c.values()) or 1
        return {k: v / total for k, v in c.items()}

    def _entropy(self, probs):
        out = 0.0
        for p in probs:
            if p > 0:
                out -= p * math.log(p, 2)
        return out

    def _linear_slope(self, xs):
        n = len(xs)
        if n < 2:
            return 0.0
        mean_x = (n - 1) / 2
        mean_y = safe_mean(xs)
        num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(xs))
        den = sum((i - mean_x) ** 2 for i in range(n)) or 1.0
        return num / den

    def _transition_matrix(self, xs, order=1, states=None):
        if len(xs) <= order:
            return {}

        trans = defaultdict(Counter)
        for i in range(len(xs) - order):
            key = tuple(xs[i:i+order])
            nxt = xs[i+order]
            trans[key][nxt] += 1

        matrix = {}
        for key, row in trans.items():
            total = sum(row.values()) or 1
            matrix[str(key)] = {str(k): round(v / total, 3) for k, v in row.items()}
        return matrix

    def _top_repeated_patterns(self, xs, k_values=(2, 3), topn=5):
        found = []
        for k in k_values:
            if len(xs) < k:
                continue
            c = Counter(tuple(xs[i:i+k]) for i in range(len(xs) - k + 1))
            for pat, cnt in c.most_common(topn):
                if cnt >= 2:
                    found.append({"k": k, "pattern": list(pat), "count": cnt})
        found.sort(key=lambda x: (-x["count"], -x["k"]))
        return found[:topn]


ANALYTICS = SequenceAnalytics(CFG)


# =========================
# TELEGRAM ACCESS CONTROL
# =========================

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
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait_turn(self):
        async with self._lock:
            diff = time.time() - self._last
            if diff < self.cooldown:
                await asyncio.sleep(self.cooldown - diff)
            self._last = time.time()


SEND_LIMITER = SendLimiter(CFG.send_cooldown)


async def safe_send(app: Application, text: str):
    for attempt in range(3):
        try:
            await SEND_LIMITER.wait_turn()
            await app.bot.send_message(
                chat_id=CFG.parse_chat_id(),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            STATE.send_ok += 1
            return True
        except Exception as e:
            STATE.send_fail += 1
            STATE.last_error = f"send fail: {e}"
            await asyncio.sleep(1.5 + attempt)
    return False


# =========================
# SOURCE FETCHER
# =========================
# Map your external API into generic observation fields:
# - item_id: unique id
# - ts: unix timestamp
# - binary_value: 0/1
# - category_value: string label
# - numeric_value: float/int
#
# Replace parsing below as per your API response format.

async def fetch_sequence_data(session: aiohttp.ClientSession):
    async with session.get(
        CFG.source_api_url,
        timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"bad status {r.status}")

        data = await r.json()

        # Example generic parser:
        # expected data: {"data": {"list": [{"id": "...", "binary": 1, "category": "A", "value": 7, "ts": 1234567890}, ...]}}
        items = data.get("data", {}).get("list", [])
        out = []

        for item in items:
            try:
                item_id = str(item.get("id") or item.get("issueNumber") or item.get("issue") or "")
                if not item_id:
                    continue

                raw_num = item.get("value", item.get("number", 0))
                numeric_value = float(raw_num)

                raw_bin = item.get("binary")
                if raw_bin is None:
                    raw_bin = 1 if numeric_value >= 5 else 0
                binary_value = int(raw_bin)

                category_value = str(item.get("category") or item.get("label") or raw_num)

                ts = float(item.get("ts") or time.time())

                out.append({
                    "item_id": item_id,
                    "ts": ts,
                    "binary_value": binary_value,
                    "category_value": category_value,
                    "numeric_value": numeric_value,
                    "raw": item,
                })
            except Exception:
                continue

        out.sort(key=lambda x: str(x["item_id"]))
        return out


# =========================
# ALERT ENGINE
# =========================

class AlertEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def should_emit(self, key: str):
        last = STATE.last_alert_at_by_key.get(key, 0)
        if time.time() - last < self.cfg.alert_cooldown_sec:
            return False
        STATE.last_alert_at_by_key[key] = time.time()
        return True

    def build_alerts(self, analysis: dict):
        alerts = []

        b = analysis.get("binary") or {}
        c = analysis.get("categorical") or {}
        n = analysis.get("numeric") or {}

        if b.get("enough"):
            if b.get("regime") == "strong_shift":
                alerts.append((
                    f"binary_shift_{b.get('direction')}",
                    f"🔄 <b>Binary shift</b>\n"
                    f"Direction: <b>{h(b.get('direction'))}</b>\n"
                    f"Short rate: <code>{h(b.get('short_rate'))}</code>\n"
                    f"Long rate: <code>{h(b.get('long_rate'))}</code>\n"
                    f"Delta: <code>{h(b.get('delta'))}</code>\n"
                    f"Streak: <code>{h(b.get('streak_value'))} × {h(b.get('streak_len'))}</code>"
                ))
            if abs(float(b.get("drift_z", 0))) >= CFG.anomaly_z_threshold:
                alerts.append((
                    "binary_drift_anomaly",
                    f"⚠️ <b>Binary drift anomaly</b>\n"
                    f"z-score: <code>{h(b.get('drift_z'))}</code>"
                ))

        if c.get("enough"):
            if c.get("regime") == "strong_shift":
                alerts.append((
                    "categorical_shift",
                    f"🧭 <b>Categorical regime shift</b>\n"
                    f"Short dominant: <code>{h(c.get('dominant_short'))}</code>\n"
                    f"Long dominant: <code>{h(c.get('dominant_long'))}</code>\n"
                    f"Entropy short/long: <code>{h(c.get('entropy_short'))}/{h(c.get('entropy_long'))}</code>"
                ))

        if n.get("enough"):
            if n.get("anomaly"):
                alerts.append((
                    "numeric_anomaly",
                    f"📈 <b>Numeric anomaly</b>\n"
                    f"Last: <code>{h(n.get('last_value'))}</code>\n"
                    f"z-score: <code>{h(n.get('zscore'))}</code>\n"
                    f"Direction: <b>{h(n.get('direction'))}</b>"
                ))
            if abs(float(n.get("slope", 0))) >= 0.2:
                alerts.append((
                    f"numeric_slope_{'up' if n.get('slope',0)>0 else 'down'}",
                    f"📉 <b>Numeric trend shift</b>\n"
                    f"Slope: <code>{h(n.get('slope'))}</code>\n"
                    f"Short mean: <code>{h(n.get('short_mean'))}</code>\n"
                    f"Long mean: <code>{h(n.get('long_mean'))}</code>"
                ))

        return alerts


ALERTS = AlertEngine(CFG)


# =========================
# FORMATTERS
# =========================

def format_analysis(analysis: dict):
    if not analysis:
        return "No analysis yet."

    b = analysis.get("binary") or {}
    c = analysis.get("categorical") or {}
    n = analysis.get("numeric") or {}

    lines = ["📊 <b>Sequence Analysis</b>"]

    if b.get("enough"):
        lines += [
            "",
            "• <b>Binary</b>",
            f"short={h(b['short_rate'])}, long={h(b['long_rate'])}, delta={h(b['delta'])}",
            f"dir={h(b['direction'])}, regime={h(b['regime'])}, streak={h(b['streak_value'])}×{h(b['streak_len'])}",
            f"drift_z={h(b['drift_z'])}",
        ]

    if c.get("enough"):
        lines += [
            "",
            "• <b>Categorical</b>",
            f"short_dom={h(c['dominant_short'])}, long_dom={h(c['dominant_long'])}",
            f"entropy={h(c['entropy_short'])}/{h(c['entropy_long'])}",
            f"regime={h(c['regime'])}",
        ]
        if c.get("patterns"):
            p = c["patterns"][:3]
            pattxt = ", ".join(f"{x['pattern']}×{x['count']}" for x in p)
            lines.append(f"patterns={h(pattxt)}")

    if n.get("enough"):
        lines += [
            "",
            "• <b>Numeric</b>",
            f"last={h(n['last_value'])}, z={h(n['zscore'])}, anomaly={h(n['anomaly'])}",
            f"short_mean={h(n['short_mean'])}, long_mean={h(n['long_mean'])}, slope={h(n['slope'])}",
        ]

    return "\n".join(lines)


# =========================
# COMMANDS
# =========================

@admin_only
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(
        u,
        f"🤖 <b>{h(CFG.bot_name)}</b> online\n"
        f"/health /stats /analyze /recent /alerts /pause /resume /ping /myid /forcecheck /config",
        parse_mode="HTML",
    )


@admin_only
async def cmd_ping(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(u, "pong")


async def cmd_myid(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id if u.effective_user else "unknown"
    cid = u.effective_chat.id if u.effective_chat else "unknown"
    await reply(u, f"user_id={uid}\nchat_id={cid}")


@admin_only
async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE):
    total, alerts = STORE.counts()
    await reply(
        u,
        f"🩺 <b>Health</b>\n"
        f"Uptime: <code>{h(STATE.uptime())}</code>\n"
        f"Paused: <b>{h(STATE.paused)}</b>\n"
        f"Fetch OK/Fail: <code>{h(STATE.fetch_ok)}/{h(STATE.fetch_fail)}</code>\n"
        f"Send OK/Fail: <code>{h(STATE.send_ok)}/{h(STATE.send_fail)}</code>\n"
        f"Loop iterations: <code>{h(STATE.loop_iterations)}</code>\n"
        f"Last seen item: <code>{h(STATE.last_seen_item_id or 'n/a')}</code>\n"
        f"Stored obs: <code>{h(total)}</code>\n"
        f"Stored alerts: <code>{h(alerts)}</code>\n"
        f"Last error: <code>{h(STATE.last_error or 'none')[:300]}</code>",
        parse_mode="HTML",
    )


@admin_only
async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent(CFG.analysis_window)
    analysis = ANALYTICS.analyze(rows)
    await reply(u, format_analysis(analysis), parse_mode="HTML")


@admin_only
async def cmd_analyze(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent(CFG.analysis_window)
    analysis = ANALYTICS.analyze(rows)
    txt = format_analysis(analysis)
    STATE.last_summary = txt
    await reply(u, txt, parse_mode="HTML")


@admin_only
async def cmd_recent(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent(10)
    if not rows:
        await reply(u, "No observations.")
        return
    lines = ["🧾 <b>Recent</b>"]
    for item_id, ts, b, cat, num, _ in rows[-10:]:
        lines.append(
            f"<code>{h(item_id)}</code> | bin={h(b)} | cat={h(cat)} | num={h(num)}"
        )
    await reply(u, "\n".join(lines), parse_mode="HTML")


@admin_only
async def cmd_alerts(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = STORE.recent_alerts(10)
    if not rows:
        await reply(u, "No alerts.")
        return
    lines = ["🚨 <b>Recent Alerts</b>"]
    for k, m, ts in rows:
        lines.append(f"• <code>{h(k)}</code> | {time.strftime('%H:%M:%S', time.localtime(ts))}")
    await reply(u, "\n".join(lines), parse_mode="HTML")


@admin_only
async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    STATE.paused = True
    await reply(u, "⏸ Paused.")


@admin_only
async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    STATE.paused = False
    await reply(u, "▶️ Resumed.")


@admin_only
async def cmd_forcecheck(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(u, "Force check queued.")


@admin_only
async def cmd_config(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await reply(
        u,
        f"⚙️ <b>Config</b>\n"
        f"BOT_NAME: <code>{h(CFG.bot_name)}</code>\n"
        f"SOURCE_API_URL: <code>{h(CFG.source_api_url)[:250]}</code>\n"
        f"FETCH_EVERY: <code>{h(CFG.fetch_every)}</code>\n"
        f"REQUEST_TIMEOUT: <code>{h(CFG.request_timeout)}</code>\n"
        f"ANALYSIS_WINDOW: <code>{h(CFG.analysis_window)}</code>\n"
        f"DB_PATH: <code>{h(CFG.db_path)}</code>",
        parse_mode="HTML",
    )


# =========================
# WORKER LOOP
# =========================

async def process_cycle(app: Application, session: aiohttp.ClientSession):
    items = await fetch_sequence_data(session)
    if not items:
        return

    new_count = 0
    latest_known = STORE.latest_item_id()

    for item in items:
        item_id = item["item_id"]
        if latest_known is not None and str(item_id) <= str(latest_known):
            continue

        STORE.save_observation(
            item_id=item_id,
            ts=item["ts"],
            binary_value=item["binary_value"],
            category_value=item["category_value"],
            numeric_value=item["numeric_value"],
            raw_json=item["raw"],
        )
        STATE.last_seen_item_id = item_id
        new_count += 1

    if new_count <= 0:
        return

    rows = STORE.recent(CFG.analysis_window)
    analysis = ANALYTICS.analyze(rows)
    summary = format_analysis(analysis)
    STATE.last_summary = summary

    if not STATE.paused:
        built = ALERTS.build_alerts(analysis)
        for key, message in built:
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("forcecheck", cmd_forcecheck))
    app.add_handler(CommandHandler("config", cmd_config))

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

        if app.updater is None:
            raise RuntimeError("Updater unavailable")

        # conflict-safe polling
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)

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








