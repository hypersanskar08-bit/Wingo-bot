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
# 2. V63 QUANTUM DEEP CORE
# ==========================================
class QuantumDeepPredictor:
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

    def detect_regime(self, sizes):
        """Detect if market is TRENDING or ZIGZAGING"""
        if len(sizes) < 20: return "MIXED"
        last_20 = sizes[-20:]
        alternations = sum(1 for i in range(1, len(last_20)) if last_20[i] != last_20[i-1])
        
        if alternations >= 13: return "ZIGZAG"
        elif alternations <= 7: return "TREND"
        else: return "MIXED"

    def predict(self):
        if len(self.history) < 100:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        last_size = sizes[-1]
        last_num = nums[-1]
        
        regime = self.detect_regime(sizes)
        big_score, small_score = 0.0, 0.0
        logic_used = []

        # --- ENGINE 1: REGIME BASED PREDICTION ---
        if regime == "TREND":
            # Follow the last result
            if last_size == 1: big_score += 3.0
            else: small_score += 3.0
            logic_used.append("TrendRide")
        elif regime == "ZIGZAG":
            # Flip the last result
            if last_size == 1: small_score += 3.0
            else: big_score += 3.0
            logic_used.append("Zigzag")
        else:
            # Mixed: check short streak
            streak = 1
            for i in range(len(sizes)-2, -1, -1):
                if sizes[i] == last_size: streak += 1
                else: break
            if streak >= 2:
                if last_size == 1: big_score += 1.5
                else: small_score += 1.5
            else:
                if last_size == 1: small_score += 1.5
                else: big_score += 1.5
            logic_used.append("ShortTrend")

        # --- ENGINE 2: ORDER-3 MARKOV CHAIN ---
        if len(sizes) > 3:
            state = (sizes[-3], sizes[-2], sizes[-1])
            trans = Counter()
            for i in range(len(sizes)-3):
                if (sizes[i], sizes[i+1], sizes[i+2]) == state:
                    trans[sizes[i+3]] += 1
            if trans:
                mc_res = trans.most_common(1)[0][0]
                if mc_res == 1: big_score += 2.0
                else: small_score += 2.0
                logic_used.append("Markov-3")

        # --- ENGINE 3: N-GRAM (Length 6) ---
        if len(sizes) > 6:
            window = sizes[-6:]
            ng_b, ng_s = 0, 0
            for i in range(len(sizes) - 6):
                if sizes[i:i+6] == window:
                    if i+6 < len(sizes):
                        if sizes[i+6] == 1: ng_b += 1
                        else: ng_s += 1
            if ng_b + ng_s >= 2:
                if ng_b > ng_s: big_score += 2.5
                else: small_score += 2.5
                logic_used.append("NGram-6")

        # --- FINAL DECISION (Fallback Guarantee) ---
        # Agar sab kuch tie ho jaye, toh last result ka opposite le lo
        if big_score == small_score:
            if last_size == 1: small_score += 1.0
            else: big_score += 1.0
            
        pred_size = 1 if big_score >= small_score else 0
        
        total = big_score + small_score
        confidence = int((max(big_score, small_score) / total) * 100) if total > 0 else 55
        confidence = min(88, max(51, confidence))

        # --- SMART TARGET NUMBER ---
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        # Overdue Gap
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i
        
        # Markov for Numbers
        n_trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == last_num: n_trans[nums[i+1]] += 1

        best_num = pool[0]
        best_score = -999
        for n in pool:
            # Combine gap and markov
            score = (last_seen.get(n, 0) * 0.7) + (n_trans.get(n, 0) * 3.5)
            if score > best_score:
                best_score = score
                best_num = n

        next_issue = str(int(self.history[-1]["issue"]) + 1)

        return {
            "issue": next_issue,
            "size": "BIG" if pred_size == 1 else "SMALL",
            "num": best_num,
            "confidence": confidence,
            "regime": regime,
            "logic": "+".join(logic_used),
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

engine = QuantumDeepPredictor()

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
⚖️ **Regime**: {pred['regime']}

━━━━━━━━━━━━━━━━━━
🕒 **Recent Outcomes**:
{recent_text}
━━━━━━━━━━━━━━━━━━
⚡ *V63 QUANTUM DEEP CORE*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 V63 QUANTUM DEEP Active!")

async def background_loop(app):
    print("🚀 V63 Engine Started...")
    while True:
        try:
            if await engine.fetch_data():
                resolved = engine.check_results()
                for res in resolved:
                    text = f"🎯 **Result Out**\nPeriod: `{res['issue'][-4:]}`\nPred: {res['size']} {res['num']}\nAct: {res['actual_size']} {res['actual_num']}\nStatus: **{res['status']}**"
                    await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

                if engine.history:
                    latest_issue = str(int(engine.history[-1]["issue"]) + 1)
                    
                    # Bug Fix: Only predict if we haven't predicted for this specific issue yet
                    if engine.last_predicted_issue != latest_issue:
                        pred = engine.predict()
                        if pred:
                            engine.logs.append(pred)
                            if len(engine.logs) > 50: engine.logs.pop(0)
                            
                            engine.last_predicted_issue = latest_issue # Mark as predicted
                            
                            await app.bot.send_message(chat_id=CHAT_ID, text=format_message(pred), parse_mode="Markdown")
                            print(f"⚡ Predicted for {pred['issue']}")

            await asyncio.sleep(1) # Fast loop
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
