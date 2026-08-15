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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.metrics import scoreboard as compute_scoreboard

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
        self._score_cache: dict | None = None
        self._score_at = 0.0

    def assemble(self) -> dict:
        latest = _read_json(os.path.join(self.data_dir, "latest.json")) or {}
        account = latest.get("account") or {}
        equity = account.get("equity")
        quotes = latest.get("quotes") or {}
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
        fills_today = [e for e in ledger if e.get("kind") == "fill"
                       and e.get("ts", "")[:10] == ts[:10]]
        return {
            "ts": time.time(),
            "account": account,
            "alerts": (latest.get("alerts") or [])[-8:],
            "positions": [p for p in account.get("positions", [])
                          if p.get("quantity")],
            "equity_series": self.equity_series,
            "price_series": self.price_series,
            "quotes": quotes,
            "indicators": latest.get("indicators") or {},
            "news_summary": ((latest.get("news") or {}).get("summary") or {}),
            "events": list(reversed(events)),
            "plan": _read_json(os.path.join(self.data_dir, "day_plan.json")),
            "halted": os.path.exists(os.path.join(self.data_dir, "HALT")),
            "journal": list(reversed(journal[-5:])),
            "daily_pnl": [{"date": d.get("ts", "")[:10],
                           "pnl_pct": d.get("pnl_pct")}
                          for d in journal if d.get("kind") == "trading_day"
                          and d.get("pnl_pct") is not None][-20:],
            "hunch": (gut_checks[-1].get("hunch") if gut_checks else None),
            "trades_today": len(fills_today),
            "beliefs": {k: v.get("value") if isinstance(v, dict) else v
                        for k, v in beliefs.items()},
            "scoreboard": self._score_cache,
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
  <div class="panel wide"><h2>Watchlist (intraday last)</h2><div class="minis" id="minis"></div></div>
  <div class="panel"><h2>Daily P&amp;L (%)</h2><svg id="dailypnl" width="100%" height="120" viewBox="0 0 600 120"></svg></div>
  <div class="panel"><h2>Positions</h2><table id="positions"></table></div>
  <div class="panel"><h2>Live feed</h2><div class="feed" id="feed"></div></div>
  <div class="panel"><h2>Day plan</h2><div id="plan" style="color:var(--ink2);font-size:13px">—</div></div>
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
  const box=$("minis"),ps=s.price_series||{};
  box.innerHTML=Object.keys(ps).sort().map(sym=>{
    const series=ps[sym];if(!series||series.length<2)return "";
    const ind=(s.indicators||{})[sym]||{};
    const last=series[series.length-1][1],open=ind.day_open||series[0][1];
    const chg=open?(last-open)/open*100:0;
    const g=linePath(series,150,44,4,4);
    return `<div class="mini"><div class="h"><b style="color:var(--ink)">${sym}</b>`+
      `<span class="${cls(chg)}">${sign(chg)}${chg.toFixed(2)}%</span></div>`+
      `<svg width="100%" height="44" viewBox="0 0 150 44" preserveAspectRatio="none">`+
      `<path d="${g.d}" fill="none" stroke="#58a6ff" stroke-width="1.5"/></svg></div>`;
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
  const alertsBy={};(s.alerts||[]).forEach(a=>{(alertsBy[a.symbol]=alertsBy[a.symbol]||[]).push(a.kind)});
  const rows=Object.keys(ind).sort().map(sym=>{
    const i=ind[sym],quote=q[sym]||{},last=quote.last;
    const chg=i.day_open&&last?(last-i.day_open)/i.day_open*100:null;
    const vsV=(i.vwap&&last)?(last>=i.vwap?'<span class="pos">above</span>':'<span class="neg">below</span>'):"—";
    const rangePos=(i.range_low!=null&&i.range_high!=null&&last!=null)
      ?bullet(last,i.range_low,i.range_high,[]):"—";
    const n=news[sym]||{};
    const senti=(n.wire_sentiment!=null)?`${sign(n.wire_sentiment)}${n.wire_sentiment}/${sign(n.board_sentiment)}${n.board_sentiment}`:"—";
    const badges=(alertsBy[sym]||[]).map(k=>`<span class="badge">${k}</span>`).join("");
    return `<tr><td style="color:var(--ink)">${sym}</td><td class="num">${fmt(last)}</td>`+
      `<td class="num ${cls(chg)}">${chg==null?"—":sign(chg)+chg.toFixed(2)+"%"}</td>`+
      `<td>${bullet(i.rsi14,0,100,[30,70])}</td><td>${vsV}</td><td>${rangePos}</td>`+
      `<td class="num">${senti}</td><td>${badges}</td></tr>`;
  }).join("");
  $("signals").innerHTML=`<tr><th>sym</th><th>last</th><th>day</th><th>rsi (30/70)</th>`+
    `<th>vwap</th><th>range pos</th><th>wire/board</th><th>alerts</th></tr>${rows}`;
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
    (h&&h.suspected_day_type?`<span class="chip">gut: ${h.suspected_day_type} (${h.based_on} days)</span>`:"")+
    `<span class="chip">instrument: ${(p&&p.instrument)||"shares"}</span>`+
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
    `stops ${p.stop_atr}/${p.target_atr} ATR — ${(p.rationale||"").slice(0,300)}`
    :"default mechanical plan (benchmark)";
}
function score(s){
  const sb=s.scoreboard;if(!sb){$("score").innerHTML="";return}
  const t=sb.per_trade||{},d=sb.per_day||{},h=sb.hunch_calibration||{};
  const rows=[["trades",t.trades],["win rate",t.win_rate!=null?(t.win_rate*100).toFixed(1)+"%":"—"],
    ["expectancy/trade",t.expectancy_per_trade!=null?`<span class="${cls(t.expectancy_per_trade)}">${sign(t.expectancy_per_trade)}${fmt(t.expectancy_per_trade)}</span>`:"—"],
    ["profit factor",t.profit_factor??"—"],["days",d.days],["max drawdown",d.max_drawdown!=null?(d.max_drawdown*100).toFixed(2)+"%":"—"],
    ["flat-at-close",d.flat_at_close_rate!=null?(d.flat_at_close_rate*100).toFixed(0)+"%":"—"],
    ["hunch accuracy",h.graded?`${(h.accuracy*100).toFixed(0)}% of ${h.graded}`:"—"]];
  $("score").innerHTML=rows.map(([k,v])=>`<tr><td>${k}</td><td class="num">${v??"—"}</td></tr>`).join("");
}
function beliefs(s){
  $("beliefs").innerHTML=Object.entries(s.beliefs||{}).map(([k,v])=>`<div>• <b>${k}</b>: ${String(v).slice(0,120)}</div>`).join("")||"—";
}
const es=new EventSource("/events");
es.onmessage=m=>{lastMsg=Date.now();const s=JSON.parse(m.data);
  $("halt").style.display=s.halted?"block":"none";
  tiles(s);chips(s);spark(s.equity_series);minis(s);signals(s);meters(s);
  dailyPnl(s);positions(s);feed(s);plan(s);score(s);beliefs(s);};
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
