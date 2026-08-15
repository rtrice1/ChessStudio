"""The desk front end: one live page, streaming, no refresh.

`python -m agent.dashboard` serves a single dark terminal-style page on
:8899. The browser connects once; state streams in over Server-Sent
Events every 2 seconds (EventSource reconnects on its own). Everything
shown is read from the ledgers and desk state on disk — the dashboard is
a *view*, it owns no state and can be killed and restarted freely.

Panels: equity tiles + intraday equity line, positions, the live event
feed (fills, rejects, gut checks, focus moves, daily stops), the day
plan, hunch, beliefs, scoreboard, and the cost of judgment.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from agent.events import active_blackouts, load_events
from agent.metrics import day_trades_last_sessions
from agent.metrics import scoreboard as compute_scoreboard
from agent.rumors import context as rumors_context
from agent.strategist import score_entry

ET = ZoneInfo("America/New_York")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENT_KINDS = ("fill", "risk_reject", "gut_check", "focus", "daily_stop",
               "session_start", "session_end", "live_session_start")


def _read_jsonl_tail(path: str, n: int) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f.readlines()[-2000:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-n:]


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


class StateAssembler:
    """Builds the streamed state dict; keeps the intraday equity series in
    memory and caches the (ledger-scanning) scoreboard."""

    def __init__(self, data_dir: str, desk_dir: str):
        self.data_dir, self.desk_dir = data_dir, desk_dir
        self.equity_series: list[list] = []   # [iso_ts, equity]
        self.price_series: dict[str, list] = {}   # symbol -> [[hh:mm:ss, last]]
        # symbol -> [[hh:mm:ss, vwap, bb_upper, bb_lower]] — same ticks as
        # price_series, so the chart overlays share x positions.
        self.overlay_series: dict[str, list] = {}
        self._score_cache: dict | None = None
        self._score_at = 0.0

    def assemble(self) -> dict:
        latest = _read_json(os.path.join(self.data_dir, "latest.json")) or {}
        account = latest.get("account") or {}
        equity = account.get("equity")
        quotes = latest.get("quotes") or {}
        indicators = latest.get("indicators") or {}
        ts = latest.get("timestamp") or ""
        if equity is not None:
            if not self.equity_series or self.equity_series[-1][0] != ts:
                self.equity_series.append([ts, float(equity)])
                self.equity_series = self.equity_series[-500:]
                clock = ts[11:19] if len(ts) >= 19 else ts
                for sym, q in quotes.items():
                    if q.get("last") is not None:
                        series = self.price_series.setdefault(sym, [])
                        series.append([clock, float(q["last"])])
                        del series[:-240]
                        ind = indicators.get(sym) or {}
                        over = self.overlay_series.setdefault(sym, [])
                        over.append([clock, ind.get("vwap"),
                                     ind.get("bb_upper"), ind.get("bb_lower")])
                        del over[:-240]

        ledger = _read_jsonl_tail(os.path.join(self.data_dir, "ledger.jsonl"), 400)
        events = [e for e in ledger if e.get("kind") in EVENT_KINDS][-25:]

        if time.time() - self._score_at > 30:
            try:
                self._score_cache = compute_scoreboard(self.data_dir, self.desk_dir)
            except Exception:
                self._score_cache = None
            self._score_at = time.time()

        journal = _read_jsonl_tail(os.path.join(self.desk_dir, "journal.jsonl"), 40)
        beliefs = _read_json(os.path.join(self.desk_dir, "beliefs.json")) or {}
        gut_checks = [e for e in ledger if e.get("kind") == "gut_check"]
        hunch = gut_checks[-1].get("hunch") if gut_checks else None
        news_summary = (latest.get("news") or {}).get("summary") or {}
        # The same ranking the engine uses for the position budget — every
        # watched name gets a live score so the "best" is visible before
        # (and whether or not) it triggers.
        entry_scores = {}
        for sym, ind in indicators.items():
            try:
                sc, why = score_entry(ind, news_summary.get(sym), hunch)
                entry_scores[sym] = {"score": round(sc, 2), "why": why}
            except Exception:
                continue
        # The 40/day risk cap counts entries (BUYs) per SESSION — several
        # sim days can share one calendar date, so scope to the last
        # session_start rather than the date, mirroring ctx.trades_today.
        session_ts = ""
        for e in ledger:
            if e.get("kind") in ("session_start", "live_session_start"):
                session_ts = e.get("ts", "")
        fills_today = [e for e in ledger if e.get("kind") == "fill"
                       and e.get("action") == "BUY"
                       and (e.get("ts", "") >= session_ts if session_ts
                            else e.get("ts", "")[:10] == ts[:10])]
        return {
            "ts": time.time(),
            "account": account,
            "alerts": (latest.get("alerts") or [])[-8:],
            "positions": [p for p in account.get("positions", [])
                          if p.get("quantity")],
            "equity_series": self.equity_series,
            "price_series": self.price_series,
            "overlay_series": self.overlay_series,
            "quotes": quotes,
            "indicators": indicators,
            "news_summary": news_summary,
            "entry_scores": entry_scores,
            "events": list(reversed(events)),
            "plan": _read_json(os.path.join(self.data_dir, "day_plan.json")),
            "halted": os.path.exists(os.path.join(self.data_dir, "HALT")),
            "journal": list(reversed(journal[-5:])),
            "daily_pnl": [{"date": d.get("ts", "")[:10],
                           "pnl_pct": d.get("pnl_pct")}
                          for d in journal if d.get("kind") == "trading_day"
                          and d.get("pnl_pct") is not None][-20:],
            "hunch": hunch,
            "trades_today": len(fills_today),
            "beliefs": {k: v.get("value") if isinstance(v, dict) else v
                        for k, v in beliefs.items()},
            "scoreboard": self._score_cache,
            "rumors": rumors_context(self.desk_dir),
            "blackouts": [{"kind": b.kind, "reason": b.reason,
                           "flatten": b.flatten}
                          for b in active_blackouts(
                              load_events(self.data_dir), datetime.now(ET))],
            "short_interest": _read_json(
                os.path.join(self.data_dir, "short_interest.json")) or {},
            "filings": list(reversed(_read_jsonl_tail(
                os.path.join(self.data_dir, "edgar_filings.jsonl"), 8))),
            "day_trades_5d": day_trades_last_sessions(
                os.path.join(self.data_dir, "ledger.jsonl"), sessions=5),
        }


PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>The Desk</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#21262d;--ink:#e6edf3;
  --ink2:#9198a1;--muted:#656d76;--accent:#58a6ff;--good:#3fb950;--bad:#f85149}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font:14px/1.45 ui-monospace,Menlo,monospace;padding:14px}
h1{font-size:15px;letter-spacing:.06em;color:var(--ink2);margin-bottom:10px}
h1 .dot{color:var(--good)} h1 .dot.stale{color:var(--bad)}
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.panel h2{font-size:11px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:130px}
.tile .k{font-size:10px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.tile .v{font-size:22px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:3px 6px;text-align:left;border-bottom:1px solid var(--line);color:var(--ink2)}
th{color:var(--muted);font-weight:normal;font-size:11px;text-transform:uppercase;letter-spacing:.08em}
td.num{text-align:right}
.pos{color:var(--good)} .neg{color:var(--bad)}
.feed{max-height:340px;overflow-y:auto}
.feed .t{color:var(--muted);margin-right:6px}
.feed div{padding:2px 0;border-bottom:1px solid var(--line);font-size:12.5px;color:var(--ink2)}
.kind-fill{color:var(--ink)} .kind-daily_stop{color:var(--bad)}
svg text{fill:var(--ink2);font:10px ui-monospace,monospace}
.halt{background:var(--bad);color:#fff;padding:6px 10px;border-radius:6px;display:none;margin-bottom:10px}
small{color:var(--muted)}
.wide{grid-column:1/-1}
.minis{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.mini{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px}
.mini .h{display:flex;justify-content:space-between;font-size:11px;color:var(--ink2)}
.meter{margin:7px 0}.meter .lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--ink2)}
.meter .track{height:6px;background:var(--bg);border:1px solid var(--line);border-radius:3px;margin-top:3px}
.meter .fill{height:100%;border-radius:3px;background:var(--accent)}
.meter .fill.hot{background:var(--bad)}
.badge{display:inline-block;padding:0 5px;border:1px solid var(--line);border-radius:4px;font-size:10px;color:var(--ink2);margin-left:3px}
#tip{position:fixed;display:none;background:#000c;border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:11px;color:var(--ink);pointer-events:none;z-index:9}
.chip{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 9px;font-size:12px;color:var(--ink2);margin-right:6px}
</style></head><body>
<h1>THE DESK <span class="dot" id="dot">●</span> <small id="mode"></small></h1>
<div id="tip"></div>
<div class="halt" id="halt">⛔ HALT file present — all trading stopped</div>
<div class="tiles" id="tiles"></div>
<div style="margin-bottom:10px" id="chips"></div>
<div class="grid">
  <div class="panel"><h2>Equity (intraday)</h2><svg id="spark" width="100%" height="130" viewBox="0 0 600 130"></svg></div>
  <div class="panel"><h2>Risk limits — headroom</h2><div id="meters"></div></div>
  <div class="panel wide"><h2>Signal board</h2><table id="signals"></table></div>
  <div class="panel wide"><h2>Watchlist (intraday last)</h2>
    <div style="font-size:11px;color:var(--muted);margin-bottom:6px">
      <span style="color:#4a94e8">—</span> price&ensp;
      <span style="color:#bd8b1e">—</span> vwap&ensp;
      <span style="color:#4a94e8;opacity:.35">▮</span> bollinger(20,2)&ensp;
      <span style="color:#656d76">┄</span> opening range</div>
    <div class="minis" id="minis"></div></div>
  <div class="panel"><h2>Daily P&amp;L (%)</h2><svg id="dailypnl" width="100%" height="120" viewBox="0 0 600 120"></svg></div>
  <div class="panel"><h2>Positions</h2><table id="positions"></table></div>
  <div class="panel"><h2>Live feed</h2><div class="feed" id="feed"></div></div>
  <div class="panel"><h2>Day plan</h2><div id="plan" style="color:var(--ink2);font-size:13px">—</div></div>
  <div class="panel"><h2>Overnight rumors &amp; filings</h2><div id="rumors" style="font-size:12.5px;color:var(--ink2)">—</div></div>
  <div class="panel"><h2>Scoreboard</h2><table id="score"></table></div>
  <div class="panel"><h2>Desk beliefs</h2><div id="beliefs" style="font-size:12.5px;color:var(--ink2)"></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmt=(x,d=2)=>x==null?"—":Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const cls=x=>x>0?"pos":x<0?"neg":"";
const sign=x=>x>0?"+":"";
let lastMsg=Date.now();
function tiles(s){
  const a=s.account||{},eq=a.equity,series=s.equity_series||[];
  const open=series.length?series[0][1]:eq;
  const pnl=eq!=null&&open?eq-open:null;
  const sb=(s.scoreboard||{}),jc=sb.judgment_cost||{};
  $("tiles").innerHTML=[
    ["EQUITY",fmt(eq)],
    ["DAY P&L",pnl==null?"—":`<span class="${cls(pnl)}">${sign(pnl)}${fmt(pnl)}</span>`],
    ["CASH",fmt(a.cash)],
    ["POSITIONS",(s.positions||[]).length],
    ["JUDGMENT $",fmt(jc.est_cost_usd,4)],
  ].map(([k,v])=>`<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}
function linePath(series,W,H,top,bot){
  const ys=series.map(p=>p[1]),min=Math.min(...ys),max=Math.max(...ys),pad=(max-min)||1;
  const X=i=>i/(series.length-1)*(W-8)+4,Y=v=>(H-bot)-((v-min)/pad)*(H-bot-top);
  return {min,max,X,Y,
    d:series.map((p,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("")};
}
function hover(svg,series,geo){
  const tip=$("tip");
  svg.onmousemove=e=>{
    const r=svg.getBoundingClientRect();
    const frac=(e.clientX-r.left)/r.width;
    const i=Math.min(series.length-1,Math.max(0,Math.round(frac*(series.length-1))));
    const [t,v]=series[i];
    tip.style.display="block";tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY-10)+"px";
    tip.textContent=`${t}  ${fmt(v)}`;
    const x=geo.X(i).toFixed(1);
    let ch=svg.querySelector(".xh");
    if(!ch){ch=document.createElementNS("http://www.w3.org/2000/svg","line");
      ch.setAttribute("class","xh");ch.setAttribute("stroke","#656d76");
      ch.setAttribute("stroke-dasharray","3,3");svg.appendChild(ch);}
    ch.setAttribute("x1",x);ch.setAttribute("x2",x);
    ch.setAttribute("y1","6");ch.setAttribute("y2",svg.viewBox.baseVal.height-14);};
  svg.onmouseleave=()=>{tip.style.display="none";
    const ch=svg.querySelector(".xh");if(ch)ch.remove();};
}
function spark(series){
  const svg=$("spark");if(!series||series.length<2){svg.innerHTML="";return}
  const g=linePath(series,600,130,12,16);
  svg.innerHTML=`<line x1="4" y1="114" x2="596" y2="114" stroke="#21262d"/>`+
    `<path d="${g.d}" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linejoin="round"/>`+
    `<text x="4" y="10">${fmt(g.max)}</text><text x="4" y="128">${fmt(g.min)}</text>`;
  hover(svg,series,g);
}
function minis(s){
  const box=$("minis"),ps=s.price_series||{},os=s.overlay_series||{};
  box.innerHTML=Object.keys(ps).sort().map(sym=>{
    const series=ps[sym];if(!series||series.length<2)return "";
    const over=os[sym]||[];
    const ind=(s.indicators||{})[sym]||{};
    const last=series[series.length-1][1],open=ind.day_open||series[0][1];
    const chg=open?(last-open)/open*100:0;
    // One y-scale shared by price, vwap, bands, and range levels — the
    // overlays only mean something on the same axis as the price.
    const vals=series.map(p=>p[1]);
    over.forEach(o=>{for(let j=1;j<4;j++)if(o[j]!=null)vals.push(o[j])});
    if(ind.range_high!=null)vals.push(ind.range_high);
    if(ind.range_low!=null)vals.push(ind.range_low);
    const min=Math.min(...vals),max=Math.max(...vals),pad=(max-min)||1;
    const W=150,H=44;
    const X=i=>i/(series.length-1)*(W-8)+4,Y=v=>(H-4)-((v-min)/pad)*(H-8);
    const path=pts=>pts.map((p,k)=>`${k?"L":"M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    // overlays were appended on the same ticks as price; align by tail offset
    const off=series.length-over.length;
    const oPts=j=>over.map((o,i)=>o[j]==null?null:[X(Math.max(0,i+off)),Y(o[j])]).filter(Boolean);
    const vw=oPts(1),bu=oPts(2),bl=oPts(3);
    let band="";
    if(bu.length>1&&bl.length>1)
      band=`<path d="${path(bu)}${[...bl].reverse().map(p=>`L${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("")}Z" fill="#4a94e8" opacity="0.10"/>`;
    const rng=[["range_high",ind.range_high],["range_low",ind.range_low]]
      .filter(([,v])=>v!=null)
      .map(([,v])=>`<line x1="4" y1="${Y(v).toFixed(1)}" x2="${W-4}" y2="${Y(v).toFixed(1)}" stroke="#656d76" stroke-dasharray="3,3" stroke-width="0.8"/>`)
      .join("");
    return `<div class="mini"><div class="h"><b style="color:var(--ink)">${sym}</b>`+
      `<span class="${cls(chg)}">${sign(chg)}${chg.toFixed(2)}%</span></div>`+
      `<svg width="100%" height="44" viewBox="0 0 150 44" preserveAspectRatio="none">`+
      band+rng+
      (vw.length>1?`<path d="${path(vw)}" fill="none" stroke="#bd8b1e" stroke-width="1"/>`:"")+
      `<path d="${path(series.map((p,i)=>[X(i),Y(p[1])]))}" fill="none" stroke="#4a94e8" stroke-width="1.5"/></svg></div>`;
  }).join("");
}
function bullet(v,lo,hi,ticks){
  if(v==null)return "—";
  const W=90,x=t=>4+(Math.min(hi,Math.max(lo,t))-lo)/(hi-lo)*(W-8);
  return `<svg width="${W}" height="14" viewBox="0 0 ${W} 14">`+
    `<line x1="4" y1="7" x2="${W-4}" y2="7" stroke="#21262d" stroke-width="4" stroke-linecap="round"/>`+
    ticks.map(t=>`<line x1="${x(t)}" y1="2" x2="${x(t)}" y2="12" stroke="#656d76"/>`).join("")+
    `<circle cx="${x(v)}" cy="7" r="4" fill="#58a6ff"/></svg>`;
}
function signals(s){
  const ind=s.indicators||{},q=s.quotes||{},news=s.news_summary||{};
  const scores=s.entry_scores||{};
  const alertsBy={};(s.alerts||[]).forEach(a=>{(alertsBy[a.symbol]=alertsBy[a.symbol]||[]).push(a.kind)});
  // Ranked by entry score — the table IS the queue for the position budget.
  const syms=Object.keys(ind).sort((a,b)=>
    (((scores[b]||{}).score)??-99)-(((scores[a]||{}).score)??-99)||a.localeCompare(b));
  const rows=syms.map(sym=>{
    const i=ind[sym],quote=q[sym]||{},last=quote.last;
    const es=scores[sym]||{};
    const scoreCell=es.score==null?"—":
      `<span class="${cls(es.score)}" title="${(es.why||"").replace(/"/g,"'")}">${sign(es.score)}${Number(es.score).toFixed(2)}</span>`;
    const chg=i.day_open&&last?(last-i.day_open)/i.day_open*100:null;
    const vsV=(i.vwap&&last)?(last>=i.vwap?'<span class="pos">above</span>':'<span class="neg">below</span>'):"—";
    const rangePos=(i.range_low!=null&&i.range_high!=null&&last!=null)
      ?bullet(last,i.range_low,i.range_high,[]):"—";
    const n=news[sym]||{};
    const senti=(n.wire_sentiment!=null)?`${sign(n.wire_sentiment)}${n.wire_sentiment}/${sign(n.board_sentiment)}${n.board_sentiment}`:"—";
    const vel=n.board_velocity;
    const velCell=vel==null?"—":`<span class="${vel>=3?"neg":""}">${Number(vel).toFixed(1)}x</span>`;
    const si=((s.short_interest||{})[sym]||{}).short_pct_float;
    const siCell=si==null?"—":`<span class="${si>=15?"neg":""}">${Number(si).toFixed(0)}%</span>`;
    const badges=(alertsBy[sym]||[]).map(k=>`<span class="badge">${k}</span>`).join("");
    const mh=i.macd_hist,adx=i.adx,pb=i.bb_percent_b,rv=i.rel_volume;
    const PHASE={accelerating:['▲▲','var(--good)','vel + accel both up'],
      exhausting:['▲▼','#bd8b1e','up but decelerating — inflection risk'],
      basing:['▼▲','#4a94e8','down-move decelerating — turn forming'],
      falling:['▼▼','var(--bad)','vel + accel both down']};
    const ph=PHASE[i.momentum_phase];
    const phCell=ph?`<span style="color:${ph[1]}" title="${ph[2]} (roc_accel ${i.roc_accel==null?"?":Number(i.roc_accel).toFixed(2)})">${ph[0]}</span>`:"—";
    const macdCell=mh==null?"—":`<span class="${cls(mh)}">${sign(mh)}${Number(mh).toFixed(2)}</span>`;
    const adxCell=adx==null?"—":`${Number(adx).toFixed(0)}${adx>=25?" ▲":""}`;
    const pbCell=pb==null?"—":bullet(pb,0,1,[0.5]);
    const rvCell=rv==null?"—":`<span class="${rv>1.5?"pos":""}">${Number(rv).toFixed(1)}x</span>`;
    return `<tr><td style="color:var(--ink)">${sym}</td>`+
      `<td class="num">${scoreCell}</td><td class="num">${fmt(last)}</td>`+
      `<td class="num ${cls(chg)}">${chg==null?"—":sign(chg)+chg.toFixed(2)+"%"}</td>`+
      `<td>${bullet(i.rsi14,0,100,[30,70])}</td><td class="num">${macdCell}</td>`+
      `<td style="text-align:center">${phCell}</td>`+
      `<td class="num">${adxCell}</td><td>${pbCell}</td><td class="num">${rvCell}</td>`+
      `<td>${vsV}</td><td>${rangePos}</td>`+
      `<td class="num">${senti}</td><td class="num">${velCell}</td>`+
      `<td class="num">${siCell}</td><td>${badges}</td></tr>`;
  }).join("");
  $("signals").innerHTML=`<tr><th>sym</th><th>score</th><th>last</th><th>day</th><th>rsi (30/70)</th>`+
    `<th>macd-h</th><th>phase</th><th>adx</th><th>%b</th><th>rvol</th>`+
    `<th>vwap</th><th>range pos</th><th>wire/board</th><th>vel</th><th>si</th><th>alerts</th></tr>${rows}`;
}
function meters(s){
  const eq=(s.account||{}).equity||0,series=s.equity_series||[];
  const open=series.length?series[0][1]:eq;
  const dd=open?Math.max(0,(open-eq)/open):0;
  const optPrem=(s.positions||[]).filter(p=>/^[A-Z]{1,6}\\d{6}[CP]\\d{8}$/.test(p.symbol))
    .reduce((a,p)=>a+(p.marketValue||0),0);
  const gross=(s.positions||[]).reduce((a,p)=>a+(p.marketValue||0),0);
  const rows=[["trades today",s.trades_today||0,40,""],
    ["drawdown",(dd*100).toFixed(2)+"%",2,dd*100/2],
    ["gross exposure",eq?(gross/eq*100).toFixed(0)+"%":"0%",100,eq?gross/eq:0],
    ["option premium",eq?(optPrem/eq*100).toFixed(1)+"%":"0%",6,eq?optPrem/eq*100/6:0]];
  // The FINRA PDT budget only binds below $25k equity (margin accounts).
  if(eq&&eq<25000)rows.push(["day trades (5d, PDT)",s.day_trades_5d||0,3,""]);
  $("meters").innerHTML=rows.map(([k,v,cap,frac])=>{
    const f=typeof frac==="number"?Math.min(1,frac):Math.min(1,(v||0)/cap);
    return `<div class="meter"><div class="lbl"><span>${k}</span><span>${v} / cap ${cap}${typeof v==="string"&&v.includes("%")?"%":""}</span></div>`+
      `<div class="track"><div class="fill${f>0.8?" hot":""}" style="width:${(f*100).toFixed(0)}%"></div></div></div>`;
  }).join("");
}
function dailyPnl(s){
  const svg=$("dailypnl"),days=s.daily_pnl||[];
  if(!days.length){svg.innerHTML="";return}
  const vals=days.map(d=>d.pnl_pct*100);
  const ext=Math.max(0.1,...vals.map(Math.abs));
  const W=600,mid=58,bw=Math.min(34,(W-20)/days.length-2);
  svg.innerHTML=`<line x1="4" y1="${mid}" x2="${W-4}" y2="${mid}" stroke="#21262d"/>`+
    days.map((d,i)=>{
      const v=vals[i],h=Math.abs(v)/ext*46;
      const x=10+i*(bw+2),y=v>=0?mid-h:mid;
      return `<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(h,1)}" rx="2" `+
        `fill="${v>=0?"#3fb950":"#f85149"}"><title>${d.date}  ${sign(v)}${v.toFixed(2)}%</title></rect>`;
    }).join("")+
    `<text x="4" y="10">${sign(ext)}${ext.toFixed(1)}%</text><text x="4" y="116">-${ext.toFixed(1)}%</text>`;
}
function chips(s){
  const h=s.hunch,p=s.plan;
  $("chips").innerHTML=
    (s.blackouts||[]).map(b=>`<span class="chip" style="border-color:var(--bad);color:var(--bad)">⏸ ${b.reason}${b.flatten?" [flatten]":""}</span>`).join("")+
    (h&&h.suspected_day_type?`<span class="chip">gut: ${h.suspected_day_type} (${h.based_on} days)</span>`:"")+
    `<span class="chip">instrument: ${(p&&p.instrument)||"shares"}</span>`+
    `<span class="chip">book: ${(s.positions||[]).length}/${(p&&p.max_positions)||4} slots</span>`+
    `<span class="chip">trades: ${s.trades_today||0}/40</span>`;
}
function positions(s){
  const rows=(s.positions||[]).map(p=>`<tr><td>${p.symbol}</td><td class="num">${p.quantity}</td>`+
    `<td class="num">${fmt(p.averagePrice)}</td><td class="num">${fmt(p.marketValue)}</td>`+
    `<td class="num ${cls(p.unrealizedPnl)}">${sign(p.unrealizedPnl||0)}${fmt(p.unrealizedPnl)}</td></tr>`).join("");
  $("positions").innerHTML=rows?`<tr><th>sym</th><th>qty</th><th>avg</th><th>value</th><th>upl</th></tr>${rows}`
    :"<tr><td>flat</td></tr>";
}
function feed(s){
  $("feed").innerHTML=(s.events||[]).map(e=>{
    const t=(e.ts||"").slice(11,19);
    let txt=e.kind;
    if(e.kind==="fill")txt=`${e.action} ${e.quantity}x ${e.symbol} — ${(e.rationale||"").slice(0,80)}`;
    else if(e.kind==="risk_reject")txt=`REJECT ${e.symbol||""}: ${(e.reason||"").slice(0,70)}`;
    else if(e.kind==="gut_check")txt=`gut: ${(e.hunch&&e.hunch.note)||""}`;
    else if(e.kind==="focus")txt=`focus: width ${e.width} — ${(e.reason||"").slice(0,60)}`;
    else if(e.kind==="daily_stop")txt=`DAILY STOP — down ${(e.drawdown*100).toFixed(2)}%`;
    return `<div class="kind-${e.kind}"><span class="t">${t}</span>${txt}</div>`;}).join("");
}
function plan(s){
  const p=s.plan;
  $("plan").textContent=p?`[${p.instrument||"shares"}] risk ${p.per_trade_risk_pct}, `+
    `stops ${p.stop_atr}/${p.target_atr} ATR, `+
    `${p.max_positions||4} position slots — ${(p.rationale||"").slice(0,300)}`
    :"default mechanical plan (benchmark)";
}
function score(s){
  const sb=s.scoreboard;if(!sb){$("score").innerHTML="";return}
  const t=sb.per_trade||{},d=sb.per_day||{},h=sb.hunch_calibration||{},sl=sb.slippage||{};
  const rows=[["trades",t.trades],["win rate",t.win_rate!=null?(t.win_rate*100).toFixed(1)+"%":"—"],
    ["expectancy/trade",t.expectancy_per_trade!=null?`<span class="${cls(t.expectancy_per_trade)}">${sign(t.expectancy_per_trade)}${fmt(t.expectancy_per_trade)}</span>`:"—"],
    ["profit factor",t.profit_factor??"—"],["days",d.days],["max drawdown",d.max_drawdown!=null?(d.max_drawdown*100).toFixed(2)+"%":"—"],
    ["flat-at-close",d.flat_at_close_rate!=null?(d.flat_at_close_rate*100).toFixed(0)+"%":"—"],
    ["spread paid",sl.measured_fills?`<span class="neg">${fmt(sl.total_vs_mid)}</span> (${fmt(sl.avg_per_fill)}/fill)`:"—"],
    ["hunch accuracy",h.graded?`${(h.accuracy*100).toFixed(0)}% of ${h.graded}`:"—"]];
  $("score").innerHTML=rows.map(([k,v])=>`<tr><td>${k}</td><td class="num">${v??"—"}</td></tr>`).join("");
}
function rumors(s){
  const r=s.rumors,f=s.filings||[];
  let html="";
  if(r&&r.scan){
    const t=r.scan.tickers||{};
    const rows=Object.entries(t).sort((a,b)=>b[1].mentions-a[1].mentions).slice(0,6)
      .map(([sym,v])=>`<tr><td style="color:var(--ink)">${sym}</td>`+
        `<td class="num">x${v.mentions}</td>`+
        `<td class="num ${cls(v.sentiment)}">${sign(v.sentiment)}${v.sentiment}</td></tr>`).join("");
    html+=`<div style="margin-bottom:4px"><small>scan for ${r.scan.for_date} — ${r.scan.posts_seen} posts</small></div>`+
      (rows?`<table>${rows}</table>`:"<div>no tickers above the mention floor</div>");
    const cal=Object.entries(r.calibration||{});
    if(cal.length)
      html+=`<div style="margin-top:6px"><small>track record: `+
        cal.map(([k,v])=>`${k} ${v.direction_hit_rate==null?"—":(v.direction_hit_rate*100).toFixed(0)+"%"} of ${v.graded}`).join(" · ")+
        `</small></div>`;
    else html+=`<div style="margin-top:6px"><small>track record: nothing graded yet — rumors are unweighted noise until they earn a number</small></div>`;
  } else html+="no overnight scan yet";
  if(f.length)
    html+=`<div style="margin-top:8px;border-top:1px solid var(--line);padding-top:5px">`+
      f.map(x=>`<div><span style="color:var(--muted);margin-right:6px">${(x.updated||"").slice(5,16)}</span>${x.symbol||""} ${x.form||""} <small>${(x.company||"").slice(0,40)}</small></div>`).join("")+`</div>`;
  $("rumors").innerHTML=html;
}
function beliefs(s){
  $("beliefs").innerHTML=Object.entries(s.beliefs||{}).map(([k,v])=>`<div>• <b>${k}</b>: ${String(v).slice(0,120)}</div>`).join("")||"—";
}
// One broken panel must never blank the whole desk: each renders inside
// its own try/catch, and any failure lands visibly in the tab title.
const PANELS=[["tiles",tiles],["chips",chips],["spark",s=>spark(s.equity_series)],
  ["minis",minis],["signals",signals],["meters",meters],["dailyPnl",dailyPnl],
  ["positions",positions],["feed",feed],["plan",plan],["rumors",rumors],
  ["score",score],["beliefs",beliefs]];
function render(s){
  $("halt").style.display=s.halted?"block":"none";
  for(const [name,fn] of PANELS){
    try{fn(s)}catch(e){document.title="DESK "+name+" error: "+e.message}}}
const es=new EventSource("/events");
es.onmessage=m=>{lastMsg=Date.now();
  try{render(JSON.parse(m.data))}
  catch(e){document.title="DESK parse error: "+e.message}};
setInterval(()=>{$("dot").className="dot"+(Date.now()-lastMsg>8000?" stale":"")},2000);
</script></body></html>"""


def make_handler(assembler: StateAssembler, interval: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/state":
                body = json.dumps(assembler.assemble()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        payload = json.dumps(assembler.assemble())
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(interval)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            else:
                self.send_error(404)
    return Handler


def create_server(host: str = "0.0.0.0", port: int = 8899,
                  data_dir: str = None, desk_dir: str = None,
                  interval: float = 2.0) -> ThreadingHTTPServer:
    assembler = StateAssembler(data_dir or os.path.join(BASE, "data"),
                               desk_dir or os.path.join(BASE, "desk_state"))
    return ThreadingHTTPServer((host, port), make_handler(assembler, interval))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data-dir", default=os.path.join(BASE, "data"))
    ap.add_argument("--desk-dir", default=os.path.join(BASE, "desk_state"))
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()
    server = create_server(args.host, args.port, args.data_dir,
                           args.desk_dir, args.interval)
    print(f"desk dashboard: http://{args.host}:{args.port}/  (SSE, no refresh)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
