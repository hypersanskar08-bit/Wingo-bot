import asyncio
import aiohttp
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# 1. CONFIGURATION (Naya Token)
# ==========================================
TELEGRAM_TOKEN = "8611789455:AAEQb9B7N565qGDp--AR6Z1v_4lhzUIu-PQ" 
CHAT_ID = "1264164655"
API_URL = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-30s-1000"

# ==========================================
# 2. V61 ADAPTIVE AI ENGINE
# ==========================================
class AdaptivePredictor:
    def __init__(self):
        self.history = []
        self.logs = []
        self.loss_streak = 0
        self.mistake_book = defaultdict(lambda: [0, 0]) # pattern -> [wins, losses]

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

    def get_trend_pred(self, sizes, idx):
        last = sizes[idx]
        stk = 1
        for j in range(idx-1, -1, -1):
            if sizes[j] == last: stk += 1
            else: break
        if stk >= 3: return last # Ride trend
        else: return 1 - last # Break small streaks

    def get_ngram_pred(self, sizes, idx):
        if idx < 5: return None
        win = sizes[idx-4:idx+1]
        ng_b, ng_s = 0, 0
        for j in range(idx-4):
            if sizes[j:j+5] == win:
                nxt = sizes[j+5]
                if nxt == 1: ng_b += 1
                else: ng_s += 1
        if ng_b + ng_s >= 2:
            return 1 if ng_b > ng_s else 0
        return None

    def predict(self):
        if len(self.history) < 100:
            return None

        sizes = [x["size"] for x in self.history]
        nums = [x["num"] for x in self.history]
        
        # --- 1. ADAPTIVE BACKTEST (Pichle 50 results par engines ko test karo) ---
        trend_correct, ng_correct, valid_checks = 0, 0, 0
        for i in range(len(sizes) - 50, len(sizes)):
            if i < 10: continue
            actual = sizes[i]
            
            if self.get_trend_pred(sizes, i-1) == actual: trend_correct += 1
            ng_p = self.get_ngram_pred(sizes, i-1)
            if ng_p is not None and ng_p == actual: ng_correct += 1
            valid_checks += 1
            
        w_trend = max(0.5, (trend_correct / max(1, valid_checks)) * 4)
        w_ngram = max(0.5, (ng_correct / max(1, valid_checks)) * 4)

        # --- 2. CURRENT VOTES ---
        big_score, small_score = 0.0, 0.0
        engines_used = 0

        # Trend Vote
        t_pred = self.get_trend_pred(sizes, len(sizes)-1)
        if t_pred == 1: big_score += w_trend
        else: small_score += w_trend
        engines_used += 1

        # N-Gram Vote
        ng_pred = self.get_ngram_pred(sizes, len(sizes)-1)
        if ng_pred is not None:
            if ng_pred == 1: big_score += w_ngram
            else: small_score += w_ngram
            engines_used += 1

        # Markov (Order 2) Vote
        trans = Counter()
        if len(sizes) > 2:
            w2 = (sizes[-2], sizes[-1])
            for i in range(len(sizes)-2):
                if (sizes[i], sizes[i+1]) == w2: trans[sizes[i+2]] += 1
        if trans:
            mc_res = trans.most_common(1)[0][0]
            if mc_res == 1: big_score += 1.5
            else: small_score += 1.5
            engines_used += 1

        # Mistake Book Check
        pattern = tuple(sizes[-4:])
        if pattern in self.mistake_book and self.mistake_book[pattern][1] > self.mistake_book[pattern][0]:
            # Agar is pattern par pehle loss khaya hai, toh is baar opposite jao
            if big_score > small_score: small_score += 2.0
            else: big_score += 2.0

        # Loss Streak Penalty (Agar lagatar 2 loss hain, toh confidence kam karo)
        pred_size = 1 if big_score >= small_score else 0
        
        # --- 3. CONFIDENCE CALC ---
        total = big_score + small_score
        confidence = int((max(big_score, small_score) / total) * 100) if total > 0 else 50
        if self.loss_streak >= 2: confidence -= 10
        confidence = min(85, max(51, confidence))

        # --- 4. TARGET NUMBER ---
        pool = [5,6,7,8,9] if pred_size == 1 else [0,1,2,3,4]
        
        last_seen = {i: 1000 for i in range(10)}
        for i in range(len(nums)-1, -1, -1):
            if last_seen[nums[i]] == 1000: last_seen[nums[i]] = len(nums) - i
        
        n_trans = Counter()
        for i in range(len(nums)-1):
            if nums[i] == nums[-1]: n_trans[nums[i+1]] += 1

        best_num = pool[0]
        best_score = -999
        for n in pool:
            score = (last_seen.get(n, 0) * 0.8) + (n_trans.get(n, 0) * 3.0)
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
                
                # Save to mistake book
                idx = self.history.index(res)
                if idx >= 4:
                    pattern = tuple(x["size"] for x in self.history[idx-4:idx])
                    if size_win: self.mistake_book[pattern][0] += 1
                    else: self.mistake_book[pattern][1] += 1
                
                if size_win:
                    log["status"] = "JACK 🎰" if res["num"] == log["num"] else "WIN ✅"
                    self.loss_streak = 0 
                else:
                    log["status"] = "LOSS ❌"
                    self.loss_streak += 1 
                
                log["actual_num"] = res["num"]
                log["actual_size"] = "BIG" if res["size"] == 1 else "SMALL"
                new_results.append(log)
        return new_results

engine = AdaptivePredictor()

def format_message(pred):
    recent_text = ""
    valid_logs = [l for l in engine.logs if l["status"] != "PENDING"]
    
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
🧠 *V61 ADAPTIVE AI - Learning from Mistakes*
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 V61 ADAPTIVE AI Active!")

async def background_loop(app):
    print("🚀 V61 Engine Started...")
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
