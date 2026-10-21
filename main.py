import asyncio
import aiohttp
from collections import Counter
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# 1. CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = "8611789455:AAEQb9B7N565qGDp--AR6Z1v_4lhzUIu-PQ" 
CHAT_ID = "1264164655"
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"

# ==========================================
# 2. V72 UNIFIED PROBABILITY ENGINE
# ==========================================
class UnifiedPredictor:
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
        
        logic_tags = []
        
        # ---------------------------------------------
        # ENGINE 1: LONGEST PATTERN (Probability)
        # ---------------------------------------------
        p_pattern = 0.5 # Neutral base
        w_pattern = 0.0
        pat_info = ""
        
        for k in range(12, 3, -1):
            if len(sizes) < k + 1: continue
            pattern = tuple(sizes[-k:])
            big_f, small_f = 0, 0
            
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pattern:
                    if sizes[i+k] == 1: big_f += 1
                    else: small_f += 1
                    
            total = big_f + small_f
            min_occur = 1 if k >= 8 else 2
            
            if total >= min_occur:
                # Bayesian Smoothing
                p_pattern = (big_f + 1) / (total + 2)
                w_pattern = min(0.4, k * 0.04) # Longer pattern = higher weight
                pat_info = f"P{k}({big_f}B/{small_f}S)"
                break # Stop at longest pattern

        # ---------------------------------------------
        # ENGINE 2: LIVE TREND (Probability)
        # ---------------------------------------------
        p_trend = 0.5
        w_trend = 0.1
        
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == last_size: streak += 1
            else: break
            
        if streak >= 4:
            p_trend = 0.9 if last_size == 1 else 0.1
            w_trend = 0.4 # Trend is very strong
        elif streak == 3:
            p_trend = 0.75 if last_size == 1 else 0.25
            w_trend = 0.3
        elif streak == 2:
            p_trend = 0.6 if last_size == 1 else 0.4
            w_trend = 0.2
            
        if streak >= 2: logic_tags.append(f"Trend{streak}x")

        # ---------------------------------------------
        # ENGINE 3: MEAN REVERSION (Probability)
        # ---------------------------------------------
        p_mr = 0.5
        w_mr = 0.1
        
        last_20 = sizes[-20:]
        big_count = last_20.count(1)
        if big_count >= 15:
            p_mr = 0.2 # Too many BIG, expect SMALL
            w_mr = 0.25
            logic_tags.append("MeanRev")
        elif big_count <= 5:
            p_mr = 0.8 # Too many SMALL, expect BIG
            w_mr = 0.25
            logic_tags.append("MeanRev")

        # ---------------------------------------------
        # ENGINE 4: MARKOV CHAIN (Probability)
        # ---------------------------------------------
        p_markov = 0.5
        w_markov = 0.15
        
        if len(sizes) > 2:
            w2 = (sizes[-2], sizes[-1])
            trans = Counter()
            for i in range(len(sizes)-2):
                if (sizes[i], sizes[i+1]) == w2: trans[sizes[i+2]] += 1
            if trans:
                total_t = sum(trans.values())
                p_markov = (trans.get(1, 0) + 1) / (total_t + 2)
                w_markov = 0.2
                logic_tags.append("Markov")

        # ---------------------------------------------
        # COMBINE ALL PROBABILITIES (Weighted Average)
        # ---------------------------------------------
        total_weight = w_pattern + w_trend + w_mr + w_markov
        
        if total_weight == 0: total_weight = 1 # Fallback
            
        final_p_big = (
            (p_pattern * w_pattern) + 
            (p_trend * w_trend) + 
            (p_mr * w_mr) + 
            (p_markov * w_markov)
        ) / total_weight
        
        # If no pattern found, add small base weight to trend
        if w_pattern == 0:
             final_p_big = (p_trend * 0.5 + p_mr * 0.2 + p_markov * 0.3)
             
        if w_pattern > 0: logic_tags.insert(0, pat_info)

        # Final Decision
        pred_size = 1 if final_p_big >= 0.5 else 0
        confidence = int(max(final_p_big, 1 - final_p_big) * 100)
        
        # Cap confidence
        confidence = min(90, max(51, confidence))

        # ---------------------------------------------
        # DEEP NUMBER SELECTION
        # ---------------------------------------------
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i

        freq = Counter(nums[-50:])

        best_num = pool[0]
        best_score = -1
        for n in pool:
            score = (last_seen[n] * 0.2) + (freq.get(n, 0) * 1.5)
            if score > best_score:
                best_score = score
                best_num = n

        logic_str = " + ".join(logic_tags[:3])
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

engine = UnifiedPredictor()

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
🧬 *V72 UNIFIED PROBABILITY (Combined)*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧬 V72 UNIFIED ENGINE Active!")

async def background_loop(app):
    print("🚀 V72 Engine Started...")
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
