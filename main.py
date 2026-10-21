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
# 2. V66 ULTIMATE ADAPTIVE ENGINE (Learning)
# ==========================================
class UltimateAdaptivePredictor:
    def __init__(self):
        self.history = []
        self.logs = []
        self.last_predicted_issue = None
        
        # LEARNING FEATURE 1: Mistake Book
        # Format: { 'pattern_key': {'wins': X, 'losses': Y} }
        self.mistake_book = defaultdict(lambda: {'wins': 0, 'losses': 0})
        
        # LEARNING FEATURE 2: Engine Adaptive Weights
        self.engine_stats = {
            'pattern': {'wins': 0, 'losses': 0},
            'trend': {'wins': 0, 'losses': 0},
            'markov': {'wins': 0, 'losses': 0}
        }

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

    def get_engine_weight(self, engine_name):
        """Khud ko update karta hai based on recent performance"""
        stats = self.engine_stats[engine_name]
        total = stats['wins'] + stats['losses']
        if total < 3: return 1.5 # Default weight for new engines
        acc = stats['wins'] / total
        # Agar accuracy 50% se kam hai, weight kam karo (min 0.5)
        return max(0.5, acc * 2)

    def predict(self):
        if len(self.history) < 50:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        big_votes, small_votes = 0.0, 0.0
        logic_used = []

        # --- ENGINE 1: DEEP PATTERN MATCHING (With Mistake Book) ---
        pattern_found = False
        for k in range(7, 2, -1):
            if len(sizes) < k + 1: continue
            pattern = tuple(sizes[-k:])
            pattern_key = "".join(str(x) for x in pattern)
            
            big_follow, small_follow = 0, 0
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pattern:
                    if sizes[i+k] == 1: big_follow += 1
                    else: small_follow += 1
                    
            total = big_follow + small_follow
            if total >= 2:
                weight = k * 1.5 * self.get_engine_weight('pattern')
                
                # Check Mistake Book (Learning from past mistakes)
                mb = self.mistake_book[pattern_key]
                if mb['losses'] > mb['wins']:
                    # Agar is pattern par loss khaya hai, toh is baar opposite karo
                    big_votes += small_follow * weight
                    small_votes += big_follow * weight
                    logic_used.append(f"Pat-{k}(Flipped)")
                else:
                    big_votes += big_follow * weight
                    small_votes += small_follow * weight
                    logic_used.append(f"Pat-{k}({big_follow}B/{small_follow}S)")
                pattern_found = True
                break
                
        # --- ENGINE 2: TREND SEQUENCE ---
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break
            
        if streak >= 3:
            trend_w = self.get_engine_weight('trend')
            if sizes[-1] == 1: big_votes += streak * trend_w
            else: small_votes += streak * trend_w
            logic_used.append(f"Trend{streak}x")

        # --- ENGINE 3: ORDER-2 MARKOV CHAIN ---
        if len(sizes) > 2:
            w2 = (sizes[-2], sizes[-1])
            trans = Counter()
            for i in range(len(sizes)-2):
                if (sizes[i], sizes[i+1]) == w2: trans[sizes[i+2]] += 1
            if trans:
                mc_w = self.get_engine_weight('markov')
                mc_res = trans.most_common(1)[0][0]
                if mc_res == 1: big_votes += 2.0 * mc_w
                else: small_votes += 2.0 * mc_w
                logic_used.append("Markov")

        # --- FINAL SIZE DECISION ---
        if big_votes == small_votes:
            pred_size = sizes[-1] # Fallback
        else:
            pred_size = 1 if big_votes > small_votes else 0
            
        total_votes = big_votes + small_votes
        confidence = int((max(big_votes, small_votes) / total_votes) * 100) if total_votes > 0 else 50
        confidence = min(92, max(51, confidence))

        # --- DEEP NUMBER PATTERN MATCHING ---
        num_votes = Counter()
        for k in range(5, 1, -1):
            if len(nums) < k + 1: continue
            num_pattern = tuple(nums[-k:])
            for i in range(len(nums) - k):
                if tuple(nums[i:i+k]) == num_pattern:
                    num_votes[nums[i+k]] += k * 2.0
                    
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i

        best_num = pool[0]
        best_score = -1
        
        for n in pool:
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
            "pattern_key": "".join(str(x) for x in sizes[-4:]), # Track k=4 for mistake book
            "status": "PENDING"
        }

    def check_results(self):
        """Ye function bot ko sikhata hai (Learning Phase)"""
        new_results = []
        history_dict = {x["issue"]: x for x in self.history}
        
        for log in self.logs:
            if log["status"] == "PENDING" and log["issue"] in history_dict:
                res = history_dict[log["issue"]]
                size_win = (res["size"] == (1 if log["size"] == "BIG" else 0))
                
                # --- LEARNING UPDATE START ---
                p_key = log.get("pattern_key")
                if p_key:
                    if size_win:
                        self.mistake_book[p_key]['wins'] += 1
                    else:
                        self.mistake_book[p_key]['losses'] += 1
                
                # Engine Weight Update
                if "Pat" in log["logic"]:
                    if size_win: self.engine_stats['pattern']['wins'] += 1
                    else: self.engine_stats['pattern']['losses'] += 1
                if "Trend" in log["logic"]:
                    if size_win: self.engine_stats['trend']['wins'] += 1
                    else: self.engine_stats['trend']['losses'] += 1
                if "Markov" in log["logic"]:
                    if size_win: self.engine_stats['markov']['wins'] += 1
                    else: self.engine_stats['markov']['losses'] += 1
                # --- LEARNING UPDATE END ---
                
                if size_win:
                    log["status"] = "JACK 🎰" if res["num"] == log["num"] else "WIN ✅"
                else:
                    log["status"] = "LOSS ❌"
                
                log["actual_num"] = res["num"]
                log["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                new_results.append(log)
        return new_results

engine = UltimateAdaptivePredictor()

# ==========================================
# 3. FORMATTING
# ==========================================
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
🧠 **Logic**: {pred['logic']}

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}
━━━━━━━━━━━━━━━━━━
🧬 *V66 ULTIMATE ADAPTIVE (Self-Learning)*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧬 V66 ULTIMATE ADAPTIVE Active! Ye bot galtiyon se seekhta hai.")

async def background_loop(app):
    print("🚀 V66 Engine Started...")
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
