import asyncio
import aiohttp
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# 1. CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = "8611789455:AAEQb9B7N565qGDp--AR6Z1v_4lhzUIu-PQ" 
CHAT_ID = "1264164655"
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"

# ==========================================
# 2. V71 ULTIMATE DEEP MATH (Single Longest Pattern)
# ==========================================
class UltimateDeepPredictor:
    def __init__(self):
        self.history = []
        self.logs = []
        self.last_predicted_issue = None

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
            pass
        return False

    def predict(self):
        if len(self.history) < 50: return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        last_size = sizes[-1]
        
        logic_used = []
        prob_big, prob_small = 0.5, 0.5
        
        # --- 1. FIND SINGLE LONGEST PATTERN (12 to 4) ---
        longest_pattern_info = None
        
        for k in range(12, 3, -1):
            if len(sizes) < k + 1: continue
            current_pattern = tuple(sizes[-k:])
            
            big_f, small_f = 0, 0
            # Search exact pattern in 1000 history
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == current_pattern:
                    if sizes[i+k] == 1: big_f += 1
                    else: small_f += 1
                    
            total = big_f + small_f
            # Agar 8 se lamba pattern hai, toh 1 match bhi kaafi hai
            min_occur = 1 if k >= 8 else 2
            
            if total >= min_occur:
                longest_pattern_info = {
                    "length": k,
                    "big": big_f,
                    "small": small_f,
                    "total": total
                }
                # Pattern mil gaya! Ab loop break karo, chhote patterns mat dhoondho
                break 
                
        # --- 2. APPLY BAYESIAN MATH ON LONGEST PATTERN ---
        if longest_pattern_info:
            k = longest_pattern_info["length"]
            b = longest_pattern_info["big"]
            s = longest_pattern_info["small"]
            t = longest_pattern_info["total"]
            
            # Bayesian Smoothing (Laplace) to avoid over-confidence
            bayes_big = (b + 1) / (t + 2)
            bayes_small = (s + 1) / (t + 2)
            
            # Weight based on pattern length (Longer = Higher Trust)
            weight = min(0.35, k * 0.03) 
            
            prob_big += (bayes_big - 0.5) * weight * 2
            prob_small += (bayes_small - 0.5) * weight * 2
            logic_used.append(f"P{k}({b}B/{s}S)")

        # --- 3. MEAN REVERSION (Balance Theory) ---
        last_20 = sizes[-20:]
        big_count = last_20.count(1)
        if big_count >= 15:
            prob_small += 0.15
            logic_used.append("MeanRev(S)")
        elif big_count <= 5:
            prob_big += 0.15
            logic_used.append("MeanRev(B)")

        # --- 4. STRICT TREND OVERRIDE ---
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == last_size: streak += 1
            else: break

        if streak >= 3:
            # Agar lagatar 3 ya zyada hain, toh trend ko priority do
            if last_size == 1: 
                prob_big += 0.2
                prob_small -= 0.1
            else: 
                prob_small += 0.2
                prob_big -= 0.1
            logic_used.append(f"Trend{streak}x")

        # --- FINAL SIZE DECISION ---
        total_prob = prob_big + prob_small
        final_prob_big = prob_big / total_prob
        
        if final_prob_big > 0.5:
            pred_size = 1
            confidence = int(final_prob_big * 100)
        else:
            pred_size = 0
            confidence = int((1 - final_prob_big) * 100)
            
        # Cap confidence to realistic levels
        confidence = min(92, max(52, confidence))

        # --- 5. DEEP NUMBER SELECTION ---
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        # Gap (Overdue)
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i

        # Recent Frequency (Hot numbers)
        recent_nums = nums[-50:]
        freq = Counter(recent_nums)

        best_num = pool[0]
        best_score = -1
        
        for n in pool:
            gap_score = last_seen[n] * 0.2
            freq_score = freq.get(n, 0) * 1.5
            
            # Transition Score
            trans_score = 0
            for i in range(len(nums)-1):
                if nums[i] == nums[-1] and nums[i+1] == n:
                    trans_score += 2.0
            
            total_score = gap_score + freq_score + trans_score
            if total_score > best_score:
                best_score = total_score
                best_num = n

        logic_str = " + ".join(logic_used)
        next_issue = str(int(self.history[-1]["issue"]) + 1)

        return {
            "issue": next_issue,
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic_str,
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
                else:
                    log["status"] = "LOSS ❌"
                
                log["actual_num"] = res["num"]
                log["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                new_results.append(log)
        return new_results

engine = UltimateDeepPredictor()

def format_message(pred):
    recent_text = ""
    valid_logs = [l for l in engine.logs if l["status"] != "PENDING"]
    
    for log in valid_logs[-12:]:
        recent_text += f"➜ `{log['issue'][-4:]}` {log['actual_size']} {log['actual_num']} → **{log['status']}**\n"

    return f"""
📌 **Period No**: `{pred['issue'][-4:]}`
🎯 **Target No**: `{pred['num']}`
📊 **Size**: **{pred['size']}**

📈 **Confidence**: {pred['confidence']}%
🧮 **Math Logic**: {pred['logic']}

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}
━━━━━━━━━━━━━━━━━━
👑 *V71 SINGLE LONGEST PATTERN + DEEP MATH*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👑 V71 ULTIMATE DEEP MATH Active!")

async def background_loop(app):
    print("🚀 V71 Engine Started...")
    while True:
        try:
            if await engine.fetch_data():
                resolved = engine.check_results()
                for res in resolved:
                    text = f"🎯 **Result Out**\nPeriod: `{res['issue'][-4:]}`\nPred: {res['size']} {res['num']}\nAct: {res['actual_size']} {res['actual_num']}\nStatus: **{res['status']}**"
                    await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

                if engine.history:
                    latest_issue = str(int(engine.history[-1]["issue"]) + 1)
                    
                    if engine.last_predicted_issue != latest_issue:
                        pred = engine.predict()
                        if pred:
                            engine.logs.append(pred)
                            if len(engine.logs) > 50: engine.logs.pop(0)
                            engine.last_predicted_issue = latest_issue 
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
