import asyncio
import aiohttp
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import time

# ==========================================
# 1. CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = "8611789455:AAFcnSZ7nlrCIPsQUKLQwdmTf2aw2szmLFk"
CHAT_ID = "1264164655" # e.g., "@my_channel" or "-100123456789"
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"

# ==========================================
# 2. DEEP PATTERN & PREDICTION ENGINE
# ==========================================
class DeepPredictor:
    def __init__(self):
        self.history = []
        self.logs = []
        self.last_issue = None

    async def fetch_data(self):
        """Super fast async fetch"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        lst = data.get("data", {}).get("list", [])
                        cleaned = []
                        for item in lst:
                            issue = str(item["issueNumber"])
                            num = int(item["number"])
                            size = 1 if num >= 5 else 0 # 1=BIG, 0=SMALL
                            cleaned.append({"issue": issue, "num": num, "size": size})
                        # Sort oldest to newest
                        cleaned.sort(key=lambda x: int(x["issue"]))
                        self.history = cleaned
                        return True
        except Exception as e:
            print(f"Fetch error: {e}")
            return False

    def predict(self):
        """Advanced Ensemble Logic (Executes in < 10ms)"""
        if len(self.history) < 100:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        last_size = sizes[-1]
        last_num = nums[-1]

        big_score, small_score = 0.0, 0.0
        engines_used = []

        # --- ENGINE 1: N-Gram Sequence Matching (Size) ---
        # Last 4 sizes ko history me dhoondo
        window_sz = sizes[-4:]
        ng_big, ng_small = 0, 0
        for i in range(len(sizes) - 4):
            if sizes[i:i+4] == window_sz:
                if i+4 < len(sizes):
                    if sizes[i+4] == 1: ng_big += 1
                    else: ng_small += 1
        
        if ng_big + ng_small >= 3:
            if ng_big > ng_small: big_score += 2.0
            else: small_score += 2.0
            engines_used.append("N-GRAM")

        # --- ENGINE 2: Streak Continuation vs Break ---
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == last_size: streak += 1
            else: break

        if streak >= 3:
            # Trend continuation
            if last_size == 1: big_score += 1.5
            else: small_score += 1.5
            engines_used.append("STREAK")
        else:
            # Zigzag detection (opposite)
            if last_size == 1: small_score += 1.0
            else: big_score += 1.0

        # --- ENGINE 3: Markov Chain (Digit transition) ---
        # Jab last_num aaya tha, uske baad kya aaya?
        trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num:
                trans[nums[i+1]] += 1
        
        if trans:
            most_common_next = trans.most_common(1)[0][0]
            if most_common_next >= 5: big_score += 1.5
            else: small_score += 1.5
            engines_used.append("MARKOV")

        # --- ENGINE 4: Number Gap Analysis ---
        # Sabse zyada time se nahi aaya number
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000:
                last_seen[nums[i]] = len(nums) - i
        
        most_overdue = max(last_seen, key=last_seen.get)
        if last_seen[most_overdue] > 10:
            if most_overdue >= 5: big_score += 1.0
            else: small_score += 1.0
            engines_used.append("GAP")

        # --- FINAL DECISION ---
        pred_size = 1 if big_score >= small_score else 0
        total = big_score + small_score
        confidence = int((max(big_score, small_score) / total) * 100) if total > 0 else 55
        confidence = min(88, max(55, confidence + 5)) # Base boost

        # --- TARGET NUMBER SELECTION (Deep) ---
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        # Number frequency
        freq = Counter(nums[-50:])
        
        # Recent transition (kis number ke baad kya aata hai)
        recent_trans = Counter()
        if len(nums) > 1:
            for i in range(len(nums)-10, len(nums)-1):
                if nums[i] == last_num: recent_trans[nums[i+1]] += 1

        best_num = pool[0]
        best_score = -999
        
        for n in pool:
            score = 0
            score += freq.get(n, 0) * 1.5
            score += last_seen.get(n, 0) * 0.8
            score += recent_trans.get(n, 0) * 3.0
            if score > best_score:
                best_score = score
                best_num = n

        next_issue = str(int(self.history[-1]["issue"]) + 1)

        return {
            "issue": next_issue,
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "engines": len(engines_used),
            "status": "PENDING"
        }

    def check_results(self):
        """Check pending predictions"""
        new_results = []
        history_dict = {x["issue"]: x for x in self.history}
        
        for log in self.logs:
            if log["status"] == "PENDING" and log["issue"] in history_dict:
                res = history_dict[log["issue"]]
                size_win = (res["size"] == (1 if log["size"] == "BIG" else 0))
                num_win = (res["num"] == log["num"])
                
                if size_win and num_win:
                    log["status"] = "JACK 🎰"
                elif size_win:
                    log["status"] = "WIN ✅"
                else:
                    log["status"] = "LOSS ❌"
                
                log["actual_num"] = res["num"]
                log["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                new_results.append(log)
        return new_results

# ==========================================
# 3. BOT HANDLERS & FORMATTING
# ==========================================
engine = DeepPredictor()

def format_message(pred):
    # Recent Outcomes (5 to 12)
    recent_text = ""
    valid_logs = [l for l in engine.logs if l["status"] != "PENDING"]
    
    # Last 12 results show karo
    for log in valid_logs[-12:]:
        recent_text += f"➜ `{log['issue'][-4:]}` {log['actual_size']} {log['actual_num']} → **{log['status']}**\n"

    return f"""
📌 **Period No**: `{pred['issue']}`
🎯 **Target No**: `{pred['num']}`
📊 **Size**: **{pred['size']}**

📈 **Confidence**: {pred['confidence']}% ({pred['engines']} Engines)

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}
━━━━━━━━━━━━━━━━━━
⚡ *FTW HYPER V58 - 0.2s Execution*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 FTW HYPER V58 Active!\nCommands: /predict, /history")

async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⚡ Fetching...")
    if await engine.fetch_data():
        pred = engine.predict()
        if pred:
            if not any(l["issue"] == pred["issue"] for l in engine.logs):
                engine.logs.append(pred)
                if len(engine.logs) > 50: engine.logs.pop(0)
            await msg.edit_text(format_message(pred), parse_mode="Markdown")
        else:
            await msg.edit_text("❌ Not enough data.")
    else:
        await msg.edit_text("❌ API Error.")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valid_logs = [l for l in engine.logs if l["status"] != "PENDING"]
    if not valid_logs:
        await update.message.reply_text("No history yet.")
        return
    text = "📜 Last 12 Results\n\n"
    for log in valid_logs[-12:]:
        text += f"`{log['issue'][-4:]}` {log['status']} | Pred: {log['size']} {log['num']} | Act: {log['actual_size']} {log['actual_num']}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================================
# 4. FAST BACKGROUND LOOP (1 SEC INTERVAL)
# ==========================================
async def background_loop(app):
    print("🚀 Super Fast Loop Started...")
    while True:
        try:
            # 1. Fetch Data instantly
            if await engine.fetch_data():
                
                # 2. Check old results
                resolved = engine.check_results()
                for res in resolved:
                    text = f"🎯 **Result Out**\nPeriod: `{res['issue'][-4:]}`\nPred: {res['size']} {res['num']}\nAct: {res['actual_size']} {res['actual_num']}\nStatus: **{res['status']}**"
                    await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

                # 3. Check if new issue arrived
                if engine.history:
                    latest_issue = engine.history[-1]["issue"]
                    
                    # Agar last prediction is issue ka nahi tha, to naya prediction banao
                    if not any(l["issue"] == str(int(latest_issue) + 1) for l in engine.logs):
                        pred = engine.predict()
                        if pred:
                            engine.logs.append(pred)
                            if len(engine.logs) > 50: engine.logs.pop(0)
                            
                            # Instantly Send
                            await app.bot.send_message(chat_id=CHAT_ID, text=format_message(pred), parse_mode="Markdown")
                            print(f"⚡ Predicted for {pred['issue']} in < 0.2s")

            # 1 second wait for next check
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Loop Error: {e}")
            await asyncio.sleep(2)

async def main():
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ Please set token and chat id.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    
    asyncio.create_task(background_loop(app))
    
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

