"""Overnight rumor scanner with a next-day backtrace.

The night before a session, scan public message boards (subreddits via
Reddit's public JSON listings; the mock's boards in sim) for ticker
chatter, and log the *aggregate* per-ticker picture — mention count,
summed sentiment, a few sample headlines — timestamped, so the causality
rule holds: rumors are only ever analyzed against moves that happened
AFTER the scan. No usernames, no per-author anything: we grade the
crowd's noise, not people.

Then the honest part, same pattern as the gut: after each session,
`grade` looks up what every rumored ticker actually did and writes the
verdict next to the rumor. `calibration` aggregates those verdicts into
the only question that matters — do loud overnight rumors predict
direction (historically: barely), or just volatility (usually)? That
number, with its sample size, is what the strategist sees at 09:35, not
the rumors themselves.

    python -m agent.rumors scan     # night before (systemd timer)
    python -m agent.rumors grade    # after the close (systemd timer)
    python -m agent.rumors report   # the calibration table

Legality note, explicit: this reads public posts via public endpoints,
rate-limited and identified by User-Agent. It does not scrape private
data, does not track individuals, and we never post — a desk that talks
its own book on the boards it trades from would be manipulation, so the
module has no write path at all.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .poller import sentiment_score

ET = ZoneInfo("America/New_York")
USER_AGENT = "trader-desk rumor scanner (personal research; read-only)"
DEFAULT_SUBREDDITS = ["wallstreetbets", "stocks", "options", "StockMarket"]
# Sentiment with fewer mentions than this is a shrug, not a rumor.
MIN_MENTIONS = 2
# A move smaller than this is noise; grading a rumor against it teaches
# nothing either way.
MOVE_FLOOR_PCT = 0.2


def extract_tickers(text: str, watch: list[str]) -> set[str]:
    """Cashtags plus bare watchlist symbols. Watch-list filtering is the
    disambiguator — we never guess whether 'A' meant Agilent."""
    found = set()
    for m in re.findall(r"\$([A-Za-z]{1,6})\b", text):
        if m.upper() in watch:
            found.add(m.upper())
    for sym in watch:
        if re.search(rf"\b{re.escape(sym)}\b", text):
            found.add(sym)
    return found


class RedditSource:
    """Subreddit listings, one request per subreddit, properly identified.

    Reddit's post-2023 API policy refuses unauthenticated JSON from
    scripts (verified 2026-08-17: 403 from www and api.reddit.com under
    any User-Agent; old.reddit 200s but serves the HTML homepage).
    Programmatic access wants a registered app: with REDDIT_CLIENT_ID and
    REDDIT_CLIENT_SECRET in the env, this uses the application-only OAuth
    grant against oauth.reddit.com (free tier, 100 req/min — a nightly
    4-sub scan is far inside polite use). Without credentials it still
    tries the public endpoint, which may work on other networks.

    Failures return [] — a dead source must never break the scan — but
    they are COUNTED in self.fetch_errors, so the scan record can tell
    "the boards were quiet" from "the boards were unreachable"."""

    def __init__(self, subreddits: list[str] | None = None, limit: int = 100):
        self.subreddits = subreddits or DEFAULT_SUBREDDITS
        self.limit = limit
        self.fetch_errors = 0
        self._token: str | None = None

    def _app_token(self) -> str | None:
        """Application-only OAuth token, or None to go unauthenticated."""
        cid = os.environ.get("REDDIT_CLIENT_ID", "")
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        if not cid or not secret:
            return None
        if self._token:
            return self._token
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token",
            data=b"grant_type=client_credentials",
            headers={"User-Agent": USER_AGENT,
                     "Authorization": f"Basic {auth}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._token = json.loads(resp.read().decode()).get("access_token")
        except Exception:
            self.fetch_errors += 1
            self._token = None
        return self._token

    def fetch(self, watch: list[str]) -> list[dict]:
        posts: list[dict] = []
        token = self._app_token()
        for sub in self.subreddits:
            if token:
                url = (f"https://oauth.reddit.com/r/{sub}/new"
                       f"?limit={self.limit}&raw_json=1")
                headers = {"User-Agent": USER_AGENT,
                           "Authorization": f"bearer {token}"}
            else:
                url = (f"https://www.reddit.com/r/{sub}/new.json"
                       f"?limit={self.limit}&raw_json=1")
                headers = {"User-Agent": USER_AGENT}
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = json.load(resp)
            except Exception:
                self.fetch_errors += 1
                continue
            for child in (payload.get("data") or {}).get("children") or []:
                d = child.get("data") or {}
                title = str(d.get("title") or "")
                body = str(d.get("selftext") or "")[:500]
                tickers = extract_tickers(f"{title} {body}", watch)
                if not tickers:
                    continue
                posts.append({
                    "ts": datetime.fromtimestamp(
                        float(d.get("created_utc") or time.time()),
                        tz=ZoneInfo("UTC")).isoformat(),
                    "source": f"r/{sub}",
                    "title": title[:200],
                    "tickers": sorted(tickers),
                })
            time.sleep(1.0)  # polite gap between subreddits
        return posts


class MockBoardSource:
    """The sim's message boards through the same interface, so the whole
    scan->grade->calibrate loop is testable without touching Reddit."""

    def __init__(self, client):
        self.client = client
        self.fetch_errors = 0

    def fetch(self, watch: list[str]) -> list[dict]:
        try:
            news = self.client.news(watch, limit=20)
        except Exception:
            self.fetch_errors += 1
            return []
        posts = []
        for sym, items in (news or {}).items():
            for item in items or []:
                if item.get("source") != "board":
                    continue
                posts.append({"ts": item.get("ts", ""),
                              "source": "mock_board",
                              "title": str(item.get("headline") or "")[:200],
                              "tickers": [sym]})
        return posts


def for_date(now: datetime) -> str:
    """The trading date this scan is ABOUT: scans before 09:30 ET belong
    to that day; anything later belongs to the next weekday. (Holidays are
    graded as no-data and skipped — no calendar dependency.)"""
    now = now.astimezone(ET)
    target = now.date()
    if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target.isoformat()


def aggregate(posts: list[dict]) -> dict:
    """Per-ticker crowd summary. Aggregate only — this is the level we
    grade at, and the level the strategist ever sees."""
    tickers: dict[str, dict] = {}
    for p in posts:
        for sym in p.get("tickers", []):
            t = tickers.setdefault(sym, {"mentions": 0, "sentiment": 0,
                                         "sample": []})
            t["mentions"] += 1
            t["sentiment"] += sentiment_score(p.get("title", ""))
            if len(t["sample"]) < 3:
                t["sample"].append(p.get("title", "")[:120])
    return {sym: t for sym, t in tickers.items()
            if t["mentions"] >= MIN_MENTIONS}


def scan(sources: list, watch: list[str], path: str,
         now: datetime | None = None) -> dict:
    """Run all sources, aggregate, append the timestamped record."""
    now = now or datetime.now(ET)
    posts: list[dict] = []
    for src in sources:
        posts.extend(src.fetch(watch))
    # A zero-post scan with fetch errors is a BLOCKED scan, not a quiet
    # night — the record must carry the difference (empty is information;
    # ambiguous is poison).
    fetch_errors = sum(getattr(src, "fetch_errors", 0) for src in sources)
    record = {
        "kind": "rumor_scan",
        "scanned_at": now.isoformat(),
        "for_date": for_date(now),
        "posts_seen": len(posts),
        "fetch_errors": fetch_errors,
        "tickers": aggregate(posts),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def day_move_pct(client, symbol: str, date: str) -> float | None:
    """Open-to-close move for `date` from price history, or None."""
    try:
        history = client.price_history(symbol, days=5, interval=5)
    except Exception:
        return None
    candles = [c for c in (history.get("candles") or [])
               if str(c.get("datetime", ""))[:10] == date]
    if not candles:
        return None
    day_open = float(candles[0].get("open") or 0.0)
    day_close = float(candles[-1].get("close") or 0.0)
    if day_open <= 0:
        return None
    return (day_close - day_open) / day_open * 100.0


def grade(rumors_path: str, grades_path: str, client,
          today: str | None = None) -> list[dict]:
    """Grade every ungraded past scan against what actually happened.

    A rumor 'hit' means the crowd's direction matched the day's direction
    (both past the noise floor). `abs_move_pct` is kept regardless of
    direction — the honest prior is that loud rumors predict volatility,
    not direction, and the calibration table should be able to show that.
    """
    today = today or datetime.now(ET).date().isoformat()
    graded_keys = {(g.get("for_date"), g.get("symbol"))
                   for g in _read_jsonl(grades_path)}
    out: list[dict] = []
    for scan_rec in _read_jsonl(rumors_path):
        fd = scan_rec.get("for_date", "")
        if not fd or fd > today:
            continue  # session not finished yet
        for sym, t in (scan_rec.get("tickers") or {}).items():
            if (fd, sym) in graded_keys:
                continue
            move = day_move_pct(client, sym, fd)
            sent = int(t.get("sentiment") or 0)
            hit = None
            if (move is not None and sent != 0
                    and abs(move) >= MOVE_FLOOR_PCT):
                hit = (sent > 0) == (move > 0)
            g = {"kind": "rumor_grade", "for_date": fd, "symbol": sym,
                 "mentions": int(t.get("mentions") or 0),
                 "sentiment": sent,
                 "day_move_pct": None if move is None else round(move, 3),
                 "abs_move_pct": None if move is None else round(abs(move), 3),
                 "direction_hit": hit,
                 "graded_at": datetime.now(ET).isoformat()}
            out.append(g)
            graded_keys.add((fd, sym))
    if out:
        os.makedirs(os.path.dirname(grades_path) or ".", exist_ok=True)
        with open(grades_path, "a", encoding="utf-8") as f:
            for g in out:
                f.write(json.dumps(g) + "\n")
    return out


LOUD_MENTIONS = 5


def calibration(grades: list[dict]) -> dict:
    """The rumor gut: per bucket (loud/quiet x positive/negative), how
    often direction was right and how big the day moved. Sample sizes are
    always shown — a 60% hit rate on n=5 is a coin, not an edge."""
    buckets: dict[str, dict] = {}
    for g in grades:
        sent = int(g.get("sentiment") or 0)
        if sent == 0:
            continue
        loud = "loud" if int(g.get("mentions") or 0) >= LOUD_MENTIONS else "quiet"
        key = f"{loud}_{'positive' if sent > 0 else 'negative'}"
        b = buckets.setdefault(key, {"n": 0, "graded": 0, "hits": 0,
                                     "abs_moves": []})
        b["n"] += 1
        if g.get("direction_hit") is not None:
            b["graded"] += 1
            b["hits"] += 1 if g["direction_hit"] else 0
        if g.get("abs_move_pct") is not None:
            b["abs_moves"].append(float(g["abs_move_pct"]))
    out = {}
    for key, b in buckets.items():
        out[key] = {
            "n": b["n"],
            "direction_hit_rate": (round(b["hits"] / b["graded"], 3)
                                   if b["graded"] else None),
            "graded": b["graded"],
            "avg_abs_move_pct": (round(sum(b["abs_moves"]) / len(b["abs_moves"]), 2)
                                 if b["abs_moves"] else None),
        }
    return out


def latest_scan(rumors_path: str, for_day: str | None = None) -> dict | None:
    """The most recent scan record (optionally: the one for a given day)."""
    scans = _read_jsonl(rumors_path)
    if for_day:
        scans = [s for s in scans if s.get("for_date") == for_day]
    return scans[-1] if scans else None


def context(desk_dir: str, for_day: str | None = None) -> dict | None:
    """What the strategist sees at 09:35: the latest overnight scan with
    its track record attached. None when there's no scan — the plan prompt
    simply doesn't mention rumors rather than inventing an empty section."""
    scan_rec = latest_scan(os.path.join(desk_dir, "rumors.jsonl"), for_day)
    if not scan_rec:
        return None
    cal = calibration(_read_jsonl(os.path.join(desk_dir, "rumor_grades.jsonl")))
    return {"scan": scan_rec, "calibration": cal}


DEFAULT_WATCH = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
                 "SPY", "QQQ", "TSLA", "JPM", "XOM"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["scan", "grade", "report"])
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--desk-dir", default=os.path.join(base, "desk_state"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8788")
    ap.add_argument("--symbols", default=",".join(DEFAULT_WATCH))
    ap.add_argument("--source", choices=["reddit", "mock"], default="reddit",
                    help="mock reads the sim's boards through the same path")
    args = ap.parse_args()
    watch = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rumors_path = os.path.join(args.desk_dir, "rumors.jsonl")
    grades_path = os.path.join(args.desk_dir, "rumor_grades.jsonl")

    from .client import BrokerClient
    client = BrokerClient(args.base_url)

    if args.cmd == "scan":
        src = (RedditSource() if args.source == "reddit"
               else MockBoardSource(client))
        rec = scan([src], watch, rumors_path)
        print(f"scan for {rec['for_date']}: {rec['posts_seen']} posts, "
              f"{len(rec['tickers'])} tickers with >= {MIN_MENTIONS} mentions")
        if rec.get("fetch_errors"):
            print(f"  WARNING: {rec['fetch_errors']} source fetch(es) FAILED — "
                  "this scan may be blocked, not quiet. Reddit needs "
                  "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET (see RedditSource).")
        for sym, t in sorted(rec["tickers"].items(),
                             key=lambda kv: -kv[1]["mentions"]):
            print(f"  {sym:6} x{t['mentions']:<3} sentiment {t['sentiment']:+d}")
    elif args.cmd == "grade":
        graded = grade(rumors_path, grades_path, client)
        print(f"graded {len(graded)} rumor(s)")
        for g in graded:
            print(f"  {g['for_date']} {g['symbol']:6} sent {g['sentiment']:+d} "
                  f"move {g['day_move_pct']}% hit={g['direction_hit']}")
    else:
        cal = calibration(_read_jsonl(grades_path))
        if not cal:
            print("no graded rumors yet")
        for key, b in sorted(cal.items()):
            rate = ("—" if b["direction_hit_rate"] is None
                    else f"{b['direction_hit_rate']:.0%}")
            move = ("—" if b["avg_abs_move_pct"] is None
                    else f"{b['avg_abs_move_pct']:.2f}%")
            print(f"  {key:16} n={b['n']:<4} direction hit {rate} "
                  f"(graded {b['graded']}), avg |move| {move}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
