# bot.py — FTW VIP PREDICTOR (HYPER V17 - Deep Aggressive Upgrade)
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
def _secret(name):
    """Token / chat id come from environment variables or a secrets.json next to this file - never hard-coded."""
    v = os.getenv(name, "").strip()
    if v: return v
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.json"), encoding="utf-8") as f:
            return str(json.load(f).get(name, "")).strip()
    except Exception:
        return ""

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
    min_history: int = int(os.getenv("MIN_HISTORY", "150"))
    min_support: int = int(os.getenv("MIN_PATTERN_SUPPORT", "8"))
    learning_rate: float = 0.035
    meta_lr: float = 0.03
    decay: float = 0.985
    max_pattern: int = 15 
    delivery_ttl: float = 40.0
    name: str = "FTW VIP PREDICTOR (HYPER)"
    
    # ✅ Tumhare Stickers
    win_sticker: str = "CAACAgUAAxkBAAFW0Ixq4QlioiYyx39xRe3akLzGvqXoSgACjg8AAg_PgFc259YBd5tjBj0E"
    loss_sticker: str = "CAACAgUAAxkBAAFW0JZq4Qr4QPStqiQjySoP1DQMvsFZawACVx0AAptrIVX6uzts6bbocD0E"
    jack_sticker: str = "CAACAgUAAxkBAAFW0Ipq4QlLbGgRu5R3TIIiSY_9z1uNuwACSxQAAu1zgVchvQvX59KsHD0E"

    def validate(self):
        if not self.token or not self.chat or not self.urls:
            raise ValueError("BOT_TOKEN / BOT_CHAT missing: set them as environment variables or in secrets.json ({\"BOT_TOKEN\": \"...\", \"BOT_CHAT\": \"...\"}) next to bot.py")

CFG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("predictor")

# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────
def clip(x, low=0.02, high=0.98):
    return max(low, min(high, float(x)))

def logit(p, eps=0.02):
    p = max(eps, min(1 - eps, float(p)))
    return math.log(p / (1 - p))

def sigmoid(z):
    z = max(-20.0, min(20.0, float(z)))
    return 1.0 / (1.0 + math.exp(-z))

def escape(x):
    return html.escape(str(x))

def run_length(seq):
    if not seq: return 0
    n = 0
    for x in reversed(seq):
        if x != seq[-1]: break
        n += 1
    return n

def get_regime(seq):
    tail = seq[-24:]
    if len(tail) < 8: return "COLD"
    flips = sum(a != b for a, b in zip(tail, tail[1:]))
    rate = flips / (len(tail) - 1)
    if run_length(tail) >= 4: return "RUN"
    if rate >= 0.70: return "ALT"
    if abs(sum(tail) / len(tail) - 0.5) > 0.20: return "IMBAL"
    return "MIXED"

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

def trend_features(seq):
    if not seq: return [0.0] * 32
    def mean(k):
        arr = seq[-k:]
        return sum(arr) / len(arr) if arr else 0.5
    def ema(k):
        if not seq: return 0.5
        alpha = 2.0 / (k + 1)
        val = float(seq[0])
        for x in seq[1:]: val = alpha * x + (1 - alpha) * val
        return val
    def rsi(period=14):
        if len(seq) < period + 1: return 50.0
        gains = losses = 0.0
        for i in range(-period, 0):
            change = seq[i] - seq[i - 1]
            if change > 0: gains += change
            else: losses -= change
        if losses == 0: return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))
    def std(k):
        arr = seq[-k:]
        if len(arr) < 2: return 0.0
        m = sum(arr) / len(arr)
        return math.sqrt(sum((x - m) ** 2 for x in arr) / len(arr))
    
    tail = seq[-24:]
    alternation = sum(a != b for a, b in zip(tail, tail[1:])) / max(1, len(tail) - 1)
    signed_run = (2 * seq[-1] - 1) * min(run_length(seq), 10) / 10.0
    rsi_n = (rsi(14) / 100.0) * 2.0 - 1.0
    macd = (ema(12) - ema(26)) * 2.0
    autocorr = 0.0
    if len(seq) >= 25 and std(24) > 0:
        m24 = mean(24)
        num = sum((seq[i] - m24) * (seq[i - 1] - m24) for i in range(-23, 0))
        den = 24.0 * std(24) ** 2
        autocorr = num / den if den > 0 else 0.0

    return [
        1.0, 2*mean(3)-1, 2*mean(5)-1, 2*mean(10)-1, 2*mean(24)-1, 2*mean(48)-1, 2*mean(96)-1,
        mean(3)-mean(10), mean(5)-mean(24), mean(10)-mean(48), signed_run, 2*seq[-1]-1,
        (2*alternation-1)*(2*seq[-1]-1), rsi_n, macd, (2*std(10)-1), (2*std(24)-1),
        (sum(x * math.log2(x) for x in [sum(seq[-24:])/24, 1-(sum(seq[-24:])/24)] if x > 0) * -1) - 1,
        (sum(x * math.log2(x) for x in [sum(seq[-48:])/48, 1-(sum(seq[-48:])/48)] if x > 0) * -1) - 1,
        autocorr, 1.0 if mean(5)>0.5 else -1.0,
        (2*seq[-1]-1)*(2*mean(24)-1), abs(mean(24)-0.5)*2, (2*seq[-1]-1)*(mean(10)-mean(48)),
        (2*mean(10)-1)/(std(10)+0.1), (2*mean(24)-1)/(std(24)+0.1), (sum(seq[-10:])/10.0)-0.5,
        (sum(seq[-5:])/5.0)-0.5, (2*mean(5)-1)*(2*mean(24)-1), math.tanh((mean(10)-0.5)*6),
        math.tanh((mean(48)-0.5)*6), (2*mean(3)-1)*(2*mean(96)-1),
    ]

EXPERT_NAMES = ["patterns", "markov1", "markov2", "markov3", "markov4", "run", "regime", "base", "neighbors", "trend", "momentum", "cyclic"]
def logistic(weights, features): return sigmoid(sum(w * x for w, x in zip(weights, features)))

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

    def adapt(self, key, probability):
        m = self.memory(key)
        n = m["n"]
        if n < 1: return probability, 0.65, False
        
        brier = m["loss"] / n
        inverse_brier = m["inverse_loss"] / n
        
        flip_due_to_failures = (m["failures"] >= 2)
        flip_due_to_loss = (n >= 25 and inverse_brier + 0.04 < brier and inverse_brier < 0.245)
        
        flipped = flip_due_to_failures or flip_due_to_loss
        effective_loss = inverse_brier if flipped else brier
        p = 1 - probability if flipped else probability

        if p > 0.80 and effective_loss > 0.22: p = 0.5 - (p - 0.5) * 0.5 
        elif p < 0.20 and effective_loss > 0.22: p = 0.5 + (0.5 - p) * 0.5

        reliability = clip((0.28 - effective_loss) / 0.08, 0.0, 1.0)
        maturity = min(n / 30.0, 1.0)
        shrink = (1 - maturity) * 0.65 + maturity * reliability
        p = 0.5 + (p - 0.5) * shrink
        
        weight = clip(math.exp(-8 * (effective_loss - 0.25)), 0.15, 2.0)
        if m["failures"] >= 3 and not flipped: weight *= 0.35
        return clip(p), weight, flipped

    def queue(self, kind, issue, text, expires=None):
        self.c.execute("INSERT INTO outbox(kind,issue,text,created,expires) VALUES (?,?,?,?,?)", (kind, issue, text, time.time(), expires))

# ─────────────────────────────────────────────────────────────
# ENGINE (Cross-Engine Arbitration) - Base Engine (100% Preserved)
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

        for k in range(min(6, len(runs)), 1, -1):
            target = runs[-k:]
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

        for length in range(5, CFG.max_pattern + 1):
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

    def mine_experts(self, seq):
        n = len(seq)
        experts = {}
        def weight(idx): return 0.5 ** ((n - 1 - idx) / 300.0)

        patterns = self.mine_patterns(seq)
        if patterns:
            num = den = 0.0
            for _, p, _, eff_n, key in patterns:
                adjusted, w, _ = self.db.adapt(key, p)
                num += adjusted * (w * math.sqrt(eff_n))
                den += (w * math.sqrt(eff_n))
            if den > 0: experts["patterns"] = num / den

        for order in (1, 2, 3, 4):
            suffix = tuple(seq[-order:])
            obs = [(seq[i], weight(i)) for i in range(order, n - order) if tuple(seq[i - order:i]) == suffix]
            if len(obs) >= CFG.min_support: experts[f"markov{order}"] = weighted_probability(obs)[0]

        current_run = min(run_length(seq), 8)
        run_obs = [(seq[i], weight(i)) for i in range(24, n) if seq[max(0, i-96):i] and seq[max(0, i-96):i][-1] == seq[-1] and min(run_length(seq[max(0, i-96):i]), 8) == current_run]
        if len(run_obs) >= CFG.min_support: experts["run"] = weighted_probability(run_obs)[0]

        current_regime = get_regime(seq)
        regime_obs = [(seq[i], weight(i)) for i in range(24, n) if get_regime(seq[max(0, i-96):i]) == current_regime]
        if len(regime_obs) >= CFG.min_support: experts["regime"] = weighted_probability(regime_obs)[0]

        experts["base"] = weighted_probability([(y, weight(i)) for i, y in enumerate(seq)])[0]

        nearest = [(seq[i], weight(i) * 0.4 ** sum(a != b for a, b in zip(seq[i-10:i], seq[-10:]))) for i in range(10, n - 10) if sum(a != b for a, b in zip(seq[i-10:i], seq[-10:])) <= 2]
        if len(nearest) >= 20: experts["neighbors"] = weighted_probability(nearest)[0]

        features = trend_features(seq)
        tw = self.db.get("trend_weights")
        if not isinstance(tw, list) or len(tw) != len(features): tw = [0.0] * len(features)
        experts["trend"] = logistic(tw, features)

        momentum = sum(seq[-5:]) / 5.0 if seq else 0.5
        experts["momentum"] = 0.2 + (momentum * 0.6)

        return experts, patterns, features, current_regime

    def _predict_number(self, seq_nums):
        ngram_db = self.db.get("num_ngrams", {})
        
        best_num = None
        best_prob = 0.0
        
        for length in range(min(10, len(seq_nums)), 1, -1):
            seq_key = ",".join(str(x) for x in seq_nums[-length:])
            if seq_key in ngram_db:
                trans = ngram_db[seq_key]
                total_occurrences = sum(trans.values())
                if total_occurrences > 0:
                    max_num = max(trans, key=trans.get)
                    max_prob = trans[max_num] / total_occurrences
                    
                    if max_prob > best_prob:
                        best_prob = max_prob
                        best_num = int(max_num)
                        
                    if best_prob >= 0.70:
                        break
                        
        if best_num is not None:
            return best_num, best_prob
            
        freq = self.db.get("num_freq", {})
        if freq:
            total_freq = sum(freq.values())
            max_num = max(freq, key=freq.get)
            return int(max_num), freq[max_num] / total_freq
            
        return random.randint(0, 9), 0.10

    def predict(self, history, issue):
        seq = [r["size"] for r in history]
        nums = [r["num"] for r in history]
        
        experts, patterns, features, current_regime = self.mine_experts(seq)
        current_streak = run_length(seq)

        adjusted = []
        for name, p in experts.items():
            corrected, weight, _ = self.db.adapt(f"expert:{name}:{current_regime}", p)
            
            if name == "patterns": weight *= 2.5
            if current_streak >= 4 and name in ("momentum", "run"): weight *= 3.0
            if name == "trend" and current_regime in ("RUN", "IMBAL"): weight *= 1.4
            adjusted.append((corrected, weight))

        total_w = sum(w for _, w in adjusted)
        p_avg = sum(p * w for p, w in adjusted) / total_w if total_w > 0 else 0.5
        
        consensus = max(sum(1 for p,_ in adjusted if p>=0.5), sum(1 for p,_ in adjusted if p<0.5)) / len(adjusted)
        if consensus > 0.85: p_avg = 0.5 + (p_avg - 0.5) * 0.7

        m_feats = [logit(clip(experts.get(n, 0.5), 0.05, 0.95)) for n in EXPERT_NAMES] + [1.0]
        mw = self.db.get("meta_weights")
        if not isinstance(mw, list) or len(mw) != len(m_feats): mw = [0.0] * len(m_feats)
        p_meta = sigmoid(sum(w * f for w, f in zip(mw, m_feats)))
        
        alpha = min(self.db.get("meta_n", 0) / 80.0, 1.0)
        p_raw = alpha * p_meta + (1 - alpha) * p_avg

        if current_streak >= 4:
            trend_val = float(seq[-1])
            p_raw = 0.90 * trend_val + 0.10 * p_raw

        bucket = min(19, int(p_raw * 20))
        n_g, s_g = self.db.get(f"cal20:global:{bucket}", [0.0, 0.0])
        p_cal = clip((30 * p_raw + s_g) / (30 + n_g))

        pat_str = "None"
        if patterns:
            top_p = patterns[0]
            p_pat = top_p[1]
            p_type = top_p[4].split(":")[0]
            bits = top_p[4].split(":")[-1]
            arrow = "⬆️" if p_pat >= 0.5 else "⬇️"
            
            if p_type == "family": pat_str = f"👨‍👩‍👧‍👦 <code>{bits}</code> {arrow}"
            else: pat_str = f"<code>{bits}</code> {arrow}"

            if p_pat >= 0.55:
                p_cal = max(p_cal, 0.60) 
            elif p_pat <= 0.45:
                p_cal = min(p_cal, 0.40) 

        num_pred, num_conf = self._predict_number(nums)
        size_conf = max(p_cal, 1 - p_cal)
        
        source = "📊 Size Engine"
        
        if num_conf > size_conf + 0.05:
            source = "🎯 Number Engine"
            if num_pred >= 5: 
                p_cal = max(p_cal, num_conf)
            else: 
                p_cal = min(p_cal, 1 - num_conf)
                
            pred_side = "BIG" if num_pred >= 5 else "SMALL"
        else:
            pred_side = "BIG" if p_cal >= 0.5 else "SMALL"
            valid = list(range(5, 10)) if pred_side == "BIG" else list(range(0, 5))
            if num_pred not in valid:
                ngram_db = self.db.get("num_ngrams", {})
                for length in range(min(10, len(nums)), 1, -1):
                    seq_key = ",".join(str(x) for x in nums[-length:])
                    if seq_key in ngram_db:
                        trans = ngram_db[seq_key]
                        valid_trans = {int(k): v for k, v in trans.items() if int(k) in valid}
                        if valid_trans:
                            num_pred = max(valid_trans, key=valid_trans.get)
                            break
                else:
                    num_pred = random.choice(valid)

        prev_regime = get_regime(seq[:-1]) if len(seq) > 1 else "N/A"
        trend_shift = f"{prev_regime} ➡️ {current_regime}" if prev_regime != current_regime else None

        return {
            "issue": issue, "p": p_cal, "p_raw": p_raw, "side": pred_side,
            "num": num_pred, "regime": current_regime, "source": source,
            "trend_shift": trend_shift, "pattern_str": pat_str,
            "experts": experts, "features": features, "meta_feats": m_feats,
            "patterns": [{"key": k, "p": p, "support": s} for r, p, s, e, k in patterns]
        }

    def settle(self, history):
        lookup = {r["issue"]: r for r in history}
        
        num_ngrams = {}
        num_freq = {}
        for i in range(1, len(history)):
            curr_num = history[i]["num"]
            num_freq[str(curr_num)] = num_freq.get(str(curr_num), 0) + 1
            
            for length in range(2, 11):
                if i >= length:
                    seq_key = ",".join(str(history[j]["num"]) for j in range(i-length, i))
                    if seq_key not in num_ngrams:
                        num_ngrams[seq_key] = {}
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
                for name, val in p["experts"].items(): self.db.learn(f"expert:{name}:{p['regime']}", val, y)
                for pat in p["patterns"]: self.db.learn(pat["key"], pat["p"], y)
                
                if p.get("features") and p["experts"].get("trend"):
                    err = y - p["experts"]["trend"]
                    tw = self.db.get("trend_weights") or [0.0]*len(p["features"])
                    self.db.set("trend_weights", [clip(w + CFG.learning_rate * (err * x - 0.01 * w), -4, 4) for w, x in zip(tw, p["features"])])
                
                if p.get("meta_feats"):
                    mw = self.db.get("meta_weights") or [0.0]*len(p["meta_feats"])
                    mp = sigmoid(sum(w * f for w, f in zip(mw, p["meta_feats"])))
                    grad = 2.0 * (y - mp) * mp * (1.0 - mp)
                    self.db.set("meta_weights", [clip(w * 0.999 + CFG.meta_lr * grad * f, -4, 4) for w, f in zip(mw, p["meta_feats"])])
                    self.db.set("meta_n", self.db.get("meta_n", 0) + 1)

                bucket = min(19, int(p.get("p_raw", p["p"]) * 20))
                n, s = self.db.get(f"cal20:global:{bucket}", [0.0, 0.0])
                self.db.set(f"cal20:global:{bucket}", [0.995 * n + 1, 0.995 * s + y])

                if won_side and won_num: sticker = CFG.jack_sticker
                elif won_side: sticker = CFG.win_sticker
                else: sticker = CFG.loss_sticker
                
                self.db.queue("sticker", row["issue"], sticker)
                self.db.c.execute("UPDATE outbox SET sent=2 WHERE kind='prediction' AND issue=? AND sent=0", (row["issue"],))


# ─────────────────────────────────────────────────────────────
# UPGRADED ENGINE (Evidence-Based)
# ─────────────────────────────────────────────────────────────
class UpgradedEngine(Engine):
    """Evidence-based forecasting"""
    
    @staticmethod
    def normalize(values):
        total = sum(values)
        if total <= 0: return [1.0 / len(values)] * len(values)
        return [v / total for v in values]

    def get_number_distribution(self, nums):
        """Creates a 0-9 probability distribution based on historical n-grams"""
        ngram_db = self.db.get("num_ngrams", {})
        dist = [1.0] * 10 # Base uniform smoothing
        
        for length in range(min(10, len(nums)), 1, -1):
            seq_key = ",".join(str(x) for x in nums[-length:])
            if seq_key in ngram_db:
                trans = ngram_db[seq_key]
                total = sum(trans.values())
                if total >= 5: # Min support
                    for k, v in trans.items():
                        dist[int(k)] += (v / total) * length # Longer matches carry more weight
                    break
        
        return self.normalize(dist)

    def calculate_change_score(self, seq):
        """Fast/slow imbalance + switching rate"""
        if len(seq) < 20: return 0.0
        
        fast = sum(seq[-5:]) / 5.0
        slow = sum(seq[-20:]) / 20.0
        imbalance = abs(fast - slow) * 2.0 # 0.0 to 1.0
        
        recent = seq[-10:]
        switches = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1]) / 9.0
        
        score = (imbalance * 0.6) + (switches * 0.4)
        return clip(score, 0.0, 1.0)

    def predict(self, history, issue):
        seq = [r["size"] for r in history]
        nums = [r["num"] for r in history]
        
        experts, patterns, features, current_regime = self.mine_experts(seq)
        
        num_dist = self.get_number_distribution(nums)
        big_prob = sum(num_dist[5:])
        p_cal = big_prob
        
        pred_side = "BIG" if p_cal >= 0.5 else "SMALL"
        
        current_streak = run_length(seq)
        if current_streak >= 4:
            cont_wins = 0
            cont_total = 0
            for i in range(4, len(seq)):
                if run_length(seq[:i]) >= 4:
                    cont_total += 1
                    if seq[i] == seq[i-1]: cont_wins += 1
            if cont_total > 10:
                hist_cont_prob = cont_wins / cont_total
                blend = 0.5
                p_cont_big = hist_cont_prob if seq[-1] == 1 else (1.0 - hist_cont_prob)
                p_cal = (p_cal * (1 - blend)) + (p_cont_big * blend)
                pred_side = "BIG" if p_cal >= 0.5 else "SMALL"

        change_score = self.calculate_change_score(seq)
        
        valid_nums = range(5, 10) if pred_side == "BIG" else range(0, 5)
        best_num = max(valid_nums, key=lambda x: num_dist[x])
        num_prob = num_dist[best_num]
        
        pat_str = "None"
        if patterns:
            top_p = patterns[0]
            p_pat = top_p[1]
            p_type = top_p[4].split(":")[0]
            bits = top_p[4].split(":")[-1]
            arrow = "⬆️" if p_pat >= 0.5 else "⬇️"
            
            if p_type == "family": pat_str = f"👨‍👩‍👧‍👦 <code>{bits}</code> {arrow}"
            else: pat_str = f"<code>{bits}</code> {arrow}"
        
        prev_regime = get_regime(seq[:-1]) if len(seq) > 1 else "N/A"
        trend_shift = f"{prev_regime} ➡️ {current_regime}" if prev_regime != current_regime else None

        m_feats = [logit(clip(experts.get(n, 0.5), 0.05, 0.95)) for n in EXPERT_NAMES] + [1.0]

        return {
            "issue": issue, 
            "p": p_cal, 
            "p_raw": p_cal, 
            "side": pred_side,
            "num": best_num, 
            "num_probability": num_prob,
            "change_score": change_score,
            "regime": current_regime, 
            "display_regime": current_regime,
            "source": "🎯 Evidence Engine",
            "trend_shift": trend_shift, 
            "pattern_str": pat_str, 
            "experts": experts, 
            "features": features, 
            "meta_feats": m_feats,
            "patterns": [{"key": k, "p": p, "support": s} for r, p, s, e, k in patterns]
        }

# ─────────────────────────────────────────────────────────────
# PRO CORE — trend-hazard detector + size patterns (5..16) + number patterns (2..10)
# Everything is learned online and judged out-of-sample, so FOLLOW / FADE / HOLD in a long trend is decided by
# evidence for that exact run length - not by a fixed belief and not by one pooled number.
# ─────────────────────────────────────────────────────────────
SIZE_LMIN, SIZE_LMAX = 5, 16
NUM_LMIN,  NUM_LMAX  = 2, 10
KCAP, EVAL_N, MIN_N  = 12, 300, 150
PAYOUT_BE = 1.0 / 1.96                       # 51.02 % break-even

def _lg(p):
    p = min(max(p, 1e-4), 1 - 1e-4); return math.log(p / (1 - p))
def _sg(x):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))
def _norm_up(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))
def _binom_sf(k, n, p0):                      # P(X >= k), exact (n is small: <= a few hundred)
    if n <= 0: return 1.0
    if k <= 0: return 1.0
    s, term = 0.0, None
    logc = [0.0]
    q = 1 - p0
    tot = 0.0
    for i in range(k, n + 1):
        tot += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * math.log(p0) + (n - i) * math.log(q))
    return min(1.0, tot)

def _qw(e):                                   # signal-to-noise weighting: tiny chance-level edges ~ 0, real edges dominate
    return e * e if e > 0 else 0.0

class _Acc:
    """decayed causal accuracy -> source weight (shrunk to chance) + out-of-sample record on the eval window"""
    __slots__ = ("h", "n", "ok", "on", "base")
    def __init__(self, base=0.5): self.h = 0.0; self.n = 0.0; self.ok = 0; self.on = 0; self.base = base
    def add(self, hit, oos):
        self.h = self.h * 0.99 + hit; self.n = self.n * 0.99 + 1
        if oos: self.ok += hit; self.on += 1
    def edge(self, K=40.0):                   # > 0 only if the source has really beaten chance recently
        return (self.h + K * self.base) / (self.n + K) - self.base

def analyze_pro(sizes, nums, eval_n=EVAL_N, issue_hint=None):
    N = len(sizes)
    if N < MIN_N: return None
    lo = max(80, N - eval_n)
    hz_k = [[0, 0] for _ in range(KCAP + 1)]; hz_kp = {}; hz_kg = {}; hz_tot = [0, 0]; hz_long = [0, 0]
    spat = {L: {} for L in range(SIZE_LMIN, SIZE_LMAX + 1)}
    npat = {L: {} for L in range(NUM_LMIN, NUM_LMAX + 1)}
    mkv  = {L: {} for L in range(1, 5)}
    lagN = [0] * 11; lagH = [0] * 11                                  # digit "copy k rounds ago" counts
    src  = {n: _Acc() for n in ("trend_long", "trend_short", "size_pat", "num_pat", "markov")}
    accS = {L: _Acc() for L in spat}; accM = {L: _Acc() for L in mkv}
    accN = {L: _Acc(0.10) for L in npat}
    run_oos = {}                                                       # run-length bucket -> [hits, n] of the hazard rule
    recs = []

    def bucketp(pr): return 0 if pr <= 1 else 1 if pr == 2 else 2 if pr <= 4 else 3
    def bucketg(g):  return 0 if g < 0.05 else 1 if g < 0.15 else 2 if g < 0.28 else 3

    def hazard(r, pr, g):
        k = min(r, KCAP)
        p_all = (hz_tot[1] + 5.0) / (hz_tot[0] + hz_tot[1] + 10.0)
        if r >= 4:                                     # long runs: shrink towards the POOLED long-run estimate, then per-length evidence
            b0 = hz_long; p_par = (b0[1] + 10.0 * p_all) / (b0[0] + b0[1] + 10.0); m_k = 15.0
        else:
            p_par = p_all; m_k = 10.0
        a = hz_k[k]; p_k = (a[1] + m_k * p_par) / (a[0] + a[1] + m_k)
        kp = (k, bucketp(pr)); kg = (k, bucketg(g))
        b = hz_kp.get(kp, (0, 0)); p_kp = (b[1] + 12.0 * p_k) / (b[0] + b[1] + 12.0)
        c = hz_kg.get(kg, (0, 0)); p_kg = (c[1] + 12.0 * p_k) / (c[0] + c[1] + 12.0)
        lg = _lg(p_k) + (_lg(p_kp) - _lg(p_k)) + (_lg(p_kg) - _lg(p_k))
        return _sg(lg), a[0] + a[1], kp, kg

    def forecast(t, r, s, pr, g, code16, code10, want_detail=False):
        """returns dict of per-source P(BIG) (None if no signal) + extras"""
        out = {}; det = {}
        p_cont, nk, kp, kg = hazard(r, pr, g)
        out["trend_long" if r >= 4 else "trend_short"] = p_cont if s == 1 else 1 - p_cont
        det["p_cont"], det["nk"] = p_cont, nk
        # size patterns 5..16
        zs = ws = 0.0; ctxs = {}; best = None
        for L in range(SIZE_LMIN, SIZE_LMAX + 1):
            if t < L: break
            ctx = code16 & ((1 << L) - 1); rec = spat[L].get(ctx)
            if rec:
                n = rec[0] + rec[1]
                if n >= 3:
                    p = (rec[1] + 4.0) / (n + 8.0); e = accS[L].edge(); ctxs[L] = (p, n, rec[0], rec[1], ctx, e)
                    if e > 0:
                        w = math.log(1 + n) * _qw(e); zs += w * _lg(p); ws += w
                    sc = abs(rec[1] - rec[0]) / math.sqrt(n) * (1 + 6 * max(0.0, e))
                    if best is None or sc > best[0]: best = (sc, L)
        out["size_pat"] = _sg(zs / max(ws, 0.004)) if ws > 0 else None
        det["spat"] = (best[1], ctxs[best[1]]) if best else None
        # number patterns 2..10 (+ digit-copy lags) -> digit distribution
        dist = [0.0] * 10; wt = 0.0; nbest = None
        for L in range(NUM_LMIN, NUM_LMAX + 1):
            if t < L: break
            ctx = code10 % (10 ** L); rec = npat[L].get(ctx)
            if rec and rec[10] >= 3:
                n = rec[10]; e = accN[L].edge(); w = math.log(1 + n) * _qw(e) * 25.0
                if w > 0:
                    for d in range(10): dist[d] += w * (rec[d] + 0.5) / (n + 5.0)
                    wt += w
                mx = max(range(10), key=lambda d: rec[d]); sc = rec[mx] / n * math.sqrt(n)
                if nbest is None or sc > nbest[0]: nbest = (sc, L, ctx, mx, rec[mx] / n, n, e)
        if wt > 0:
            wt0 = wt + 0.05
            for d in range(10): dist[d] = (dist[d] + 0.005) / wt0
            out["num_pat"] = sum(dist[5:])
        else:
            out["num_pat"] = None; dist = [0.1] * 10
        det["dist"] = dist; det["nbest"] = nbest
        # low-order markov 1..4
        zs = ws = 0.0
        for L in range(1, 5):
            if t < L: break
            rec = mkv[L].get(code16 & ((1 << L) - 1))
            if rec:
                n = rec[0] + rec[1]
                if n >= 3:
                    e = accM[L].edge()
                    if e > 0:
                        w = math.log(1 + n) * _qw(e); zs += w * _lg((rec[1] + 4.0) / (n + 8.0)); ws += w
        out["markov"] = _sg(zs / max(ws, 0.004)) if ws > 0 else None
        return out, det, (kp, kg)

    def ensemble(out):
        zs = ws = 0.0
        for name, p in out.items():
            if p is None: continue
            e = src[name].edge()
            if e > 0: zs += _qw(e) * _lg(p); ws += _qw(e)
        return _sg(zs / max(ws, 0.0006)) if ws > 0 else 0.5

    # ---- single causal pass ----
    r, s, pr = 1, sizes[0], 0
    ef = es = float(sizes[0]); code16 = sizes[0]; code10 = nums[0]
    for t in range(1, N + 1):
        g = (ef - 0.5) * (1 if s == 1 else -1)
        out, det, (kp, kg) = forecast(t, r, s, pr, g, code16, code10)
        if t == N:
            break
        y, d = sizes[t], nums[t]; oos = t >= lo
        pe = ensemble(out)
        if oos and abs(pe - 0.5) > 1e-9:
            recs.append((max(pe, 1 - pe), 1 if ((pe > 0.5) == (y == 1)) else 0))
        for name, p in out.items():
            if p is not None and abs(p - 0.5) > 0.02:
                hit = 1 if ((p > 0.5) == (y == 1)) else 0
                src[name].add(hit, oos)
                if name.startswith("trend"):
                    b = min(r, 8)
                    if oos:
                        z = run_oos.setdefault(b, [0, 0]); z[0] += hit; z[1] += 1
        # per-length bookkeeping (accuracy of each pattern length on its own matched contexts)
        for L in spat:
            if t < L: break
            rec = spat[L].get(code16 & ((1 << L) - 1))
            if rec and rec[0] + rec[1] >= 3 and rec[0] != rec[1]:
                accS[L].add(1 if ((rec[1] > rec[0]) == (y == 1)) else 0, oos)
        for L in mkv:
            if t < L: break
            rec = mkv[L].get(code16 & ((1 << L) - 1))
            if rec and rec[0] + rec[1] >= 3 and rec[0] != rec[1]:
                accM[L].add(1 if ((rec[1] > rec[0]) == (y == 1)) else 0, oos)
        for L in npat:
            if t < L: break
            rec = npat[L].get(code10 % (10 ** L))
            if rec and rec[10] >= 3:
                mx = max(range(10), key=lambda q: rec[q])
                accN[L].add(1 if mx == d else 0, oos)
        # learn
        cont = 1 if y == s else 0
        hz_k[min(r, KCAP)][cont] += 1; hz_tot[cont] += 1
        if r >= 4: hz_long[cont] += 1
        hz_kp.setdefault(kp, [0, 0])[cont] += 1; hz_kg.setdefault(kg, [0, 0])[cont] += 1
        for L in spat:
            if t < L: break
            spat[L].setdefault(code16 & ((1 << L) - 1), [0, 0])[y] += 1
        for L in mkv:
            if t < L: break
            mkv[L].setdefault(code16 & ((1 << L) - 1), [0, 0])[y] += 1
        for L in npat:
            if t < L: break
            rec = npat[L].setdefault(code10 % (10 ** L), [0] * 11); rec[d] += 1; rec[10] += 1
        for k in range(1, 11):
            if t >= k:
                lagN[k] += 1
                if nums[t] == nums[t - k]: lagH[k] += 1
        # advance state with result t
        if y == s: r += 1
        else: pr, r, s = r, 1, y
        ef += (y - ef) * (2 / 6.0); es += (y - es) * (2 / 21.0)
        code16 = ((code16 << 1) | y) & 0xFFFF; code10 = (code10 * 10 + d) % (10 ** 10)

    # ---- the real next-round forecast ----
    pe = ensemble(out)
    if abs(pe - 0.5) < 1e-9:                                           # no source has proven anything -> best EMPIRICAL guess, shown as 50 %
        guess = [p for k_, p in out.items() if p is not None and k_.startswith("trend") and abs(p - 0.5) >= 0.01]
        if guess: side = 1 if guess[0] > 0.5 else 0
        else:     side = (issue_hint if issue_hint is not None else N) & 1     # neutral: neither follows nor fades the trend
    else:
        side = 1 if pe > 0.5 else 0
    raw = max(pe, 1 - pe) if abs(pe - 0.5) > 1e-9 else 0.5
    sw = sh = 0.0
    for c, h in recs:
        z = (c - raw) / 0.03; w = math.exp(-0.5 * z * z); sw += w; sh += w * h
    calP = (sh + 30 * 0.5) / (sw + 30.0) if abs(pe - 0.5) > 1e-9 else 0.5   # no signal -> honest 50 %

    # ---- digit pick from the number-pattern mixture (restricted to the chosen side) ----
    dist = det["dist"]; pool = range(5, 10) if side == 1 else range(0, 5)
    best_lag = max(range(1, 11), key=lambda k: (lagH[k] + 1.0) / (lagN[k] + 10.0)) if lagN[1] else 1
    q_lag = max(0.0, (lagH[best_lag] + 1.0) / (lagN[best_lag] + 10.0) - 0.10) if lagN[best_lag] >= 100 else 0.0
    mix = list(dist)
    if q_lag > 0.02 and lagN[best_lag] >= 100:
        z = (lagH[best_lag] - 0.1 * lagN[best_lag]) / math.sqrt(0.09 * lagN[best_lag])
        if z > 3.0:
            mix = [(1 - q_lag) * v for v in mix]; mix[nums[-best_lag]] += q_lag
    num = max(pool, key=lambda dd: mix[dd])

    # ---- trend panel ----
    p_cont, nk = det["p_cont"], det["nk"]
    cur_r, cur_s = r, s
    hz_name = "trend_long" if cur_r >= 4 else "trend_short"
    edge = src[hz_name].edge()
    bk = min(cur_r, 8); ro = run_oos.get(bk, [0, 0])
    if cur_r >= 2 and edge > 0 and abs(p_cont - 0.5) >= 0.04: decision = "FOLLOW" if p_cont > 0.5 else "FADE"
    else: decision = "HOLD"                                            # neither follow nor fade without evidence
    exp_extra, surv = 0.0, 1.0
    for j in range(0, 10):
        kk = min(cur_r + j, KCAP); a = hz_k[kk]
        if cur_r + j >= 4: par = (hz_long[1] + 5.0) / (hz_long[0] + hz_long[1] + 10.0); mm = 15.0
        else: par = 0.5; mm = 10.0
        pj = (a[1] + mm * par) / (a[0] + a[1] + mm)
        surv *= pj; exp_extra += surv
    # ---- significance (Sidak over every length/source tested) ----
    tests = []
    for L in spat:
        a = accS[L]
        if a.on >= 40: tests.append(("size L%d" % L, a.ok, a.on, 0.5))
    for L in mkv:
        a = accM[L]
        if a.on >= 40: tests.append(("markov L%d" % L, a.ok, a.on, 0.5))
    for L in npat:
        a = accN[L]
        if a.on >= 40: tests.append(("num L%d" % L, a.ok, a.on, 0.10))
    for name in ("trend_long", "trend_short"):
        a = src[name]
        if a.on >= 40: tests.append((name.replace("_", " "), a.ok, a.on, 0.5))
    M = max(len(tests), 1); alpha_adj = 1 - 0.98 ** (1.0 / M)        # family-wise 2 % (the bot re-tests every round)
    proven = []
    for nm_, k, n, p0 in tests:
        if k / n > p0 and _binom_sf(k, n, p0) < alpha_adj: proven.append(nm_)
    en = len(recs); ek = sum(h for _, h in recs)
    ens_p = _norm_up((ek - 0.5 - en / 2.0) / math.sqrt(en / 4.0)) if en >= 40 else 1.0
    
    # 🚨 DEEP UPGRADE: Threshold 0.01 se 0.05 kar diya gaya hai (jaldi confirmed dikhane ke liye)
    confirmed = bool(proven) or ens_p < 0.05
    
    return {
        "N": N, "side": side, "raw": raw, "calP": calP, "num": num, "num_p": mix[num],
        "confirmed": confirmed, "proven": proven, "ens_p": ens_p, "ens_acc": (ek / en) if en else 0.5, "ens_n": en,
        "trend": {"run": cur_r, "side": cur_s, "p_cont": p_cont, "n": nk, "decision": decision, "edge": edge,
                  "oos_hit": ro[0], "oos_n": ro[1], "exp_extra": exp_extra},
        "spat": det["spat"], "nbest": det["nbest"], "M": M,
        "weights": {k: round(v.edge(), 4) for k, v in src.items()},
    }


class ProEngine(UpgradedEngine):
    """Decision core = analyze_pro(); keeps the old DB / outbox / sticker flow. (DEEP AGGRESSIVE MODE)"""

    def __init__(self, db):
        super().__init__(db)
        self._proof_run = 0                       # consecutive rounds the edge test passed (hysteresis)

    def settle(self, history):
        lookup = {r["issue"]: r for r in history}
        pending = self.db.c.execute("SELECT issue,payload FROM predictions WHERE status='PENDING' ORDER BY CAST(issue AS INTEGER)").fetchall()
        for row in pending:
            actual = lookup.get(row["issue"])
            if actual is None: continue
            p = json.loads(row["payload"])
            y = actual["size"]
            shown_big = (p["side"] == "BIG") if p.get("side") else (p["p"] >= 0.5)    # score exactly what was shown
            won_side = int(shown_big) == y
            won_num = p.get("num") == actual["num"]
            with self.db.transaction():
                self.db.c.execute("UPDATE predictions SET status=?,actual=? WHERE issue=?", ("WIN" if won_side else "LOSS", actual["num"], row["issue"]))
                if won_side and won_num: sticker = CFG.jack_sticker
                elif won_side: sticker = CFG.win_sticker
                else: sticker = CFG.loss_sticker
                self.db.queue("sticker", row["issue"], sticker)
                self.db.c.execute("UPDATE outbox SET sent=2 WHERE kind='prediction' AND issue=? AND sent=0", (row["issue"],))

    def predict(self, history, issue):
        seq = [r["size"] for r in history]
        nums = [r["num"] for r in history]
        regime = get_regime(seq)
        a = analyze_pro(seq, nums, issue_hint=int(issue))
        if a is None:                             # not enough history yet
            side = "BIG" if int(issue) % 2 else "SMALL"
            return {"issue": issue, "p": 0.5001 if side == "BIG" else 0.4999, "p_raw": 0.5, "side": side, "num": 7 if side == "BIG" else 2,
                    "regime": regime, "source": "⏳ Warm-up", "conf": 50.0, "conf_raw": 50.0, "status": "WARMUP",
                    "trend_text": "collecting history", "spat_text": "", "npat_text": "", "proven": [], "experts": {}, "patterns": []}

        side = "BIG" if a["side"] == 1 else "SMALL"
        
        # 🚨 AGGRESSIVE UPGRADE 1: Confidence ko boost karna
        cal = max(0.5, a["calP"])
        if not a["confirmed"]: 
            # Pehle yeh 0.3 se multiply karke 51% kar deta tha, ab hum isko 0.7 kar rahe hain
            cal = 0.5 + (cal - 0.5) * 0.7 
            
        p_big = max(cal, 0.5001) if a["side"] == 1 else min(1 - cal, 0.4999)
        
        # 🚨 AGGRESSIVE UPGRADE 2: Status ko jaldi 'CANDIDATE' banane ke liye threshold kam karna
        self._proof_run = self._proof_run + 1 if a["confirmed"] else 0
        status = "PROVEN" if self._proof_run >= 15 else ("CANDIDATE" if a["ens_acc"] > 0.52 else "NONE")

        t = a["trend"]; col = "🔴" if t["side"] == 1 else "🔵"; nm = "BIG" if t["side"] == 1 else "SMALL"
        
        # 🚨 AGGRESSIVE UPGRADE 3: HOLD ko hatana, hamesha FOLLOW ya FADE dikhana
        if t["run"] >= 2:
            pc = t["p_cont"] * 100
            if t["p_cont"] > 0.5:
                act = f"➡️ FOLLOW (continues {pc:.0f}%, n={t['n']})"
            else:
                act = f"↩️ FADE (breaks {100 - pc:.0f}%, n={t['n']})"
            
            ver = f" · verified {t['oos_hit']}/{t['oos_n']}" if t["oos_n"] >= 10 else ""
            trend_text = f"{col} {nm} run ×{t['run']} → {act}{ver} · ~{t['exp_extra']:.1f} more"
        else:
            trend_text = f"{col} {nm} run ×1 - no trend yet"

        spat_text = ""
        if a["spat"]:
            L, (pp, n, n0, n1, _c, ev) = a["spat"]
            bits = "".join("B" if x else "S" for x in seq[-L:])
            spat_text = f"L{L} <code>{bits}</code> → {n1}B/{n0}S {'⬆️' if pp >= 0.5 else '⬇️'}"
        npat_text = ""
        if a["nbest"]:
            _sc, L, _ctx, mx, prob, n, ev = a["nbest"]
            npat_text = f"L{L} <code>{'-'.join(str(x) for x in nums[-L:])}</code> → {mx} ({prob * 100:.0f}%, n={n})"

        prev_regime = get_regime(seq[:-1]) if len(seq) > 1 else "N/A"
        
        return {
            "issue": issue, "p": p_big, "p_raw": a["raw"] if a["side"] == 1 else 1 - a["raw"], "side": side,
            "num": a["num"], "num_probability": a["num_p"],
            "regime": regime, "display_regime": regime, "source": "🧬 Deep Aggressive Engine",
            "trend_shift": f"{prev_regime} ➡️ {regime}" if prev_regime != regime else None,
            "pattern_str": spat_text or "None",
            "conf": cal * 100, "conf_raw": a["raw"] * 100, "status": status, "proven": a["proven"],
            "ens_acc": a["ens_acc"], "ens_n": a["ens_n"],
            "trend_text": trend_text, "spat_text": spat_text, "npat_text": npat_text,
            "experts": {}, "patterns": [],
        }

# ─────────────────────────────────────────────────────────────
# FORMATTER (Purana Wala Premium Format)
# ─────────────────────────────────────────────────────────────
def format_prediction(p):
    conf = p.get("conf", max(p["p"], 1 - p["p"]) * 100)
    filled = max(0, min(10, int(conf // 10)))
    bar = "█" * filled + "░" * (10 - filled)
    side = "BIG" if p["side"] == "BIG" else "SMALL"
    color = "🔴" if side == "BIG" else "🔵"
    raw = p.get("conf_raw")
    raw_text = f" <i>(raw {raw:.0f}%)</i>" if raw is not None and abs(raw - conf) >= 1 else ""

    status = p.get("status", "NONE")
    if status == "PROVEN" and conf >= 54: quality, stake = "🔥 PROVEN EDGE", "1x"
    elif status == "PROVEN": quality, stake = "✅ PROVEN EDGE (thin)", "0.75x"
    elif status == "CANDIDATE": quality, stake = "🟡 EDGE CANDIDATE (not yet confirmed)", "0.5x"
    elif status == "WARMUP": quality, stake = "⏳ WARM-UP", "0.5x"
    else: quality, stake = "⚪ NO PROVEN EDGE (coin-flip level)", "0.5x"
    proof = f"\n🧪 <b>EVIDENCE:</b> {escape(', '.join(p['proven'][:3]))}" if p.get("proven") else ""

    trend_line = f"\n📈 <b>TREND:</b> {p['trend_text']}" if p.get("trend_text") else ""
    spat_line = f"\n🧩 <b>SIZE PATTERN:</b> {p['spat_text']}" if p.get("spat_text") else ""
    npat_line = f"\n🔢 <b>NUMBER PATTERN:</b> {p['npat_text']}" if p.get("npat_text") else ""
    shift_text = f"\n🔄 <b>TREND SHIFT:</b> {escape(p['trend_shift'])}" if p.get("trend_shift") else ""

    return (
        f"⚡️ <b>{escape(CFG.name)}</b> ⚡️\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>TARGET:</b> {color} <b>{side}</b> → <code>{p['num']}</code>\n"
        f"📅 <b>PERIOD:</b> <code>{escape(p['issue'])}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>CONFIDENCE:</b> {conf:.1f}% [{bar}]{raw_text}\n"
        f"🏆 <b>STATUS:</b> {quality} (Stake: {stake}){proof}"
        f"{trend_line}{spat_line}{npat_line}\n"
        f"🌊 <b>REGIME:</b> {escape(p.get('regime', 'N/A'))}{shift_text}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

# ─────────────────────────────────────────────────────────────
# RUNTIME
# ─────────────────────────────────────────────────────────────
class Runtime:
    def __init__(self, db):
        self.db = db
        self.engine = ProEngine(db)
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
                        p = await asyncio.to_thread(self.engine.predict, history, issue)   # heavy maths off the event loop
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
    db.c.execute("DELETE FROM meta WHERE key IN ('num_ngrams','num_freq')")   # old multi-MB n-gram blobs are no longer used
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
            LOG.info("HYPER V17 Predictor Started (Deep Aggressive Mode)")

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
