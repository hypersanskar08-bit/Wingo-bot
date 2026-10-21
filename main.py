import requests
import asyncio
import time
import numpy as np
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# 1. CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655"          # e.g. "@my_channel" or "-100123456789"
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"

# ============================================================
# 2. DATA FETCHING & CLEANING
# ============================================================
class DataEngine:
    def __init__(self):
        self.history = []          # oldest -> newest

    def fetch(self):
        try:
            r = requests.get(API_URL, timeout=15)
            j = r.json()
            lst = j.get("data", {}).get("list", [])
            cleaned = []
            for item in lst:
                issue = str(item["issueNumber"])
                num = int(item["number"])
                size = 1 if num >= 5 else 0   # 1 = BIG, 0 = SMALL
                cleaned.append({"issue": issue, "num": num, "size": size})
            # sort by issue number (numeric)
            cleaned.sort(key=lambda x: int(x["issue"]))
            self.history = cleaned
            return True
        except Exception as e:
            print(f"[API ERROR] {e}")
            return False

    def get_last_issue(self):
        if not self.history:
            return None
        return self.history[-1]["issue"]

    def next_issue(self):
        last = self.get_last_issue()
        if not last:
            return None
        return str(int(last) + 1)

# ============================================================
# 3. PATTERN ENGINES (DEEP UPGRADE)
# ============================================================

def pattern_run(history, lookback=100):
    """Analyze run-length patterns (streak continuation vs break)."""
    sizes = [x["size"] for x in history[-lookback:]]
    if len(sizes) < 10:
        return None
    # current streak
    cur = sizes[-1]
    streak = 1
    for i in range(len(sizes)-2, -1, -1):
        if sizes[i] == cur:
            streak += 1
        else:
            break
    # historical continuation probability for similar streaks
    cont = 0
    brk = 0
    for i in range(len(sizes)-2):
        if sizes[i] == cur:
            s = 1
            j = i-1
            while j >= 0 and sizes[j] == cur:
                s += 1
                j -= 1
            if s == streak:
                if i+1 < len(sizes):
                    if sizes[i+1] == cur:
                        cont += 1
                    else:
                        brk += 1
    total = cont + brk
    if total < 3:
        return None
    prob_cont = (cont + 1) / (total + 2)
    vote = 1 if prob_cont >= 0.5 else -1   # +1 BIG, -1 SMALL
    conf = max(prob_cont, 1-prob_cont)
    return {"vote": vote, "conf": conf, "engine": "RUN", "detail": f"streak={streak} cont={cont}/{total}"}

def pattern_gap(history, lookback=200):
    """Find overdue numbers (gap analysis)."""
    nums = [x["num"] for x in history[-lookback:]]
    if len(nums) < 50:
        return None
    last_seen = {}
    for i in range(len(nums)-1, -1, -1):
        if nums[i] not in last_seen:
            last_seen[nums[i]] = len(nums) - i
    # expected gap for uniform 0-9 is 10
    overdue = [(n, g) for n, g in last_seen.items() if g > 10]
    if not overdue:
        return None
    # pick the most overdue number
    most_overdue = max(overdue, key=lambda x: x[1])
    num = most_overdue[0]
    vote = 1 if num >= 5 else -1
    conf = min(0.75, 0.5 + (most_overdue[1] - 10) * 0.02)
    return {"vote": vote, "conf": conf, "engine": "GAP", "detail": f"overdue {num} gap={most_overdue[1]}"}

def pattern_markov(history, lookback=300):
    """Markov chain: probability of next size given previous size."""
    sizes = [x["size"] for x in history[-lookback:]]
    if len(sizes) < 50:
        return None
    trans = defaultdict(lambda: [0, 0])   # prev -> [to_BIG, to_SMALL]
    for i in range(len(sizes)-1):
        trans[sizes[i]][sizes[i+1]] += 1
    prev = sizes[-1]
    big, small = trans[prev]
    total = big + small
    if total < 10:
        return None
    p_big = (big + 1) / (total + 2)
    vote = 1 if p_big >= 0.5 else -1
    conf = max(p_big, 1-p_big)
    return {"vote": vote, "conf": conf, "engine": "MARKOV", "detail": f"prev={prev} P(BIG)={p_big:.2f}"}

def pattern_ngram(history, n=4, lookback=400):
    """N-gram matching: find last n sizes in history and see what followed."""
    sizes = [x["size"] for x in history[-lookback:]]
    if len(sizes) < n + 10:
        return None
    window = tuple(sizes[-n:])
    follow_big = 0
    follow_small = 0
    for i in range(len(sizes)-n):
        if tuple(sizes[i:i+n]) == window:
            if i+n < len(sizes):
                if sizes[i+n] == 1:
                    follow_big += 1
                else:
                    follow_small += 1
    total = follow_big + follow_small
    if total < 3:
        return None
    p_big = (follow_big + 1) / (total + 2)
    vote = 1 if p_big >= 0.5 else -1
    conf = max(p_big, 1-p_big)
    return {"vote": vote, "conf": conf, "engine": "NGRAM", "detail": f"pattern {window} -> B={follow_big} S={follow_small}"}

def pattern_zigzag(history, lookback=50):
    """Detect alternating (zigzag) pattern."""
    sizes = [x["size"] for x in history[-lookback:]]
    if len(sizes) < 20:
        return None
    # count alternations in last 10
    alt = sum(1 for i in range(1, 10) if sizes[-i] != sizes[-i-1])
    if alt >= 7:   # strong zigzag
        vote = -1 if sizes[-1] == 1 else 1   # predict break
        conf = 0.65
        return {"vote": vote, "conf": conf, "engine": "ZIGZAG", "detail": f"alt={alt}/9"}
    return None

# ============================================================
# 4. ENSEMBLE PREDICTOR
# ============================================================
class Predictor:
    def __init__(self, engine):
        self.engine = engine
        self.logs = []   # recent predictions for result checking

    def predict(self):
        hist = self.engine.history
        if len(hist) < 60:
            return None

        results = []
        for fn in [pattern_run, pattern_gap, pattern_markov, pattern_ngram, pattern_zigzag]:
            try:
                r = fn(hist)
                if r:
                    results.append(r)
            except Exception:
                pass

        if not results:
            return None

        # weighted vote
        big_score = 0.0
        small_score = 0.0
        total_conf = 0.0
        for r in results:
            if r["vote"] == 1:
                big_score += r["conf"]
            else:
                small_score += r["conf"]
            total_conf += r["conf"]

        if big_score == small_score:
            return None

        pred_size = 1 if big_score > small_score else 0
        confidence = round((max(big_score, small_score) / total_conf) * 100, 1)

        # target number selection
        target_num = self._select_number(hist, pred_size)

        next_issue = self.engine.next_issue()
        if not next_issue:
            return None

        pred = {
            "issue": next_issue,
            "pred_size": "BIG" if pred_size == 1 else "SMALL",
            "pred_num": target_num,
            "confidence": confidence,
            "engines": results,
            "status": "PENDING",
        }
        # store for tracking
        if not any(x["issue"] == next_issue for x in self.logs):
            self.logs.append(pred)
            if len(self.logs) > 50:
                self.logs.pop(0)
        return pred

    def _select_number(self, hist, pred_size):
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        nums = [x["num"] for x in hist[-100:]]
        freq = Counter(nums)
        # gap
        last_seen = {}
        for i in range(len(hist)-1, -1, -1):
            n = hist[i]["num"]
            if n not in last_seen:
                last_seen[n] = len(hist) - i
        # markov transition from last number
        trans = defaultdict(int)
        for i in range(len(hist)-1):
            if hist[i]["num"] == hist[-1]["num"]:
                trans[hist[i+1]["num"]] += 1
        # score
        best = pool[0]
        best_score = -999
        for n in pool:
            score = 0
            score += freq.get(n, 0) * 0.4
            score += last_seen.get(n, 0) * 0.6
            score += trans.get(n, 0) * 1.5
            if score > best_score:
                best_score = score
                best = n
        return best

    def check_results(self):
        """Check pending predictions against new data."""
        new_results = []
        for p in self.logs:
            if p["status"] != "PENDING":
                continue
            for h in self.engine.history:
                if h["issue"] == p["issue"]:
                    size_win = (h["size"] == (1 if p["pred_size"] == "BIG" else 0))
                    num_win = (h["num"] == p["pred_num"])
                    if size_win and num_win:
                        p["status"] = "JACK 🎰"
                    elif size_win:
                        p["status"] = "WIN ✅"
                    else:
                        p["status"] = "LOSS ❌"
                    p["actual"] = f"{h['size']==1 and 'BIG' or 'SMALL'} {h['num']}"
                    new_results.append(p)
                    break
        return new_results

# ============================================================
# 5. TELEGRAM HANDLERS
# ============================================================
data_engine = DataEngine()
predictor = Predictor(data_engine)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 **FTW HYPER V58 Bot**\n\n"
        "Commands:\n"
        "/predict — Get next prediction\n"
        "/history — Show last 10 outcomes\n"
        "/stats — Show engine stats",
        parse_mode="Markdown"
    )

async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Fetching 1000 results...")
    if not data_engine.fetch():
        await msg.edit_text("❌ API error. Try again.")
        return
    pred = predictor.predict()
    if not pred:
        await msg.edit_text("❌ Not enough patterns. Try again.")
        return
    text = format_prediction(pred)
    await msg.edit_text(text, parse_mode="Markdown")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not predictor.logs:
        await update.message.reply_text("No history yet.")
        return
    lines = ["📜 **Recent Outcomes**\n"]
    for p in predictor.logs[-10:]:
        if p["status"] != "PENDING":
            lines.append(f"`{p['issue'][-4:]}` {p['status']} | Pred: {p['pred_size']} {p['pred_num']} | Act: {p.get('actual','?')}")
        else:
            lines.append(f"`{p['issue'][-4:]}` PENDING")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len([p for p in predictor.logs if p["status"] != "PENDING"])
    wins = len([p for p in predictor.logs if "WIN" in p["status"] or "JACK" in p["status"]])
    jack = len([p for p in predictor.logs if "JACK" in p["status"]])
    if total == 0:
        await update.message.reply_text("No completed predictions yet.")
        return
    text = f"📊 **Stats**\nTotal: {total}\nWins: {wins}\nJACK: {jack}\nAccuracy: {round(wins/total*100,1)}%"
    await update.message.reply_text(text, parse_mode="Markdown")

def format_prediction(pred):
    recent = ""
    for p in predictor.logs[-5:]:
        if p["status"] != "PENDING":
            recent += f"➜ `{p['issue'][-4:]}` {p.get('actual','?')} → **{p['status']}**\n"
        else:
            recent += f"➜ `{p['issue'][-4:]}` PENDING\n"
    engines = "\n".join([f"• {r['engine']}: {r['detail']} (conf {round(r['conf']*100)}%)" for r in pred["engines"]])
    return f"""
📌 **Period No**: `{pred['issue']}`
🎯 **Target No**: `{pred['pred_num']}`
📊 **Size**: **{pred['pred_size']}**

📈 **Confidence**: {pred['confidence']}%

━━━━━━━━━━━━━━━━━━
🔍 **Engines**:
{engines}
━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent}
━━━━━━━━━━━━━━━━━━
🤖 *FTW HYPER V58 — Deep Pattern Ensemble*
"""

# ============================================================
# 6. BACKGROUND 24/7 LOOP
# ============================================================
async def auto_loop(app):
    last_issue = None
    while True:
        try:
            if not data_engine.fetch():
                await asyncio.sleep(10)
                continue

            # resolve pending
            resolved = predictor.check_results()
            for p in resolved:
                text = f"🎯 **Result**\nPeriod: `{p['issue'][-4:]}`\nPred: {p['pred_size']} {p['pred_num']}\nActual: {p.get('actual','?')}\nStatus: **{p['status']}**"
                await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

            cur = data_engine.get_last_issue()
            if cur and cur != last_issue:
                pred = predictor.predict()
                if pred:
                    await app.bot.send_message(chat_id=CHAT_ID, text=format_prediction(pred), parse_mode="Markdown")
                last_issue = cur

            await asyncio.sleep(20)   # 30s game, poll every 20s
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            await asyncio.sleep(15)

# ============================================================
# 7. MAIN
# ============================================================
async def main():
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ Add your Telegram token and chat ID first.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    print("🚀 Bot started")
    asyncio.create_task(auto_loop(app))
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

