import asyncio
import aiohttp
import json
import os
import contextlib
import logging
from collections import Counter
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIG — secrets env vars se lo, code mein mat likho
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8611789455:AAEQb9B7N565qGDp--AR6Z1v_4lhzUIu-PQ")
CHAT_ID        = os.getenv("CHAT_ID", "1264164655")
API_URL        = os.getenv("API_URL", "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000")

STATE_FILE  = os.getenv("STATE_FILE", "predictor_state.json")
FETCH_EVERY = float(os.getenv("FETCH_EVERY", "1.5"))
MAX_LOGS    = 200
BOT_NAME    = "V80 UNIFIED HEURISTIC ENGINE"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("predictor")


# ============================================================
# PERSISTENT STATE
# ============================================================
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"logs": [], "last_predicted_issue": None}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"save_state failed: {e}")


# ============================================================
# HEURISTIC PREDICTOR  (entertainment / logging only)
# ------------------------------------------------------------
# Wingo draws are RNG-based and independent. No engine here can
# produce a real edge. Signals are transparent, bundled, and
# labelled — treat them as a filter, not a money-making tool.
# ============================================================
class HeuristicPredictor:
    def __init__(self, state):
        self.history = []
        self.logs = state.get("logs", [])
        self.last_predicted_issue = state.get("last_predicted_issue")

    def snapshot(self):
        return {"logs": self.logs, "last_predicted_issue": self.last_predicted_issue}

    async def fetch_data(self, session):
        try:
            async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                raw = data.get("data", {}).get("list", [])
                cleaned = []
                for item in raw:
                    try:
                        issue = str(item["issueNumber"])
                        num = int(item["number"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    cleaned.append({"issue": issue, "num": num, "size": 1 if num >= 5 else 0})
                cleaned.sort(key=lambda x: int(x["issue"]))
                self.history = cleaned
                return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"fetch failed: {e}")
            return False

    # ---------------- signal engines ----------------
    def _engines(self):
        sized = [h["size"] for h in self.history]
        out = []  # (p_big, weight, tag)

        # 1) Longest matching pattern
        for k in range(12, 3, -1):
            if len(sized) < k + 1:
                continue
            pat = tuple(sized[-k:])
            big = small = 0
            for i in range(len(sized) - k):
                if tuple(sized[i:i + k]) == pat:
                    if sized[i + k] == 1:
                        big += 1
                    else:
                        small += 1
            total = big + small
            need = 1 if k >= 8 else 2
            if total >= need:
                p = (big + 1) / (total + 2)
                out.append((p, min(0.35, k * 0.035), f"P{k}({big}B/{small}S)"))
                break

        # 2) Streak persistence
        last = sized[-1]
        streak = 1
        for i in range(len(sized) - 2, -1, -1):
            if sized[i] == last:
                streak += 1
            else:
                break
        if streak == 2:
            out.append((0.58 if last else 0.42, 0.10, f"Streak{streak}"))
        elif streak == 3:
            out.append((0.70 if last else 0.30, 0.18, f"Streak{streak}"))
        elif streak >= 4:
            out.append((0.82 if last else 0.18, 0.28, f"Streak{streak}"))

        # 3) Mean reversion (20-window)
        w20 = sized[-20:]
        bc = sum(w20)
        if bc >= 14:
            out.append((0.28, 0.15, "MeanRev20"))
        elif bc <= 6:
            out.append((0.72, 0.15, "MeanRev20"))

        # 4) Markov order-2
        if len(sized) > 3:
            key = (sized[-2], sized[-1])
            trans = Counter()
            for i in range(len(sized) - 2):
                if (sized[i], sized[i + 1]) == key:
                    trans[sized[i + 2]] += 1
            if trans:
                t = sum(trans.values())
                out.append(((trans.get(1, 0) + 1) / (t + 2), 0.15, "Markov"))

        # 5) Global frequency bias (50-window)
        w50 = sized[-50:]
        ratio = sum(w50) / len(w50) if w50 else 0.5
        p = 0.5 + max(-0.15, min(0.15, (0.5 - ratio) * 0.5))
        out.append((p, 0.10, "Freq50"))

        return out

    def predict(self):
        if len(self.history) < 50:
            return None

        engines = self._engines()
        if engines:
            wsum = sum(w for _, w, _ in engines)
            p_big = sum(p * w for p, w, _ in engines) / wsum
            tags = [t for _, _, t in engines]
        else:
            p_big, tags = 0.5, ["Neutral"]

        pred_size = 1 if p_big >= 0.5 else 0
        confidence = int(min(90, max(51, round(max(p_big, 1 - p_big) * 100))))

        # number pick inside the predicted bucket (coldest + least frequent)
        nums = [h["num"] for h in self.history]
        pool = list(range(5, 10)) if pred_size == 1 else list(range(0, 5))
        last_seen = {n: len(nums) for n in range(10)}
        for idx in range(len(nums) - 1, -1, -1):
            n = nums[idx]
            if last_seen[n] == len(nums):
                last_seen[n] = len(nums) - idx
        freq = Counter(nums[-50:])
        best_num = max(pool, key=lambda n: (last_seen[n], -freq.get(n, 0)))

        return {
            "issue": str(int(self.history[-1]["issue"]) + 1),
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": " + ".join(tags[:3]),
            "status": "PENDING",
        }

    def check_results(self):
        resolved = []
        hist = {h["issue"]: h for h in self.history}
        for log_ in self.logs:
            if log_["status"] == "PENDING" and log_["issue"] in hist:
                res = hist[log_["issue"]]
                win = res["size"] == (1 if log_["size"] == "BIG" else 0)
                if win:
                    log_["status"] = "JACK 🎰" if res["num"] == log_["num"] else "WIN ✅"
                else:
                    log_["status"] = "LOSS ❌"
                log_["actual_num"] = res["num"]
                log_["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                resolved.append(log_)
        return resolved

    def stats(self):
        done = [l for l in self.logs if l["status"] != "PENDING"]
        if not done:
            return None
        wins = sum(1 for l in done if "WIN" in l["status"] or "JACK" in l["status"])
        return {
            "total": len(done),
            "wins": wins,
            "loss": len(done) - wins,
            "rate": round(100 * wins / len(done), 1),
        }


engine = HeuristicPredictor(load_state())


# ============================================================
# TELEGRAM HELPERS
# ============================================================
async def safe_send(app, text, retries=3):
    for attempt in range(retries):
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
            return True
        except Exception as e:
            log.warning(f"send failed ({attempt + 1}/{retries}): {e}")
            await asyncio.sleep(2)
    return False


def fmt_pred(p):
    resolved = [l for l in engine.logs if l["status"] != "PENDING"][-10:]
    recent = "".join(
        f"➜ <code>{l['issue'][-4:]}</code> {l.get('actual_size', '?')} "
        f"{l.get('actual_num', '?')} → <b>{l['status']}</b>\n"
        for l in resolved
    )
    return (
        f"📌 <b>Period</b>: <code>{p['issue'][-4:]}</code>\n"
        f"🎯 <b>Target</b>: <code>{p['num']}</code>\n"
        f"📊 <b>Size</b>: <b>{p['size']}</b>\n\n"
        f"📈 <b>Confidence</b>: {p['confidence']}% <i>(heuristic)</i>\n"
        f"🧮 <b>Signals</b>: {p['logic']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Recent</b>:\n{recent}"
        f"━━━━━━━━━━━━━━━\n"
        f"🧬 <i>{BOT_NAME}</i>"
    )


# ============================================================
# COMMANDS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🧬 {BOT_NAME} active.\n"
        "Heuristic prediction + result tracking.\n"
        "⚠️ Draws are RNG — no real edge. Entertainment only.",
        parse_mode="HTML",
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = engine.stats()
    if not s:
        await update.message.reply_text("📊 Abhi koi resolved result nahi hai.")
        return
    await update.message.reply_text(
        f"📊 <b>Stats</b>\n"
        f"Total: {s['total']}\n"
        f"Wins: {s['wins']} | Loss: {s['loss']}\n"
        f"Win rate: <b>{s['rate']}%</b>",
        parse_mode="HTML",
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resolved = [l for l in engine.logs if l["status"] != "PENDING"][-15:]
    if not resolved:
        await update.message.reply_text("🗂 History khaali hai.")
        return
    lines = [
        f"<code>{l['issue'][-4:]}</code> {l.get('actual_size', '?')} "
        f"{l.get('actual_num', '?')} → {l['status']}"
        for l in resolved
    ]
    await update.message.reply_text("🗂 <b>Last Results</b>\n" + "\n".join(lines), parse_mode="HTML")


# ============================================================
# BACKGROUND LOOP
# ============================================================
async def background_loop(app):
    log.info("Background loop started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if await engine.fetch_data(session):
                    for res in engine.check_results():
                        await safe_send(
                            app,
                            f"🎯 <b>Result</b>\nPeriod: <code>{res['issue'][-4:]}</code>\n"
                            f"Pred: {res['size']} {res['num']}\n"
                            f"Actual: {res['actual_size']} {res['actual_num']}\n"
                            f"Status: <b>{res['status']}</b>",
                        )
                        save_state(engine.snapshot())

                    if engine.history:
                        latest = str(int(engine.history[-1]["issue"]) + 1)
                        if engine.last_predicted_issue != latest:
                            pred = engine.predict()
                            if pred:
                                engine.logs.append(pred)
                                if len(engine.logs) > MAX_LOGS:
                                    del engine.logs[: len(engine.logs) - MAX_LOGS]
                                engine.last_predicted_issue = latest
                                save_state(engine.snapshot())
                                await safe_send(app, fmt_pred(pred))
                                log.info(f"Predicted for {pred['issue']}")
                await asyncio.sleep(FETCH_EVERY)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(f"loop error: {e}")
                await asyncio.sleep(3)


async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("history", history_cmd))

    async with app:
        await app.start()
        await app.updater.start_polling()
        bg = asyncio.create_task(background_loop(app))
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

