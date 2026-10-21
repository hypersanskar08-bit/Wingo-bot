import asyncio
import aiohttp
from collections import Counter
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# 1. CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk" # Token change kar lena baad me
CHAT_ID = "1264164655"
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"

# ==========================================
# 2. V60 ULTRA DEEP PREDICTOR
# ==========================================
class UltraPredictor:
    def __init__(self):
        self.history = []
        self.logs = []
        self.loss_streak = 0

    async def fetch_data(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, timeout=4) as response:
                    if response.status == 200:
                        data = await response.json()
                        lst = data.get("data", {}).get("list", [])
                        cleaned = []
                        for item in lst:
                            issue = str(item["issueNumber"])
                            num = int(item["number"])
                            size = 1 if num >= 5 else 0
                            cleaned.append({"issue": issue, "num": num, "size": size})
                        cleaned.sort(key=lambda x: int(x["issue"]))
                        self.history = cleaned
                        return True
        except Exception as e:
            print(f"Fetch Error: {e}")
            return False

    def predict(self):
        if len(self.history) < 100:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        last_size = sizes[-1]
        last_num = nums[-1]
        
        big_score, small_score = 0.0, 0.0
        engines_used = 0

        # --- 1. SHORT TERM TREND ---
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == last_size: streak += 1
            else: break

        # --- LOSS RECOVERY LOGIC (Circuit Breaker) ---
        # Agar lagatar 2 loss hue, toh market chop me hai. Trend ke against jao.
        if self.loss_streak >= 2:
            if last_size == 1: small_score += 3.0 # Break the BIG streak
            else: big_score += 3.0                # Break the SMALL streak
            engines_used += 1
        else:
            # Normal Trend Logic
            if streak >= 4:
                if last_size == 1: big_score += 2.5
                else: small_score += 2.5
                engines_used += 1
            elif streak <= 2:
                if last_size == 1: small_score += 1.5
                else: big_score += 1.5

        # --- 2. DEEP MARKOV CHAIN (Order-2) ---
        trans = Counter()
        if len(sizes) > 2:
            w2 = (sizes[-2], sizes[-1])
            for i in range(len(sizes)-2):
                if (sizes[i], sizes[i+1]) == w2:
                    trans[sizes[i+2]] += 1
        
        if trans:
            mc_res = trans.most_common(1)[0][0]
            if mc_res == 1: big_score += 2.0
            else: small_score += 2.0
            engines_used += 1

        # --- 3. N-GRAM (Length 5) ---
        window_sz = sizes[-5:]
        ng_big, ng_small = 0, 0
        for i in range(len(sizes) - 5):
            if sizes[i:i+5] == window_sz:
                if i+5 < len(sizes):
                    if sizes[i+5] == 1: ng_big += 1
                    else: ng_small += 1
        
        if ng_big + ng_small >= 2:
            if ng_big > ng_small: big_score += 1.5
            else: small_score += 1.5
            engines_used += 1

        # --- FINAL DECISION ---
        pred_size = 1 if big_score >= small_score else 0
        total = big_score + small_score
        confidence = int((max(big_score, small_score) / total) * 100) if total > 0 else 55
        
        # Cap confidence
        confidence = min(88, max(52, confidence))

        # --- TARGET NUMBER ---
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        # Overdue gap
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i
        
        # Recent transitions
        n_trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num: n_trans[nums[i+1]] += 1

        best_num = pool[0]
        best_score = -999
        for n in pool:
            score = (last_seen.get(n, 0) * 0.6) + (n_trans.get(n, 0) * 4.0)
            if score > best_score:
                best_score = score
                best_num = n

        next_issue = str(int(self.history[-1]["issue"]) + 1)

        return {
            "issue": next_issue,
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "engines": engines_used,
            "status": "PENDING"
        }

    def check_results(self):
        new_results = []
        history_dict = {x["issue"]: x for x in self.history}
        
        for log in self.logs:
            if log["status"] == "PENDING" and log["issue"] in history_dict:
                res = history_dict[log["issue"]]
                size_win = (res["size"] == (1 if log["size"] == "BIG" else 0))
                
                if size_win:
                    log["status"] = "JACK 🎰" if res["num"] == log["num"] else "WIN ✅"
                    self.loss_streak = 0 # Reset on win
                else:
                    log["status"] = "LOSS ❌"
                    self.loss_streak += 1 # Track loss
                
                log["actual_num"] = res["num"]
                log["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                new_results.append(log)
        return new_results

engine = UltraPredictor()

def format_message(pred):
    recent_text = ""
    valid_logs = [l for l in engine.logs if l["status"] != "PENDING"]
    
    # Last 12 Outcomes
    for log in valid_logs[-12:]:
        recent_text += f"➜ `{log['issue'][-4:]}` {log['actual_size']} {log['actual_num']} → **{log['status']}**\n"

    return f"""
📌 **Period No**: `{pred['issue'][-4:]}`
🎯 **Target No**: `{pred['num']}`
📊 **Size**: **{pred['size']}**

📈 **Confidence**: {pred['confidence']}% ({pred['engines']} Engines)

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}
━━━━━━━━━━━━━━━━━━
⚡ *V60 QUANTUM DEEP - Auto Recovery Active*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 V60 QUANTUM Active!")

async def background_loop(app):
    print("🚀 V60 Engine Started...")
    while True:
        try:
            if await engine.fetch_data():
                resolved = engine.check_results()
                for res in resolved:
                    text = f"🎯 **Result Out**\nPeriod: `{res['issue'][-4:]}`\nPred: {res['size']} {res['num']}\nAct: {res['actual_size']} {res['actual_num']}\nStatus: **{res['status']}**\nLoss Streak: {engine.loss_streak}"
                    await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

                if engine.history:
                    latest_issue = engine.history[-1]["issue"]
                    if not any(l["issue"] == str(int(latest_issue) + 1) for l in engine.logs):
                        pred = engine.predict()
                        if pred:
                            engine.logs.append(pred)
                            if len(engine.logs) > 50: engine.logs.pop(0)
                            await app.bot.send_message(chat_id=CHAT_ID, text=format_message(pred), parse_mode="Markdown")
                            print(f"⚡ Predicted for {pred['issue']}")

            await asyncio.sleep(1)
        except Exception as e:
            print(f"Loop Error: {e}")
            await asyncio.sleep(2)

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    
    asyncio.create_task(background_loop(app))
    
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
