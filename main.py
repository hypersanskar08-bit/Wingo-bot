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
# 2. V68 DEEP SEQUENCE ENGINE (4 to 12 Length)
# ==========================================
class DeepSequencePredictor:
    def __init__(self):
        self.history = []
        self.logs = []
        self.last_predicted_issue = None
        self.mistake_book = defaultdict(lambda: {'wins': 0, 'losses': 0})

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

    def detect_regime(self, sizes):
        last_20 = sizes[-20:]
        alternations = sum(1 for i in range(1, len(last_20)) if last_20[i] != last_20[i-1])
        if alternations >= 13: return "ZIGZAG"
        elif alternations <= 7: return "TREND"
        else: return "MIXED"

    def predict(self):
        if len(self.history) < 50: return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        big_votes, small_votes = 0.0, 0.0
        logic_used = []

        # --- ENGINE 1: DEEP PATTERN MATCHING (Length 12 to 4) ---
        for k in range(12, 3, -1):
            if len(sizes) < k + 1: continue
            pattern = tuple(sizes[-k:])
            
            big_f, small_f = 0, 0
            # History me exact pattern dhoondho
            for i in range(len(sizes) - k):
                if tuple(sizes[i:i+k]) == pattern:
                    if sizes[i+k] == 1: big_f += 1
                    else: small_f += 1
                    
            total = big_f + small_f
            
            # Smart Threshold: Agar pattern 8 se lamba hai, toh 1 match bhi kaafi hai
            min_occurrence = 1 if k >= 8 else 2
            
            if total >= min_occurrence:
                weight = k * 1.5  # Lamba pattern = Zyada weight
                big_votes += big_f * weight
                small_votes += small_f * weight
                logic_used.append(f"P{k}({big_f}B/{small_f}S)")

        # --- ENGINE 2: REGIME / TREND ---
        regime = self.detect_regime(sizes)
        streak = 1
        for i in range(len(sizes)-2, -1, -1):
            if sizes[i] == sizes[-1]: streak += 1
            else: break

        if regime == "TREND" or streak >= 4:
            if sizes[-1] == 1: big_votes += 3.5
            else: small_votes += 3.5
            logic_used.append(f"Trend{streak}x")
        elif regime == "ZIGZAG":
            if sizes[-1] == 1: small_votes += 2.5
            else: big_votes += 2.5
            logic_used.append("Zigzag")

        # --- ENGINE 3: ORDER-3 MARKOV CHAIN ---
        if len(sizes) > 3:
            w3 = (sizes[-3], sizes[-2], sizes[-1])
            trans = Counter()
            for i in range(len(sizes)-3):
                if (sizes[i], sizes[i+1], sizes[i+2]) == w3:
                    trans[sizes[i+3]] += 1
            if trans:
                mc_res = trans.most_common(1)[0][0]
                if mc_res == 1: big_votes += 2.0
                else: small_votes += 2.0
                logic_used.append("Markov-3")

        # --- MISTAKE BOOK CHECK ---
        p_key = "".join(str(x) for x in sizes[-5:]) # 5 length key for mistake book
        if p_key in self.mistake_book and self.mistake_book[p_key]['losses'] > self.mistake_book[p_key]['wins']:
            if big_votes > small_votes: small_votes += 3.0
            else: big_votes += 3.0
            logic_used.append("Flipped")

        # --- FINAL SIZE DECISION ---
        if big_votes == small_votes:
            pred_size = sizes[-1]
        else:
            pred_size = 1 if big_votes > small_votes else 0
            
        total_votes = big_votes + small_votes
        confidence = int((max(big_votes, small_votes) / total_votes) * 100) if total_votes > 0 else 50
        confidence = min(92, max(52, confidence))

        # --- NUMBER PREDICTION (Exact Numbers 4 to 8 Length) ---
        num_votes = Counter()
        for k in range(8, 3, -1): # 8 to 4 length for numbers
            if len(nums) < k + 1: continue
            n_pattern = tuple(nums[-k:])
            for i in range(len(nums) - k):
                if tuple(nums[i:i+k]) == n_pattern:
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

        logic_str = " + ".join(logic_used[:3]) 
        if len(logic_used) > 3: logic_str += ".."
        
        next_issue = str(int(self.history[-1]["issue"]) + 1)

        return {
            "issue": next_issue,
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "logic": logic_str,
            "pattern_key": p_key,
            "status": "PENDING"
        }

    def check_results(self):
        new_results = []
        history_dict = {x["issue"]: x for x in self.history}
        for log in self.logs:
            if log["status"] == "PENDING" and log["issue"] in history_dict:
                res = history_dict[log["issue"]]
                size_win = (res["size"] == (1 if log["size"] == "BIG" else 0))
                
                p_key = log.get("pattern_key")
                if p_key:
                    if size_win: self.mistake_book[p_key]['wins'] += 1
                    else: self.mistake_book[p_key]['losses'] += 1
                
                if size_win:
                    log["status"] = "JACK 🎰" if res["num"] == log["num"] else "WIN ✅"
                else:
                    log["status"] = "LOSS ❌"
                
                log["actual_num"] = res["num"]
                log["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                new_results.append(log)
        return new_results

engine = DeepSequencePredictor()

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
🔥 *V68 DEEP SEQUENCE (4-12 Length)*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 V68 DEEP SEQUENCE Active!")

async def background_loop(app):
    print("🚀 V68 Engine Started...")
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
