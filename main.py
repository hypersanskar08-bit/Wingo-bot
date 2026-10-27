# bot.py — FULLY UNLOCKED (Predict Every Period)
# Python 3.10+
# Dependency: pip install aiohttp
#
# Required env vars:
#   TELEGRAM_TOKEN, CHAT_ID, API_URLS
#
# Optional:
#   DB_PATH = predictor_live.db
#   PORT = 8080
#   FETCH_EVERY = 3
#   MIN_HISTORY = 120

import asyncio
import contextlib
import html
import json
import logging
import math
import os
import signal
import sqlite3
import time
from collections import deque
from dataclasses import dataclass

import aiohttp
from aiohttp import web


@dataclass(frozen=True)
class Config:
    token: str = os.getenv("TELEGRAM_TOKEN", "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo").strip()
    chat: str = os.getenv("CHAT_ID", "-1004485961768").strip()
    urls: tuple = tuple(
        u.strip() for u in os.getenv("API_URLS", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000").split(",") if u.strip()
    )
    db_path: str = os.getenv("DB_PATH", "predictor_live.db")
    port: int = int(os.getenv("PORT", "8080"))
    fetch_every: float = float(os.getenv("FETCH_EVERY", "3"))
    min_history: int = int(os.getenv("MIN_HISTORY", "120"))
    max_history: int = 2000
    validation_window: int = 500
    loss_limit: int = 3
    timeout: float = 12.0
    delivery_ttl: float = 40.0
    name: str = "FTW VIP PREDICTOR (LIVE)"

    def validate(self):
        if not self.token or not self.chat or not self.urls:
            raise ValueError("Set TELEGRAM_TOKEN, CHAT_ID and API_URLS.")


CFG = Config()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("predictor")


def clip(x, low=0.01, high=0.99):
    return max(low, min(high, float(x)))


def esc(v):
    return html.escape(str(v))


def integer(v):
    if isinstance(v, bool):
        return None
    t = str(v).strip()
    if not t.isascii() or not t.isdigit():
        return None
    return int(t) if len(t) <= 18 else None


def parse_history(payload):
    obj = payload
    for _ in range(8):
        if isinstance(obj, list):
            break
        if not isinstance(obj, dict):
            return []
        obj = next(
            (
                obj[k]
                for k in ("data", "list", "result", "items", "history")
                if isinstance(obj.get(k), (dict, list))
            ),
            None,
        )
    if not isinstance(obj, list):
        return []

    records = {}
    for row in obj:
        if not isinstance(row, dict):
            continue
        issue = integer(row.get("issueNumber", row.get("issue")))
        number = integer(row.get("number", row.get("value")))
        if issue is None or number is None or not 0 <= number <= 9:
            continue
        if issue in records and records[issue] != number:
            raise ValueError("Conflicting duplicate result.")
        records[issue] = number
    return sorted(records.items())


def wilson_lower(wins, total, z=1.96):
    if not total:
        return 0.0
    p = wins / total
    denom = 1 + z * z / total
    center = p + z * z / (2 * total)
    radius = z * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total)
    )
    return (center - radius) / denom


def features(seq):
    def mean(n):
        a = seq[-n:]
        return sum(a) / len(a) if a else 0.5

    def ema(period):
        alpha = 2 / (period + 1)
        val = 0.5
        for y in seq[-120:]:
            val += alpha * (y - val)
        return val

    last = seq[-1] if seq else 0
    run = 0
    for y in reversed(seq):
        if y != last:
            break
        run += 1
    tail = seq[-24:]
    flips = sum(a != b for a, b in zip(tail, tail[1:]))
    flip_rate = flips / max(1, len(tail) - 1)

    return [
        1.0,
        2 * mean(8) - 1,
        2 * mean(24) - 1,
        2 * mean(96) - 1,
        ema(12) - ema(26),
        (2 * last - 1) * min(run, 8) / 8,
        (2 * flip_rate - 1) * (2 * last - 1),
    ]


class Model:
    def __init__(self):
        self.rows = deque(maxlen=CFG.max_history)
        self.weights = [0.0] * 7
        self.losses = {}
        self.calibration = {}
        self.validation = deque(maxlen=CFG.validation_window)

    def predict(self):
        nums = [n for _, n in self.rows]
        seq = [int(n >= 5) for n in nums]
        x = features(seq)

        def posterior(obs, strength=24.0):
            mass = sum(w for _, w in obs)
            succ = sum(y * w for y, w in obs)
            return (strength * 0.5 + succ) / (strength + mass)

        def weight(i):
            return 0.5 ** ((len(seq) - 1 - i) / 250)

        experts = {
            "base": posterior(
                [(y, weight(i)) for i, y in enumerate(seq)], strength=60
            ),
            "logistic": clip(
                1
                / (
                    1
                    + math.exp(
                        -clip(
                            sum(w * f for w, f in zip(self.weights, x)),
                            -20,
                            20,
                        )
                    )
                )
            ),
        }

        for order in (1, 2, 3, 4):
            suffix = seq[-order:]
            obs = [
                (seq[i], weight(i))
                for i in range(order, len(seq))
                if seq[i - order:i] == suffix
            ]
            if len(obs) >= 20:
                experts[f"markov_{order}"] = posterior(obs)

        adjusted = []
        for name, p in experts.items():
            n, loss = self.losses.get(name, [0.0, 0.0])
            brier = loss / n if n else 0.25
            maturity = min(n / 100, 1)
            skill = clip((0.25 - brier) / 0.05, 0, 1)
            shrink = 0.25 * (1 - maturity) + maturity * skill
            adj = 0.5 + (p - 0.5) * shrink
            mw = math.exp(-12 * (brier - 0.25))
            adjusted.append((adj, mw))

        total_w = sum(w for _, w in adjusted)
        raw = sum(p * w for p, w in adjusted) / total_w
        disagreement = math.sqrt(
            sum(w * (p - raw) ** 2 for p, w in adjusted) / total_w
        )

        bucket = min(9, int(raw * 10))
        n, pos = self.calibration.get(str(bucket), [0.0, 0.0])
        p = clip((100 * raw + pos) / (100 + n))
        side = int(p >= 0.5)

        counts = [2.0] * 10
        if nums:
            for i in range(1, len(nums)):
                if nums[i - 1] == nums[-1]:
                    counts[nums[i]] += weight(i)
        valid = list(range(5, 10) if side else range(5))
        number = max(valid, key=lambda v: (counts[v], -v))
        conditional = counts[number] / sum(counts[v] for v in valid)

        validation = self.metrics()

        # ALWAYS return a prediction — no gating
        return {
            "p": p,
            "raw": raw,
            "bucket": bucket,
            "side": side,
            "number": number,
            "number_estimate": max(p, 1 - p) * conditional,
            "experts": experts,
            "features": x,
            "baseline": experts["base"],
            "disagreement": disagreement,
            "candidate": True,
            "qualified": True,
            "validation": validation,
        }

    def observe(self, row, prediction=None):
        _, number = row
        y = int(number >= 5)

        if prediction is not None:
            for name, p in prediction["experts"].items():
                n, loss = self.losses.get(name, [0.0, 0.0])
                self.losses[name] = [
                    0.995 * n + 1,
                    0.995 * loss + (p - y) ** 2,
                ]

            error = y - prediction["experts"]["logistic"]
            self.weights = [
                clip(w + 0.025 * (error * f - 0.01 * w), -4, 4)
                for w, f in zip(self.weights, prediction["features"])
            ]

            key = str(prediction["bucket"])
            n, pos = self.calibration.get(key, [0.0, 0.0])
            self.calibration[key] = [0.995 * n + 1, 0.995 * pos + y]

            self.validation.append(
                {
                    "candidate": prediction["candidate"],
                    "win": prediction["side"] == y,
                    "brier": (prediction["p"] - y) ** 2,
                    "baseline_brier": (prediction["baseline"] - y) ** 2,
                }
            )

        self.rows.append(row)

    def metrics(self):
        eligible = [r for r in self.validation if r["candidate"]]
        n = len(eligible)
        wins = sum(r["win"] for r in eligible)
        return {
            "n": n,
            "wins": wins,
            "accuracy": wins / n if n else 0.0,
            "wilson_lower": wilson_lower(wins, n),
            "brier": sum(r["brier"] for r in eligible) / n if n else 1.0,
            "baseline_brier": (
                sum(r["baseline_brier"] for r in eligible) / n if n else 0.25
            ),
        }

    def dump(self):
        return {
            "rows": list(self.rows),
            "weights": self.weights,
            "losses": self.losses,
            "calibration": self.calibration,
            "validation": list(self.validation),
        }

    @classmethod
    def restore(cls, data):
        model = cls()
        if data:
            model.rows.extend(tuple(r) for r in data["rows"])
            model.weights = data["weights"]
            model.losses = data["losses"]
            model.calibration = data["calibration"]
            model.validation.extend(data["validation"])
        return model


class Database:
    def __init__(self):
        self.c = sqlite3.connect(CFG.db_path, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=FULL")
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS predictions(
                issue INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                actual INTEGER,
                delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                expires REAL NOT NULL,
                state INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    @contextlib.contextmanager
    def transaction(self):
        self.c.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.c.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.c.execute(
            "INSERT INTO meta VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def queue(self, issue, kind, text, ttl):
        self.c.execute(
            "INSERT INTO outbox(issue,kind,text,expires) VALUES (?,?,?,?)",
            (issue, kind, text, time.time() + ttl),
        )


def prediction_text(issue, p):
    side = "BIG" if p["side"] else "SMALL"
    conf = max(p["p"], 1 - p["p"]) * 100
    filled = int(conf // 10)
    bar = "█" * filled + "░" * (10 - filled)
    color = "🔴" if side == "BIG" else "🔵"

    if conf >= 65:
        quality = "🔥 HIGH VIP (Stake: 1x)"
    elif conf >= 58:
        quality = "⭐ VIP (Stake: 1x)"
    else:
        quality = "✅ NORMAL (Stake: 0.5x)"

    return (
        f"⚡️ <b>{esc(CFG.name)}</b> ⚡️\n"
        f"──────────────────\n"
        f"🎯 <b>TARGET:</b> {color} <b>{side}</b>  [ <code>{p['number']}</code> ]\n"
        f"📅 <b>PERIOD:</b> <code>{issue}</code>\n"
        f"📊 <b>CONFIDENCE:</b> {conf:.1f}% [{bar}]\n"
        f"🏆 <b>QUALITY:</b> {quality}\n"
        f"──────────────────"
    )


class Runtime:
    def __init__(self, db):
        self.db = db
        self.model = Model.restore(db.get("model"))
        self.stop = asyncio.Event()
        self.last_fetch = 0.0
        self.last_advance = 0.0
        self.error = None
        self.telegram_blocked = False

        # Clear undelivered predictions so they regenerate fresh
        db.c.execute(
            "UPDATE outbox SET state=2 WHERE kind='prediction' AND state=0"
        )
        db.c.execute(
            "DELETE FROM predictions WHERE delivered=0 AND actual IS NULL"
        )

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(
                self.stop.wait(), timeout=max(0.05, seconds)
            )
        except asyncio.TimeoutError:
            pass

    async def fetch(self, session):
        async def one(url):
            try:
                async with session.get(url) as r:
                    r.raise_for_status()
                    data = parse_history(
                        await r.json(content_type=None)
                    )
                    return data or None
            except Exception:
                return None

        sources = [
            x
            for x in await asyncio.gather(*(one(u) for u in CFG.urls))
            if x
        ]
        if not sources:
            raise RuntimeError("HistoryUnavailable")

        merged = {}
        for rows in sources:
            for issue, number in rows:
                if issue in merged and merged[issue] != number:
                    raise ValueError("SourceConflict")
                merged[issue] = number

        return sorted(merged.items())[-CFG.max_history:]

    def settle(self, records):
        for issue, number in records:
            row = self.db.c.execute(
                "SELECT * FROM predictions WHERE issue=? AND actual IS NULL",
                (issue,),
            ).fetchone()
            if row is None:
                continue

            p = json.loads(row["payload"])
            won = p["side"] == int(number >= 5)
            won_num = p["number"] == number

            self.db.c.execute(
                "UPDATE predictions SET actual=? WHERE issue=?",
                (number, issue),
            )
            self.db.c.execute(
                "UPDATE outbox SET state=2 "
                "WHERE issue=? AND kind='prediction' AND state=0",
                (issue,),
            )

            if row["delivered"]:
                stats = self.db.get(
                    "live_stats",
                    {"signals": 0, "wins": 0, "number_hits": 0},
                )
                stats["signals"] += 1
                stats["wins"] += int(won)
                stats["number_hits"] += int(won_num)
                self.db.set("live_stats", stats)

                streak = 0 if won else self.db.get("loss_streak", 0) + 1
                self.db.set("loss_streak", streak)

                if won and won_num:
                    emoji = "🎰🎰🎰"
                elif won:
                    emoji = "🏆"
                else:
                    emoji = "❌"

                self.db.queue(issue, "result", emoji, ttl=3600)

    def process(self, records, allow_signal=True):
        before = self.model.rows[-1][0] if self.model.rows else None
        known = dict(self.model.rows)

        for issue, number in records:
            if issue in known and known[issue] != number:
                raise ValueError("SettledResultChanged")

        new_rows = [
            row for row in records if before is None or row[0] > before
        ]

        with self.db.transaction():
            self.settle(records)

            for row in new_rows:
                p = (
                    self.model.predict()
                    if len(self.model.rows) >= CFG.min_history
                    else None
                )
                self.model.observe(row, p)

            if new_rows:
                self.last_advance = time.time()

            # ALWAYS ensure prediction for next period
            if allow_signal and len(self.model.rows) >= CFG.min_history:
                latest = self.model.rows[-1][0]
                next_issue = latest + 1
                exists = self.db.c.execute(
                    "SELECT 1 FROM predictions WHERE issue=?",
                    (next_issue,),
                ).fetchone()
                if not exists and not self.telegram_blocked:
                    p = self.model.predict()
                    cursor = self.db.c.execute(
                        "INSERT OR IGNORE INTO predictions(issue,payload) "
                        "VALUES (?,?)",
                        (next_issue, json.dumps(p)),
                    )
                    if cursor.rowcount:
                        self.db.queue(
                            next_issue,
                            "prediction",
                            prediction_text(next_issue, p),
                            CFG.delivery_ttl,
                        )

            self.db.set("model", self.model.dump())

    async def worker(self, session):
        failures = 0

        while not self.stop.is_set():
            try:
                records = await self.fetch(session)
                self.process(records, True)
                failures = 0
                self.last_fetch = time.time()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.model = Model.restore(self.db.get("model"))
                self.error = type(exc).__name__
                failures += 1
                LOG.error("Worker failure: %s", self.error)

            await self.pause(
                min(60, CFG.fetch_every * 2 ** min(failures, 4))
            )

    async def sender(self, session):
        endpoint = f"https://api.telegram.org/bot{CFG.token}/sendMessage"
        backoff = 1

        while not self.stop.is_set():
            if self.telegram_blocked:
                await self.pause(5)
                continue

            self.db.c.execute(
                "UPDATE outbox SET state=2 WHERE state=0 AND expires<=?",
                (time.time(),),
            )
            row = self.db.c.execute(
                "SELECT * FROM outbox WHERE state=0 "
                "ORDER BY CASE kind WHEN 'prediction' THEN 0 ELSE 1 END,id "
                "LIMIT 1"
            ).fetchone()

            if row is None:
                await self.pause(0.5)
                continue

            # Drop only if issue already settled (not just because it's not latest+1)
            if row["kind"] == "prediction":
                latest = (
                    self.model.rows[-1][0] if self.model.rows else None
                )
                if latest is None or row["issue"] <= latest:
                    self.db.c.execute(
                        "UPDATE outbox SET state=2 WHERE id=?",
                        (row["id"],),
                    )
                    continue

            delay = backoff
            try:
                async with session.post(
                    endpoint,
                    json={
                        "chat_id": CFG.chat,
                        "text": row["text"],
                        "parse_mode": "HTML",
                    },
                ) as response:
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict):
                        raise ValueError("InvalidTelegramResponse")

                    if response.status == 200 and data.get("ok"):
                        with self.db.transaction():
                            self.db.c.execute(
                                "UPDATE outbox SET state=1 WHERE id=?",
                                (row["id"],),
                            )
                            if row["kind"] == "prediction":
                                self.db.c.execute(
                                    "UPDATE predictions SET delivered=1 "
                                    "WHERE issue=?",
                                    (row["issue"],),
                                )
                        backoff = 1
                        continue

                    code = data.get("error_code", response.status)
                    if code == 429:
                        delay = max(
                            1,
                            float(
                                data.get("parameters", {}).get(
                                    "retry_after", 10
                                )
                            ),
                        )
                    elif code in (401, 403, 404):
                        self.telegram_blocked = True
                        LOG.error("Telegram blocked: %s", code)
                    elif code == 400:
                        self.db.c.execute(
                            "UPDATE outbox SET state=3 WHERE id=?",
                            (row["id"],),
                        )
                        LOG.error("Telegram rejected message (400).")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning(
                    "Telegram transport failure: %s", type(exc).__name__
                )

            await self.pause(delay)
            backoff = min(60, backoff * 2)

    async def health(self, request):
        now = time.time()
        fetch_age = now - self.last_fetch if self.last_fetch else None
        advance_age = (
            now - self.last_advance if self.last_advance else None
        )
        healthy = (
            fetch_age is not None
            and fetch_age < 90
            and advance_age is not None
            and advance_age < 180
            and self.error is None
            and not self.telegram_blocked
        )

        return web.json_response(
            {
                "status": "ok" if healthy else "degraded",
                "last_fetch_age": fetch_age,
                "last_advance_age": advance_age,
                "error": self.error,
                "telegram_blocked": self.telegram_blocked,
                "history_size": len(self.model.rows),
                "mode": "PREDICT_EVERY_PERIOD",
                "candidate_validation": self.model.metrics(),
                "live_results": self.db.get("live_stats", {}),
                "loss_streak": self.db.get("loss_streak", 0),
            },
            status=200 if healthy else 503,
        )


async def main():
    CFG.validate()
    db = Database()
    runtime = Runtime(db)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime.stop.set)

    app = web.Application()
    app.router.add_get("/", runtime.health)
    app.router.add_get("/health", runtime.health)
    runner = web.AppRunner(app)
    tasks = []
    stopper = None

    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", CFG.port).start()

        timeout = aiohttp.ClientTimeout(total=CFG.timeout)
        connector = aiohttp.TCPConnector(limit=12, ttl_dns_cache=300)

        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            tasks = [
                asyncio.create_task(runtime.worker(session)),
                asyncio.create_task(runtime.sender(session)),
            ]
            stopper = asyncio.create_task(runtime.stop.wait())

            LOG.info("LIVE predictor started (every-period mode).")
            done, _ = await asyncio.wait(
                [*tasks, stopper],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is not stopper:
                    task.result()

    finally:
        runtime.stop.set()
        for task in tasks:
            task.cancel()
        if stopper:
            stopper.cancel()
        await asyncio.gather(
            *tasks,
            *([stopper] if stopper else []),
            return_exceptions=True,
        )
        await runner.cleanup()
        db.c.close()


if __name__ == "__main__":
    asyncio.run(main())
