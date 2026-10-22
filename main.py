import asyncio
import contextlib
import gc
import html
import json
import logging
import math
import os
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# CONFIG
# =========================
@dataclass
class Config:
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA")
    chat_id_raw: str = os.getenv("CHAT_ID", "1264164655")
    source_api_url: str = os.getenv("SOURCE_API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000")
    db_path: str = "predictor.db"
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 6
    bot_name: str = "V111 SKYNET 1M DEEP"

    def parse_chat_id(self):
        return int(self.chat_id_raw) if self.chat_id_raw.isdigit() else self.chat_id_raw

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
    fh = RotatingFileHandler(Path(CFG.log_dir) / "bot.log", maxBytes=1_000_000, backupCount=1)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v111_1m_deep")

def h(x: Any) -> str:
    return html.escape(str(x))

ENGINE_NAMES = ["pattern", "markov", "cycle", "trend", "entropy", "streak"]

# =========================
# STORAGE
# =========================
class Storage:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    issue TEXT PRIMARY KEY, size TEXT, num INTEGER,
                    status TEXT DEFAULT 'PENDING', actual_num INTEGER, actual_size TEXT,
                    logic TEXT, engine_probs TEXT, confidence REAL, created_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS engine_weights (
                    name TEXT PRIMARY KEY,
                    w_big REAL DEFAULT 1.0, w_small REAL DEFAULT 1.0,
                    wins_big INTEGER DEFAULT 0, losses_big INTEGER DEFAULT 0,
                    wins_small INTEGER DEFAULT 0, losses_small INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS calibration (
                    bucket INTEGER PRIMARY KEY,
                    wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0
                )
            """)
            for col in ["confidence"]:
                try:
                    c.execute(f"ALTER TABLE predictions ADD COLUMN {col} REAL")
                except sqlite3.OperationalError:
                    pass

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c

    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None

    def save_prediction(self, issue, size, num, logic, engine_probs, confidence):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO predictions (issue, size, num, logic, engine_probs, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (issue, size, num, logic, json.dumps(engine_probs), confidence, time.time())
            )

    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue, size, num, engine_probs, confidence FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, probs_json, confidence in rows:
            if issue in history_dict:
                act = history_dict[issue]
                act_size_str = "BIG" if act['size'] == 1 else "SMALL"
                win = (p_size == act_size_str)
                status = ("JACK 🏆" if p_num == act['num'] else "WIN ✅") if win else "LOSS ❌"
                with self._conn() as c:
                    c.execute(
                        "UPDATE predictions SET status=?, actual_num=?, actual_size=? WHERE issue=?",
                        (status, act['num'], act_size_str, issue)
                    )
                resolved.append({
                    "issue": issue, "size": p_size, "num": p_num,
                    "actual_num": act['num'], "actual_size": act_size_str,
                    "status": status, "engine_probs": probs_json,
                    "confidence": confidence or 50.0, "win": win
                })
        return resolved

    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute(
                "SELECT issue, actual_size, actual_num, status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return rows[::-1]

    def save_engine_weights(self, engines: dict):
        with self._conn() as c:
            for name, e in engines.items():
                c.execute("""
                    INSERT INTO engine_weights (name, w_big, w_small, wins_big, losses_big, wins_small, losses_small)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        w_big=excluded.w_big, w_small=excluded.w_small,
                        wins_big=excluded.wins_big, losses_big=excluded.losses_big,
                        wins_small=excluded.wins_small, losses_small=excluded.losses_small
                """, (name, e['w_big'], e['w_small'], e['wins_big'], e['losses_big'], e['wins_small'], e['losses_small']))

    def load_engine_weights(self):
        out = {}
        with self._conn() as c:
            rows = c.execute("SELECT name, w_big, w_small, wins_big, losses_big, wins_small, losses_small FROM engine_weights").fetchall()
        for name, w_big, w_small, wb, lb, ws, ls in rows:
            out[name] = {'w_big': w_big, 'w_small': w_small, 'wins_big': wb, 'losses_big': lb, 'wins_small': ws, 'losses_small': ls}
        return out

    def update_calibration(self, bucket: int, won: bool):
        with self._conn() as c:
            c.execute("INSERT INTO calibration (bucket, wins, losses) VALUES (?, ?, ?) ON CONFLICT(bucket) DO NOTHING", (bucket, 0, 0))
            if won:
                c.execute("UPDATE calibration SET wins = wins + 1 WHERE bucket=?", (bucket,))
            else:
                c.execute("UPDATE calibration SET losses = losses + 1 WHERE bucket=?", (bucket,))

    def load_calibration(self):
        out = {}
        with self._conn() as c:
            rows = c.execute("SELECT bucket, wins, losses FROM calibration").fetchall()
        for bucket, wins, losses in rows:
            out[bucket] = (wins, losses)
        return out

STORE = Storage(CFG.db_path)

# =========================
# V111 SKYNET DEEP ENGINE
# =========================
class SkynetPredictor:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.win_streak = 0

        default = lambda: {'w_big': 1.0, 'w_small': 1.0, 'wins_big': 0, 'losses_big': 0, 'wins_small': 0, 'losses_small': 0}
        loaded = STORE.load_engine_weights()
        self.engines = {}
        for name in ENGINE_NAMES:
            self.engines[name] = loaded.get(name, default())

        self.calibration = STORE.load_calibration()

    # ---------- DATA ----------
    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])
                items.sort(key=lambda x: int(x["issueNumber"]))

                out = []
                seen = set()
                for item in items:
                    try:
                        issue = str(item["issueNumber"])
                        if issue in seen:
                            continue
                        seen.add(issue)
                        num = int(item["number"])
                        out.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                    except Exception:
                        continue
                self.history = out
                return True
        except Exception:
            return False

    # ---------- RECENCY WEIGHT HELPER ----------
    @staticmethod
    def _decay_weight(distance_from_end, decay=0.985):
        return decay ** distance_from_end

    # ---------- ENGINES ----------
    def engine_pattern(self, sizes):
        n = len(sizes)
        for k in range(15, 3, -1):
            if n < k + 1:
                continue
            pat = tuple(sizes[-k:])
            big, small = 0.0, 0.0
            for i in range(n - k):
                if tuple(sizes[i:i + k]) == pat:
                    w = self._decay_weight(n - (i + k))
                    if sizes[i + k] == 1:
                        big += w
                    else:
                        small += w
            total = big + small
            if total >= 1.0:
                return (big + 1) / (total + 2), f"P{k}"
        return 0.5, "P_None"

    def engine_markov(self, sizes, loss_streak):
        n = len(sizes)
        orders = [2, 3, 4, 5]
        combined_num, combined_den = 0.0, 0.0
        used_orders = []

        for order in orders:
            if n < order + 1:
                continue
            state = tuple(sizes[-order:])
            big, small = 0.0, 0.0
            for i in range(n - order):
                if tuple(sizes[i:i + order]) == state:
                    w = self._decay_weight(n - (i + order))
                    if sizes[i + order] == 1:
                        big += w
                    else:
                        small += w
            total = big + small
            if total >= 1.0:
                p_order = (big + 1) / (total + 2)
                order_weight = order * min(1.0, total / 8.0)
                if loss_streak >= 2 and order >= 4:
                    order_weight *= 1.6  # trust higher order more in recovery
                combined_num += p_order * order_weight
                combined_den += order_weight
                used_orders.append(str(order))

        if combined_den <= 0:
            return 0.5, "M_None"
        return combined_num / combined_den, f"M[{','.join(used_orders)}]"

    def engine_cycle(self, sizes):
        n = len(sizes)
        best_lag, best_score = 1, 0.0
        for lag in range(2, 10):
            if n < lag * 2 + 20:
                continue
            score = 0.0
            window = range(max(0, n - lag - 40), n - lag)
            for i in window:
                if sizes[i] == sizes[i + lag]:
                    score += self._decay_weight(n - (i + lag))
            if score > best_score:
                best_score = score
                best_lag = lag
        if best_score >= 5.0:
            pred = sizes[-best_lag]
            return (0.76 if pred == 1 else 0.24), f"Cyc{best_lag}"
        return 0.5, "C_None"

    def engine_trend(self, sizes):
        if len(sizes) < 20:
            return 0.5, "T_None"
        ema = sizes[-20]
        alpha = 0.3
        for s in sizes[-19:]:
            ema = alpha * s + (1 - alpha) * ema
        sma = sum(sizes[-20:]) / 20
        diff = ema - sma
        if diff > 0.15:
            return 0.7, "Trend_Bull"
        if diff < -0.15:
            return 0.3, "Trend_Bear"
        return 0.5, "Trend_Neutral"

    def engine_entropy(self, sizes):
        if len(sizes) < 20:
            return 0.5, "E_None", 1.0
        w = sizes[-20:]
        p1 = sum(w) / 20.0
        p0 = 1 - p1
        ent = 0.0
        if p1 > 0:
            ent -= p1 * math.log2(p1)
        if p0 > 0:
            ent -= p0 * math.log2(p0)
        if ent < 0.8:
            if p1 > 0.5:
                return 0.8, "Ent_Big", ent
            else:
                return 0.2, "Ent_Small", ent
        return 0.5, "Ent_Rand", ent

    def engine_streak_reversal(self, sizes):
        n = len(sizes)
        if n < 10:
            return 0.5, "Str_None"
        cur_val = sizes[-1]
        cur_len = 1
        for i in range(n - 2, -1, -1):
            if sizes[i] == cur_val:
                cur_len += 1
            else:
                break

        # learn: historically, when a streak of this length (or longer) occurred, did it reverse?
        reversed_w, continued_w, total_w = 0.0, 0.0, 0.0
        i = 0
        while i < n - 1:
            j = i
            while j < n - 1 and sizes[j] == sizes[i]:
                j += 1
            seg_len = j - i
            if seg_len >= max(2, cur_len - 1) and j < n:
                w = self._decay_weight(n - j)
                total_w += w
                if sizes[j] != sizes[i]:
                    reversed_w += w
                else:
                    continued_w += w
            i = j

        if total_w < 2.0 or cur_len < 2:
            return 0.5, "Str_None"

        rev_prob = reversed_w / total_w
        # if reversal is likely, predict opposite of current streak value
        p_big = rev_prob if cur_val == 0 else (1 - rev_prob)
        return p_big, f"Str{cur_len}"

    # ---------- WEIGHT HELPERS ----------
    def _engine_weight(self, name, p_big, loss_streak):
        e = self.engines[name]
        base = e['w_big'] if p_big >= 0.5 else e['w_small']

        mult = 1.0
        if loss_streak >= 1:
            if name in ("pattern", "trend"):
                mult *= 0.45
            if name in ("markov", "streak", "cycle"):
                mult *= 1.7
        if loss_streak >= 2:
            if name in ("pattern", "trend"):
                mult *= 0.15  # near-exclude
            if name in ("markov", "streak"):
                mult *= 1.3
        return base * mult

    def _calibrated_confidence(self, raw_conf):
        bucket = int(raw_conf // 5) * 5
        best = None
        for b, (w, l) in self.calibration.items():
            if abs(b - bucket) <= 5 and (w + l) >= 8:
                rate = w / (w + l)
                best = rate * 100
                break
        if best is None:
            return raw_conf
        return 0.5 * raw_conf + 0.5 * best

    # ---------- NUMBER ENSEMBLE ----------
    def _pick_number(self, nums, pool, loss_streak):
        n = len(nums)
        last_num = nums[-1]

        trans = Counter()
        for i in range(n - 1):
            if nums[i] == last_num:
                w = self._decay_weight(n - 1 - i)
                trans[nums[i + 1]] += w

        gaps = {i: 1000 for i in range(10)}
        for i in range(n - 1, -1, -1):
            if gaps[nums[i]] == 1000:
                gaps[nums[i]] = n - i

        freq = Counter()
        for i, v in enumerate(nums[-60:]):
            freq[v] += self._decay_weight(60 - i)

        # exact-number k-gram match
        kgram = Counter()
        for k in range(6, 2, -1):
            if n < k + 1:
                continue
            pat = tuple(nums[-k:])
            for i in range(n - k):
                if tuple(nums[i:i + k]) == pat:
                    w = self._decay_weight(n - (i + k))
                    kgram[nums[i + k]] += w * k  # longer match = stronger signal
            if kgram:
                break

        total_trans = sum(trans.values()) or 1.0
        total_freq = sum(freq.values()) or 1.0
        total_kgram = sum(kgram.values()) or 1.0

        best_num, best_score = pool[0], -1e9
        for num in pool:
            t_score = (trans.get(num, 0) / total_trans) * 100
            g_score = min(gaps[num], 30) / 30 * 100
            f_score = (freq.get(num, 0) / total_freq) * 100
            k_score = (kgram.get(num, 0) / total_kgram) * 100 if kgram else 0

            score = (t_score * 0.35) + (g_score * 0.20) + (f_score * 0.20) + (k_score * 0.25)

            if num == (last_num + 1) % 10 or num == (last_num - 1 + 10) % 10:
                score += 10

            if score > best_score:
                best_score = score
                best_num = num
        return best_num

    # ---------- MAIN PREDICTION ----------
    def generate_prediction(self):
        if len(self.history) < 50:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]

        p_pat, l_pat = self.engine_pattern(sizes)
        p_mark, l_mark = self.engine_markov(sizes, self.loss_streak)
        p_cyc, l_cyc = self.engine_cycle(sizes)
        p_trd, l_trd = self.engine_trend(sizes)
        p_ent, l_ent, entropy_val = self.engine_entropy(sizes)
        p_str, l_str = self.engine_streak_reversal(sizes)

        engine_probs = {
            'pattern': p_pat, 'markov': p_mark, 'cycle': p_cyc,
            'trend': p_trd, 'entropy': p_ent, 'streak': p_str
        }

        weighted_sum, total_w = 0.0, 0.0
        for name, p_big in engine_probs.items():
            w = self._engine_weight(name, p_big, self.loss_streak)
            weighted_sum += p_big * w
            total_w += w

        final_p_big = weighted_sum / total_w if total_w > 0 else 0.5

        # volatility dampening only when NOT recovering (recovery needs decisiveness)
        if self.loss_streak == 0 and entropy_val > 0.95:
            final_p_big = 0.5 + (final_p_big - 0.5) * 0.6

        pred_size = 1 if final_p_big >= 0.5 else 0
        raw_conf = abs(final_p_big - 0.5) * 2 * 100
        conf = self._calibrated_confidence(raw_conf)

        min_conf = 58 if self.loss_streak >= 1 else 52
        max_conf = 93 if self.loss_streak >= 1 else 88
        confidence = int(min(max_conf, max(min_conf, conf)))

        pool = [5, 6, 7, 8, 9] if pred_size == 1 else [0, 1, 2, 3, 4]
        best_num = self._pick_number(nums, pool, self.loss_streak)

        mode_tag = "REC2" if self.loss_streak >= 2 else ("REC1" if self.loss_streak >= 1 else "NORM")
        logic = f"{l_pat}+{l_mark}+{l_cyc}+{l_trd}+{l_str}|{mode_tag}"

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic,
            "engine_probs": engine_probs
        }

    def update_engine_weights(self, engine_probs_json, actual_size):
        if not engine_probs_json:
            return
        try:
            probs = json.loads(engine_probs_json)
        except Exception:
            return
        for name, p_big in probs.items():
            if name not in self.engines:
                continue
            e = self.engines[name]
            pred_dir = 1 if p_big >= 0.5 else 0
            correct = pred_dir == actual_size
            if pred_dir == 1:
                if correct:
                    e['wins_big'] += 1
                    e['w_big'] = min(3.0, e['w_big'] + 0.08)
                else:
                    e['losses_big'] += 1
                    e['w_big'] = max(0.05, e['w_big'] - 0.08)
            else:
                if correct:
                    e['wins_small'] += 1
                    e['w_small'] = min(3.0, e['w_small'] + 0.08)
                else:
                    e['losses_small'] += 1
                    e['w_small'] = max(0.05, e['w_small'] - 0.08)
        STORE.save_engine_weights(self.engines)

    def update_calibration(self, confidence, won):
        bucket = int((confidence or 50) // 5) * 5
        STORE.update_calibration(bucket, won)
        w, l = self.calibration.get(bucket, (0, 0))
        if won:
            w += 1
        else:
            l += 1
        self.calibration[bucket] = (w, l)

PREDICTOR = SkynetPredictor()

# =========================
# FORMATTING
# =========================
def format_prediction(p, recent_rows):
    recent_text = ""
    for r in recent_rows:
        recent_text += f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n"

    mode = "🚨 RECOVERY x2" if PREDICTOR.loss_streak >= 2 else ("⚠️ RECOVERY" if PREDICTOR.loss_streak >= 1 else "🧠 NORMAL")

    return f"""
📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Logic</b>: {h(p['logic'])}
⚖️ <b>Mode</b>: {mode}

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{recent_text}━━━━━━━━━━━━━━━━━━
⚡ <i>V111 SKYNET 1M DEEP</i>
"""

async def send_msg(app, text):
    try:
        await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
    except Exception as e:
        log.error(f"Send error: {e}")

# =========================
# WORKER LOOP
# =========================
async def worker_loop(app, session):
    log.info("V111 Skynet Deep 1M loop started...")
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hist_dict = {x["issue"]: x for x in PREDICTOR.history}

                resolved = STORE.resolve_pending(hist_dict)
                for res in resolved:
                    if "LOSS" in res['status']:
                        PREDICTOR.loss_streak += 1
                        PREDICTOR.win_streak = 0
                    else:
                        PREDICTOR.loss_streak = 0
                        PREDICTOR.win_streak += 1

                    act_int = 1 if res['actual_size'] == 'BIG' else 0
                    PREDICTOR.update_engine_weights(res.get('engine_probs'), act_int)
                    PREDICTOR.update_calibration(res.get('confidence'), res.get('win'))

                    text = (f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\n"
                            f"Pred: {h(res['size'])} {h(res['num'])}\n"
                            f"Act: {h(res['actual_size'])} {h(res['actual_num'])}\n"
                            f"<b>{h(res['status'])}</b>")
                    await send_msg(app, text)

                latest_issue = PREDICTOR.history[-1]["issue"]
                next_issue = str(int(latest_issue) + 1)

                if not STORE.prediction_exists(next_issue):
                    p = PREDICTOR.generate_prediction()
                    if p:
                        STORE.save_prediction(p['issue'], p['size'], p['num'], p['logic'], p['engine_probs'], p['confidence'])
                        recent = STORE.recent_outcomes(12)
                        await send_msg(app, format_prediction(p, recent))

            gc.collect()
            await asyncio.sleep(CFG.fetch_every)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            await asyncio.sleep(2)

# =========================
# TELEGRAM COMMANDS
# =========================
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"🤖 {CFG.bot_name}", f"Loss streak: {PREDICTOR.loss_streak}", f"Win streak: {PREDICTOR.win_streak}", "", "Engine weights:"]
    for name, e in PREDICTOR.engines.items():
        lines.append(f"{name}: big={e['w_big']:.2f} small={e['w_small']:.2f}")
    await update.message.reply_text("\n".join(lines))

# =========================
# WEB SERVER (health check)
# =========================
async def handle_ping(request):
    return web.Response(text="V111 Deep 1M is running perfectly!")

async def main():
    CFG.validate()

    web_app = web.Application()
    web_app.router.add_get('/', handle_ping)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server running on port {port}")

    app = Application.builder().token(CFG.telegram_token).build()
    app.add_handler(CommandHandler("status", cmd_status))

    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)

        async with aiohttp.ClientSession() as session:
            bg_task = asyncio.create_task(worker_loop(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
