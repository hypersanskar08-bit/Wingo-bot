import asyncio, contextlib, gc, html, json, logging, math, os, sqlite3, time
from collections import Counter
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TimedOut, NetworkError

@dataclass
class Config:
    telegram_token: str = "8611789455:AAFulww2_9jPKSwUdwAmTwZjYJw1KTPNisA"
    chat_id_raw: str = "1264164655"
    source_api_url: str = "https://sky-predictor-1012593186417.asia-southeast1.run.app/api/wingo-history-1m-1000"
    db_path: str = "predictor.db"
    log_dir: str = "logs"
    fetch_every: float = 2.0
    request_timeout: float = 15.0
    bot_name: str = "V165 PRE-PATTERN AI"

    def parse_chat_id(self):
        return int(self.chat_id_raw) if self.chat_id_raw.isdigit() else self.chat_id_raw
    def validate(self):
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)

CFG = Config()

def setup_logging():
    Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    fh = RotatingFileHandler(Path(CFG.log_dir)/"bot.log", maxBytes=1_000_000, backupCount=1)
    fh.setFormatter(fmt); root.addHandler(fh)

setup_logging()
log = logging.getLogger("v165")
def h(x: Any) -> str: return html.escape(str(x))

class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()
    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS predictions (
                issue TEXT PRIMARY KEY, size TEXT, num INTEGER, status TEXT DEFAULT 'PENDING',
                actual_num INTEGER, actual_size TEXT, mode TEXT, logic TEXT, sigs TEXT, created_at REAL)""")
            try: c.execute("ALTER TABLE predictions ADD COLUMN sigs TEXT")
            except sqlite3.OperationalError: pass
            c.execute("""CREATE TABLE IF NOT EXISTS mistake_book (
                pat_key TEXT PRIMARY KEY, wins INTEGER, total INTEGER, last_seen REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS model_weights (id INTEGER PRIMARY KEY, value REAL)""")
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.execute("PRAGMA journal_mode=TRUNCATE")
        return c
    def prediction_exists(self, issue):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM predictions WHERE issue=?", (issue,)).fetchone() is not None
    def save_prediction(self, issue, size, num, mode, logic, sigs):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO predictions (issue,size,num,mode,logic,sigs,created_at) VALUES (?,?,?,?,?,?,?)",
                      (issue, size, num, mode, logic, json.dumps(sigs), time.time()))
    def resolve_pending(self, history_dict):
        resolved = []
        with self._conn() as c:
            rows = c.execute("SELECT issue,size,num,mode,sigs FROM predictions WHERE status='PENDING'").fetchall()
        for issue, p_size, p_num, mode, sigs_json in rows:
            if issue in history_dict:
                act = history_dict[issue]
                act_size_str = "BIG" if act['size'] == 1 else "SMALL"
                win = (p_size == act_size_str)
                status = ("JACK 🏆" if p_num == act['num'] else "WIN ✅") if win else "LOSS ❌"
                with self._conn() as c:
                    c.execute("UPDATE predictions SET status=?,actual_num=?,actual_size=? WHERE issue=?",
                              (status, act['num'], act_size_str, issue))
                resolved.append({"issue":issue,"size":p_size,"num":p_num,"actual_num":act['num'],
                                 "actual_size":act_size_str,"status":status,"mode":mode,"sigs":sigs_json,"win":win})
        return resolved
    def recent_outcomes(self, limit=12):
        with self._conn() as c:
            rows = c.execute("SELECT issue,actual_size,actual_num,status FROM predictions WHERE status!='PENDING' ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?",(limit,)).fetchall()
        return rows[::-1]
    def save_weights(self, weights):
        with self._conn() as c:
            c.execute("DELETE FROM model_weights")
            for i, w in enumerate(weights):
                c.execute("INSERT INTO model_weights (id,value) VALUES (?,?)",(i,w))
    def load_weights(self):
        try:
            with self._conn() as c:
                return [r[0] for r in c.execute("SELECT value FROM model_weights ORDER BY id").fetchall()]
        except: return None
    def update_book(self, key, hit):
        with self._conn() as c:
            row = c.execute("SELECT wins,total FROM mistake_book WHERE pat_key=?",(key,)).fetchone()
            if row: c.execute("UPDATE mistake_book SET wins=?,total=?,last_seen=? WHERE pat_key=?",(row[0]+hit,row[1]+1,time.time(),key))
            else: c.execute("INSERT INTO mistake_book (pat_key,wins,total,last_seen) VALUES (?,?,?,?)",(key,hit,1,time.time()))
    def book_mult(self, key):
        with self._conn() as c:
            row = c.execute("SELECT wins,total FROM mistake_book WHERE pat_key=?",(key,)).fetchone()
        if not row: return 1.0
        w, t = row
        if t < 3: return 1.0
        acc = (w + 1) / (t + 2)
        return max(0.2, min(1.5, acc * 2))

STORE = Storage(CFG.db_path)

def skewness(seq):
    n=len(seq); mean=sum(seq)/n; var=sum((x-mean)**2 for x in seq)/n
    if var==0: return 0
    return sum((x-mean)**3 for x in seq)/n / (var**1.5)

def autocorr(seq, lag):
    n=len(seq)
    if n<=lag: return 0
    mean=sum(seq)/n; var=sum((x-mean)**2 for x in seq)
    if var==0: return 0
    return sum((seq[i]-mean)*(seq[i+lag]-mean) for i in range(n-lag)) / var

class PrePatternAI:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.lr = 0.05
        saved = STORE.load_weights()
        if saved and len(saved) == 26:
            self.weights = saved; self.warmed_up = True
        else:
            self.weights = [0.0]*26; self.warmed_up = False
        self.save_counter = 0

    async def fetch_data(self, session):
        try:
            async with session.get(CFG.source_api_url, timeout=aiohttp.ClientTimeout(total=CFG.request_timeout)) as r:
                if r.status != 200: return False
                data = await r.json()
                items = data.get("data", {}).get("list", [])
                items.sort(key=lambda x: int(x["issueNumber"]))
                out=[]; seen=set()
                for item in items:
                    try:
                        issue=str(item["issueNumber"])
                        if issue in seen: continue
                        seen.add(issue)
                        num=int(item["number"])
                        out.append({"issue":issue,"num":num,"size":1 if num>=5 else 0})
                    except: continue
                self.history=out
                return True
        except: return False

    # 🔥 PRE-PATTERN RULE (Number & Size)
    def find_pre_pattern(self, nums, seq):
        """Look at result BEFORE pattern vs result AFTER pattern (SAME/OPP)"""
        best = None  # (dir, conf, tag, num_guess)
        for L in [6,5,4,3]:
            if len(nums) < L + 5: continue
            target = tuple(nums[-L:])
            cur_pre_idx = len(nums) - L - 1
            if cur_pre_idx < 0: continue
            cur_pre_size = 1 if nums[cur_pre_idx] >= 5 else 0
            cur_pre_num = nums[cur_pre_idx]

            same_n, opp_n = 0, 0
            post_nums_opp, post_nums_same = [], []
            for i in range(len(nums) - L):
                pre_idx = i - 1
                if pre_idx < 0: continue
                if tuple(nums[i:i+L]) != target: continue
                if i+L >= len(nums): continue
                post_size = 1 if nums[i+L] >= 5 else 0
                pre_size = 1 if nums[pre_idx] >= 5 else 0
                if post_size == pre_size:
                    same_n += 1; post_nums_same.append(nums[i+L])
                else:
                    opp_n += 1; post_nums_opp.append(nums[i+L])
            total = same_n + opp_n
            if total >= 3:
                if opp_n / total >= 0.70 and opp_n >= 3:
                    dir_ = 1 - cur_pre_size
                    conf = opp_n / total
                    nums_pool = [n for n in post_nums_opp if (n >= 5) == (dir_ == 1)]
                    guess = Counter(nums_pool).most_common(1)[0][0] if nums_pool else None
                    cand = (dir_, conf, f"PreNum{L}:OPP", guess)
                    if not best or cand[1] > best[1]: best = cand
                    break
                elif same_n / total >= 0.70 and same_n >= 3:
                    dir_ = cur_pre_size
                    conf = same_n / total
                    nums_pool = [n for n in post_nums_same if (n >= 5) == (dir_ == 1)]
                    guess = Counter(nums_pool).most_common(1)[0][0] if nums_pool else None
                    cand = (dir_, conf, f"PreNum{L}:SAME", guess)
                    if not best or cand[1] > best[1]: best = cand
                    break

        # Size-only pre-pattern
        for L in [10,8,6,4]:
            if len(seq) < L + 5: continue
            target = tuple(seq[-L:])
            cur_pre_idx = len(seq) - L - 1
            if cur_pre_idx < 0: continue
            cur_pre_size = seq[cur_pre_idx]
            same_n, opp_n = 0, 0
            for i in range(len(seq) - L):
                if i == 0: continue
                if tuple(seq[i:i+L]) != target: continue
                if i+L >= len(seq): continue
                if seq[i+L] == seq[i-1]: same_n += 1
                else: opp_n += 1
            total = same_n + opp_n
            if total >= 3:
                if opp_n / total >= 0.65:
                    cand = (1 - cur_pre_size, opp_n/total, f"PreSz{L}:OPP", None)
                    if not best or cand[1] > best[1]: best = cand
                    break
                elif same_n / total >= 0.65:
                    cand = (cur_pre_size, same_n/total, f"PreSz{L}:SAME", None)
                    if not best or cand[1] > best[1]: best = cand
                    break
        return best

    # 🔥 POST-PATTERN (Trend Shift — what came AFTER pattern)
    def find_post_pattern(self, seq):
        """After a size pattern completes, what usually happens?"""
        for L in [8,6,4]:
            if len(seq) < L + 5: continue
            target = tuple(seq[-L:])
            big, small = 0, 0
            for i in range(len(seq) - L):
                if tuple(seq[i:i+L]) == target:
                    if i+L < len(seq):
                        if seq[i+L] == 1: big += 1
                        else: small += 1
            total = big + small
            if total >= 3:
                p = (big+1)/(total+2)
                return (1 if p>=0.5 else 0), max(p,1-p), f"Post{L}"
        return None

    def scan_single_rep(self, nums):
        if len(nums) < 12: return 0.0
        for L in [12,10,8]:
            if len(nums) < L + 5: continue
            window = nums[-L:]
            freq = Counter(window)
            dom, cnt = freq.most_common(1)[0]
            if cnt < 4: continue
            fb, fs = 0, 0
            for i in range(len(nums)-L):
                hw = nums[i:i+L]
                if hw.count(dom) >= cnt and i+L < len(nums):
                    if nums[i+L] >= 5: fb += 1
                    else: fs += 1
            total = fb + fs
            if total >= 3:
                p = (fb+1)/(total+2)
                return (p-0.5)*2.0
        return 0.0

    def extract_features(self, seq, nums):
        if len(seq) < 50: return None, None
        feat = [1.0]
        for i in range(1,6): feat.append(1.0 if seq[-i]==1 else -1.0)
        for order in [2,3,4,5,6]:
            target = tuple(seq[-order:])
            b,s=0,0
            for i in range(len(seq)-order):
                if tuple(seq[i:i+order])==target:
                    if seq[i+order]==1: b+=1
                    else: s+=1
            t=b+s; p=(b+1)/(t+2) if t>0 else 0.5
            feat.append((p-0.5)*2.0)
        w30 = seq[-30:]
        z = (sum(w30)-15.0)/math.sqrt(30*0.25)
        feat.append(max(-2,min(2,z))/2.0)
        pos,neg=0.0,0.0
        for s in seq[-30:]:
            pos=max(0,pos+s-0.6); neg=max(0,neg-s+0.4)
        feat.append(max(-1,min(1,(pos-neg)/3.0)))
        w20=seq[-20:]; p_big=sum(w20)/20.0; p_small=1-p_big
        ent=0.0
        if p_big>0: ent -= p_big*math.log2(p_big)
        if p_small>0: ent -= p_small*math.log2(p_small)
        feat.append((ent-0.5)*2.0)
        streak=1
        for i in range(len(seq)-2,-1,-1):
            if seq[i]==seq[-1]: streak+=1
            else: break
        feat.append((min(10,streak)/10.0)*(1.0 if seq[-1]==1 else -1.0))
        feat.append((sum(seq[-10:])/10.0-0.5)*2.0)
        # Pre-pattern (18-19)
        pre = self.find_pre_pattern(nums, seq)
        pre_sig = 0.0; pre_num = None; pre_conf = 0.0; pre_tag = ""
        if pre:
            dir_, conf, tag, gn = pre
            pre_sig = (1.0 if dir_==1 else -1.0)*conf
            pre_num = gn; pre_conf = conf; pre_tag = tag
        feat.append(pre_sig)
        feat.append(pre_conf)
        # Post-pattern (20)
        post = self.find_post_pattern(seq)
        if post:
            dir_, conf, tag = post
            feat.append((1.0 if dir_==1 else -1.0)*conf)
        else: feat.append(0.0)
        # Single Rep (21)
        feat.append(self.scan_single_rep(nums))
        # Autocorr (22-24)
        feat.append(max(-1,min(1,autocorr(seq[-30:],1))))
        feat.append(max(-1,min(1,autocorr(seq[-30:],2))))
        feat.append(max(-1,min(1,autocorr(seq[-30:],3))))
        # Skew (25)
        feat.append(max(-2,min(2,skewness(seq[-30:])))/2.0)
        return feat[:26], (pre_num, pre_conf, pre_tag)

    def sigmoid(self,x):
        try: return 1/(1+math.exp(-max(-20,min(20,x))))
        except OverflowError: return 0.0 if x<0 else 1.0

    def train(self, feat, y):
        if not feat or len(feat)!=26: return
        z = sum(w*f for w,f in zip(self.weights,feat))
        pred = self.sigmoid(z)
        err = y - pred
        for i in range(26):
            reg = 0.001*self.weights[i]
            self.weights[i] += self.lr*(err*feat[i]-reg)
        self.save_counter+=1
        if self.save_counter%10==0: STORE.save_weights(self.weights)

    def warmup(self, seq, nums):
        print("🔥 Warmup started (1000 results, 3 epochs)...")
        for _ in range(3):
            for i in range(50, len(seq)):
                f,_ = self.extract_features(seq[:i], nums[:i])
                if f: self.train(f, seq[i])
        STORE.save_weights(self.weights)
        self.warmed_up=True
        print("🔥 Warmup done")

    def get_num(self, data, side, pre_num_guess=None):
        nums = [x['num'] for x in data]
        if pre_num_guess is not None and (pre_num_guess>=5)==(side==1):
            return pre_num_guess
        for L in [5,4,3]:
            if len(nums)<L+5: continue
            tgt = tuple(nums[-L:]); fol=[]
            for i in range(len(nums)-L):
                if tuple(nums[i:i+L])==tgt and i+L<len(nums): fol.append(nums[i+L])
            if fol:
                p=[n for n in fol if (n>=5)==(side==1)]
                if p: return Counter(p).most_common(1)[0][0]
        d=nums[::-1]; ln=d[0]; f30=Counter(d[:30]); tm=Counter()
        for i in range(1,len(d)): tm[f"{d[i]}>{d[i-1]}"]+=1
        pool=[5,6,7,8,9] if side==1 else [0,1,2,3,4]
        best,bs=pool[0],-1
        for n in pool:
            gp=d.index(n) if n in pool else 60; gg=min(gp,30)
            tr=tm.get(f"{ln}>{n}",0); nh=1.5 if (n==(ln+1)%10 or n==(ln+9)%10) else 0
            s=gg*1.2+f30.get(n,0)*1.5+tr*3+nh
            if s>bs: bs=s; best=n
        return best

    def predict(self):
        if len(self.history)<100: return None
        seq=[x["size"] for x in self.history]; nums=[x["num"] for x in self.history]
        if not self.warmed_up: self.warmup(seq, nums)
        feat, pre = self.extract_features(seq, nums)
        if not feat: return None
        z = sum(w*f for w,f in zip(self.weights,feat))
        p_big = self.sigmoid(z)

        logic_tags=[]; conf_mult=1.0
        pre_num, pre_conf, pre_tag = pre if pre else (None,0.0,"")

        # 🔥 RECOVERY MODE (1 Loss = Ultra)
        if self.loss_streak >= 1:
            mode="ULTRA_RECOVERY"
            if pre and pre_conf >= 0.65:
                # Highest confidence: use pre-pattern rule directly
                p_big = 1.0 if pre[0]==1 else 0.0
                p_big = 0.85 if pre[0]==1 else 0.15
                logic_tags=[pre_tag+"_RECOVERY"]
                conf_mult=1.2
            elif abs(feat[11])>0.7:  # Strong Z-score
                p_big = 0.80 if feat[11]<0 else 0.20
                logic_tags=["ZScore_Recovery"]
            else:
                logic_tags=["NeuralRecovery"]
        else:
            mode="NEURAL_DEEP"
            if pre and pre_conf >= 0.6:
                logic_tags.append(pre_tag)
            if abs(feat[20])>0.2: logic_tags.append("PostPattern")
            if abs(feat[21])>0.2: logic_tags.append("SingleRep")
            if not logic_tags: logic_tags=["PureMath"]

        pred_size = 1 if p_big >= 0.5 else 0
        conf_val = p_big if pred_size==1 else (1-p_big)
        confidence = int(min(92, max(55, conf_val*100*conf_mult)))
        if mode=="ULTRA_RECOVERY": confidence=max(82, confidence)

        target = self.get_num(self.history, pred_size, pre_num)
        return {
            "issue": str(int(self.history[-1]["issue"])+1),
            "size": "BIG" if pred_size==1 else "SMALL",
            "num": target, "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode, "sigs": logic_tags
        }

PREDICTOR = PrePatternAI()

def fmt(p, rows):
    rtxt = "".join(f"➜ <code>{h(r[0][-4:])}</code> {h(r[1])} {h(r[2])} → <b>{h(r[3])}</b>\n" for r in rows)
    return f"""📌 <b>Period No</b>: <code>{h(p['issue'][-4:])}</code>
🎯 <b>Target No</b>: <code>{h(p['num'])}</code>
📊 <b>Size</b>: <b>{h(p['size'])}</b>

📈 <b>Confidence</b>: {p['confidence']}%
🧠 <b>Logic</b>: {h(p['logic'])}
⚖️ <b>Mode</b>: <b>{h(p['mode'])}</b>

━━━━━━━━━━━━━━━━━━
🕒 <b>Recent Outcomes</b>:
{rtxt}━━━━━━━━━━━━━━━━━━
⚡ <i>V165 PRE-PATTERN AI</i>"""

async def send_msg(app, text, retries=3):
    for a in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return
        except (TimedOut, NetworkError): await asyncio.sleep(2)
        except Exception as e: log.error(f"Send: {e}"); break

async def worker(app, session):
    log.info("V165 started...")
    last = None
    while True:
        try:
            if await PREDICTOR.fetch_data(session):
                hd = {x["issue"]:x for x in PREDICTOR.history}
                for res in STORE.resolve_pending(hd):
                    if res['win']: PREDICTOR.loss_streak=0
                    else: PREDICTOR.loss_streak+=1
                    try:
                        for s in json.loads(res.get('sigs') or '[]'): STORE.update_book(s, 1 if res['win'] else 0)
                    except: pass
                    await send_msg(app, f"🎯 <b>Result</b>\nPeriod: <code>{h(res['issue'][-4:])}</code>\nPred: {h(res['size'])} {h(res['num'])}\nAct: {h(res['actual_size'])} {h(res['actual_num'])}\n<b>{h(res['status'])}</b>")
                latest = PREDICTOR.history[-1]["issue"]
                nxt = str(int(latest)+1)
                if latest != last and not STORE.prediction_exists(nxt):
                    if len(PREDICTOR.history)>1:
                        ps = [x["size"] for x in PREDICTOR.history[:-1]]
                        pn = [x["num"] for x in PREDICTOR.history[:-1]]
                        f,_ = PREDICTOR.extract_features(ps,pn)
                        if f: PREDICTOR.train(f, PREDICTOR.history[-1]["size"])
                    p = PREDICTOR.predict()
                    if p:
                        STORE.save_prediction(p['issue'],p['size'],p['num'],p['mode'],p['logic'],p['sigs'])
                        await send_msg(app, fmt(p, STORE.recent_outcomes(12)))
                        last = latest
            gc.collect(); await asyncio.sleep(CFG.fetch_every)
        except asyncio.CancelledError: break
        except Exception as e:
            log.error(f"Loop: {e}"); await asyncio.sleep(2)

async def ping(req): return web.Response(text="V165 running!")

async def main():
    CFG.validate()
    app_w = web.Application(); app_w.router.add_get('/', ping)
    runner = web.AppRunner(app_w); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT",8080))).start()
    app = Application.builder().token(CFG.telegram_token).connect_timeout(20).read_timeout(20).build()
    async with app:
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True)
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(worker(app, session))
            await asyncio.Event().wait()

if __name__ == "__main__": asyncio.run(main())
