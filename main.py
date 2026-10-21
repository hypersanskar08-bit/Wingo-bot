import asyncio
import aiohttp
import contextlib
import json
import logging
import math
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIG
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8611789455:AAEQb9B7N565qGDp--AR6Z1v_4lhzUIu-PQ")
CHAT_ID        = os.getenv("CHAT_ID", "1264164655")
API_URL        = os.getenv("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000")
DB_PATH        = os.getenv("DB_PATH", "predictor.db")
FETCH_EVERY    = float(os.getenv("FETCH_EVERY", "1.5"))
BOT_NAME       = "V94 DEEP PIPELINE"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("predictor")

STATUS_EMOJI = {"WIN": "✅", "LOSS": "❌", "JACK": "🏆"}

def status_emoji(s):
    s = (s or "").upper()
    for k, e in STATUS_EMOJI.items():
        if k in s:
            return e
    return s or "?"

def size_label(v):
    if v is None: return "?"
    if isinstance(v, str):
        u = v.upper()
        if u in ("BIG", "SMALL"): return u
        return "BIG" if v == "1" else "SMALL"
    return "BIG" if int(v) == 1 else "SMALL"

def two_sided_p(z):
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))

def binomial_z(hits, total, p=0.5):
    if total == 0: return 0.0
    mu = total * p
    sd = math.sqrt(total * p * (1 - p)) or 1e-9
    return (hits - mu) / sd


# ============================================================
# STORAGE
# ============================================================
class Storage:
    def __init__(self, path):
        self.path = path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS predictions (
                issue TEXT PRIMARY KEY,
                size TEXT, num INTEGER, confidence INTEGER,
                calibrated INTEGER,
                logic TEXT, signals TEXT, status TEXT DEFAULT 'PENDING',
                actual_num INTEGER, actual_size TEXT, created_at REAL
            )""")

    def save(self, p):
        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO predictions
                (issue,size,num,confidence,calibrated,logic,signals,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (p["issue"], p["size"], p["num"], p["confidence"],
                 p.get("calibrated", p["confidence"]), p["logic"],
                 json.dumps(p.get("signals", {})), p["status"], time.time()))

    def resolve(self, hist, limit=200):
        resolved = []
        with self._conn() as c:
            rows = c.execute("""SELECT issue,size,num FROM predictions
                                WHERE status='PENDING' ORDER BY created_at DESC LIMIT ?""",
                             (limit,)).fetchall()
        for issue, size, num in rows:
            if issue not in hist:
                continue
            r = hist[issue]
            win = size == ("BIG" if r["size"] else "SMALL")
            status = ("JACK" if r["num"] == num else "WIN") if win else "LOSS"
            actual_size = "BIG" if r["size"] else "SMALL"
            with self._conn() as c:
                c.execute("""UPDATE predictions SET status=?, actual_num=?, actual_size=?
                             WHERE issue=?""", (status, r["num"], actual_size, issue))
            resolved.append({"issue": issue, "size": size, "num": num, "status": status,
                             "actual_num": r["num"], "actual_size": actual_size})
        return resolved

    def stats(self):
        with self._conn() as c:
            total, wins, jacks = c.execute("""SELECT COUNT(*),
                SUM(CASE WHEN status IN ('WIN','JACK') THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'JACK' THEN 1 ELSE 0 END)
                FROM predictions WHERE status != 'PENDING'""").fetchone()
        if not total: return None
        wins = wins or 0; jacks = jacks or 0
        return {"total": total, "wins": wins, "loss": total - wins,
                "jacks": jacks, "rate": round(100 * wins / total, 1)}

    def recent(self, n=12):
        with self._conn() as c:
            return c.execute("""SELECT issue,size,num,actual_num,actual_size,status
                                FROM predictions WHERE status != 'PENDING'
                                ORDER BY created_at DESC LIMIT ?""", (n,)).fetchall()

    def recent_pred_nums(self, n=6):
        with self._conn() as c:
            rows = c.execute("""SELECT num FROM predictions
                                WHERE num IS NOT NULL
                                ORDER BY created_at DESC LIMIT ?""", (n,)).fetchall()
        return [r[0] for r in rows][::-1]

    def signal_accuracy(self):
        acc = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""SELECT signals, actual_size FROM predictions
                                WHERE status != 'PENDING' AND signals IS NOT NULL""").fetchall()
        for sig_json, actual in rows:
            if actual is None: continue
            act_int = 1 if size_label(actual) == "BIG" else 0
            try:
                sig = json.loads(sig_json)
            except Exception:
                continue
            for name, p in sig.items():
                acc[name][1] += 1
                if (1 if p >= 0.5 else 0) == act_int:
                    acc[name][0] += 1
        return {k: tuple(v) for k, v in acc.items()}

    def recent_signal_accuracy(self, limit=60):
        acc = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""SELECT signals, actual_size FROM predictions
                                WHERE status != 'PENDING' AND signals IS NOT NULL
                                ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
        for sig_json, actual in rows:
            if actual is None: continue
            act_int = 1 if size_label(actual) == "BIG" else 0
            try:
                sig = json.loads(sig_json)
            except Exception:
                continue
            for name, p in sig.items():
                acc[name][1] += 1
                if (1 if p >= 0.5 else 0) == act_int:
                    acc[name][0] += 1
        return {k: tuple(v) for k, v in acc.items()}

    def calibration_buckets(self):
        buckets = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""SELECT confidence, status FROM predictions
                                WHERE status != 'PENDING' AND confidence IS NOT NULL""").fetchall()
        for conf, status in rows:
            b = (conf // 10) * 10
            buckets[b][1] += 1
            if status in ("WIN", "JACK"):
                buckets[b][0] += 1
        return {k: tuple(v) for k, v in buckets.items()}

    def last_issue(self):
        with self._conn() as c:
            row = c.execute("SELECT issue FROM predictions ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def recent_pred_bias(self, n=20):
        with self._conn() as c:
            rows = c.execute("""SELECT size FROM predictions
                                ORDER BY created_at DESC LIMIT ?""", (n,)).fetchall()
        big = sum(1 for r in rows if size_label(r[0]) == "BIG")
        return big, len(rows) - big


# ============================================================
# SIGNAL FRAMEWORK
# ============================================================
@dataclass
class SignalResult:
    name: str
    p_big: float
    strength: float
    detail: str = ""

class Signal:
    name = "base"
    def compute(self, sizes, nums) -> Optional[SignalResult]:
        raise NotImplementedError

class PatternSignal(Signal):
    name = "pattern"
    def compute(self, sizes, nums):
        for k in range(12, 3, -1):
            if len(sizes) < k + 1: continue
            pat = tuple(sizes[-k:])
            big = small = 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i + k]) == pat:
                    if sizes[i + k]: big += 1
                    else: small += 1
            t = big + small
            if t >= (1 if k >= 8 else 2):
                return SignalResult(self.name, (big + 1) / (t + 2),
                                    min(0.9, 0.15 + t * 0.1), f"P{k}:{big}B/{small}S")
        return None

class StreakSignal(Signal):
    name = "streak"
    def compute(self, sizes, nums):
        if len(sizes) < 2: return None
        last = sizes[-1]; streak = 1
        for i in range(len(sizes) - 2, -1, -1):
            if sizes[i] == last: streak += 1
            else: break
        if streak < 2: return SignalResult(self.name, 0.5, 0.0, "none")
        d = 1 if last else -1
        p = max(0.05, min(0.95, 0.5 + d * min(0.25, 0.06 * streak)))
        return SignalResult(self.name, p, min(0.6, 0.12 * streak), f"streak{streak}")

class MeanRevSignal(Signal):
    name = "meanrev"
    def __init__(self, window=20): self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window: return None
        bc = sum(sizes[-self.window:]); ratio = bc / self.window
        return SignalResult(self.name, 1 - ratio, min(0.7, abs(ratio - 0.5) * 2),
                            f"{bc}/{self.window}B")

class MarkovSignal(Signal):
    def __init__(self, order=2):
        self.order = order; self.name = f"markov{order}"
    def compute(self, sizes, nums):
        if len(sizes) <= self.order + 2: return None
        key = tuple(sizes[-self.order:]); trans = Counter()
        for i in range(len(sizes) - self.order):
            if tuple(sizes[i:i + self.order]) == key:
                trans[sizes[i + self.order]] += 1
        if not trans: return None
        t = sum(trans.values())
        return SignalResult(self.name, (trans.get(1, 0) + 1) / (t + 2),
                            min(0.7, t * 0.1), f"{t}obs")

class FreqSignal(Signal):
    name = "freq"
    def __init__(self, window=50): self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window: return None
        ratio = sum(sizes[-self.window:]) / self.window
        p = 0.5 + max(-0.15, min(0.15, (0.5 - ratio) * 0.5))
        return SignalResult(self.name, p, min(0.4, abs(ratio - 0.5)), f"{ratio:.2f}")

class EntropySignal(Signal):
    name = "entropy"
    def __init__(self, window=20): self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window: return None
        w = sizes[-self.window:]; p1 = sum(w) / len(w); p0 = 1 - p1
        ent = 0.0
        for p in (p0, p1):
            if p > 0: ent -= p * (p ** 0.5)
        major = 1 if p1 >= 0.5 else 0
        return SignalResult(self.name, 1.0 if major else 0.0,
                            max(0.0, 0.4 - ent), f"ent~{ent:.2f}")

class AutocorrSignal(Signal):
    name = "autocorr"
    def __init__(self, lag=1): self.lag = lag
    def compute(self, sizes, nums):
        if len(sizes) < 30: return None
        x = sizes[-40:]; n = len(x) - self.lag
        if n < 10: return None
        mean = sum(x) / len(x)
        num = sum((x[i] - mean) * (x[i + self.lag] - mean) for i in range(n))
        den = sum((v - mean) ** 2 for v in x) or 1e-9
        r = num / den; last = sizes[-1]
        p = 0.5 + r * (0.3 if last else -0.3)
        return SignalResult(self.name, max(0.05, min(0.95, p)), min(0.4, abs(r)), f"r={r:+.2f}")

class AlternationSignal(Signal):
    name = "alternation"
    def __init__(self, window=20): self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window: return None
        w = sizes[-self.window:]
        switches = sum(1 for i in range(1, len(w)) if w[i] != w[i - 1])
        sw = switches / (len(w) - 1); last = sizes[-1]
        p = (1 - last) * sw + last * (1 - sw)
        return SignalResult(self.name, p, abs(sw - 0.5), f"sw={sw:.2f}")

class MomentumSignal(Signal):
    name = "momentum"
    def __init__(self, window=10): self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window: return None
        r = sum(sizes[-self.window:]) / self.window
        return SignalResult(self.name, r, min(0.3, abs(r - 0.5)), f"{r:.2f}")

class RunLengthSignal(Signal):
    name = "runlength"
    def compute(self, sizes, nums):
        if len(sizes) < 30: return None
        runs = []; cur = 1
        for i in range(1, len(sizes)):
            if sizes[i] == sizes[i - 1]: cur += 1
            else: runs.append(cur); cur = 1
        runs.append(cur); avg = sum(runs) / len(runs) if runs else 1
        last = sizes[-1]
        if runs[-1] > avg:
            p = 0.0 if last else 1.0
            strength = min(0.4, (runs[-1] - avg) / max(avg, 1))
        else:
            p = 1.0 if last else 0.0; strength = 0.15
        return SignalResult(self.name, p, strength, f"run{runs[-1]}/avg{avg:.1f}")

class SelfCorrectionSignal(Signal):
    name = "selfcorrect"
    def __init__(self, store):
        self.store = store
    def compute(self, sizes, nums):
        rows = self.store.recent(6)
        if len(rows) < 3:
            return None
        wrong_streak = 0
        for r in rows:
            status = r[5]
            if status == "LOSS":
                wrong_streak += 1
            else:
                break
        if wrong_streak < 2:
            return SignalResult(self.name, 0.5, 0.0, "ok")
        last_pred_big = 1 if size_label(rows[0][1]) == "BIG" else 0
        p = 1 - last_pred_big
        strength = min(0.5, 0.12 * wrong_streak)
        return SignalResult(self.name, p, strength, f"wrong{wrong_streak}")


# ============================================================
# PREDICTOR
# ============================================================
class Predictor:
    def __init__(self, storage):
        self.store = storage
        self.history = []
        self.signals = [
            PatternSignal(), StreakSignal(), MeanRevSignal(20),
            MarkovSignal(2), MarkovSignal(3), FreqSignal(50),
            EntropySignal(20), AutocorrSignal(1), AlternationSignal(20),
            MomentumSignal(10), RunLengthSignal(),
            SelfCorrectionSignal(self.store),
        ]
        self.acc_weights = self._acc_weights()
        self.recent_pred_nums = deque(self.store.recent_pred_nums(6), maxlen=6)

    def _acc_weights(self):
        long_acc = self.store.signal_accuracy()
        short_acc = self.store.recent_signal_accuracy(60)
        w = {}
        for s in self.signals:
            hl, tl = long_acc.get(s.name, (0, 0))
            hs, ts = short_acc.get(s.name, (0, 0))
            long_rate = (hl + 5) / (tl + 10)
            short_rate = (hs + 3) / (ts + 6)
            blended = 0.4 * long_rate + 0.6 * short_rate
            w[s.name] = 0.5 + blended
        return w

    def refresh_weights(self):
        self.acc_weights = self._acc_weights()

    async def fetch(self, session):
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200: return False
                data = await r.json()
                raw = data.get("data", {}).get("list", [])
                out = []
                for item in raw:
                    try:
                        n = int(item["number"])
                        out.append({"issue": str(item["issueNumber"]),
                                    "num": n, "size": 1 if n >= 5 else 0})
                    except (KeyError, ValueError, TypeError):
                        continue
                out.sort(key=lambda x: int(x["issue"]))
                self.history = out
                return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"fetch: {e}")
            return False

    def _core(self, sizes, nums):
        if len(sizes) < 50: return None
        results = {}
        for sig in self.signals:
            try: r = sig.compute(sizes, nums)
            except Exception: r = None
            if r and r.strength > 0:
                results[r.name] = r
        if not results: return None

        num = den = 0.0; votes = {}
        for name, r in results.items():
            w = r.strength * self.acc_weights.get(name, 1.0)
            num += r.p_big * w; den += w
            votes[name] = round(r.p_big, 3)
        p_big = num / den if den else 0.5

        edge = abs(p_big - 0.5)
        agree = sum(1 for v in votes.values() if (v >= 0.5) == (p_big >= 0.5))
        disagree_ratio = agree / len(votes)
        if edge < 0.02 or disagree_ratio < 0.45:
            return None

        pred_size = 1 if p_big >= 0.5 else 0

        big_pred, small_pred = self.store.recent_pred_bias(20)
        total_pred = big_pred + small_pred
        if total_pred >= 10:
            bias_ratio = big_pred / total_pred
            if bias_ratio > 0.70 and pred_size == 1:
                edge *= 0.6
            elif bias_ratio < 0.30 and pred_size == 0:
                edge *= 0.6

        confidence = 50 + round(min(1.0, edge * 2) * 40)
        return {"p_big": p_big, "pred_size": pred_size, "confidence": confidence,
                "signals": votes, "logic": " + ".join(list(votes)[:4])}

    def _number_scores(self, nums, pool):
        if not nums:
            return {n: 1.0 for n in pool}

        trans = defaultdict(Counter)
        for i in range(len(nums) - 1):
            trans[nums[i]][nums[i + 1]] += 1
        last_num = nums[-1]
        row = trans[last_num]; row_total = sum(row.values())

        freq = Counter(nums[-100:]); freq_total = sum(freq.values()) or 1

        gaps = {}
        for n in pool:
            gaps[n] = len(nums)
            for i in range(len(nums) - 1, -1, -1):
                if nums[i] == n:
                    gaps[n] = len(nums) - i; break

        scores = {}
        for n in pool:
            p_trans = (row.get(n, 0) + 0.5) / (row_total + 5.0)
            p_freq = (freq.get(n, 0) + 0.5) / (freq_total + 5.0)
            gap_norm = min(1.0, gaps[n] / 20.0)
            anti_hot = 1.0 - min(1.0, p_freq * 10)

            score = (0.35 * p_trans * 10 +
                     0.20 * p_freq * 10 +
                     0.20 * gap_norm +
                     0.10 * anti_hot)
            if n in self.recent_pred_nums:
                score *= 0.45
            scores[n] = score
        return scores

    def _pick_number(self, nums, pred_size, temperature=0.55):
        pool = list(range(5, 10)) if pred_size else list(range(0, 5))
        scores = self._number_scores(nums, pool)
        max_s = max(scores.values())
        exp_s = {n: math.exp((s - max_s) / temperature) for n, s in scores.items()}
        total = sum(exp_s.values()) or 1.0
        r = random.random() * total; cum = 0.0
        chosen = max(scores, key=scores.get)
        for n in sorted(exp_s):
            cum += exp_s[n]
            if r < cum:
                chosen = n; break

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        top = [n for n, _ in ranked[:3]]
        return chosen, top

    def _calibrate(self, raw_conf):
        buckets = self.store.calibration_buckets()
        b = (raw_conf // 10) * 10
        for delta in (0, 10, -10, 20, -20, 30, -30):
            probe = b + delta
            if probe in buckets and buckets[probe][1] >= 15:
                hit, tot = buckets[probe]
                return round(100 * hit / tot)
        return raw_conf

    def predict(self):
        sizes = [h["size"] for h in self.history]
        nums = [h["num"] for h in self.history]
        core = self._core(sizes, nums)
        if not core: return None

        best_num, top = self._pick_number(nums, core["pred_size"])
        self.recent_pred_nums.append(best_num)
        calibrated = self._calibrate(core["confidence"])

        return {"issue": str(int(self.history[-1]["issue"]) + 1),
                "size": "BIG" if core["pred_size"] else "SMALL",
                "num": best_num, "top": top,
                "confidence": core["confidence"], "calibrated": calibrated,
                "logic": core["logic"], "signals": core["signals"],
                "status": "PENDING"}

    def backtest(self, min_history=80, greedy=True):
        sizes = [h["size"] for h in self.history]
        nums = [h["num"] for h in self.history]
        if len(sizes) < min_history + 20: return None

        size_hits = size_tot = 0
        num_hits_cond = num_tot_cond = 0
        sig_hits, sig_tot = defaultdict(int), defaultdict(int)
        calib = defaultdict(lambda: [0, 0])
        skipped = 0

        for i in range(min_history, len(sizes)):
            core = self._core(sizes[:i], nums[:i])
            if not core:
                skipped += 1
                continue
            actual = sizes[i]
            size_tot += 1
            ok_size = core["pred_size"] == actual
            size_hits += ok_size

            pool = list(range(5, 10)) if core["pred_size"] else list(range(0, 5))
            scores = self._number_scores(nums[:i], pool)
            pred_num = max(scores, key=scores.get)
            if ok_size:
                num_tot_cond += 1
                if pred_num == nums[i]:
                    num_hits_cond += 1

            for name, p in core["signals"].items():
                sig_tot[name] += 1
                if (1 if p >= 0.5 else 0) == actual:
                    sig_hits[name] += 1

            b = (core["confidence"] // 10) * 10
            calib[b][1] += 1; calib[b][0] += ok_size

        tail = sizes[min_history:]
        big_base = max(sum(tail), len(tail) - sum(tail)) / max(1, len(tail))
        z = binomial_z(size_hits, size_tot)
        pval = two_sided_p(z)

        return {"total": size_tot, "hits": size_hits, "skipped": skipped,
                "acc": size_hits / size_tot if size_tot else 0,
                "big_baseline": big_base,
                "z": z, "p": pval,
                "num_cond_hits": num_hits_cond,
                "num_cond_total": num_tot_cond,
                "num_cond_acc": (num_hits_cond / num_tot_cond) if num_tot_cond else 0,
                "signal_acc": {k: (sig_hits[k], sig_tot[k]) for k in sig_tot},
                "calib": {k: tuple(v) for k, v in sorted(calib.items())}}


predictor = Predictor(Storage(DB_PATH))


# ============================================================
# TELEGRAM
# ============================================================
async def safe_send(app, text):
    for attempt in range(3):
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
            return
        except Exception as e:
            log.warning(f"send {attempt+1}: {e}")
            await asyncio.sleep(2)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"🧬 <b>{BOT_NAME}</b> online\n"
        "12 size-engines • number-Markov • Boltzmann pick • Platt calibration\n"
        "Confidence-gated + bias-corrected ensemble.\n"
        "Commands: /stats /history /signals /weights /backtest\n"
        "⚠️ RNG draws — heuristic/entertainment. No real edge.",
        parse_mode="HTML")

async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = predictor.store.stats()
    if not s:
        await u.message.reply_text("📊 Koi resolved prediction nahi abhi."); return
    jack_rate = 100 * s["jacks"] / s["total"] if s["total"] else 0
    await u.message.reply_text(
        f"📊 <b>Stats</b>\nTotal: {s['total']}\n"
        f"✅ Win: {s['wins']} | ❌ Loss: {s['loss']}\n"
        f"🏆 JACK: {s['jacks']} ({jack_rate:.1f}%)\n"
        f"Size rate: <b>{s['rate']}%</b>  <i>(coin-flip = 50%)</i>",
        parse_mode="HTML")

async def cmd_history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = predictor.store.recent(15)
    if not rows:
        await u.message.reply_text("🗂 Empty."); return
    rows = rows[::-1]
    lines = [f"<code>{r[0][-4:]}</code> {size_label(r[4])} {r[3]} → {status_emoji(r[5])}" for r in rows]
    await u.message.reply_text("🗂 <b>Recent</b> <i>(oldest→newest)</i>\n" + "\n".join(lines), parse_mode="HTML")

async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    acc = predictor.store.signal_accuracy()
    if not acc:
        await u.message.reply_text("Signals ko 20+ samples chahiye."); return
    lines = [f"{n:<12} {100*h/t:5.1f}%  ({h}/{t})"
             for n, (h, t) in sorted(acc.items(), key=lambda kv: -kv[1][1])]
    await u.message.reply_text(
        "🧪 <b>Per-signal accuracy (all-time)</b>\n<pre>" + "\n".join(lines) +
        "</pre>\n<i>50% = no edge</i>", parse_mode="HTML")

async def cmd_weights(u: Update, c: ContextTypes.DEFAULT_TYPE):
    w = predictor.acc_weights
    lines = [f"{n:<12} {v:.2f}" for n, v in sorted(w.items(), key=lambda kv: -kv[1])]
    await u.message.reply_text(
        "⚖️ <b>Ensemble weights</b>\n<pre>" + "\n".join(lines) +
        "</pre>\n<i>Blend: 60% recent form + 40% long-term record</i>", parse_mode="HTML")

async def cmd_backtest(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ Backtesting...")
    bt = await asyncio.to_thread(predictor.backtest)
    if not bt:
        await u.message.reply_text("History kam hai (100+ chahiye)."); return

    edge = "EDGE ✅" if bt["p"] < 0.05 else "NO EDGE ❌"
    sig_lines = [f"{n:<12} {100*h/t:5.1f}%  ({h}/{t})"
                 for n, (h, t) in sorted(bt["signal_acc"].items(), key=lambda kv: -kv[1][1])]
    calib_lines = [f"{b}-{b+9}%: {100*h/t:5.1f}% actual ({t})"
                   for b, (h, t) in bt["calib"].items()]

    await u.message.reply_text(
        f"📉 <b>Backtest ({bt['total']} samples, {bt['skipped']} skipped)</b>\n"
        f"Size accuracy: <b>{100*bt['acc']:.1f}%</b>\n"
        f"Baseline (always-majority): {100*bt['big_baseline']:.1f}%\n"
        f"z = {bt['z']:+.2f}, p = {bt['p']:.3f}  → <b>{edge}</b>\n"
        f"<i>(p&lt;0.05 = statistically real; else noise)</i>\n\n"
        f"🎲 <b>Conditional number acc</b>: {100*bt['num_cond_acc']:.1f}% "
        f"({bt['num_cond_hits']}/{bt['num_cond_total']})\n"
        f"<i>(random in 5-pool = 20%)</i>\n\n"
        f"🧪 <b>Per-signal</b>:\n<pre>" + "\n".join(sig_lines) + "</pre>\n\n"
        f"🎯 <b>Calibration</b>:\n<pre>" + "\n".join(calib_lines) + "</pre>",
        parse_mode="HTML")


# ============================================================
# LOOP
# ============================================================
async def loop(app):
    log.info("loop start")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if await predictor.fetch(session):
                    hist = {h["issue"]: h for h in predictor.history}
                    for res in predictor.store.resolve(hist):
                        predictor.refresh_weights()
                        await safe_send(app,
                            f"🎯 <b>Result</b>\nPeriod <code>{res['issue'][-4:]}</code>\n"
                            f"Pred {res['size']} {res['num']}\n"
                            f"Actual {res['actual_size']} {res['actual_num']}\n"
                            f"{status_emoji(res['status'])}")
                    latest = str(int(predictor.history[-1]["issue"]) + 1) if predictor.history else None
                    if latest and latest != predictor.store.last_issue():
                        p = predictor.predict()
                        if p:
                            predictor.store.save(p)
                            await safe_send(app, fmt(p))
                            log.info(f"pred {p['issue']}")
                        else:
                            log.info("prediction skipped (weak/conflicted signals)")
                await asyncio.sleep(FETCH_EVERY)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(f"loop: {e}")
                await asyncio.sleep(3)

def fmt(p):
    rows = predictor.store.recent(10)
    rows = rows[::-1]
    recent = "".join(
        f"➜ <code>{r[0][-4:]}</code> {size_label(r[4])} {r[3]} → {status_emoji(r[5])}\n"
        for r in rows)
    top_str = " ".join(str(n) for n in p.get("top", [p["num"]]))
    return (f"📌 <b>Period</b>: <code>{p['issue'][-4:]}</code>\n"
            f"🎯 <b>Size</b>: <b>{p['size']}</b>  |  🔢 <b>Target</b>: <code>{p['num']}</code>\n"
            f"🎲 <b>Candidates</b>: {top_str}\n\n"
            f"📈 <b>Confidence</b>: {p['confidence']}% "
            f"<i>(calibrated: {p.get('calibrated', p['confidence'])}%)</i>\n"
            f"🧮 <b>Signals</b>: {p['logic']}\n"
            f"━━━━━━━━━━━━━\n🕒 <b>Recent</b> <i>(oldest→newest)</i>:\n{recent}"
            f"━━━━━━━━━━━━━\n🧬 <i>{BOT_NAME}</i>")

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for name, fn in [("start", cmd_start), ("stats", cmd_stats), ("history", cmd_history),
                     ("signals", cmd_signals), ("weights", cmd_weights),
                     ("backtest", cmd_backtest)]:
        app.add_handler(CommandHandler(name, fn))
    async with app:
        await app.start()
        await app.updater.start_polling()
        bg = asyncio.create_task(loop(app))
        try:
            await asyncio.Event().wait()
        finally:
            bg.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bg
            await app.updater.stop()
            await app.stop()

if __name__ == "__main__":
    asyncio.run(main())





