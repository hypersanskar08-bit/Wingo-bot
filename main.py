import asyncio
import aiohttp
import contextlib
import json
import logging
import os
import sqlite3
import time
from collections import Counter, defaultdict
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
BOT_NAME       = "V90 DEEP PIPELINE"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("predictor")


# ============================================================
# STORAGE (SQLite — no more data loss on restart)
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
                logic TEXT, signals TEXT, status TEXT DEFAULT 'PENDING',
                actual_num INTEGER, actual_size INTEGER, created_at REAL
            )""")

    def save(self, p):
        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO predictions
                (issue,size,num,confidence,logic,signals,status,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (p["issue"], p["size"], p["num"], p["confidence"], p["logic"],
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
            with self._conn() as c:
                c.execute("""UPDATE predictions SET status=?, actual_num=?, actual_size=?
                             WHERE issue=?""", (status, r["num"], r["size"], issue))
            resolved.append({"issue": issue, "size": size, "num": num, "status": status,
                             "actual_num": r["num"], "actual_size": "BIG" if r["size"] else "SMALL"})
        return resolved

    def stats(self):
        with self._conn() as c:
            total, wins = c.execute("""SELECT COUNT(*),
                SUM(CASE WHEN status IN ('WIN','JACK') THEN 1 ELSE 0 END)
                FROM predictions WHERE status != 'PENDING'""").fetchone()
        if not total:
            return None
        wins = wins or 0
        return {"total": total, "wins": wins, "loss": total - wins,
                "rate": round(100 * wins / total, 1)}

    def recent(self, n=12):
        with self._conn() as c:
            return c.execute("""SELECT issue,size,num,actual_num,actual_size,status
                                FROM predictions WHERE status != 'PENDING'
                                ORDER BY created_at DESC LIMIT ?""", (n,)).fetchall()

    def signal_accuracy(self):
        acc = defaultdict(lambda: [0, 0])
        with self._conn() as c:
            rows = c.execute("""SELECT signals, actual_size FROM predictions
                                WHERE status != 'PENDING' AND signals IS NOT NULL""").fetchall()
        for sig_json, actual in rows:
            if actual is None:
                continue
            try:
                sig = json.loads(sig_json)
            except Exception:
                continue
            for name, p in sig.items():
                acc[name][1] += 1
                if (1 if p >= 0.5 else 0) == actual:
                    acc[name][0] += 1
        return {k: tuple(v) for k, v in acc.items()}

    def last_issue(self):
        with self._conn() as c:
            row = c.execute("SELECT issue FROM predictions ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else None


# ============================================================
# SIGNAL FRAMEWORK
# ============================================================
@dataclass
class SignalResult:
    name: str
    p_big: float       # 0..1 predicted probability of BIG
    strength: float    # 0..1 how strongly this signal votes
    detail: str = ""


class Signal:
    name = "base"
    def compute(self, sizes, nums) -> Optional[SignalResult]:
        raise NotImplementedError


class PatternSignal(Signal):
    """Longest matching historical pattern -> Bayesian smoothed next outcome."""
    name = "pattern"
    def compute(self, sizes, nums):
        for k in range(12, 3, -1):
            if len(sizes) < k + 1:
                continue
            pat = tuple(sizes[-k:])
            big = small = 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i + k]) == pat:
                    big += 1 if sizes[i + k] else 0
                    small += 0 if sizes[i + k] else 1
            t = big + small
            if t >= (1 if k >= 8 else 2):
                return SignalResult(self.name, (big + 1) / (t + 2),
                                    min(0.9, 0.15 + t * 0.1), f"P{k}:{big}B/{small}S")
        return None


class StreakSignal(Signal):
    """Streak continuation / momentum."""
    name = "streak"
    def compute(self, sizes, nums):
        if len(sizes) < 2:
            return None
        last = sizes[-1]
        streak = 1
        for i in range(len(sizes) - 2, -1, -1):
            if sizes[i] == last:
                streak += 1
            else:
                break
        if streak < 2:
            return SignalResult(self.name, 0.5, 0.0, "none")
        direction = 1 if last else -1
        p = max(0.05, min(0.95, 0.5 + direction * min(0.35, 0.08 * streak)))
        return SignalResult(self.name, p, min(0.8, 0.15 * streak), f"streak{streak}")


class MeanRevSignal(Signal):
    """Mean reversion over rolling window."""
    name = "meanrev"
    def __init__(self, window=20):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        bc = sum(sizes[-self.window:])
        ratio = bc / self.window
        strength = min(0.7, abs(ratio - 0.5) * 2)
        return SignalResult(self.name, 1 - ratio, strength, f"{bc}/{self.window}B")


class MarkovSignal(Signal):
    """Markov chain of given order."""
    def __init__(self, order=2):
        self.order = order
        self.name = f"markov{order}"
    def compute(self, sizes, nums):
        if len(sizes) <= self.order + 2:
            return None
        key = tuple(sizes[-self.order:])
        trans = Counter()
        for i in range(len(sizes) - self.order):
            if tuple(sizes[i:i + self.order]) == key:
                trans[sizes[i + self.order]] += 1
        if not trans:
            return None
        t = sum(trans.values())
        p = (trans.get(1, 0) + 1) / (t + 2)
        return SignalResult(self.name, p, min(0.7, t * 0.1), f"{t}obs")


class FreqSignal(Signal):
    """Global frequency bias -> deviation from fair 50/50."""
    name = "freq"
    def __init__(self, window=50):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        ratio = sum(sizes[-self.window:]) / self.window
        p = 0.5 + max(-0.15, min(0.15, (0.5 - ratio) * 0.5))
        return SignalResult(self.name, p, min(0.4, abs(ratio - 0.5)), f"{ratio:.2f}")


class EntropySignal(Signal):
    """Shannon entropy — low entropy = predictable, high = neutral."""
    name = "entropy"
    def __init__(self, window=20):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        w = sizes[-self.window:]
        p1 = sum(w) / len(w)
        p0 = 1 - p1
        ent = 0.0
        for p in (p0, p1):
            if p > 0:
                ent -= p * (p ** 0.5)  # sqrt-entropy cheap proxy
        major = 1 if p1 >= 0.5 else 0
        return SignalResult(self.name, 1.0 if major else 0.0,
                            max(0.0, 0.4 - ent), f"ent~{ent:.2f}")


class AutocorrSignal(Signal):
    """Lag-N autocorrelation of size sequence."""
    name = "autocorr"
    def __init__(self, lag=1):
        self.lag = lag
    def compute(self, sizes, nums):
        if len(sizes) < 30:
            return None
        x = sizes[-40:]
        n = len(x) - self.lag
        if n < 10:
            return None
        mean = sum(x) / len(x)
        num = sum((x[i] - mean) * (x[i + self.lag] - mean) for i in range(n))
        den = sum((v - mean) ** 2 for v in x) or 1e-9
        r = num / den
        # positive autocorr -> trend continues; negative -> flips
        last = sizes[-1]
        p = 0.5 + r * (0.3 if last else -0.3)
        return SignalResult(self.name, max(0.05, min(0.95, p)), min(0.4, abs(r)), f"r={r:+.2f}")


class AlternationSignal(Signal):
    """How often sequence switches vs repeats."""
    name = "alternation"
    def __init__(self, window=20):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        w = sizes[-self.window:]
        switches = sum(1 for i in range(1, len(w)) if w[i] != w[i - 1])
        sw_rate = switches / (len(w) - 1)
        last = sizes[-1]
        # high switch rate -> predict flip; low -> predict repeat
        p_flip = sw_rate
        p = (1 - last) * p_flip + last * (1 - p_flip)
        return SignalResult(self.name, p, abs(sw_rate - 0.5), f"sw={sw_rate:.2f}")


class MomentumSignal(Signal):
    """Recent short-window majority."""
    name = "momentum"
    def __init__(self, window=10):
        self.window = window
    def compute(self, sizes, nums):
        if len(sizes) < self.window:
            return None
        r = sum(sizes[-self.window:]) / self.window
        return SignalResult(self.name, r, min(0.3, abs(r - 0.5)), f"{r:.2f}")


class RunLengthSignal(Signal):
    """Average run length vs current run."""
    name = "runlength"
    def compute(self, sizes, nums):
        if len(sizes) < 30:
            return None
        runs = []
        cur = 1
        for i in range(1, len(sizes)):
            if sizes[i] == sizes[i - 1]:
                cur += 1
            else:
                runs.append(cur)
                cur = 1
        runs.append(cur)
        avg = sum(runs) / len(runs) if runs else 1
        last = sizes[-1]
        if runs[-1] > avg:
            p = 0.0 if last else 1.0  # overdue for flip
            strength = min(0.4, (runs[-1] - avg) / max(avg, 1))
        else:
            p = 1.0 if last else 0.0
            strength = 0.15
        return SignalResult(self.name, p, strength, f"run{runs[-1]}/avg{avg:.1f}")


# ============================================================
# PREDICTOR (ensemble orchestrator)
# ============================================================
class Predictor:
    def __init__(self, storage: Storage):
        self.store = storage
        self.history = []
        self.signals = [
            PatternSignal(), StreakSignal(), MeanRevSignal(20),
            MarkovSignal(2), MarkovSignal(3), FreqSignal(50),
            EntropySignal(20), AutocorrSignal(1), AlternationSignal(20),
            MomentumSignal(10), RunLengthSignal(),
        ]
        self.acc_weights = self._acc_weights()

    def _acc_weights(self):
        acc = self.store.signal_accuracy()
        w = {}
        for s in self.signals:
            hit, tot = acc.get(s.name, (0, 0))
            w[s.name] = (0.5 + hit / tot) if tot >= 20 else 1.0
        return w

    async def fetch(self, session):
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                raw = data.get("data", {}).get("list", [])
                out = []
                for item in raw:
                    try:
                        out.append({"issue": str(item["issueNumber"]),
                                    "num": int(item["number"]),
                                    "size": 1 if int(item["number"]) >= 5 else 0})
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

    # ---------- pure core (used by predict AND backtest) ----------
    def _core(self, sizes, nums):
        if len(sizes) < 50:
            return None
        results = {}
        for sig in self.signals:
            try:
                r = sig.compute(sizes, nums)
            except Exception:
                r = None
            if r and r.strength > 0:
                results[r.name] = r
        if not results:
            return None

        num = den = 0.0
        votes = {}
        for name, r in results.items():
            w = r.strength * self.acc_weights.get(name, 1.0)
            num += r.p_big * w
            den += w
            votes[name] = round(r.p_big, 3)
        p_big = num / den if den else 0.5
        pred_size = 1 if p_big >= 0.5 else 0
        confidence = 50 + round(min(1.0, abs(p_big - 0.5) * 2) * 40)

        pool = list(range(5, 10)) if pred_size else list(range(0, 5))
        seen = {n: len(nums) for n in range(10)}
        for i in range(len(nums) - 1, -1, -1):
            n = nums[i]
            if seen[n] == len(nums):
                seen[n] = len(nums) - i
        freq = Counter(nums[-50:])
        best_num = max(pool, key=lambda n: (seen[n], -freq.get(n, 0)))

        return {"p_big": p_big, "pred_size": pred_size, "confidence": confidence,
                "signals": votes, "num": best_num,
                "logic": " + ".join(list(votes)[:4])}

    def predict(self):
        core = self._core([h["size"] for h in self.history], [h["num"] for h in self.history])
        if not core:
            return None
        return {"issue": str(int(self.history[-1]["issue"]) + 1),
                "size": "BIG" if core["pred_size"] else "SMALL",
                "num": core["num"], "confidence": core["confidence"],
                "logic": core["logic"], "signals": core["signals"], "status": "PENDING"}

    # ---------- backtest ----------
    def backtest(self, min_history=80):
        sizes = [h["size"] for h in self.history]
        nums = [h["num"] for h in self.history]
        if len(sizes) < min_history + 20:
            return None
        hits = total = 0
        sig_hits, sig_tot = defaultdict(int), defaultdict(int)
        calib = defaultdict(lambda: [0, 0])
        for i in range(min_history, len(sizes)):
            core = self._core(sizes[:i], nums[:i])
            if not core:
                continue
            actual = sizes[i]
            total += 1
            ok = core["pred_size"] == actual
            hits += ok
            for name, p in core["signals"].items():
                sig_tot[name] += 1
                if (1 if p >= 0.5 else 0) == actual:
                    sig_hits[name] += 1
            b = (core["confidence"] // 10) * 10
            calib[b][1] += 1
            calib[b][0] += ok
        tail = sizes[min_history:]
        big_base = max(sum(tail), len(tail) - sum(tail)) / max(1, len(tail))
        return {"total": total, "hits": hits,
                "acc": hits / total if total else 0,
                "big_baseline": big_base,
                "signal_acc": {k: (sig_hits[k], sig_tot[k]) for k in sig_tot},
                "calib": {k: tuple(v) for k, v in sorted(calib.items())}}

    def refresh_weights(self):
        self.acc_weights = self._acc_weights()


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
        "11 signal engines • adaptive ensemble • backtesting\n"
        "Commands: /stats /history /signals /backtest\n"
        "⚠️ RNG draws — no real edge. Heuristic/entertainment only.",
        parse_mode="HTML")


async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = predictor.store.stats()
    if not s:
        await u.message.reply_text("📊 Koi resolved prediction nahi abhi.")
        return
    await u.message.reply_text(
        f"📊 <b>Stats</b>\nTotal: {s['total']}\n"
        f"Win: {s['wins']} | Loss: {s['loss']}\n"
        f"Rate: <b>{s['rate']}%</b>  <i>(coin-flip = 50%)</i>",
        parse_mode="HTML")


async def cmd_history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = predictor.store.recent(15)
    if not rows:
        await u.message.reply_text("🗂 Empty.")
        return
    lines = [f"<code>{r[0][-4:]}</code> {r[4]} {r[3]} → <b>{r[5]}</b>" for r in rows]
    await u.message.reply_text("🗂 <b>Recent</b>\n" + "\n".join(lines), parse_mode="HTML")


async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    acc = predictor.store.signal_accuracy()
    lines = []
    for name, (hit, tot) in sorted(acc.items(), key=lambda kv: -kv[1][1]):
        rate = 100 * hit / tot if tot else 0
        lines.append(f"{name:<12} {rate:5.1f}%  ({hit}/{tot})")
    if not lines:
        await u.message.reply_text("Signals ko abhi 20+ samples chahiye.")
        return
    await u.message.reply_text(
        "🧪 <b>Per-signal accuracy</b>\n<pre>" + "\n".join(lines) + "</pre>\n"
        "<i>50% = no edge</i>", parse_mode="HTML")


async def cmd_backtest(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ Backtesting...")
    bt = await asyncio.to_thread(predictor.backtest)
    if not bt:
        await u.message.reply_text("History kam hai (50+ chahiye).")
        return
    sig_lines = []
    for name, (hit, tot) in sorted(bt["signal_acc"].items(), key=lambda kv: -kv[1][1]):
        sig_lines.append(f"{name:<12} {100*hit/tot:5.1f}%  ({hit}/{tot})")
    calib_lines = []
    for b, (hit, tot) in bt["calib"].items():
        calib_lines.append(f"{b}-{b+9}%: {100*hit/tot:5.1f}% actual ({tot})")
    await u.message.reply_text(
        f"📉 <b>Backtest ({bt['total']} samples)</b>\n"
        f"Model accuracy: <b>{100*bt['acc']:.1f}%</b>\n"
        f"Always-majority baseline: <b>{100*bt['big_baseline']:.1f}%</b>\n"
        f"<i>(50% = coin flip. Edge = accuracy above baseline.)</i>\n\n"
        f"🧪 <b>Per-signal</b>:\n<pre>" + "\n".join(sig_lines) + "</pre>\n\n"
        f"🎯 <b>Confidence calibration</b>:\n<pre>" + "\n".join(calib_lines) + "</pre>\n"
        f"<i>Calibration flat hona chahiye — matlab confidence fake hai.</i>",
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
                            f"<b>{res['status']}</b>")
                    latest = str(int(predictor.history[-1]["issue"]) + 1) if predictor.history else None
                    if latest and latest != predictor.store.last_issue():
                        p = predictor.predict()
                        if p:
                            predictor.store.save(p)
                            await safe_send(app, fmt(p))
                            log.info(f"pred {p['issue']}")
                await asyncio.sleep(FETCH_EVERY)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(f"loop: {e}")
                await asyncio.sleep(3)


def fmt(p):
    rows = predictor.store.recent(10)
    recent = "".join(f"➜ <code>{r[0][-4:]}</code> {r[4]} {r[3]} → <b>{r[5]}</b>\n" for r in rows)
    return (f"📌 <b>Period</b>: <code>{p['issue'][-4:]}</code>\n"
            f"🎯 <b>Target</b>: <code>{p['num']}</code>\n"
            f"📊 <b>Size</b>: <b>{p['size']}</b>\n\n"
            f"📈 <b>Confidence</b>: {p['confidence']}% <i>(heuristic)</i>\n"
            f"🧮 <b>Signals</b>: {p['logic']}\n"
            f"━━━━━━━━━━━━━\n🕒 <b>Recent</b>:\n{recent}"
            f"━━━━━━━━━━━━━\n🧬 <i>{BOT_NAME}</i>")


async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for name, fn in [("start", cmd_start), ("stats", cmd_stats), ("history", cmd_history),
                     ("signals", cmd_signals), ("backtest", cmd_backtest)]:
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


