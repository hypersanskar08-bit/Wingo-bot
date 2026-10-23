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
    bot_name: str = "V166 PURE PATTERN ENGINE"

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
log = logging.getLogger("v166")
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

class PurePatternEngine:
    def __init__(self):
        self.history = []
        self.loss_streak = 0
        self.lr = 0.05
        saved = STORE.load_weights()
        if saved and len(saved) == 24:
            self.weights = saved; self.warmed_up = True
        else:
            self.weights = [0.0]*24; self.warmed_up = False
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

    # 🔥 PURE PATTERN SCANNERS (No Opposite Logic)
    def scan_size_pattern(self, seq):
        for L in range(12, 2, -1):
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
                if p >= 0.6 or p <= 0.4:
                    dir_ = 1 if p >= 0.5 else 0
                    return (1.0 if dir_==1 else -1.0)*max(p, 1-p), f"SzPat{L}", total
        return 0.0, "SzPat0", 0

    def scan_num_short(self, nums):
        for L in range(5, 2, -1):
            if len(nums) < L + 5: continue
            target = tuple(nums[-L:])
            big, small = 0, 0
            for i in range(len(nums) - L):
                if tuple(nums[i:i+L]) == target:
                    if i+L < len(nums):
                        if nums[i+L] >= 5: big += 1
                        else: small += 1
            total = big + small
            if total >= 2:
                p = (big+1)/(total+2)
                if p >= 0.6 or p <= 0.4:
                    dir_ = 1 if p >= 0.5 else 0
                    return (1.0 if dir_==1 else -1.0)*max(p, 1-p), f"NumPat{L}", total
        return 0.0, "NumPat0", 0

    def scan_single_rep(self, nums):
        if len(nums) < 12: return 0.0, "Rep0", 0
        for L in [12, 10, 8, 7]:
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
                dir_ = 1 if p >= 0.5 else 0
                return (1.0 if dir_==1 else -1.0)*max(p, 1-p), f"Rep{L}", total
        return 0.0, "Rep0", 0

    def scan_trend_shift(self, seq):
        if len(seq) < 15: return 0.0, "Trd0", 0
        target = tuple(seq[-6:])
        last_side = seq[-1]
        same, opp = 0, 0
        for i in range(len(seq)-6):
            if tuple(seq[i:i+6]) == target:
                if i+6 < len(seq):
                    if seq[i+6] == last_side: same += 1
                    else: opp += 1
        total = same + opp
        if total >= 3:
            p_same = (same+1)/(total+2)
            p_big = p_same if last_side==1 else (1-p_same)
            return (p_big-0.5)*2.0, "TrendShift", total
        return 0.0, "Trd0", 0

    def extract_features(self, seq, nums):
        if len(seq) < 50: return None
        feat = [1.0]
        # Sizes (1-5)
        for i in range(1,6): feat.append(1.0 if seq[-i]==1 else -1.0)
        # Markov (6-10)
        for order in [2,3,4,5,6]:
            target = tuple(seq[-order:])
            b,s=0,0
            for i in range(len(seq)-order):
                if tuple(seq[i:i+order])==target:
                    if seq[i+order]==1: b+=1
                    else: s+=1
            t=b+s; p=(b+1)/(t+2) if t>0 else 0.5
            feat.append((p-0.5)*2.0)
        # Z-Score (11)
        w30 = seq[-30:]
        z = (sum(w30)-15.0)/math.sqrt(30*0.25)
        feat.append(max(-2,min(2,z))/2.0)
        # CUSUM (12)
        pos,neg=0.0,0.0
        for s in seq[-30:]:
            pos=max(0,pos+s-0.6); neg=max(0,neg-s+0.4)
        feat.append(max(-1,min(1,(pos-neg)/3.0)))
        # Entropy (13)
        w20=seq[-20:]; p_big=sum(w20)/20.0; p_small=1-p_big
        ent=0.0
        if p_big>0: ent -= p_big*math.log2(p_big)
        if p_small>0: ent -= p_small*math.log2(p_small)
        feat.append((ent-0.5)*2.0)
        # Streak (14)
        streak=1
        for i in range(len(seq)-2,-1,-1):
            if seq[i]==seq[-1]: streak+=1
            else: break
        feat.append((min(10,streak)/10.0)*(1.0 if seq[-1]==1 else -1.0))
        # Momentum (15)
        feat.append((sum(seq[-10:])/10.0-0.5)*2.0)
        # Patterns (16-19)
        sz_sig, sz_tag, sz_occ = self.scan_size_pattern(seq)
        num_sig, num_tag, num_occ = self.scan_num_short(nums)
        rep_sig, rep_tag, rep_occ = self.scan_single_rep(nums)
        trd_sig, trd_tag, trd_occ = self.scan_trend_shift(seq)
        feat.append(sz_sig)
        feat.append(num_sig)
        feat.append(rep_sig)
        feat.append(trd_sig)
        # Autocorr (20-22)
        feat.append(max(-1,min(1,autocorr(seq[-30:],1))))
        feat.append(max(-1,min(1,autocorr(seq[-30:],2))))
        feat.append(max(-1,min(1,autocorr(seq[-30:],3))))
        # Skew (23)
        feat.append(max(-2,min(2,skewness(seq[-30:])))/2.0)
        
        meta = {
            'sz': (sz_sig, sz_tag, sz_occ), 'num': (num_sig, num_tag, num_occ),
            'rep': (rep_sig, rep_tag, rep_occ), 'trd': (trd_sig, trd_tag, trd_occ)
        }
        return feat[:24], meta

    def sigmoid(self,x):
        try: return 1/(1+math.exp(-max(-20,min(20,x))))
        except OverflowError: return 0.0 if x<0 else 1.0

    def train(self, feat, y):
        if not feat or len(feat)!=24: return
        z = sum(w*f for w,f in zip(self.weights,feat))
        pred = self.sigmoid(z)
        err = y - pred
        for i in range(24):
            reg = 0.001*self.weights[i]
            self.weights[i] += self.lr*(err*feat[i]-reg)
        self.save_counter+=1
        if self.save_counter%10==0: STORE.save_weights(self.weights)

    def warmup(self, seq, nums):
        print("🔥 Warmup started (1000 results, 3 epochs)...")
        for _ in range(3):
            for i in range(50, len(seq)):
                f, _ = self.extract_features(seq[:i], nums[:i])
                if f: self.train(f, seq[i])
        STORE.save_weights(self.weights)
        self.warmed_up=True
        print("🔥 Warmup done")

    def get_num(self, data, side, num_sig, num_tag):
        nums = [x['num'] for x in data]
        # 1. If short pattern is strong, use its number
        if abs(num_sig) > 0.2:
            for L in [5,4,3]:
                if len(nums) < L + 5: continue
                tgt = tuple(nums[-L:]); fol=[]
                for i in range(len(nums)-L):
                    if tuple(nums[i:i+L])==tgt and i+L<len(nums): fol.append(nums[i+L])
                if fol:
                    p=[n for n in fol if (n>=5)==(side==1)]
                    if p: return Counter(p).most_common(1)[0][0]
        
        # 2. Single Repetition
        if len(nums) >= 12:
            recent_10 = nums[-10:]
            freq = Counter(recent_10)
            dom, cnt = freq.most_common(1)[0]
            if cnt >= 4:
                next_nums = []
                for i in range(len(nums)-10):
                    w = nums[i:i+10]
                    if w.count(dom) >= cnt and i+10 < len(nums):
                        next_nums.append(nums[i+10])
                if next_nums:
                    p=[n for n in next_nums if (n>=5)==(side==1)]
                    if p: return Counter(p).most_common(1)[0][0]

        # 3. Fallback: CSV logic
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
        feat, meta = self.extract_features(seq, nums)
        if not feat: return None
        z = sum(w*f for w,f in zip(self.weights,feat))
        p_big = self.sigmoid(z)

        logic_tags = []
        sz_sig, sz_tag, sz_occ = meta['sz']
        num_sig, num_tag, num_occ = meta['num']
        rep_sig, rep_tag, rep_occ = meta['rep']
        trd_sig, trd_tag, trd_occ = meta['trd']

        # 🔥 RECOVERY MODE (1 Loss = Ultra)
        if self.loss_streak >= 1:
            mode = "ULTRA_RECOVERY"
            best_mult = 0.0
            best_dir = None
            
            # Choose strongest signal
            for sig, tag, occ, mult in [(sz_sig, sz_tag, sz_occ, 1.0), (num_sig, num_tag, num_occ, 1.2),
                                       (rep_sig, rep_tag, rep_occ, 1.5), (feat[11], "ZScore", 10, 1.3)]:
                w = STORE.book_mult(tag)
                score = abs(sig) * mult * w
                if abs(sig) > 0.5 and score > best_mult:
                    best_mult = score
                    best_dir = 1 if sig > 0 else 0
                    best_tag = tag
            
            if best_dir is not None:
                p_big = 1.0 if best_dir==1 else 0.0
                logic_tags = [f"REC_{best_tag}"]
                confidence = 85
            else:
                logic_tags = ["Recovery_Neural"]
                confidence = 72
        else:
            mode = "NEURAL_PATTERN"
            if abs(sz_sig) > 0.2: logic_tags.append(sz_tag)
            if abs(num_sig) > 0.2: logic_tags.append(num_tag)
            if abs(rep_sig) > 0.2: logic_tags.append(rep_tag)
            if abs(trd_sig) > 0.2: logic_tags.append(trd_tag)
            if not logic_tags: logic_tags.append("PureMath")
            
            pred_size = 1 if p_big >= 0.5 else 0
            conf_val = p_big if pred_size==1 else (1-p_big)
            confidence = int(min(88, max(55, conf_val*100)))

        pred_size = 1 if p_big >= 0.5 else 0
        target = self.get_num(self.history, pred_size, num_sig, num_tag)

        return {
            "issue": str(int(self.history[-1]["issue"])+1),
            "size": "BIG" if pred_size==1 else "SMALL",
            "num": target, "confidence": confidence,
            "logic": " + ".join(logic_tags[:3]),
            "mode": mode, "sigs": logic_tags
        }

PREDICTOR = PurePatternEngine()

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
⚡ <i>V166 PURE PATTERN ENGINE</i>"""

async def send_msg(app, text, retries=3):
    for a in range(retries):
        try:
            await app.bot.send_message(chat_id=CFG.parse_chat_id(), text=text, parse_mode="HTML")
            return
        except (TimedOut, NetworkError): await asyncio.sleep(2)
        except Exception as e: log.error(f"Send: {e}"); break

async def worker(app, session):
    log.info("V166 started...")
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

async def ping(req): return web.Response(text="V166 running!")

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
