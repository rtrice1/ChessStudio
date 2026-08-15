"""Schwab Streamer: real-time level-one quotes over websocket.

Design:
- A background thread owns the websocket (login -> subscribe -> read
  loop), writing every tick into a QuoteCache. Reconnects with backoff
  forever; the thread never raises into the trading loop.
- `StreamingDataFeed` is what the desk consumes: quotes are served from
  the cache when fresh, and **fall back to REST polling when stale** —
  a dead stream degrades to exactly yesterday's behavior, never to
  silence. Candles/chains/account stay REST either way.
- First dependency on the box, by explicit decision: `websocket-client`
  (requirements.txt). Everything else remains stdlib; without the
  package installed, StreamingDataFeed simply runs in REST-only mode.

Field mappings below follow the published Streamer spec (LEVELONE_
EQUITIES / LEVELONE_OPTIONS numeric fields). The first live frame is
logged raw at startup so Monday's dry run verifies the mapping against
reality before anything depends on it.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

# Level-one field numbers (subset we subscribe to).
EQ_FIELDS = "0,1,2,3,8"          # key, bid, ask, last, volume
OPT_FIELDS = "0,2,3,4"           # key, bid, ask, last
EQ_MAP = {"1": "bid", "2": "ask", "3": "last", "8": "volume"}
OPT_MAP = {"2": "bid", "3": "ask", "4": "last"}

FRESH_SECONDS = 5.0              # cache older than this -> REST fallback


class QuoteCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._quotes: dict[str, dict] = {}

    def update(self, symbol: str, fields: dict) -> None:
        with self._lock:
            entry = self._quotes.setdefault(
                symbol, {"symbol": symbol, "bid": None, "ask": None,
                         "last": None})
            entry.update({k: v for k, v in fields.items() if v is not None})
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
            entry["_mono"] = time.monotonic()

    def fresh(self, symbol: str, max_age: float = FRESH_SECONDS) -> dict | None:
        with self._lock:
            entry = self._quotes.get(symbol)
            if not entry or entry.get("last") is None:
                return None
            if time.monotonic() - entry.get("_mono", 0) > max_age:
                return None
            return {k: v for k, v in entry.items() if k != "_mono"}

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            ages = [now - e.get("_mono", now) for e in self._quotes.values()]
        return {"symbols": len(ages),
                "oldest_age_s": round(max(ages), 1) if ages else None}


def parse_stream_message(raw: str, cache: QuoteCache) -> int:
    """Translate one streamer frame into cache updates. Returns the number
    of quote updates applied (0 for heartbeats/acks). Pure enough to test
    against canned frames."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    applied = 0
    for item in msg.get("data", []):
        service = item.get("service", "")
        field_map = (EQ_MAP if service == "LEVELONE_EQUITIES"
                     else OPT_MAP if service == "LEVELONE_OPTIONS" else None)
        if field_map is None:
            continue
        for content in item.get("content", []):
            symbol = str(content.get("key", "")).replace(" ", "")
            if not symbol:
                continue
            fields = {}
            for num, name in field_map.items():
                if num in content:
                    try:
                        fields[name] = float(content[num])
                    except (TypeError, ValueError):
                        continue
            if fields:
                cache.update(symbol, fields)
                applied += 1
    return applied


class SchwabStreamer:
    """Owns the websocket in a daemon thread. Requires `websocket-client`."""

    def __init__(self, schwab_client, symbols: list[str], cache: QuoteCache):
        self.client = schwab_client
        self.symbols = symbols
        self.cache = cache
        self.option_symbols: list[str] = []
        self._ws = None
        self._req_id = 0
        self._logged_first_frame = False
        self.connected = False

    # --- streamer bootstrap: preferences carry the socket url + ids ---

    def _streamer_info(self) -> dict:
        prefs = self.client._get("/trader/v1/userPreference")
        info = (prefs.get("streamerInfo") or [{}])[0]
        if not info.get("streamerSocketUrl"):
            raise RuntimeError("no streamerInfo in user preferences")
        return info

    def _request(self, service: str, command: str, params: dict,
                 info: dict) -> dict:
        self._req_id += 1
        return {"service": service, "command": command,
                "requestid": str(self._req_id),
                "SchwabClientCustomerId": info.get("schwabClientCustomerId"),
                "SchwabClientCorrelId": info.get("schwabClientCorrelId"),
                "parameters": params}

    def _connect_once(self) -> None:
        import websocket  # websocket-client
        info = self._streamer_info()
        self._ws = websocket.create_connection(
            info["streamerSocketUrl"], timeout=30)
        login = self._request("ADMIN", "LOGIN", {
            "Authorization": self.client.tokens.access_token(),
            "SchwabClientChannel": info.get("schwabClientChannel"),
            "SchwabClientFunctionId": info.get("schwabClientFunctionId"),
        }, info)
        self._ws.send(json.dumps({"requests": [login]}))
        self._ws.recv()  # login ack
        subs = [self._request("LEVELONE_EQUITIES", "SUBS", {
            "keys": ",".join(self.symbols), "fields": EQ_FIELDS}, info)]
        if self.option_symbols:
            subs.append(self._request("LEVELONE_OPTIONS", "SUBS", {
                "keys": ",".join(self.option_symbols),
                "fields": OPT_FIELDS}, info))
        self._ws.send(json.dumps({"requests": subs}))
        self.connected = True
        self._info = info

    def subscribe_options(self, occ_symbols: list[str]) -> None:
        """Called when contracts are opened so their quotes stream too."""
        from agent.schwab import occ_to_schwab
        new = [occ_to_schwab(s) for s in occ_symbols
               if occ_to_schwab(s) not in self.option_symbols]
        if not new:
            return
        self.option_symbols += new
        if self._ws is not None and self.connected:
            try:
                self._ws.send(json.dumps({"requests": [self._request(
                    "LEVELONE_OPTIONS", "ADD",
                    {"keys": ",".join(new), "fields": OPT_FIELDS},
                    self._info)]}))
            except Exception:
                self.connected = False  # reconnect loop re-subscribes all

    def _read_loop(self) -> None:
        while True:
            raw = self._ws.recv()
            if not raw:
                raise ConnectionError("empty frame")
            if not self._logged_first_frame:
                self._logged_first_frame = True
                print(f"[stream] first frame: {raw[:400]}")
            parse_stream_message(raw, self.cache)

    def run_forever(self) -> None:
        """The daemon thread body: connect, read, reconnect on anything."""
        backoff = 1.0
        while True:
            try:
                self._connect_once()
                print(f"[stream] connected; {len(self.symbols)} equities")
                backoff = 1.0
                self._read_loop()
            except Exception as exc:
                self.connected = False
                print(f"[stream] disconnected ({exc}); retry in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, daemon=True,
                                  name="schwab-streamer")
        thread.start()
        return thread


class StreamingDataFeed:
    """Drop-in for SchwabClient on the data side: streamed quotes when
    fresh, REST when not. Non-quote calls delegate straight through."""

    def __init__(self, schwab_client, symbols: list[str],
                 enable_stream: bool = True):
        self.client = schwab_client
        self.cache = QuoteCache()
        self.streamer = None
        self.stream_hits = 0
        self.rest_fallbacks = 0
        if enable_stream:
            try:
                import websocket  # noqa: F401 — availability probe
                self.streamer = SchwabStreamer(schwab_client, symbols, self.cache)
                self.streamer.start()
            except ImportError:
                print("[stream] websocket-client not installed — REST-only "
                      "(pip install -r requirements.txt)")

    def quotes(self, symbols: list[str]) -> dict:
        out, missing = {}, []
        for s in symbols:
            hit = self.cache.fresh(s)
            if hit is not None:
                out[s] = hit
                self.stream_hits += 1
            else:
                missing.append(s)
        if missing:
            self.rest_fallbacks += len(missing)
            out.update(self.client.quotes(missing))
        return out

    def subscribe_options(self, occ_symbols: list[str]) -> None:
        if self.streamer is not None:
            self.streamer.subscribe_options(occ_symbols)

    def stats(self) -> dict:
        return {"stream_hits": self.stream_hits,
                "rest_fallbacks": self.rest_fallbacks,
                "connected": bool(self.streamer and self.streamer.connected),
                **self.cache.stats()}

    def __getattr__(self, name):
        return getattr(self.client, name)
