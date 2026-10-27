# bot.py
# Python 3.10+
# Dependency: pip install aiohttp
#
# Required environment variables:
#   TELEGRAM_TOKEN=<new token>
#   CHAT_ID=<chat ID or @channel username>
#   API_URLS=<comma-separated history endpoints>
#
# Optional:
#   DB_PATH=predictor_v2.db
#   PORT=8080
#   FETCH_EVERY=3
#   MIN_HISTORY=150
#   MIN_EDGE=0.06
#   MIN_PATTERN_SUPPORT=12
#   COOLDOWN_ROUNDS=2

import asyncio
import contextlib
import html
import json
import logging
import math
import os
import random
import signal
import socket
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web


@dataclass(frozen=True)
class Config:
    # Added .strip() to prevent hidden spaces in Render env variables
    token: str = os.getenv("TELEGRAM_TOKEN", "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo").strip()
    chat: str = os.getenv("CHAT_ID", "-1004485961768").strip()
    urls: tuple = tuple(
        x.strip()
        for x in os.getenv("API_URLS", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000").split(",")
        if x.strip()
    )
    db_path: str = os.getenv("DB_PATH", "predictor_v2.db")
    port: int = int(os.getenv("PORT", "8080"))
    fetch_every: float = float(os.getenv("FETCH_EVERY", "3"))
    timeout: float = 15.0
    max_history: int = 3000
    min_history: int = int(os.getenv("MIN_HISTORY", "150"))
    min_support: int = int(os.getenv("MIN_PATTERN_SUPPORT", "12"))
    min_edge: float = float(os.getenv("MIN_EDGE", "0.06"))
    cooldown_rounds: int = int(os.getenv("COOLDOWN_ROUNDS", "2"))
    loss_limit: int = 3
    learning_rate: float = 0.035
    decay: float = 0.985
    max_pattern: int = 12
    round_seconds: float = 60.0
    delivery_ttl: float = 40.0
    name: str = "FTW ADAPTIVE PATTERN"

    def validate(self):
        if not self.token or not self.chat or not self.urls:
            raise ValueError(
                "Set TELEGRAM_TOKEN, CHAT_ID and API_URLS."
            )
        if self.fetch_every <= 0 or self.min_history < 30:
            raise ValueError("Invalid polling interval or history minimum.")
        if self.min_support < 3 or not 0 < self.min_edge < 0.5:
            raise ValueError("Invalid support or edge configuration.")
        if self.cooldown_rounds < 0:
            raise ValueError("COOLDOWN_ROUNDS cannot be negative.")


CFG = Config()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("predictor")


def clip(x, low=0.02, high=0.98):
    return max(low, min(high, float(x)))


def escape(x):
    return html.escape(str(x))


def run_length(seq):
    if not seq:
        return 0
    n = 0
    for x in reversed(seq):
        if x != seq[-1]:
            break
        n += 1
    return n


def regime(seq):
    tail = seq[-24:]
    if len(tail) < 8:
        return "COLD"
    flips = sum(a != b for a, b in zip(tail, tail[1:]))
    rate = flips / (len(tail) - 1)
    if run_length(tail) >= 4:
        return "RUN"
    if rate >= 0.70:
        return "ALTERNATING"
    if abs(sum(tail) / len(tail) - 0.5) > 0.20:
        return "IMBALANCED"
    return "MIXED"


def numeric(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def parse_history(payload):
    obj = payload
    for _ in range(6):
        if isinstance(obj, list):
            break
        if not isinstance(obj, dict):
            return []
        obj = next(
            (
                obj[k]
                for k in ("data", "list", "result", "items", "history")
                if isinstance(obj.get(k), (list, dict))
            ),
            None,
        )
    if not isinstance(obj, list):
        return []

    records = {}
    for item in obj:
        if not isinstance(item, dict):
            continue
        issue = numeric(item.get("issueNumber", item.get("issue")))
        number = numeric(item.get("number", item.get("value")))
        if issue is None or number is None or not 0 <= number <= 9:
            continue
        key = str(issue)
        if key in records and records[key]["num"] != number:
            raise ValueError(f"Conflicting duplicate issue: {key}")
        records[key] = {
            "issue": key,
            "num": number,
            "size": int(number >= 5),
        }
    return sorted(records.values(), key=lambda x: int(x["issue"]))


class Database:
    def __init__(self, path):
        self.c = sqlite3.connect(path, isolation_level=None, timeout=20)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS history(
                issue TEXT PRIMARY KEY,
                num INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS predictions(
                issue TEXT PRIMARY KEY,
                created REAL NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                actual INTEGER
            );
            CREATE TABLE IF NOT EXISTS models(
                key TEXT PRIMARY KEY,
                n REAL NOT NULL,
                loss REAL NOT NULL,
                inverse_loss REAL NOT NULL,
                failures INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                issue TEXT,
                text TEXT NOT NULL,
                created REAL NOT NULL,
                expires REAL,
                sent INTEGER NOT NULL DEFAULT 0
            );
        """)

    @contextmanager
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

    def ingest(self, records):
        with self.transaction():
            for r in records:
                old = self.c.execute(
                    "SELECT num FROM history WHERE issue=?", (r["issue"],)
                ).fetchone()
                if old and old["num"] != r["num"]:
                    raise ValueError(
                        f"Source changed settled issue {r['issue']}"
                    )
                self.c.execute(
                    "INSERT OR IGNORE INTO history VALUES (?,?)",
                    (r["issue"], r["num"]),
                )
            self.c.execute(
                "DELETE FROM history WHERE issue NOT IN "
                "(SELECT issue FROM history "
                "ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?)",
                (CFG.max_history,),
            )

    def history(self):
        rows = self.c.execute(
            "SELECT issue,num FROM history "
            "ORDER BY CAST(issue AS INTEGER)"
        ).fetchall()
        return [
            {
                "issue": r["issue"],
                "num": r["num"],
                "size": int(r["num"] >= 5),
            }
            for r in rows
        ]

    def memory(self, key):
        row = self.c.execute(
            "SELECT * FROM models WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else {
            "n": 0.0,
            "loss": 0.0,
            "inverse_loss": 0.0,
            "failures": 0,
        }

    def learn(self, key, probability, actual):
        m = self.memory(key)
        n = CFG.decay * m["n"] + 1
        loss = CFG.decay * m["loss"] + (probability - actual) ** 2
        inv = (
            CFG.decay * m["inverse_loss"]
            + (1 - probability - actual) ** 2
        )
        wrong = int(probability >= 0.5) != actual
        failures = m["failures"] + 1 if wrong else 0
        self.c.execute(
            "INSERT INTO models VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "n=excluded.n,loss=excluded.loss,"
            "inverse_loss=excluded.inverse_loss,"
            "failures=excluded.failures",
            (key, n, loss, inv, failures),
        )

    def adapt(self, key, probability):
        m = self.memory(key)
        n = m["n"]
        if n < 1:
            return probability, 0.65, False

        brier = m["loss"] / n
        inverse_brier = m["inverse_loss"] / n

        flipped = (
            n >= 25
            and inverse_brier + 0.04 < brier
            and inverse_brier < 0.245
        )
        effective_loss = inverse_brier if flipped else brier
        p = 1 - probability if flipped else probability

        reliability = clip(
            (0.28 - effective_loss) / 0.08, 0.0, 1.0
        )
        maturity = min(n / 30, 1)
        shrink = (1 - maturity) * 0.65 + maturity * reliability
        p = 0.5 + (p - 0.5) * shrink

        weight = clip(
            math.exp(-8 * (effective_loss - 0.25)), 0.15, 2.0
        )
        if m["failures"] >= 3 and not flipped:
            weight *= 0.35
        return clip(p), weight, flipped

    def queue(self, kind, issue, text, expires=None):
        self.c.execute(
            "INSERT INTO outbox(kind,issue,text,created,expires) "
            "VALUES (?,?,?,?,?)",
            (kind, issue, text, time.time(), expires),
        )


def weighted_probability(observations):
    sw = sum(w for _, w in observations)
    sw2 = sum(w * w for _, w in observations)
    effective_n = sw * sw / sw2 if sw2 else 0.0
    probability = (
        4 + sum(y * w for y, w in observations)
    ) / (8 + sw)
    return probability, effective_n


def trend_features(seq):
    def mean(n):
        arr = seq[-n:]
        return sum(arr) / len(arr)

    tail = seq[-24:]
    alternation = sum(
        a != b for a, b in zip(tail, tail[1:])
    ) / max(1, len(tail) - 1)
    signed_run = (2 * seq[-1] - 1) * min(run_length(seq), 10) / 10
    return [
        1.0,
        2 * mean(8) - 1,
        2 * mean(24) - 1,
        2 * mean(96) - 1,
        mean(8) - mean(24),
        signed_run,
        2 * seq[-1] - 1,
        (2 * alternation - 1) * (2 * seq[-1] - 1),
    ]


def logistic(weights, features):
    z = sum(w * x for w, x in zip(weights, features))
    return clip(1 / (1 + math.exp(-clip(z, -20, 20))))


class Engine:
    def __init__(self, db):
        self.db = db

    def mine(self, seq):
        experts = {}
        candidates = []
        size = len(seq)
        current_regime = regime(seq)
        current_run = min(run_length(seq), 8)

        def weight(index):
            return 0.5 ** ((size - 1 - index) / 250)

        for length in range(2, CFG.max_pattern + 1):
            if size < 3 * length:
                continue
            suffix = tuple(seq[-length:])
            observations = []
            last_index = -length - 1
            for i in range(length, size - length):
                if tuple(seq[i - length:i]) != suffix:
                    continue
                if i - last_index < length + 1:
                    continue
                observations.append((seq[i], weight(i)))
                last_index = i

            if len(observations) < CFG.min_support:
                continue
            p, effective_n = weighted_probability(observations)
            if effective_n < CFG.min_support * 0.6:
                continue

            bits = "".join("B" if x else "S" for x in suffix)
            key = f"exact:{bits}"
            ranking = abs(p - 0.5) * math.sqrt(effective_n)
            candidates.append(
                (ranking, key, p, len(observations), effective_n)
            )

        candidates.sort(reverse=True)
        top = candidates[:5]
        if top:
            numerator = denominator = 0.0
            for _, key, p, support, effective_n in top:
                adjusted, w, _ = self.db.adapt(key, p)
                w *= math.sqrt(effective_n)
                numerator += adjusted * w
                denominator += w
            experts["patterns"] = numerator / denominator

        for order in (1, 2, 3, 4):
            suffix = tuple(seq[-order:])
            obs = [
                (seq[i], weight(i))
                for i in range(order, size - order)
                if tuple(seq[i - order:i]) == suffix
            ]
            if len(obs) >= CFG.min_support:
                experts[f"markov{order}"] = weighted_probability(obs)[0]

        run_obs = []
        regime_obs = []
        for i in range(24, size):
            past = seq[max(0, i - 96):i]
            if (
                past[-1] == seq[-1]
                and min(run_length(past), 8) == current_run
            ):
                run_obs.append((seq[i], weight(i)))
            if regime(past) == current_regime:
                regime_obs.append((seq[i], weight(i)))

        if len(run_obs) >= CFG.min_support:
            experts["run"] = weighted_probability(run_obs)[0]
        if len(regime_obs) >= CFG.min_support:
            experts["regime"] = weighted_probability(regime_obs)[0]

        experts["base"] = weighted_probability(
            [(y, weight(i)) for i, y in enumerate(seq)]
        )[0]

        length = 10
        nearest = []
        suffix = seq[-length:]
        for i in range(length, size - length):
            previous = seq[i - length:i]
            distance = sum(a != b for a, b in zip(previous, suffix))
            if distance <= 2:
                nearest.append(
                    (seq[i], weight(i) * 0.4 ** distance)
                )
        if len(nearest) >= 20:
            experts["neighbors"] = weighted_probability(nearest)[0]

        features = trend_features(seq)
        weights = self.db.get("trend_weights", [0.0] * len(features))
        experts["trend"] = logistic(weights, features)
        return experts, top, features

    def predict(self, history, issue):
        seq = [r["size"] for r in history]
        experts, patterns, features = self.mine(seq)
        current_regime = regime(seq)

        adjusted = []
        flipped_models = []
        for name, p in experts.items():
            key = f"expert:{name}:{current_regime}"
            corrected, weight, flipped = self.db.adapt(key, p)
            adjusted.append((corrected, weight))
            if flipped:
                flipped_models.append(name)

        denominator = sum(w for _, w in adjusted)
        p_raw = sum(p * w for p, w in adjusted) / denominator
        disagreement = math.sqrt(
            sum(w * (p - p_raw) ** 2 for p, w in adjusted)
            / denominator
        )

        bucket = min(9, int(p_raw * 10))
        calibration = self.db.get(f"cal:{bucket}", [0.0, 0.0])
        n, successes = calibration
        p = (30 * p_raw + successes) / (30 + n)
        p = clip(p)

        reasons = []
        if len(history) < CFG.min_history:
            reasons.append("WARMUP")
        if self.db.get("cooldown", 0) > 0:
            reasons.append("COOLDOWN")
        if abs(p - 0.5) < CFG.min_edge:
            reasons.append("LOW_EDGE")
        if disagreement > 0.18:
            reasons.append("MODEL_DISAGREEMENT")

        return {
            "issue": issue,
            "p": p,
            "p_raw": p_raw,
            "bucket": bucket,
            "side": "BIG" if p >= 0.5 else "SMALL",
            "action": "SKIP" if reasons else "SIGNAL",
            "reasons": reasons,
            "regime": current_regime,
            "experts": experts,
            "patterns": [
                {"key": key, "p": prob, "support": count}
                for _, key, prob, count, _ in patterns
            ],
            "features": features,
            "disagreement": disagreement,
            "flipped": flipped_models,
        }

    def settle(self, history):
        lookup = {r["issue"]: r for r in history}
        pending = self.db.c.execute(
            "SELECT issue,payload FROM predictions WHERE status='PENDING' "
            "ORDER BY CAST(issue AS INTEGER)"
        ).fetchall()

        for row in pending:
            actual = lookup.get(row["issue"])
            if actual is None:
                continue

            prediction = json.loads(row["payload"])
            y = actual["size"]
            won = int(prediction["p"] >= 0.5) == y
            skip = prediction["action"] == "SKIP"
            status = "OBSERVED" if skip else ("WIN" if won else "LOSS")

            with self.db.transaction():
                self.db.c.execute(
                    "UPDATE predictions SET status=?,actual=? WHERE issue=?",
                    (status, actual["num"], row["issue"]),
                )

                for name, p in prediction["experts"].items():
                    self.db.learn(
                        f"expert:{name}:{prediction['regime']}", p, y
                    )
                for pattern in prediction["patterns"]:
                    self.db.learn(pattern["key"], pattern["p"], y)

                weights = self.db.get(
                    "trend_weights",
                    [0.0] * len(prediction["features"]),
                )
                error = y - prediction["experts"]["trend"]
                weights = [
                    clip(
                        w + CFG.learning_rate * (error * x - 0.01 * w),
                        -4,
                        4,
                    )
                    for w, x in zip(weights, prediction["features"])
                ]
                self.db.set("trend_weights", weights)

                key = f"cal:{prediction['bucket']}"
                n, successes = self.db.get(key, [0.0, 0.0])
                self.db.set(
                    key, [0.995 * n + 1, 0.995 * successes + y]
                )

                stats = self.db.get(
                    "stats",
                    {"observed": 0, "signals": 0, "wins": 0, "brier": 0.0},
                )
                stats["observed"] += 1
                stats["brier"] += (prediction["p"] - y) ** 2

                if skip:
                    self.db.set(
                        "cooldown",
                        max(0, self.db.get("cooldown", 0) - 1),
                    )
                else:
                    stats["signals"] += 1
                    stats["wins"] += int(won)
                    streak = 0 if won else self.db.get("loss_streak", 0) + 1
                    self.db.set("loss_streak", streak)
                    if streak >= CFG.loss_limit:
                        self.db.set("cooldown", CFG.cooldown_rounds)
                        self.db.set("loss_streak", 0)

                    actual_side = "BIG" if y else "SMALL"
                    self.db.queue(
                        "result",
                        row["issue"],
                        f"<b>{status}</b>\n"
                        f"Period: <code>{escape(row['issue'])}</code>\n"
                        f"Prediction: {prediction['side']}\n"
                        f"Actual: {actual_side} / {actual['num']}",
                    )

                self.db.set("stats", stats)

                self.db.c.execute(
                    "UPDATE outbox SET sent=2 "
                    "WHERE kind='prediction' AND issue=? AND sent=0",
                    (row["issue"],),
                )


def format_prediction(p):
    selected_probability = max(p["p"], 1 - p["p"])
    pattern_text = "\n".join(
        f"{escape(x['key'])} | samples={x['support']}"
        for x in p["patterns"][:3]
    ) or "No sufficiently supported exact pattern"

    reasons = ", ".join(p["reasons"]) or "Filters passed"
    return (
        f"<b>{escape(CFG.name)}</b>\n"
        f"Period: <code>{escape(p['issue'])}</code>\n"
        f"Action: <b>{p['action']}</b>\n"
        f"Model direction: <b>{p['side']}</b>\n"
        f"Estimated probability: {selected_probability:.1%}\n"
        f"Regime: {p['regime']}\n"
        f"Disagreement: {p['disagreement']:.3f}\n"
        f"Filter: {escape(reasons)}\n\n"
        f"<b>Pattern miner</b>\n{pattern_text}\n\n"
        "<i>Experimental estimate, not a guaranteed win rate. "
        "No exact-number prediction or recovery stake.</i>"
    )


class Runtime:
    def __init__(self, db):
        self.db = db
        self.engine = Engine(db)
        self.stop = asyncio.Event()
        self.last_fetch = 0.0
        self.last_error = None

    async def fetch(self, session):
        best = None
        for url in CFG.urls:
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    records = parse_history(
                        await response.json(content_type=None)
                    )
                    if records and (
                        best is None
                        or int(records[-1]["issue"]) > int(best[-1]["issue"])
                    ):
                        best = records
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                LOG.warning("A history source failed")
        if not best:
            raise RuntimeError("No valid history source available")
        return best

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def worker(self, session):
        failures = 0
        while not self.stop.is_set():
            try:
                records = await self.fetch(session)
                self.db.ingest(records)
                history = self.db.history()
                self.last_fetch = time.time()
                self.last_error = None
                failures = 0

                self.engine.settle(history)

                latest = int(history[-1]["issue"])
                seen = self.db.get("latest_seen")
                self.db.set("latest_seen", latest)

                if seen is None or latest <= seen:
                    await self.pause(CFG.fetch_every)
                    continue

                if latest != seen + 1:
                    LOG.warning("History gap or issue rollover: resynchronizing")
                    await self.pause(CFG.fetch_every)
                    continue

                start = len(history) - 1
                while start > 0 and (
                    int(history[start]["issue"])
                    == int(history[start - 1]["issue"]) + 1
                ):
                    start -= 1
                continuous = history[start:]
                if len(continuous) < 30:
                    await self.pause(CFG.fetch_every)
                    continue

                issue = str(latest + 1)
                exists = self.db.c.execute(
                    "SELECT 1 FROM predictions WHERE issue=?", (issue,)
                ).fetchone()
                if not exists:
                    p = self.engine.predict(continuous, issue)
                    with self.db.transaction():
                        self.db.c.execute(
                            "INSERT INTO predictions VALUES (?,?,?,?,NULL)",
                            (
                                issue,
                                time.time(),
                                "PENDING",
                                json.dumps(p),
                            ),
                        )
                        self.db.queue(
                            "prediction",
                            issue,
                            format_prediction(p),
                            time.time() + CFG.delivery_ttl,
                        )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.last_error = type(exc).__name__
                LOG.exception("Worker failure")

            delay = min(
                60,
                CFG.fetch_every * 2 ** min(failures, 4),
            )
            await self.pause(delay + random.uniform(0, 0.3))

    async def sender(self, session):
        # Telegram delivery is at-least-once: a network timeout after
        # server acceptance can produce a duplicate on retry.
        endpoint = f"https://api.telegram.org/bot{CFG.token}/sendMessage"
        backoff = 1.0

        while not self.stop.is_set():
            row = self.db.c.execute(
                "SELECT * FROM outbox WHERE sent=0 "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                await self.pause(0.5)
                continue

            if row["expires"] and time.time() >= row["expires"]:
                self.db.c.execute(
                    "UPDATE outbox SET sent=2 WHERE id=?", (row["id"],)
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

                    if response.status == 200 and data.get("ok"):
                        self.db.c.execute(
                            "UPDATE outbox SET sent=1 WHERE id=?",
                            (row["id"],),
                        )
                        backoff = 1.0
                        continue

                    code = data.get("error_code", response.status)
                    if code == 429:
                        delay = float(
                            data.get("parameters", {}).get("retry_after", 10)
                        )
                    elif code in (400, 401, 403, 404):
                        LOG.error(
                            "Telegram rejected delivery with code %s; "
                            "check token, chat ID and bot permissions",
                            code,
                        )
                        delay = 60
                    else:
                        LOG.warning("Telegram delivery failed: code %s", code)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Better error logging to catch exact network issues
                LOG.warning(f"Telegram transport failure: {type(e).__name__} - {str(e)}")

            await self.pause(delay)
            backoff = min(60, backoff * 2)

    async def health(self, request):
        age = time.time() - self.last_fetch if self.last_fetch else None
        healthy = age is not None and age < 90
        return web.json_response(
            {
                "status": "ok" if healthy else "degraded",
                "last_fetch_age_seconds": round(age, 1) if age else age,
                "last_error": self.last_error,
                "metrics": self.db.get("stats", {}),
                "pending_messages": self.db.c.execute(
                    "SELECT COUNT(*) FROM outbox WHERE sent=0"
                ).fetchone()[0],
            },
            status=200 if healthy else 503,
        )


async def main():
    CFG.validate()
    db = Database(CFG.db_path)
    runtime = Runtime(db)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime.stop.set)

    app = web.Application()
    app.router.add_get("/", runtime.health)
    runner = web.AppRunner(app)
    tasks = []
    stop_task = None

    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", CFG.port).start()

        timeout = aiohttp.ClientTimeout(total=CFG.timeout)
        
        # 🚀 FIX: Force IPv4 and disable SSL verification for Render.com
        connector = aiohttp.TCPConnector(
            limit=8, 
            ttl_dns_cache=300, 
            ssl=False, 
            family=socket.AF_INET
        )
        
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            tasks = [
                asyncio.create_task(runtime.worker(session), name="worker"),
                asyncio.create_task(runtime.sender(session), name="sender"),
            ]
            stop_task = asyncio.create_task(runtime.stop.wait())
            LOG.info("Adaptive predictor started")

            await asyncio.wait(
                [*tasks, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            runtime.stop.set()

            for task in tasks:
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc:
                        raise exc

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    finally:
        runtime.stop.set()
        for task in tasks:
            task.cancel()
        if stop_task:
            stop_task.cancel()
        await asyncio.gather(
            *tasks,
            *([stop_task] if stop_task else []),
            return_exceptions=True,
        )
        await runner.cleanup()
        db.c.close()


if __name__ == "__main__":
    asyncio.run(main())
