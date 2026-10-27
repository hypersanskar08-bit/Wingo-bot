# bot.py - GUARANTEED MESSAGE VERSION
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
    urls: tuple = tuple(u.strip() for u in os.getenv("API_URLS", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000").split(",") if u.strip())
    db_path: str = os.getenv("DB_PATH", "predictor_v6_debug.db")
    port: int = int(os.getenv("PORT", "8080"))
    fetch_every: float = 3.0
    min_history: int = 50 # Kam kar diya taaki jaldi start ho
    timeout: float = 12.0
    delivery_ttl: float = 40.0 # TTL badha diya
    name: str = "FTW VIP PREDICTOR"

    def validate(self):
        if not self.token or not self.chat or not self.urls:
            raise ValueError("Missing TELEGRAM_TOKEN, CHAT_ID or API_URLS.")

CFG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("predictor")

def esc(value): return html.escape(str(value))

def integer(value):
    if isinstance(value, bool): return None
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None

def parse_history(payload):
    obj = payload
    for _ in range(6):
        if isinstance(obj, list): break
        if not isinstance(obj, dict): return []
        obj = next((obj[k] for k in ("data", "list", "result", "items", "history") if isinstance(obj.get(k), (dict, list))), None)
    if not isinstance(obj, list): return []
    records = {}
    for row in obj:
        if not isinstance(row, dict): continue
        issue = integer(row.get("issueNumber", row.get("issue")))
        number = integer(row.get("number", row.get("value")))
        if issue is None or number is None or not 0 <= number <= 9: continue
        records[issue] = number
    return sorted(records.items())

class Model:
    def __init__(self):
        self.rows = deque(maxlen=1000)

    def predict(self):
        nums = [number for _, number in self.rows]
        seq = [int(number >= 5) for number in nums]
        
        # Basic logic for debug (No strict gates)
        if len(seq) < 10:
            p = 0.5
        else:
            recent = sum(seq[-10:]) / 10.0
            p = 0.5 + (recent - 0.5) * 0.5 # Simple trend following
            
        side = int(p >= 0.5)
        
        valid = list(range(5, 10) if side else range(5))
        number = valid[len(self.rows) % len(valid)] # Random-ish but side specific

        return {
            "p": p,
            "side": side,
            "number": number,
        }

    def observe(self, row):
        self.rows.append(row)

    def dump(self):
        return {"rows": list(self.rows)}

    @classmethod
    def restore(cls, data):
        model = cls()
        if data: model.rows.extend(tuple(r) for r in data["rows"])
        return model

class Database:
    def __init__(self):
        self.c = sqlite3.connect(CFG.db_path, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, issue INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, expires REAL NOT NULL, state INTEGER NOT NULL DEFAULT 0);
        """)
    @contextlib.contextmanager
    def transaction(self):
        self.c.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.c.execute("COMMIT")
        except: 
            self.c.execute("ROLLBACK")
            raise
    def get(self, key, default=None):
        row = self.c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
    def set(self, key, value):
        self.c.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    def queue(self, issue, kind, text, ttl):
        self.c.execute("INSERT INTO outbox(issue,kind,text,expires) VALUES (?,?,?,?)", (issue, kind, text, time.time() + ttl))

def prediction_text(issue, p):
    side = "BIG" if p["side"] else "SMALL"
    conf_percent = max(p["p"], 1 - p["p"]) * 100
    filled = int(conf_percent // 10)
    bar = "█" * filled + "░" * (10 - filled)
    color_emoji = "🔴" if side == "BIG" else "🔵"
    
    return (
        f"⚡️ <b>{esc(CFG.name)}</b> ⚡️\n"
        f"──────────────────\n"
        f"🎯 <b>TARGET:</b> {color_emoji} <b>{side}</b>  [ <code>{p['number']}</code> ]\n"
        f"📅 <b>PERIOD:</b> <code>{issue}</code>\n"
        f"📊 <b>CONFIDENCE:</b> {conf_percent:.1f}% [{bar}]\n"
        f"──────────────────"
    )

class Runtime:
    def __init__(self, db):
        self.db = db
        self.model = Model.restore(db.get("model"))
        self.stop = asyncio.Event()
        self.telegram_blocked = False

    async def pause(self, seconds):
        try: await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError: pass

    async def fetch(self, session):
        async def one(url):
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return parse_history(await response.json(content_type=None))
            except: return None

        sources = [r for r in await asyncio.gather(*(one(url) for url in CFG.urls)) if r]
        if not sources: raise RuntimeError("HistoryUnavailable")
        merged = {}
        for rows in sources:
            for issue, number in rows: merged[issue] = number
        return sorted(merged.items())[-1000:]

    def settle(self, records):
        for issue, number in records:
            row = self.db.c.execute("SELECT * FROM meta WHERE key=?", (f"pred_{issue}",)).fetchone()
            if not row: continue
            
            p = json.loads(row["value"])
            won = p["side"] == int(number >= 5)
            won_num = p["number"] == number
            
            # Delete prediction state
            self.db.c.execute("DELETE FROM meta WHERE key=?", (f"pred_{issue}",))
            
            if won and won_num: result_emoji = "🎰🎰🎰"
            elif won: result_emoji = "🏆"
            else: result_emoji = "❌"
            
            self.db.queue(issue, "result", result_emoji, ttl=3600)

    def process(self, records):
        before = self.model.rows[-1][0] if self.model.rows else None
        
        new_rows = [row for row in records if before is None or row[0] > before]

        with self.db.transaction():
            self.settle(records)

            for row in new_rows:
                self.model.observe(row)

            if new_rows:
                issue = self.model.rows[-1][0] + 1
                
                # ALWAYS PREDICT (No gates)
                p = self.model.predict()
                
                # Save prediction for result checking
                self.db.set(f"pred_{issue}", p)
                
                # Queue message
                self.db.queue(issue, "prediction", prediction_text(issue, p), CFG.delivery_ttl)

            self.db.set("model", self.model.dump())

    async def worker(self, session):
        while not self.stop.is_set():
            try:
                records = await self.fetch(session)
                self.process(records)
            except Exception as exc:
                LOG.error("Worker error: %s", exc)
            await self.pause(CFG.fetch_every)

    async def sender(self, session):
        endpoint = f"https://api.telegram.org/bot{CFG.token}/sendMessage"
        while not self.stop.is_set():
            if self.telegram_blocked:
                await self.pause(5)
                continue

            row = self.db.c.execute("SELECT * FROM outbox WHERE state=0 ORDER BY id LIMIT 1").fetchone()
            if row is None:
                await self.pause(0.5)
                continue

            if time.time() > row["expires"]:
                self.db.c.execute("UPDATE outbox SET state=2 WHERE id=?", (row["id"],))
                continue

            try:
                async with session.post(endpoint, json={"chat_id": CFG.chat, "text": row["text"], "parse_mode": "HTML"}) as response:
                    data = await response.json(content_type=None)
                    if response.status == 200 and data.get("ok"):
                        self.db.c.execute("UPDATE outbox SET state=1 WHERE id=?", (row["id"],))
                    else:
                        code = data.get("error_code", response.status)
                        LOG.error(f"Telegram Error: {code} - {data.get('description')}")
                        if code in (401, 403, 404):
                            self.telegram_blocked = True
                        self.db.c.execute("UPDATE outbox SET state=2 WHERE id=?", (row["id"],))
            except Exception as e:
                LOG.error(f"Network Error: {e}")
                await self.pause(5)

    async def health(self, request):
        return web.json_response({"status": "ok"})

async def main():
    CFG.validate()
    db = Database()
    runtime = Runtime(db)
    app = web.Application()
    app.router.add_get("/", runtime.health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CFG.port).start()

    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(runtime.worker(session)), asyncio.create_task(runtime.sender(session))]
        LOG.info("Debug Predictor Started - Guaranteed Messages")
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
