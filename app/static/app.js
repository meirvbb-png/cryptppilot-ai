"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STATE = { tab: "bots", ov: null, tradesSeg: "open", chartSym: "BTCUSDT", chartTf: "1h", chartTrade: null, ai: null };
const SYM = { BTCUSDT: "BTC", ETHUSDT: "ETH", SOLUSDT: "SOL" };
const REASON = { tp: "טייק-פרופיט", sl: "סטופ-לוס", signal: "איתות יציאה", time: "זמן מקסימלי", manual: "סגירה ידנית" };

function token() { try { return localStorage.getItem("token") || ""; } catch { return ""; } }
async function api(path, opt = {}) {
  const r = await fetch(path, { ...opt, headers: { "Content-Type": "application/json", "x-token": token(), ...(opt.headers || {}) } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.detail || j.error || r.statusText); e.status = r.status; e.body = j; throw e; }
  return j;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), 3200); }

// ---------- formatting ----------
function dec(sym, p) { if (p == null) return 2; return p >= 1000 ? 1 : p >= 10 ? 2 : 4; }
const fmtPx = (p, sym) => p == null ? "—" : Number(p).toLocaleString("en-US", { minimumFractionDigits: dec(sym, p), maximumFractionDigits: dec(sym, p) });
const usd = (v, sign = false) => v == null ? "—" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v, sign = true) => v == null ? "—" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%";
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
const when = ts => { if (!ts) return "—"; const d = new Date(ts * 1000); return d.toLocaleDateString("he-IL", { day: "2-digit", month: "2-digit" }) + " " + d.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" }); };
const sideChip = s => s > 0 ? '<span class="chip long">לונג ▲</span>' : '<span class="chip short">שורט ▼</span>';
const verdictChip = v => `<span class="chip ${v === "עברה" ? "pass" : v === "גבולית" ? "warn" : "fail"}">בבדיקות: ${esc(v)}</span>`;
const modeChip = (m, he) => `<span class="chip ${m}">${esc(he)}</span>`;

// ---------- header ----------
function renderHeader(ov) {
  $("#totEq").innerHTML = `<span class="num">${usd(ov.total.equity)}</span>`;
  const p = ov.total.pnl, pp = 100 * p / ov.total.allocation;
  $("#totPnl").innerHTML = `<span class="num ${cls(p)}">${usd(p, true)} (${pct(pp)})</span>`;
  const ws = ov.status.ws || "";
  $("#wsDot").className = "dot " + (ws === "connected" ? "ok" : ws.startsWith("reconnect") ? "bad" : "");
  $("#wsTxt").textContent = ws === "connected" ? "מחובר ל-Bybit (חי)" : "מתחבר מחדש…";
  const modes = [...new Set(ov.bots.map(b => b.mode_he))];
  $("#modeAll").textContent = modes.join(" / ");
  $("#prices").innerHTML = Object.entries(ov.prices).map(([s, p]) => {
    const r = ov.regimes?.[s]?.regime; return `<div class="px"><b>${SYM[s] || s}</b> <span class="num">${fmtPx(p, s)}</span>${r ? ` <span class="muted tiny">${esc(r)}</span>` : ""}</div>`;
  }).join("");
}

// ---------- trade card ----------
function tradeCard(t, showBot = true) {
  const open = t.status === "open";
  const pnl = open ? t.upnl : t.pnl_net, pp = open ? t.upnl_pct : t.pnl_pct;
  return `<div class="trade" onclick="showTradeOnChart(${t.id},'${t.symbol}')">
    <div class="row wrap">
      <div>${showBot ? `<b>${esc(t.bot_name || "")}</b><br>` : ""}<b>${SYM[t.symbol] || t.symbol}</b> ${sideChip(t.side)} <span class="chip num">x${(t.leverage || 0).toFixed(2)}</span>
        ${t.mode !== "paper" ? modeChip(t.mode, t.mode === "signals" ? "איתות" : "חי") : ""}</div>
      <div style="text-align:left"><div class="num ${cls(pnl)}" style="font-weight:700">${usd(pnl, true)}</div><div class="num small ${cls(pnl)}">${pct(pp)}</div></div>
    </div>
    <div class="levels">
      <div class="lv e"><div class="l">כניסה</div><div class="v num">${fmtPx(t.entry_px, t.symbol)}</div></div>
      <div class="lv s"><div class="l">סטופ-לוס</div><div class="v num">${fmtPx(t.sl, t.symbol)}</div></div>
      <div class="lv t"><div class="l">טייק-פרופיט</div><div class="v num">${fmtPx(t.tp, t.symbol)}</div></div>
    </div>
    <div class="row small muted" style="margin-top:6px">
      <span>${open ? `מחיר נוכחי <span class="num">${fmtPx(t.price, t.symbol)}</span>` : `יציאה <span class="num">${fmtPx(t.exit_px, t.symbol)}</span> · ${REASON[t.reason] || esc(t.reason)}`}</span>
      <span>${when(open ? t.entry_ts : t.exit_ts)}</span>
    </div>
    <div class="tiny muted">כמות <span class="num">${t.qty}</span> · שווי <span class="num">${usd(t.notional)}</span> · עמלות+מימון <span class="num">${usd((t.fees || 0) + (t.funding || 0))}</span>${t.regime ? " · שוק: " + esc(t.regime) : ""}</div>
  </div>`;
}

// ---------- tabs ----------
function renderBots() {
  const ov = STATE.ov;
  const warn = ov.status.warn ? `<div class="card ins high small">⚠️ ${esc(ov.status.warn)}</div>` : "";
  return warn + `<h2 style="margin-top:4px">הבוטים שלי</h2>` + ov.bots.map(b => {
    const fill = Math.max(2, Math.min(100, 50 + b.pnl_pct * 2));
    const wr = b.closed ? Math.round(100 * b.wins / b.closed) + "%" : "—";
    return `<div class="card tap" onclick="openBot('${b.id}')">
      <div class="row"><div class="bot-name">${esc(b.name)}</div>${modeChip(b.mode, b.mode_he)}</div>
      <div class="small muted" style="margin-top:2px">${esc(b.strategy)} · ${b.tf} · BTC/ETH/SOL ${b.paused ? '· <span class="down">מושהה</span>' : ""}</div>
      <div style="margin-top:6px">${verdictChip(b.bench.verdict)}</div>
      <div class="kpis">
        <div class="kpi"><div class="l">יתרה</div><div class="v num">${usd(b.equity)}</div><div class="tiny muted num">מתוך ${usd(b.allocation)}</div></div>
        <div class="kpi"><div class="l">רווח/הפסד נטו</div><div class="v num ${cls(b.pnl)}">${usd(b.pnl, true)}</div><div class="tiny num ${cls(b.pnl)}">${pct(b.pnl_pct)}</div></div>
        <div class="kpi"><div class="l">עסקאות</div><div class="v num">${b.closed}</div><div class="tiny muted">הצלחה ${wr} · פתוחות ${b.open.length}</div></div>
      </div>
      <div class="bar"><i style="width:${fill}%;background:${b.pnl >= 0 ? "var(--green)" : "var(--red)"}"></i></div>
      ${b.open.map(t => tradeCard({ ...t, bot_name: "" }, false)).join("")}
    </div>`;
  }).join("") + `<p class="tiny muted">הרווח/הפסד מוצג נטו אחרי עמלות Bybit (טייקר 0.055%, מייקר 0.02%), החלקה ומימון. עסקאות פתוחות כוללות עמלת יציאה משוערת.</p>`;
}

async function openBot(id) {
  const b = await api(`/api/bots/${id}`);
  const be = b.bench, liveR = b.closed ? Math.round(100 * b.wins / b.closed) : null;
  const evals = Object.entries(b.last_eval || {}).map(([s, e]) => `<tr><td>${SYM[s]}</td><td>${e.status === "warmup" ? "מתחמם (חסרים נתונים)" : e.status === "missing_data" ? '<span class="down">נר חסר נתונים - דולג</span>' : e.sig > 0 ? '<span class="up">איתות לונג</span>' : e.sig < 0 ? '<span class="down">איתות שורט</span>' : "אין איתות"}</td><td class="num tiny">${esc(e.bar_time || "")}</td></tr>`).join("");
  const blocked = (b.settings.blocked_regimes || []).join(", ");
  $("#sheetBody").innerHTML = `<div class="grab"></div>
    <div class="row"><div class="bot-name">${esc(b.name)}</div>${modeChip(b.mode, b.mode_he)}</div>
    <p class="small muted">${esc(b.desc)}</p>
    <div class="kpis">
      <div class="kpi"><div class="l">יתרה</div><div class="v num">${usd(b.equity)}</div></div>
      <div class="kpi"><div class="l">נטו</div><div class="v num ${cls(b.pnl)}">${usd(b.pnl, true)}</div></div>
      <div class="kpi"><div class="l">סיכון לעסקה</div><div class="v num">${(100 * b.risk).toFixed(1)}%</div></div>
    </div>
    <h2>מצב הפעלה</h2>
    <div class="seg">${["paper", "signals", "live"].map(m => `<button class="${b.mode === m ? "on" : ""}" onclick="setMode('${b.id}','${m}')">${{ paper: "דמו", signals: "איתותים", live: "חי 🔒" }[m]}</button>`).join("")}</div>
    <div class="tiny muted">דמו = עסקאות וירטואליות בזמן אמת על מחירי Bybit. איתותים = רק איתותים ותוצאה תיאורטית. חי = כבוי (נדרשת הפעלה ב-.env).</div>
    <div class="btns"><button class="btn ${b.paused ? "green" : "ghost"}" onclick="pauseBot('${b.id}',${!b.paused})">${b.paused ? "▶ הפעל בוט" : "⏸ השהה בוט"}</button></div>
    <h2>בדיקות עבר (2026) מול זמן אמת</h2>
    <table><tr><th></th><th>בדיקות</th><th>בפועל</th></tr>
      <tr><td>Profit Factor</td><td class="num">${be.pf}</td><td class="num">${b.live_stats?.pf ?? "—"}</td></tr>
      <tr><td>אחוז הצלחה</td><td class="num">${be.win}%</td><td class="num">${liveR == null ? "—" : liveR + "%"}</td></tr>
      <tr><td>תשואה</td><td class="num">${pct(be.net)}</td><td class="num ${cls(b.pnl)}">${pct(b.pnl_pct)}</td></tr>
      <tr><td>ירידה מקסימלית</td><td class="num">${be.mdd}%</td><td class="num">${b.live_stats?.max_dd != null ? b.live_stats.max_dd + "%" : "—"}</td></tr>
      <tr><td>עסקאות בשנה</td><td class="num">~${be.trades_year}</td><td class="num">${b.closed}</td></tr></table>
    ${blocked ? `<p class="small">מצבי שוק חסומים (אושרו על ידך): <b>${esc(blocked)}</b></p>` : ""}
    <h2>הערכה אחרונה לכל מטבע</h2><table>${evals || '<tr><td class="muted">ממתין לנר הראשון</td></tr>'}</table>
    <h2>עסקאות פתוחות</h2>${b.open.length ? b.open.map(t => tradeCard(t, false) + `<button class="btn ghost" style="width:100%;margin-top:6px" onclick="closeTrade(${t.id})">סגור עסקה עכשיו</button>`).join("") : '<div class="muted small">אין</div>'}
    <h2>עסקאות סגורות</h2>${b.trades.length ? b.trades.slice(0, 30).map(t => tradeCard(t, false)).join("") : '<div class="muted small">עדיין אין</div>'}
    <h2>יומן</h2>${b.events.slice(0, 20).map(e => `<div class="tiny" style="padding:4px 0;border-bottom:1px solid var(--line)"><span class="muted">${when(e.ts)}</span> · ${esc(e.text)}</div>`).join("")}
    <div class="btns"><button class="btn ghost" onclick="closeSheet()">סגור</button></div>`;
  $("#sheet").classList.remove("hidden");
}
function closeSheet() { $("#sheet").classList.add("hidden"); }
async function setMode(id, mode) {
  if (mode === "live" && !confirm("מצב חי שולח פקודות אמיתיות ל-Bybit. להמשיך?")) return;
  try { await api(`/api/bots/${id}/mode`, { method: "POST", body: JSON.stringify({ mode }) }); toast("המצב עודכן"); openBot(id); refresh(); }
  catch (e) { toast(e.message); }
}
async function pauseBot(id, paused) { await api(`/api/bots/${id}/pause`, { method: "POST", body: JSON.stringify({ paused }) }); openBot(id); refresh(); }
async function closeTrade(tid) { if (!confirm("לסגור את העסקה במחיר השוק?")) return; await api(`/api/trades/${tid}/close`, { method: "POST" }); toast("העסקה נסגרה"); closeSheet(); refresh(); }

async function renderTrades() {
  const rows = await api(`/api/trades?status=${STATE.tradesSeg}`);
  return `<div class="seg"><button class="${STATE.tradesSeg === "open" ? "on" : ""}" onclick="STATE.tradesSeg='open';render()">פתוחות</button>
    <button class="${STATE.tradesSeg === "closed" ? "on" : ""}" onclick="STATE.tradesSeg='closed';render()">סגורות</button></div>` +
    (rows.length ? rows.map(t => tradeCard(t)).join("") : `<div class="empty">${STATE.tradesSeg === "open" ? "אין עסקאות פתוחות כרגע.<br>הבוטים פועלים על נרות 1h/4h ופותחים עסקה רק כשיש איתות." : "עדיין אין עסקאות סגורות."}</div>`);
}

// ---------- chart ----------
let chart, series, priceLines = [], linePrices = [];
async function renderChart() {
  const syms = Object.keys(SYM), tfs = ["15m", "1h", "4h", "1d"];
  return `<div class="chips">${syms.map(s => `<button class="${STATE.chartSym === s ? "on" : ""}" onclick="STATE.chartSym='${s}';STATE.chartTrade=null;render()">${SYM[s]}</button>`).join("")}
    <span style="width:8px"></span>${tfs.map(t => `<button class="${STATE.chartTf === t ? "on" : ""}" onclick="STATE.chartTf='${t}';render()">${t}</button>`).join("")}</div>
    <div id="chart"></div>
    <div class="legend"><span><i style="background:#3b82f6"></i>כניסה</span><span><i style="background:#ef4444"></i>סטופ-לוס</span><span><i style="background:#22c55e"></i>טייק-פרופיט</span><span>▲▼ כניסה/יציאה</span></div>
    <div id="chartTrades"></div>`;
}
async function drawChart() {
  const el = $("#chart"); if (!el) return;
  if (chart) { chart.remove(); chart = null; }
  chart = LightweightCharts.createChart(el, {
    layout: { background: { color: "#0f1520" }, textColor: "#8a98ad", fontSize: 11 },
    grid: { vertLines: { color: "#1a2333" }, horzLines: { color: "#1a2333" } },
    rightPriceScale: { borderColor: "#243044" }, timeScale: { borderColor: "#243044", timeVisible: true, rightOffset: 4 },
    crosshair: { mode: 1 }, handleScale: { axisPressedMouseMove: true }, localization: { locale: "he-IL" },
  });
  series = chart.addCandlestickSeries({ upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    autoscaleInfoProvider: orig => { const r = orig(); if (r && linePrices.length) {
      r.priceRange.minValue = Math.min(r.priceRange.minValue, ...linePrices); r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ...linePrices); } return r; },
    priceFormat: { type: "price", precision: dec(STATE.chartSym, (STATE.ov?.prices || {})[STATE.chartSym]), minMove: STATE.chartSym === "BTCUSDT" ? 0.1 : 0.01 } });
  await updateChart(true);
  new ResizeObserver(() => chart && chart.applyOptions({ width: el.clientWidth })).observe(el);
}
async function updateChart(first = false) {
  if (!series) return;
  const sym = STATE.chartSym;
  const [c, trades] = await Promise.all([api(`/api/candles?symbol=${sym}&tf=${STATE.chartTf}&limit=300`), api(`/api/trades?status=all&limit=300`)]);
  series.setData(c);
  priceLines.forEach(l => series.removePriceLine(l)); priceLines = []; linePrices = [];
  const mine = trades.filter(t => t.symbol === sym);
  const shown = STATE.chartTrade ? mine.filter(t => t.id === STATE.chartTrade) : mine.filter(t => t.status === "open");
  for (const t of shown) {
    const tag = (t.bot_name || "").split("·")[0].trim();
    priceLines.push(series.createPriceLine({ price: t.entry_px, color: "#3b82f6", lineWidth: 2, title: `כניסה ${tag}` }));
    priceLines.push(series.createPriceLine({ price: t.sl, color: "#ef4444", lineWidth: 2, lineStyle: 2, title: "SL" }));
    priceLines.push(series.createPriceLine({ price: t.tp, color: "#22c55e", lineWidth: 2, lineStyle: 2, title: "TP" }));
    linePrices.push(t.entry_px, t.sl, t.tp);
  }
  const t0 = c.length ? c[0].time : 0, tfSec = { "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 }[STATE.chartTf];
  const snap = ts => Math.floor(ts / tfSec) * tfSec;
  const markers = [];
  for (const t of mine.filter(t => t.entry_ts >= t0)) {
    markers.push({ time: snap(t.entry_ts), position: t.side > 0 ? "belowBar" : "aboveBar", color: "#3b82f6", shape: t.side > 0 ? "arrowUp" : "arrowDown", text: "כניסה" });
    if (t.exit_ts) markers.push({ time: snap(t.exit_ts), position: t.side > 0 ? "aboveBar" : "belowBar", color: t.pnl_net >= 0 ? "#22c55e" : "#ef4444", shape: "circle", text: `${t.pnl_net >= 0 ? "+" : ""}${t.pnl_net.toFixed(2)}$` });
  }
  markers.sort((a, b) => a.time - b.time); series.setMarkers(markers);
  if (first) chart.timeScale().fitContent();
  const list = $("#chartTrades");
  if (list) list.innerHTML = `<h2>עסקאות ב-${SYM[sym]}</h2>` + (mine.length ? mine.slice(0, 20).map(t => tradeCard(t)).join("") : '<div class="muted small">אין עדיין עסקאות במטבע הזה.</div>');
}
function showTradeOnChart(id, sym) { STATE.chartSym = sym; STATE.chartTrade = id; closeSheet(); setTab("chart"); }

// ---------- AI ----------
async function renderAI() {
  const a = await api("/api/ai"); STATE.ai = a;
  const regs = Object.entries(a.state.regimes || {}).map(([s, r]) => `<div class="kpi"><div class="l">${SYM[s]}</div><div class="v">${esc(r.regime)}</div>
    <div class="tiny muted">ADX ${r.adx ?? "—"} · תנודתיות ${r.atr_pct ?? "—"}% (${r.atr_rank ?? "—"}%) · ${r.trend_dir > 0 ? "מגמה עולה" : r.trend_dir < 0 ? "מגמה יורדת" : ""}</div></div>`).join("");
  const pend = a.proposals.filter(p => p.status === "pending");
  const props = a.proposals.map(p => `<div class="card prop ${p.status}">
      <div class="row"><b>${esc(p.title)}</b><span class="chip ${p.status === "pending" ? "warn" : p.status === "approved" ? "pass" : ""}">${{ pending: "ממתין לאישורך", approved: "אושר", rejected: "נדחה" }[p.status]}</span></div>
      <div class="tiny muted">${p.bot_id === "all" ? "כל הבוטים" : esc(a.names[p.bot_id] || p.bot_id)} · ${when(p.ts)}</div>
      <p class="small">${esc(p.text)}</p>
      ${p.status === "pending" ? `<div class="btns"><button class="btn green" onclick="decide(${p.id},'approve')">✓ אשר</button><button class="btn red" onclick="decide(${p.id},'reject')">✕ דחה</button></div>` : ""}
    </div>`).join("");
  const ins = a.insights.map(i => `<div class="card ins ${i.severity}"><div class="tiny muted">${i.bot_id === "all" ? "" : esc(a.names[i.bot_id] || "")} · ${when(i.ts)}</div><div class="small">${esc(i.text)}</div></div>`).join("");
  const stats = Object.entries(a.state.stats || {}).map(([id, s]) => {
    const rr = Object.entries(s.regime).map(([r, v]) => `<td class="num">${v.n ? `${v.n} · PF ${v.pf ?? "—"}` : "—"}</td>`).join("");
    return `<tr><td>${esc((a.names[id] || id).split("·")[0])}</td><td class="num">${s.all.n}</td>${rr}<td class="num">${s.max_dd}%</td></tr>`;
  }).join("");
  return `<h2 style="margin-top:4px">🧠 מצב השוק עכשיו</h2><div class="kpis">${regs}</div>
    <h2>הצעות לשינוי ${pend.length ? `(${pend.length} ממתינות)` : ""}</h2>${props || '<div class="muted small">אין הצעות כרגע. ה-AI מציע שינויים רק כשיש מספיק נתונים.</div>'}
    <h2>תובנות והתראות</h2>${ins || '<div class="muted small">—</div>'}
    <h2>ביצועים לפי מצב שוק</h2>
    <div class="card" style="overflow-x:auto"><table><tr><th>בוט</th><th>עסק׳</th><th>טרנד</th><th>דשדוש</th><th>תנודתי</th><th>DD</th></tr>${stats}</table></div>
    <button class="btn ghost" style="width:100%" onclick="aiRefresh()">🔄 הרץ ניתוח עכשיו</button>
    <p class="tiny muted">ה-AI המרכזי מנתח כל 10 דקות את כל העסקאות מכל הבוטים: מצב שוק (טרנד/דשדוש/תנודתי), שעות מסחר ומטבע, ומשווה לתוצאות בדיקות העבר במבחן סטטיסטי. הוא לעולם לא משנה דבר בעצמו — כל שינוי דורש את האישור שלך.</p>`;
}
async function decide(id, action) {
  if (!confirm(action === "approve" ? "לאשר ולהחיל את השינוי?" : "לדחות את ההצעה?")) return;
  try { await api(`/api/proposals/${id}/${action}`, { method: "POST" }); toast(action === "approve" ? "השינוי הוחל" : "ההצעה נדחתה"); render(); refresh(); }
  catch (e) { toast(e.message); }
}
async function aiRefresh() { toast("מנתח…"); await api("/api/ai/refresh", { method: "POST" }); render(); }

// ---------- more ----------
async function renderMore() {
  const ev = await api("/api/events?limit=60"); const ov = STATE.ov;
  return `<h2 style="margin-top:4px">חיבור</h2><div class="card small">
      <div>נתוני שוק: ${ov.status.ws === "connected" ? '<span class="up">מחובר לזרם החי של Bybit</span>' : esc(ov.status.ws)}</div>
      <div>מסחר חי: <span class="muted">${esc(ov.live.reason)}</span></div>
      <div class="tiny muted" style="margin-top:6px">כל הבוטים במצב דמו: עסקאות וירטואליות על מחירי Bybit אמיתיים, עם עמלות והחלקה כמו בבדיקות.</div></div>
    <h2>קוד גישה</h2><div class="card"><input id="tok" placeholder="APP_TOKEN (אם הוגדר)" value="${esc(token())}"><div class="btns"><button class="btn" onclick="saveTok()">שמור</button></div></div>
    <h2>יומן פעילות</h2><div class="card">${ev.map(e => `<div class="tiny" style="padding:5px 0;border-bottom:1px solid var(--line)"><span class="muted">${when(e.ts)} · ${esc(e.bot_id || "")}</span><br>${esc(e.text)}</div>`).join("") || '<div class="muted small">—</div>'}</div>`;
}
function saveTok() { try { localStorage.setItem("token", $("#tok").value.trim()); } catch {} toast("נשמר"); refresh(); }

// ---------- shell ----------
function setTab(t) { STATE.tab = t; document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === t)); render(); window.scrollTo(0, 0); }
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => setTab(b.dataset.tab));

async function render() {
  const v = $("#view");
  if (!STATE.ov) { v.innerHTML = '<div class="loading">טוען…</div>'; return; }
  try {
    const html = { bots: () => renderBots(), trades: renderTrades, chart: renderChart, ai: renderAI, more: renderMore }[STATE.tab];
    v.innerHTML = await html();
    if (STATE.tab === "chart") await drawChart(); else if (chart) { chart.remove(); chart = null; series = null; }
  } catch (e) { v.innerHTML = `<div class="loading">שגיאה: ${esc(e.message)}</div>`; }
}

async function refresh() {
  try {
    STATE.ov = await api("/api/overview");
    renderHeader(STATE.ov);
    if (STATE.tab === "bots" && $("#sheet").classList.contains("hidden")) $("#view").innerHTML = renderBots();
    if (STATE.tab === "chart") updateChart();
    if (STATE.tab === "trades" && STATE.tradesSeg === "open") $("#view").innerHTML = await renderTrades();
    if (!refresh.ai || Date.now() - refresh.ai > 30000) {
      refresh.ai = Date.now();
      const a = await api("/api/ai"); const n = a.proposals.filter(p => p.status === "pending").length;
      const bd = $("#aiBadge"); bd.textContent = n; bd.classList.toggle("hidden", !n);
    }
  } catch (e) {
    if (e.status === 503) $("#view").innerHTML = `<div class="loading">המערכת נטענת…<br><span class="small">${esc(e.body?.msg?.msg || "מוריד היסטוריית מחירים מ-Bybit")}</span></div>`;
    else if (e.status === 401) { STATE.tab = "more"; STATE.ov = STATE.ov || { status: {}, live: {}, bots: [], prices: {}, total: {} }; $("#view").innerHTML = `<div class="card"><p>נדרש קוד גישה</p><input id="tok" placeholder="APP_TOKEN"><div class="btns"><button class="btn" onclick="saveTok()">שמור</button></div></div>`; }
  }
}
refresh().then(render);
setInterval(refresh, 5000);
