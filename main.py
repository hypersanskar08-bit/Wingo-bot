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
# 2. V65 PATTERN HUNTER ENGINE
# ==========================================
class PatternHunterPredictor:
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
        if len(self.history) < 50:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        big_votes, small_votes = 0.0, 0.0
        logic_used = []

        # --- ENGINE 1: DEEP SIZE PATTERN MATCHING (N-Gram) ---
        # 7 se 3 length tak ke patterns dhoondho
        for k in range(7, 2, -1):
            if len(sizes) < k + 1: continue
            pattern = tuple(sizes[-k:])
            
            big_follow, small_follow = 0, 0
            # Pichle 1000 results me exact pattern search karo
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pattern:
                    if sizes[i+k] == 1: big_follow += 1
                    else: small_follow += 1
                    
            total = big_follow + small_follow
            if total >= 2: # Kam se kam 2 baar repeat hona chahiye
                weight = k * 1.5  # Lamba pattern = zyada weight
                big_votes += big_follow * weight
                small_votes += small_follow * weight
                logic_used.append(f"Pat-{k}({big_follow}B/{small_follow}S)")
                break # Longest match milte hi ruk jao
                
        # --- ENGINE 2: TREND SEQUENCE ---
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
            
        if streak >= 3:
            if sizes[-1] == 1: big_votes += streak * 1.5
            else: small_votes += streak * 1.5
            logic_used.append(f"Trend{streak}x")

        # --- ENGINE 3: ORDER-2 MARKOV CHAIN ---
        if len(sizes) > 2:
            w2 = (sizes[-2], sizes[-1])
            trans = Counter()
            for i in range(len(sizes)-2):
                if (sizes[i], sizes[i+1]) == w2: trans[sizes[i+2]] += 1
            if trans:
                mc_res = trans.most_common(1)[0][0]
                if mc_res == 1: big_votes += 2.0
                else: small_votes += 2.0
                logic_used.append("Markov")

        # --- FINAL SIZE DECISION ---
        if big_votes == small_votes:
            pred_size = sizes[-1]
        else:
            pred_size = 1 if big_votes > small_votes else 0
            
        total_votes = big_votes + small_votes
        confidence = int((max(big_votes, small_votes) / total_votes) * 100) if total_votes > 0 else 50
        confidence = min(92, max(51, confidence))

        # --- DEEP NUMBER PATTERN MATCHING ---
        num_votes = Counter()
        # 5 se 2 length tak exact numbers ka sequence dhoondho
        for k in range(5, 1, -1):
            if len(nums) < k + 1: continue
            num_pattern = tuple(nums[-k:])
            
            for i in range(len(nums) - k):
                if tuple(nums[i:i+k]) == num_pattern:
                    num_votes[nums[i+k]] += k * 2.0
                    
        # Sirf wahi numbers consider karo jo predicted size me aate hain
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        # Overdue Gap Fallback
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i

        best_num = pool[0]
        best_score = -1
        
        for n in pool:
            # Number pattern score + Gap score
            score = num_votes.get(n, 0) + (last_seen[n] * 0.1)
            if score > best_score:
                best_score = score
                best_num = n
                
        logic_str = " + ".join(logic_used) if logic_used else "Majority"
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

engine = PatternHunterPredictor()

# ==========================================
# 3. FORMATTING (With 12 Recent Outcomes)
# ==========================================
def format_message(pred):
    recent_text = ""
    valid_logs = [l for l in engine.logs if l["status"] != "PENDING"]
    
    # Show last 12 results (Max 12, Min 5)
    for log in valid_logs[-12:]:
        recent_text += f"➜ `{log['issue'][-4:]}` {log['actual_size']} {log['actual_num']} → **{log['status']}**\n"

    return f"""
📌 **Period No**: `{pred['issue'][-4:]}`
🎯 **Target No**: `{pred['num']}`
📊 **Size**: **{pred['size']}**

📈 **Confidence**: {pred['confidence']}%
🧠 **Logic**: {pred['logic']}

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}
━━━━━━━━━━━━━━━━━━
🎯 *V65 PATTERN HUNTER (1000 Result Match)*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 V65 PATTERN HUNTER Active!")

async def background_loop(app):
    print("🚀 V65 Pattern Hunter Started...")
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
