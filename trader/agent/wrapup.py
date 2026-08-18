"""The daily wrap-up: what I decided, what happened, what changes.

The human's standing instruction (2026-08-15): strategy decisions belong
to the strategist — "make those decisions and then tell me about them in
the daily wrap up." This module is that report: one markdown file per
session, assembled from the ledgers at the close, written for a partner
who wasn't watching and shouldn't have to grep JSONL to find out what
their trader did with their money today.

Everything in it is recomputed from the day's records — same principle
as the scoreboard. The strategist's own post-mortem note (when the LLM
slot ran) is quoted verbatim; the numbers around it come from code, so
the narrative can't drift from the ledger.

    python -m agent.wrapup            # today's
    python -m agent.wrapup --date 2026-08-17

The live runner writes one automatically at every close, to
desk_state/wrapups/<date>.md — part of the desk's permanent record.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.metrics import (_read_jsonl, reasoning_stats, round_trips,
                           slippage_stats, trade_stats)

ET = ZoneInfo("America/New_York")


def _on(entries: list[dict], date: str) -> list[dict]:
    return [e for e in entries if str(e.get("ts", ""))[:10] == date]


def _fmt_money(x: float) -> str:
    return f"{'+' if x > 0 else ''}{x:,.2f}"


def _short_why(rationale: str) -> str:
    """First segment of a fill rationale — the trigger, without the score
    breakdown and slot details."""
    return str(rationale or "").split(" | ")[0].strip()


def _exit_reason(rationale: str) -> str:
    head = str(rationale or "").split(":")[0].strip()
    return head or "exit"


def _when(e: dict):
    try:
        return datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _narrative_section(day: list[dict], fills: list[dict]) -> list[str]:
    """The session as three acts. Events are bucketed by their position in
    the session's own wall-clock span, so this reads correctly for both a
    real 9:30-16:00 day and a compressed sim day."""
    stamped = [(t, e) for e in day if (t := _when(e)) is not None]
    if not stamped or not fills:
        return []
    t0 = min(t for t, _ in stamped)
    span = (max(t for t, _ in stamped) - t0).total_seconds() or 1.0
    acts: dict[str, list[dict]] = {"Morning": [], "Midday": [],
                                   "Afternoon & close": []}
    for t, e in sorted(stamped, key=lambda te: te[0]):
        frac = (t - t0).total_seconds() / span
        acts["Morning" if frac < 1 / 3 else
             "Midday" if frac < 2 / 3 else "Afternoon & close"].append(e)

    out = ["## The day, as it unfolded", ""]
    cap_noted = False
    for act, events in acts.items():
        buys = [e for e in events
                if e.get("kind") == "fill" and e.get("action") == "BUY"]
        sells = [e for e in events
                 if e.get("kind") == "fill" and e.get("action") != "BUY"]
        notes = []
        for e in events:
            kind = e.get("kind")
            if kind == "gut_check":
                notes.append(f"gut check: \"{(e.get('hunch') or {}).get('note', '')}\"")
            elif kind == "focus":
                notes.append(f"focus shifted: {e.get('reason', '')}")
            elif kind == "daily_stop":
                notes.append(f"**DAILY STOP** — down "
                             f"{float(e.get('drawdown') or 0) * 100:.2f}%, "
                             "flattening everything")
            elif (kind == "risk_reject" and not cap_noted
                  and str(e.get("reason", "")).startswith("daily trade cap")):
                notes.append("entry budget for the day ran out here — "
                             "exits only from this point")
                cap_noted = True
        if not buys and not sells and not notes:
            continue
        out.append(f"**{act}**")
        out += [f"- {n}" for n in notes[:3]]
        if buys:
            counts: dict[str, int] = {}
            for b in buys:
                counts[b.get("symbol", "?")] = counts.get(b.get("symbol", "?"), 0) + 1
            roll = ", ".join(f"{s}×{n}" if n > 1 else s for s, n in
                             sorted(counts.items(), key=lambda kv: -kv[1]))
            out.append(f"- {len(buys)} entries ({roll}); e.g.:")
            for b in buys[:2]:
                px = (b.get("order") or {}).get("fillPrice")
                at = f" @ {px:,.2f}" if px else ""
                out.append(f"  - `{str(b.get('ts', ''))[11:19]}` "
                           f"BUY {b.get('quantity')} {b.get('symbol')}{at} — "
                           f"{_short_why(b.get('rationale'))}")
        if sells:
            reasons: dict[str, int] = {}
            for s in sells:
                r = _exit_reason(s.get("rationale"))
                reasons[r] = reasons.get(r, 0) + 1
            roll = ", ".join(f"{n}× {r}" for r, n in
                             sorted(reasons.items(), key=lambda kv: -kv[1]))
            out.append(f"- {len(sells)} exits: {roll}")
        out.append("")
    return out


def _traded_section(fills: list[dict]) -> list[str]:
    """Per-symbol turnover. P&L is sold-minus-bought, which equals realized
    P&L because the desk ends every day flat."""
    if not fills:
        return []
    per: dict[str, dict] = {}
    for f in fills:
        s = per.setdefault(f.get("symbol", "?"),
                           {"buys": 0, "qty": 0.0, "bought": 0.0, "sold": 0.0})
        qty = float(f.get("quantity") or 0)
        px = float((f.get("order") or {}).get("fillPrice") or 0.0)
        if f.get("action") == "BUY":
            s["buys"] += 1
            s["qty"] += qty
            s["bought"] += qty * px
        else:
            s["sold"] += qty * px
    out = ["## What was traded", "",
           "| symbol | entries | shares | avg entry | bought $ | sold $ | P&L |",
           "|---|---|---|---|---|---|---|"]
    for sym, s in sorted(per.items(), key=lambda kv: -kv[1]["bought"]):
        avg = s["bought"] / s["qty"] if s["qty"] else 0.0
        out.append(f"| {sym} | {s['buys']} | {s['qty']:.0f} | {avg:,.2f} | "
                   f"{s['bought']:,.0f} | {s['sold']:,.0f} | "
                   f"{_fmt_money(s['sold'] - s['bought'])} |")
    out.append("")
    return out


def compose(data_dir: str, desk_dir: str, date: str | None = None) -> str:
    """Assemble the wrap-up markdown for one session date."""
    date = date or datetime.now(ET).date().isoformat()
    ledger = _read_jsonl(os.path.join(data_dir, "ledger.jsonl"))
    journal = _read_jsonl(os.path.join(desk_dir, "journal.jsonl"))
    day = _on(ledger, date)
    fills = [e for e in day if e.get("kind") == "fill"]
    trips = round_trips(fills)
    stats = trade_stats(trips)
    lines = [f"# Desk wrap-up — {date}", ""]

    # --- the headline ---
    trading_day = next((e for e in _on(journal, date)
                        if e.get("kind") == "trading_day"), None)
    if trading_day:
        pnl = float(trading_day.get("pnl") or 0.0)
        lines += [f"**P&L {_fmt_money(pnl)} "
                  f"({float(trading_day.get('pnl_pct') or 0) * 100:+.2f}%)** | "
                  f"{trading_day.get('trades', 0)} entries | "
                  f"flat at close: {trading_day.get('flat_at_close')} | "
                  f"day type: {trading_day.get('day_type') or '?'}"
                  + (" | **DAILY STOP HIT**"
                     if trading_day.get("daily_stop_hit") else ""), ""]
    elif not day:
        return "\n".join(lines + ["No session records for this date.", ""])

    # --- what I decided ---
    lines.append("## The plan I set")
    session_start = next((e for e in day if e.get("kind")
                          in ("session_start", "live_session_start")), None)
    plan_text = ((trading_day or {}).get("plan")
                 or (session_start or {}).get("plan") or "(no plan recorded)")
    lines += [f"> {plan_text}", ""]
    blackouts = [e for e in day if e.get("kind") == "event_blackout"]
    for b in blackouts:
        lines.append(f"- Event blackout honored: {b.get('reason')}")
    if blackouts:
        lines.append("")

    # --- the story, then the receipts ---
    lines += _narrative_section(day, fills)
    lines += _traded_section(fills)

    # --- what happened, graded by reasoning ---
    lines.append("## How it went")
    if trips:
        r = reasoning_stats(trips)
        lines.append(f"{stats['trades']} round trips, "
                     f"win rate {stats['win_rate']:.0%}, "
                     f"expectancy {_fmt_money(stats['expectancy_per_trade'])}/trade.")
        lines.append("")
        lines.append("| exit reason | n | win | P&L |")
        lines.append("|---|---|---|---|")
        for tag, b in r["by_exit"].items():
            win = "—" if b["win_rate"] is None else f"{b['win_rate']:.0%}"
            lines.append(f"| {tag} | {b['n']} | {win} | "
                         f"{_fmt_money(b['total_pnl'])} |")
        if r["by_entry_score"]:
            lines.append("")
            lines.append("| entry score | n | win | P&L |")
            lines.append("|---|---|---|---|")
            for band, b in r["by_entry_score"].items():
                win = "—" if b["win_rate"] is None else f"{b['win_rate']:.0%}"
                lines.append(f"| {band} | {b['n']} | {win} | "
                             f"{_fmt_money(b['total_pnl'])} |")
        slips = slippage_stats(fills)
        if slips.get("measured_fills"):
            lines.append("")
            lines.append(f"Spread paid: {_fmt_money(-abs(slips['total_vs_mid']))} "
                         f"across {slips['measured_fills']} fills "
                         f"({slips['avg_per_fill']:.2f} each).")
    else:
        lines.append("No round trips completed.")
    rejects = [e for e in day if e.get("kind") == "risk_reject"]
    if rejects:
        reasons: dict[str, int] = {}
        for e in rejects:
            key = " ".join(str(e.get("reason", "?")).split()[:3])
            reasons[key] = reasons.get(key, 0) + 1
        tops = ", ".join(f"{k} (x{v})" for k, v in
                         sorted(reasons.items(), key=lambda kv: -kv[1])[:4])
        lines.append(f"\nRisk gate rejections: {len(rejects)} — {tops}.")
    lines.append("")

    # --- guesses graded ---
    lines.append("## Guesses, graded")
    gut = [e for e in day if e.get("kind") == "gut_check"]
    if gut and trading_day:
        h = gut[-1].get("hunch") or {}
        actual = trading_day.get("day_type")
        called = h.get("suspected_day_type")
        verdict = ("right" if called == actual
                   else f"wrong (said {called}, was {actual})")
        lines.append(f"- Morning gut: \"{h.get('note', '')}\" — **{verdict}**.")
    grades = _read_jsonl(os.path.join(desk_dir, "rumor_grades.jsonl"))
    todays_grades = [g for g in grades if g.get("for_date") == date]
    for g in todays_grades:
        hit = g.get("direction_hit")
        word = "—" if hit is None else ("hit" if hit else "miss")
        lines.append(f"- Overnight rumor {g['symbol']} "
                     f"(x{g.get('mentions')}, sentiment {g.get('sentiment'):+d}): "
                     f"moved {g.get('day_move_pct')}% — {word}.")
    if not gut and not todays_grades:
        lines.append("- Nothing graded today.")
    lines.append("")

    # --- what I'm changing ---
    lines.append("## Decisions going forward")
    notes = [e for e in _on(journal, date)
             if e.get("kind") == "note" and "post-mortem" in (e.get("tags") or [])]
    if notes:
        lines.append(f"> {notes[-1].get('text', '')}")
    beliefs_today = [e for e in _on(journal, date)
                     if e.get("kind") == "belief_change"]
    for b in beliefs_today:
        lines.append(f"- Belief updated: **{b.get('key')}** — {b.get('reason', '')}")
    if not notes and not beliefs_today:
        lines.append("Holding course: no rule changes earned by today's "
                     "evidence. (Post-mortem slot output lands here when "
                     "the LLM harness runs live.)")
    lines.append("")
    return "\n".join(lines)


def write(data_dir: str, desk_dir: str, date: str | None = None) -> str:
    """Compose and persist to desk_state/wrapups/<date>.md; returns path."""
    date = date or datetime.now(ET).date().isoformat()
    text = compose(data_dir, desk_dir, date)
    out_dir = os.path.join(desk_dir, "wrapups")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--data-dir", default=os.path.join(base, "data"))
    ap.add_argument("--desk-dir", default=os.path.join(base, "desk_state"))
    ap.add_argument("--date", default=None)
    ap.add_argument("--save", action="store_true",
                    help="also write desk_state/wrapups/<date>.md")
    args = ap.parse_args()
    if args.save:
        print(f"saved: {write(args.data_dir, args.desk_dir, args.date)}")
    print(compose(args.data_dir, args.desk_dir, args.date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
