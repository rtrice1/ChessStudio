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
