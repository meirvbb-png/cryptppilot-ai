"""Central Meta-AI: monitors every bot from the shared DB and proposes changes that YOU approve.

Nothing here trades or changes a bot by itself: every change is a 'proposal' with an approve /
reject button in the app. Analysis:
  * market regime per coin (trend / range / volatile) + regime stored on every trade
  * per bot x regime / UTC session / coin: trades, win rate, profit factor, expectancy in R
  * degradation: one-sided t-test of live R-expectancy vs the Stage-1 backtest expectancy
  * overfitting: live PF far below the backtest after enough trades; drawdown beyond 1.5x backtest
  * proposals: block a losing regime, cut risk, pause, re-allocate capital, re-validated parameters
"""
import asyncio, hashlib, itertools, json, logging, math, time
import numpy as np, pandas as pd
from . import config as C, db
from .regime import regime_detail, REGIMES
from research.bt import engine as BT, strategies as RS
from research.bt.data import resample, map_signals

log = logging.getLogger("brain")
SESSIONS = {0: "אסיה 00-06", 1: "אירופה 06-12", 2: "ארה\"ב 12-18", 3: "ערב 18-24"}


def _stats(tr):
    if not tr:
        return dict(n=0)
    pnl = np.array([t["pnl_net"] for t in tr])
    R = np.array([t["pnl_net"] / max(1e-9, t["qty"] * abs(t["entry_px"] - t["sl"])) for t in tr])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return dict(n=len(tr), win=round(100 * (pnl > 0).mean(), 1), pf=round(gp / gl, 2) if gl > 0 else None,
                net=round(pnl.sum(), 2), exp_r=round(R.mean(), 3), sd_r=round(R.std(ddof=1), 3) if len(R) > 1 else None)


def bench_exp_r(b):
    w = b["win"] / 100
    return (1 - w) * (b["pf"] - 1)       # E[R] with -1R losers and PF-implied winners


class Brain:
    def __init__(self, store, engine):
        self.store = store; self.engine = engine
        self.state = dict(regimes={}, stats={}, updated=None)
        self.last_param_check = {}

    # ------------------------------------------------------------------ loop
    async def run(self):
        await asyncio.sleep(20)
        while True:
            try:
                await asyncio.to_thread(self.evaluate)
                await asyncio.to_thread(self.param_check)
            except Exception as e:
                log.exception("brain: %s", e)
            await asyncio.sleep(600)

    def evaluate(self):
        self.state["regimes"] = {s: regime_detail(self.store, s) for s in C.SYMBOLS}
        total_alloc = 0; scores = {}
        for bot in self.engine.bots.values():
            spec = bot.spec; b = spec["bench"]; row = bot.row
            tr = db.q("SELECT * FROM trades WHERE bot_id=? AND status='closed' ORDER BY exit_ts", (bot.id,))
            st = dict(all=_stats(tr),
                      regime={r: _stats([t for t in tr if t["regime"] == r]) for r in REGIMES},
                      session={v: _stats([t for t in tr if t["hour"] is not None and t["hour"] // 6 == k])
                               for k, v in SESSIONS.items()},
                      symbol={s: _stats([t for t in tr if t["symbol"] == s]) for s in C.SYMBOLS})
            eqs = [e["equity"] for e in db.q("SELECT equity FROM equity WHERE bot_id=? ORDER BY ts", (bot.id,))]
            peak = np.maximum.accumulate(eqs) if eqs else np.array([row["equity"]])
            dd = 100 * (np.array(eqs) / peak - 1).min() if eqs else 0.0
            st["max_dd"] = round(float(dd), 2)
            self.state["stats"][bot.id] = st
            self._alerts(bot, st, b, row)
            self._regime_proposals(bot, st)
            # allocation score: live evidence blended with the backtest prior
            n = st["all"]["n"]; w = n / (n + 30)
            live = (st["all"]["exp_r"] / st["all"]["sd_r"] * math.sqrt(12)) if n > 5 and st["all"]["sd_r"] else 0
            scores[bot.id] = w * live + (1 - w) * b["sharpe"]
            total_alloc += row["allocation"]
        self._allocation_proposal(scores, total_alloc)
        self.state["updated"] = time.time()

    # ------------------------------------------------------------------ insights
    def _insight(self, bot_id, kind, severity, text, data=None):
        db.x("DELETE FROM insights WHERE bot_id=? AND kind=?", (bot_id, kind))
        db.x("INSERT INTO insights(ts,bot_id,kind,severity,text,data) VALUES(?,?,?,?,?,?)",
             (time.time(), bot_id, kind, severity, text, db.jdump(data or {})))

    def _clear(self, bot_id, kind):
        db.x("DELETE FROM insights WHERE bot_id=? AND kind=?", (bot_id, kind))

    def _alerts(self, bot, st, b, row):
        a = st["all"]; n = a["n"]; exp_bt = bench_exp_r(b)
        if b["pf"] < 1:
            self._insight(bot.id, "experimental", "info",
                          f"{bot.spec['name']}: האסטרטגיה נכשלה בבדיקות העבר (PF {b['pf']}). רצה בדמו בלבד כניסוי - "
                          f"לא תומלץ למצב חי לפני 30+ עסקאות עם PF מעל 1.2.")
        if n < 10:
            self._insight(bot.id, "sample", "info", f"{bot.spec['name']}: {n} עסקאות סגורות - צריך לפחות 10 להערכה סטטיסטית.")
            return
        self._clear(bot.id, "sample")
        t = (a["exp_r"] - exp_bt) / (a["sd_r"] / math.sqrt(n)) if a["sd_r"] else 0
        if t < -2:
            self._insight(bot.id, "degradation", "high",
                          f"ירידה מובהקת בביצועים: תוחלת {a['exp_r']:+.2f}R מול {exp_bt:+.2f}R בבדיקות (t={t:.1f}, {n} עסקאות).",
                          dict(t=t))
            self._propose(bot.id, "risk", "הקטנת סיכון ל-0.5% לעסקה",
                          f"הביצועים בזמן אמת נמוכים מובהקות מהבדיקות. הקטנת הסיכון מ-{100*row['risk']:.1f}% ל-0.5% "
                          "מגבילה נזק עד שהמצב יתברר.", dict(risk=0.005))
        else:
            self._clear(bot.id, "degradation")
        if n >= 20 and a["pf"] is not None and a["pf"] < 1 and b["pf"] >= 1.2:
            self._insight(bot.id, "overfit", "high",
                          f"חשד להתאמת-יתר (Overfitting): PF בזמן אמת {a['pf']} מול {b['pf']} בבדיקות אחרי {n} עסקאות.")
        else:
            self._clear(bot.id, "overfit")
        if st["max_dd"] < 1.5 * b["mdd"]:
            self._insight(bot.id, "drawdown", "high",
                          f"ירידה מהשיא {st['max_dd']}% - חורגת פי 1.5 מהגרועה בבדיקות ({b['mdd']}%).")
            self._propose(bot.id, "pause", "השהיית הבוט", f"הירידה מהשיא ({st['max_dd']}%) עברה את גבול הבטיחות. "
                          "השהיה תמנע עסקאות חדשות (עסקאות פתוחות ימשיכו לפי ה-SL/TP).", dict(paused=1))
        else:
            self._clear(bot.id, "drawdown")
        best = [(r, s) for r, s in st["regime"].items() if s["n"] >= 5]
        if best:
            txt = " | ".join(f"{r}: {s['n']} עסק', PF {s['pf']}, {s['net']:+.0f}$" for r, s in best)
            self._insight(bot.id, "by_regime", "info", f"ביצועים לפי מצב שוק - {txt}")

    # ------------------------------------------------------------------ proposals
    def _propose(self, bot_id, kind, title, text, payload):
        h = hashlib.md5((bot_id + kind + json.dumps(payload, sort_keys=True)).encode()).hexdigest()[:10]
        payload = dict(payload, _h=h)
        if db.q("SELECT id FROM proposals WHERE bot_id=? AND kind=? AND payload LIKE ? AND "
                "(status='pending' OR decided_ts > ?)", (bot_id, kind, f'%"{h}"%', time.time() - 7 * 86400)):
            return
        db.x("INSERT INTO proposals(ts,bot_id,kind,title,text,payload) VALUES(?,?,?,?,?,?)",
             (time.time(), bot_id, kind, title, text, db.jdump(payload)))

    def _regime_proposals(self, bot, st):
        blocked = bot.settings.get("blocked_regimes", [])
        for r, s in st["regime"].items():
            if r in blocked or s["n"] < 10 or s["pf"] is None:
                continue
            others = [t for k, t in st["regime"].items() if k != r and t["n"] >= 5]
            if s["pf"] < 0.7 and s["net"] < 0 and any((o["pf"] or 0) > 1 for o in others):
                self._propose(bot.id, "block_regime", f"חסימת מסחר במצב '{r}'",
                              f"במצב '{r}' הבוט הפסיד: {s['n']} עסקאות, PF {s['pf']}, {s['net']:+.1f}$, "
                              "בעוד שבמצבים אחרים הוא רווחי.", dict(blocked_regimes=sorted(set(blocked + [r]))))

    def _allocation_proposal(self, scores, total):
        ids = list(scores)
        ns = [self.state["stats"].get(i, {}).get("all", {}).get("n", 0) for i in ids]
        if min(ns) < 10:          # no re-allocation on backtest priors alone: wait for live evidence
            return
        raw = np.array([max(scores[i], 0) + 0.05 for i in ids])
        w = np.clip(raw / raw.sum(), 0.10, 0.40); w = w / w.sum()
        new = {i: round(float(total * wi), 0) for i, wi in zip(ids, w)}
        cur = {i: self.engine.bots[i].row["allocation"] for i in ids}
        if max(abs(new[i] - cur[i]) for i in ids) >= 50:
            txt = " | ".join(f"{self.engine.bots[i].spec['name'].split('·')[0].strip()}: {cur[i]:.0f}$→{new[i]:.0f}$" for i in ids)
            self._propose("all", "allocation", "הקצאת הון מחדש",
                          "שילוב של ביצועים בזמן אמת (משקל עולה עם מספר העסקאות) עם תוצאות הבדיקות. "
                          f"גבולות 10%-40% לבוט. {txt}", dict(allocation=new))

    def param_check(self):
        """Weekly: re-run the backtest engine on the last ~120 days for the current parameters and
        one-step neighbours (from the Stage-1 grid). Propose a change only if clearly better."""
        for bot in self.engine.bots.values():
            name = bot.spec["strategy"]
            if name not in RS.REGISTRY or time.time() - self.last_param_check.get(bot.id, 0) < 7 * 86400:
                continue
            self.last_param_check[bot.id] = time.time()
            f, grid = RS.REGISTRY[name]
            cur = dict(bot.settings.get("params", bot.spec["params"]))
            cands = [cur]
            for k, v in cur.items():
                vals = sorted(set(grid.get(k, [v]) + [v]))
                i = vals.index(v)
                for j in (i - 1, i + 1):
                    if 0 <= j < len(vals):
                        cands.append(dict(cur, **{k: vals[j]}))
            res = []
            for p in cands:
                tot = []; pf = []
                for s in C.SYMBOLS:
                    d = self.store.frame(s)
                    if len(d) < 60 * 24 * 60:
                        continue
                    d = d.copy(); d["t"] = d.index.as_unit("ms").asi8
                    dft = resample(d, bot.spec["tf"]); a = f(dft, bot.spec["tf"], **p)
                    m = map_signals(d, dft, bot.spec["tf"], **a)
                    tr, eq = BT.run(d, m["sig"], m["sl_d"], m["tp_d"], m["hold"], m["exit_l"], m["exit_s"], tick=C.TICK[s])
                    mt = BT.metrics(tr, eq, d.t.to_numpy()); tot.append(mt["net_pct"]); pf.append(mt["pf"] if np.isfinite(mt["pf"]) else 3)
                if tot:
                    res.append((p, float(np.mean(tot)), float(np.median(pf))))
            if len(res) < 2:
                continue
            base = res[0]; best = max(res[1:], key=lambda r: r[1])
            if best[1] > base[1] + 3 and best[2] > max(1.2, base[2] + 0.2):
                diff = {k: v for k, v in best[0].items() if cur.get(k) != v}
                self._propose(bot.id, "params", f"שינוי פרמטר: {diff}",
                              f"בדיקה חוזרת על {len(C.SYMBOLS)} מטבעות ב-~120 הימים האחרונים: נוכחי {base[1]:+.1f}% (PF {base[2]:.2f}) "
                              f"מול {best[1]:+.1f}% (PF {best[2]:.2f}). אזהרה: מדגם קצר - שינוי זה עלול להיות התאמת-יתר.",
                              dict(params=best[0]))

    # ------------------------------------------------------------------ approval
    def decide(self, pid, approve):
        p = db.q("SELECT * FROM proposals WHERE id=?", (pid,))
        if not p or p[0]["status"] != "pending":
            raise ValueError("ההצעה לא קיימת או שכבר הוחלט עליה")
        p = p[0]; pay = json.loads(p["payload"])
        if approve:
            if p["kind"] == "allocation":
                for i, amt in pay["allocation"].items():
                    b = self.engine.bots[i]; row = b.row
                    db.x("UPDATE bots SET allocation=?, equity=equity+? WHERE id=?",
                         (amt, amt - row["allocation"], i))
                    db.x("INSERT INTO equity VALUES(?,?,?)", (i, time.time(), b.row["equity"]))
            else:
                b = self.engine.bots[p["bot_id"]]
                if p["kind"] == "risk":
                    db.x("UPDATE bots SET risk=? WHERE id=?", (pay["risk"], b.id))
                elif p["kind"] == "pause":
                    db.x("UPDATE bots SET paused=1 WHERE id=?", (b.id,))
                elif p["kind"] == "block_regime":
                    b.set_settings(blocked_regimes=pay["blocked_regimes"])
                elif p["kind"] == "params":
                    b.set_settings(params=pay["params"]); b.adapter.p = pay["params"]
            db.event(p["bot_id"], f"אושר: {p['title']}")
        db.x("UPDATE proposals SET status=?, decided_ts=? WHERE id=?",
             ("approved" if approve else "rejected", time.time(), pid))
