# bot.py — FTW VIP PREDICTOR (HYPER V16 - GOKU + Old Logic)
# Python 3.10+
# Dependency: pip install aiohttp

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
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass

import aiohttp
from aiohttp import web

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    token: str = "8611789455:AAFZH0iCkY5fceXQ77R18aptFZLPLKDRhbo"
    chat: str = "-1004485961768"
    urls: tuple = ("https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000",)
    
    db_path: str = os.getenv("DB_PATH", "predictor_hyper_v16.db")
    port: int = int(os.getenv("PORT", "8080"))
    fetch_every: float = float(os.getenv("FETCH_EVERY", "3"))
    timeout: float = 15.0
    max_history: int = 4000
    min_history: int = int(os.getenv("MIN_HISTORY", "120"))
    min_support: int = int(os.getenv("MIN_PATTERN_SUPPORT", "8"))
    
    # ✅ SIZE PATTERN RANGE (5 to 16) - Old Logic
    min_pattern_length: int = 5
    max_pattern: int = 16 
    
    # ✅ NUMBER PATTERN RANGE (2 to 10) - Old Logic
    min_number_length: int = 2
    max_number_length: int = 10
    
    learning_rate: float = 0.035
    meta_lr: float = 0.03
    decay: float = 0.985
    delivery_ttl: float = 40.0
    name: str = "FTW VIP PREDICTOR (HYPER)"
    
    win_sticker: str = "CAACAgUAAxkBAAFW0Ixq4QlioiYyx39xRe3akLzGvqXoSgACjg8AAg_PgFc259YBd5tjBj0E"
    loss_sticker: str = "CAACAgUAAxkBAAFW0JZq4Qr4QPStqiQjySoP1DQMvsFZawACVx0AAptrIVX6uzts6bbocD0E"
    jack_sticker: str = "CAACAgUAAxkBAAFW0Ipq4QlLbGgRu5R3TIIiSY_9z1uNuwACSxQAAu1zgVchvQvX59KsHD0E"

    def validate(self):
        if not self.token or not self.chat or not self.urls:
            raise ValueError("Missing required config: token, chat, or urls")

CFG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("predictor")

# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────
def clip(x, low=0.02, high=0.98):
    return max(low, min(high, float(x)))

def escape(x):
    return html.escape(str(x))

def numeric(value):
    if isinstance(value, bool): return None
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None

def parse_history(payload):
    obj = payload
    for _ in range(6):
        if isinstance(obj, list): break
        if not isinstance(obj, dict): return []
        obj = next((obj[k] for k in ("data", "list", "result", "items", "history") if isinstance(obj.get(k), (list, dict))), None)
    if not isinstance(obj, list): return []
    records = {}
    for item in obj:
        if not isinstance(item, dict): continue
        issue = numeric(item.get("issueNumber", item.get("issue")))
        number = numeric(item.get("number", item.get("value")))
        if issue is None or number is None or not 0 <= number <= 9: continue
        key = str(issue)
        if key in records and records[key]["num"] != number:
            raise ValueError(f"Conflicting duplicate: {key}")
        records[key] = {"issue": key, "num": number, "size": int(number >= 5)}
    return sorted(records.values(), key=lambda x: int(x["issue"]))

def weighted_probability(observations):
    sw = sum(w for _, w in observations)
    sw2 = sum(w * w for _, w in observations)
    effective_n = sw * sw / sw2 if sw2 else 0.0
    probability = (4 + sum(y * w for y, w in observations)) / (8 + sw)
    return probability, effective_n

def run_length(seq):
    if not seq: return 0
    n = 0
    for x in reversed(seq):
        if x != seq[-1]: break
        n += 1
    return n

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────
class Database:
    def __init__(self, path):
        self.c = sqlite3.connect(path, isolation_level=None, timeout=20)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS history(issue TEXT PRIMARY KEY, num INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS predictions(issue TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, actual INTEGER);
            CREATE TABLE IF NOT EXISTS models(key TEXT PRIMARY KEY, n REAL NOT NULL, loss REAL NOT NULL, inverse_loss REAL NOT NULL, failures INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, issue TEXT, text TEXT NOT NULL, created REAL NOT NULL, expires REAL, sent INTEGER NOT NULL DEFAULT 0);
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
        row = self.c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.c.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def ingest(self, records):
        with self.transaction():
            for r in records:
                old = self.c.execute("SELECT num FROM history WHERE issue=?", (r["issue"],)).fetchone()
                if old and old["num"] != r["num"]:
                    raise ValueError(f"Source changed settled issue {r['issue']}")
                self.c.execute("INSERT OR IGNORE INTO history VALUES (?,?)", (r["issue"], r["num"]))
            self.c.execute("DELETE FROM history WHERE issue NOT IN (SELECT issue FROM history ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?)", (CFG.max_history,))

    def history(self):
        rows = self.c.execute("SELECT issue,num FROM history ORDER BY CAST(issue AS INTEGER)").fetchall()
        return [{"issue": r["issue"], "num": r["num"], "size": int(r["num"] >= 5)} for r in rows]

    def memory(self, key):
        row = self.c.execute("SELECT * FROM models WHERE key=?", (key,)).fetchone()
        return dict(row) if row else {"n": 0.0, "loss": 0.0, "inverse_loss": 0.0, "failures": 0}

    def learn(self, key, probability, actual):
        m = self.memory(key)
        n = CFG.decay * m["n"] + 1
        loss = CFG.decay * m["loss"] + (probability - actual) ** 2
        inv = CFG.decay * m["inverse_loss"] + (1 - probability - actual) ** 2
        wrong = int(probability >= 0.5) != actual
        failures = m["failures"] + 1 if wrong else 0
        self.c.execute("INSERT INTO models VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET n=excluded.n,loss=excluded.loss,inverse_loss=excluded.inverse_loss,failures=excluded.failures", (key, n, loss, inv, failures))

    def queue(self, kind, issue, text, expires=None):
        self.c.execute("INSERT INTO outbox(kind,issue,text,created,expires) VALUES (?,?,?,?,?)", (kind, issue, text, time.time(), expires))

# ─────────────────────────────────────────────────────────────
# BASE ENGINE (Old Logic - Patterns & Settlement)
# ─────────────────────────────────────────────────────────────
class Engine:
    def __init__(self, db):
        self.db = db

    def get_runs(self, seq):
        if not seq: return []
        runs = []
        curr = seq[0]
        length = 1
        for x in seq[1:]:
            if x == curr: length += 1
            else:
                runs.append((curr, length))
                curr = x
                length = 1
        runs.append((curr, length))
        return runs

    def mine_family_patterns(self, seq):
        if not seq: return []
        runs = self.get_runs(seq)
        if len(runs) < 2: return []

        for k in range(min(CFG.max_pattern, len(runs)), 1, -1):
            target = runs[-k:]
            
            total_length = sum(length for _, length in target)
            if total_length < CFG.min_pattern_length or total_length > CFG.max_pattern:
                continue 

            observations = []
            for i in range(len(runs) - k - 1, -1, -1):
                window = runs[i:i+k]
                if window == target:
                    if i + k < len(runs):
                        next_run = runs[i + k]
                        observations.append((next_run[0], 1.0))
                        
            if observations:
                p, eff_n = weighted_probability(observations)
                bits = "-".join(f"{'B' if v else 'S'}{c}" for v, c in target)
                key = f"family:{bits}"
                rank = abs(p - 0.5) * math.sqrt(eff_n) + 10.0 
                return [(rank, p, len(observations), eff_n, key)]
        return []

    def mine_patterns(self, seq):
        n = len(seq)
        family_cands = self.mine_family_patterns(seq)
        if family_cands: return family_cands

        candidates = {}
        def weight(idx): return 0.5 ** ((n - 1 - idx) / 300.0)

        for length in range(CFG.min_pattern_length, CFG.max_pattern + 1):
            if n < length * 2: continue
            suffix = tuple(seq[-length:])
            obs, last = [], -length - 1
            for i in range(length, n - length):
                if tuple(seq[i - length:i]) == suffix and i - last >= length + 1:
                    obs.append((seq[i], weight(i))); last = i
            if len(obs) >= CFG.min_support:
                p, eff_n = weighted_probability(obs)
                if eff_n >= CFG.min_support * 0.5:
                    bits = "".join("B" if x else "S" for x in suffix)
                    key = f"exact:{bits}"
                    candidates[key] = (abs(p - 0.5) * math.sqrt(eff_n), p, len(obs), eff_n, key)

        return sorted(candidates.values(), key=lambda x: x[0], reverse=True)[:8]

    def settle(self, history):
        lookup = {r["issue"]: r for r in history}
        num_ngrams, num_freq = {}, {}
        for i in range(1, len(history)):
            curr_num = history[i]["num"]
            num_freq[str(curr_num)] = num_freq.get(str(curr_num), 0) + 1
            for length in range(CFG.min_number_length, CFG.max_number_length + 1):
                if i >= length:
                    seq_key = ",".join(str(history[j]["num"]) for j in range(i-length, i))
                    if seq_key not in num_ngrams: num_ngrams[seq_key] = {}
                    num_ngrams[seq_key][str(curr_num)] = num_ngrams[seq_key].get(str(curr_num), 0) + 1
        self.db.set("num_ngrams", num_ngrams)
        self.db.set("num_freq", num_freq)

        pending = self.db.c.execute("SELECT issue,payload FROM predictions WHERE status='PENDING' ORDER BY CAST(issue AS INTEGER)").fetchall()
        for row in pending:
            actual = lookup.get(row["issue"])
            if actual is None: continue
            p = json.loads(row["payload"])
            y = actual["size"]
            won_side = int(p["p"] >= 0.5) == y
            won_num = p.get("num") == actual["num"]
            
            with self.db.transaction():
                self.db.c.execute("UPDATE predictions SET status=?,actual=? WHERE issue=?", ("WIN" if won_side else "LOSS", actual["num"], row["issue"]))
                for pat in p.get("patterns", []): self.db.learn(pat["key"], pat["p"], y)

                if won_side and won_num: sticker = CFG.jack_sticker
                elif won_side: sticker = CFG.win_sticker
                else: sticker = CFG.loss_sticker
                
                self.db.queue("sticker", row["issue"], sticker)
                self.db.c.execute("UPDATE outbox SET sent=2 WHERE kind='prediction' AND issue=? AND sent=0", (row["issue"],))


# ─────────────────────────────────────────────────────────────
# UPGRADED ENGINE (GOKU MODES Triggers + Old Size/Number Logic)
# ─────────────────────────────────────────────────────────────
class UpgradedEngine(Engine):
    def __init__(self, db):
        super().__init__(db)
        # GOKU MODES Special Triggers
        self.sureshot_ngrams = {
            'BBBSBBSSBSBBBBS': 'SMALL', 'BBBBBBBB': 'SMALL', 'SSSSSSSS': 'BIG',
            'SBSBSBSB': 'SMALL', 'BSBSBSBS': 'BIG', 'BBSSBBSS': 'BIG',
            'SSBBSSBB': 'SMALL', 'BBBSSSB': 'BIG', 'SSSBBBS': 'SMALL',
            'BSBBSB': 'BIG', 'SBSBBS': 'SMALL', 'BBBBSB': 'BIG', 'SSSSBS': 'SMALL'
        }
        self.digit_matrix = {
            0: [5, 2, 8, 4, 1], 1: [6, 3, 7, 9, 2], 2: [7, 4, 8, 0, 3],
            3: [8, 5, 9, 1, 4], 4: [9, 6, 0, 2, 5], 5: [0, 7, 3, 5, 8],
            6: [1, 8, 4, 6, 9], 7: [2, 9, 5, 1, 0], 8: [3, 0, 6, 2, 7],
            9: [4, 1, 7, 3, 8]
        }

    def get_number_distribution(self, nums):
        ngram_db = self.db.get("num_ngrams", {})
        dist = [1.0] * 10 
        for length in range(min(CFG.max_number_length, len(nums)), CFG.min_number_length - 1, -1):
            seq_key = ",".join(str(x) for x in nums[-length:])
            if seq_key in ngram_db:
                trans = ngram_db[seq_key]
                total = sum(trans.values())
                if total >= 5: 
                    for k, v in trans.items():
                        dist[int(k)] += (v / total) * length 
                    break
        total = sum(dist)
        return [d / total for d in dist]

    def detect_special_trigger(self, nums):
        if len(nums) < 2: return None
        n1, n2 = nums[-1], nums[-2]
        
        if n1 == 5 and n2 == 5: return {"side": "BIG", "conf": 0.986, "name": "DOUBLE 5 VIOLET BRIDGE"}
        if n1 == 4 and n2 == 4: return {"side": "SMALL", "conf": 0.984, "name": "DOUBLE 4 RED PIVOT"}
        if n1 == 0 and n2 in (0, 5): return {"side": "BIG", "conf": 0.982, "name": "ZERO VIOLET GATE"}
        if n1 == 9 and n2 == 9: return {"side": "SMALL", "conf": 0.985, "name": "9-9 CEILING BOUNCE"}
        return None

    def detect_dragon_streak(self, seq_str):
        streak = run_length([1 if c == "B" else 0 for c in seq_str])
        current = seq_str[-1]
        flip = "S" if current == "B" else "B"
        
        if streak >= 10: return {"side": flip, "conf": 0.985, "name": f"TITAN STREAK ({streak}x)"}
        if streak >= 8: return {"side": flip, "conf": 0.950, "name": f"SUPER DRAGON ({streak}x)"}
        if streak >= 6: return {"side": flip, "conf": 0.750, "name": f"MEGA DRAGON ({streak}x)"}
        if streak >= 3: return {"side": current, "conf": 0.650, "name": f"MOMENTUM DRAGON ({streak}x)"}
        return None

    def detect_zigzag(self, seq_str):
        if len(seq_str) < 6: return None
        zig = 1
        for i in range(len(seq_str)-1, 0, -1):
            if seq_str[i] != seq_str[i-1]: zig += 1
            else: break
        if zig >= 7:
            return {"side": seq_str[-1], "conf": 0.968, "name": f"ZIGZAG EXHAUSTION ({zig}x)"}
        return None

    def predict(self, history, issue):
        seq = [r["size"] for r in history]
        nums = [r["num"] for r in history]
        seq_str = "".join("B" if s == 1 else "S" for s in seq)
        
        # 1. Old Logic: Size Patterns (5-16)
        patterns = self.mine_patterns(seq)
        p_pattern_big = 0.5
        pat_str = "None"
        
        if patterns:
            top_p = patterns[0]
            raw_p = top_p[1]
            support = top_p[2]
            p_type = top_p[4].split(":")[0]
            bits = top_p[4].split(":")[-1]
            arrow = "⬆️" if raw_p >= 0.5 else "⬇️"
            
            if p_type == "family": pat_str = f"👨‍👩‍👧‍👦 <code>{bits}</code> {arrow}"
            else: pat_str = f"<code>{bits}</code> {arrow}"
            
            weight_factor = support / (support + 5.0) 
            p_pattern_big = 0.5 + (raw_p - 0.5) * weight_factor

        # 2. Old Logic: Number Distribution (2-10)
        num_dist = self.get_number_distribution(nums)
        p_num_big = sum(num_dist[5:])
        
        # 3. GOKU MODES Triggers
        goku_triggers = []
        
        special = self.detect_special_trigger(nums)
        if special: goku_triggers.append(special)
        
        for pattern, pred in self.sureshot_ngrams.items():
            if seq_str.endswith(pattern):
                goku_triggers.append({"side": "B" if pred == "BIG" else "S", "conf": 0.98, "name": f"N-GRAM {pattern}"})
                break
                
        dragon = self.detect_dragon_streak(seq_str)
        if dragon: goku_triggers.append(dragon)
            
        zigzag = self.detect_zigzag(seq_str)
        if zigzag: goku_triggers.append(zigzag)
        
        # 4. Final Arbitration (Strict Priority)
        final_side = None
        final_conf = 0.0
        source_name = "⚖️ Neutral"
        
        # Priority A: GOKU Triggers (If very strong)
        if goku_triggers:
            goku_triggers.sort(key=lambda x: x["conf"], reverse=True)
            if goku_triggers[0]["conf"] >= 0.75: # Only take if strong
                best = goku_triggers[0]
                final_side = best["side"]
                final_conf = best["conf"]
                source_name = f"🎯 {best['name']}"
                
        # Priority B: Size Pattern (Old Logic)
        if not final_side and pat_str != "None" and abs(p_pattern_big - 0.5) >= 0.08:
            final_side = "B" if p_pattern_big >= 0.5 else "S"
            final_conf = max(p_pattern_big, 1 - p_pattern_big)
            source_name = "🧩 Strong Size Pattern"
            
        # Priority C: Number Pattern (Old Logic)
        if not final_side:
            final_side = "B" if p_num_big >= 0.5 else "S"
            final_conf = max(p_num_big, 1 - p_num_big)
            source_name = "🎯 Number Pattern"
            
        # Fallback if everything is 50-50
        if final_conf < 0.52:
            final_side = seq_str[-1]
            final_conf = 0.51
            source_name = "🛡️ Streak Fallback"

        # 5. Map to BIG/SMALL and select best digit
        pred_side = "BIG" if final_side == "B" else "SMALL"
        
        last_num = nums[-1]
        cluster = self.digit_matrix.get(last_num, [7, 8, 2, 3])
        
        if pred_side == "BIG":
            valid_nums = [n for n in cluster if n >= 5]
            if not valid_nums: valid_nums = [n for n in range(5, 10) if num_dist[n] == max(num_dist[5:])]
            best_num = max(valid_nums, key=lambda x: num_dist[x])
        else:
            valid_nums = [n for n in cluster if n < 5]
            if not valid_nums: valid_nums = [n for n in range(0, 5) if num_dist[n] == max(num_dist[:5])]
            best_num = max(valid_nums, key=lambda x: num_dist[x])

        return {
            "issue": issue, 
            "p": final_conf if pred_side == "BIG" else 1.0 - final_conf,
            "side": pred_side,
            "num": best_num, 
            "pattern_big_prob": p_pattern_big,
            "number_big_prob": p_num_big,
            "source": source_name,
            "pattern_str": pat_str,
            "patterns": [{"key": k, "p": p, "support": s} for r, p, s, e, k in patterns]
        }

# ─────────────────────────────────────────────────────────────
# FORMATTER (Clean UI with Overruled & High VIP)
# ─────────────────────────────────────────────────────────────
def format_prediction(p):
    conf = max(p["p"], 1 - p["p"]) * 100
    filled = int(conf // 10)
    bar = "█" * filled + "░" * (10 - filled)
    side = "BIG" if p["side"] == "BIG" else "SMALL"
    color = "🔴" if side == "BIG" else "🔵"
    
    quality = "🔥 HIGH VIP" if conf >= 65 else "⭐ VIP" if conf >= 60 else "✅ NORMAL"
    stake = "1.5x" if conf >= 65 else "1x" if conf >= 60 else "0.5x"
    
    pat_text = ""
    if p.get("pattern_str") and p.get("pattern_str") != "None":
        pat_str = p.get('pattern_str', 'None')
        if "⬆️" in pat_str and side == "SMALL": pat_str += " ❌ (Overruled)"
        elif "⬇️" in pat_str and side == "BIG": pat_str += " ❌ (Overruled)"
        pat_text = f"\n🧩 <b>PATTERN:</b> {pat_str}"

    source_text = f"\n🧠 <b>SOURCE:</b> {p.get('source', 'Unknown')}"
    
    pat_big = p.get("pattern_big_prob", 0.5)
    pat_side = "BIG" if pat_big >= 0.5 else "SMALL"
    pat_prob = pat_big if pat_big >= 0.5 else 1.0 - pat_big
    
    num_big = p.get("number_big_prob", 0.5)
    num_side = "BIG" if num_big >= 0.5 else "SMALL"
    num_prob = num_big if num_big >= 0.5 else 1.0 - num_big
    
    breakdown_text = f"\n📊 <b>BREAKDOWN:</b> Pattern ({pat_side} {pat_prob*100:.0f}%) | Num ({num_side} {num_prob*100:.0f}%)"

    return (
        f"⚡️ <b>{escape(CFG.name)}</b> ⚡️\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>TARGET:</b> {color} <b>{side}</b> → <code>{p['num']}</code>\n"
        f"📅 <b>PERIOD:</b> <code>{escape(p['issue'])}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>CONFIDENCE:</b> {conf:.1f}% [{bar}]\n"
        f"🏆 <b>QUALITY:</b> {quality} (Stake: {stake})\n"
        f"{source_text}{breakdown_text}{pat_text}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

# ─────────────────────────────────────────────────────────────
# RUNTIME
# ─────────────────────────────────────────────────────────────
class Runtime:
    def __init__(self, db):
        self.db = db
        self.engine = UpgradedEngine(db)
        self.stop = asyncio.Event()
        self.last_fetch = 0.0
        self.error = None

    async def pause(self, seconds):
        try: await asyncio.wait_for(self.stop.wait(), timeout=max(0.05, seconds))
        except asyncio.TimeoutError: pass

    async def fetch(self, session):
        async def one(url):
            try:
                async with session.get(url) as r:
                    r.raise_for_status()
                    return parse_history(await r.json(content_type=None))
            except: return []
        sources = [x for x in await asyncio.gather(*(one(u) for u in CFG.urls)) if x]
        if not sources: raise RuntimeError("HistoryUnavailable")
        merged = {}
        for rows in sources:
            for row in rows:
                if row["issue"] in merged and merged[row["issue"]]["num"] != row["num"]: raise ValueError("SourceConflict")
                merged[row["issue"]] = row
        return sorted(merged.values(), key=lambda x: int(x["issue"]))[-CFG.max_history:]

    async def worker(self, session):
        failures = 0
        while not self.stop.is_set():
            try:
                records = await self.fetch(session)
                self.db.ingest(records)
                history = self.db.history()
                self.last_fetch = time.time()
                self.error = None
                failures = 0

                self.engine.settle(history)

                latest = int(history[-1]["issue"])
                seen = self.db.get("latest_seen")
                self.db.set("latest_seen", latest)

                if len(history) >= CFG.min_history:
                    issue = str(latest + 1)
                    exists = self.db.c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone()
                    if not exists:
                        p = self.engine.predict(history, issue)
                        with self.db.transaction():
                            self.db.c.execute("INSERT INTO predictions VALUES (?,?,?,?,NULL)", (issue, time.time(), "PENDING", json.dumps(p)))
                            self.db.queue("prediction", issue, format_prediction(p), time.time() + CFG.delivery_ttl)

            except asyncio.CancelledError: raise
            except Exception as exc:
                self.error = type(exc).__name__
                failures += 1
                LOG.error("Worker failure: %s", self.error)

            await self.pause(min(60, CFG.fetch_every * 2 ** min(failures, 4)))

    async def sender(self, session):
        endpoint_msg = f"https://api.telegram.org/bot{CFG.token}/sendMessage"
        endpoint_sticker = f"https://api.telegram.org/bot{CFG.token}/sendSticker"
        backoff = 1.0
        
        while not self.stop.is_set():
            self.db.c.execute("UPDATE outbox SET sent=2 WHERE sent=0 AND expires <= ?", (time.time(),))
            
            row = self.db.c.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id ASC LIMIT 1").fetchone()
            if not row:
                await self.pause(0.5)
                continue

            delay = backoff
            try:
                if row["kind"] == "sticker":
                    url = endpoint_sticker
                    payload = {"chat_id": CFG.chat, "sticker": row["text"]}
                else:
                    url = endpoint_msg
                    payload = {"chat_id": CFG.chat, "text": row["text"], "parse_mode": "HTML"}

                async with session.post(url, json=payload) as response:
                    data = await response.json(content_type=None)
                    if response.status == 200 and data.get("ok"):
                        self.db.c.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
                        backoff = 1.0
                        continue
                    
                    code = data.get("error_code", response.status)
                    if code == 429: delay = float(data.get("parameters", {}).get("retry_after", 10))
                    elif code in (400, 401, 403, 404): 
                        delay = 60
                        if code == 400 and row["kind"] == "sticker":
                            LOG.error(f"Invalid Sticker ID: {row['text']}")
                            self.db.c.execute("UPDATE outbox SET sent=2 WHERE id=?", (row["id"],))
                            continue
                            
            except asyncio.CancelledError: raise
            except Exception as e: LOG.warning(f"Telegram fail: {e}")
            
            await self.pause(delay)
            backoff = min(60.0, backoff * 2)

    async def health(self, request):
        return web.json_response({"status": "ok" if self.last_fetch and time.time() - self.last_fetch < 90 else "degraded", "history": self.db.c.execute("SELECT COUNT(*) FROM history").fetchone()[0]})

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
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
        connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, ssl=False, family=socket.AF_INET)
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks = [
                asyncio.create_task(runtime.worker(session), name="worker"),
                asyncio.create_task(runtime.sender(session), name="sender"),
            ]
            stop_task = asyncio.create_task(runtime.stop.wait())
            LOG.info("HYPER V16 Predictor Started (GOKU + Old Logic)")

            await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            runtime.stop.set()

            for task in tasks:
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc: raise exc

            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    finally:
        runtime.stop.set()
        for task in tasks: task.cancel()
        if stop_task: stop_task.cancel()
        await asyncio.gather(*tasks, *([stop_task] if stop_task else []), return_exceptions=True)
        await runner.cleanup()
        db.c.close()

if __name__ == "__main__":
    asyncio.run(main())
