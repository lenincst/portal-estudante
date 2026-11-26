"""Real-time Ulife notifications: SignalR push as trigger + periodic polling as safety net.

Background: the portal keeps an ASP.NET SignalR 2.1.1 WebSocket to
signalr.ulife.com.br and receives `iLangHub.notificationCountChanged [count]` when something arrives.
This module:
- connects to the same hub using the session cookies (classic protocol: negotiate → connect → start);
- NEVER invokes hub methods (no `StartOnline`, so no "online" presence is announced to anyone);
- on a count change (or every `poll_interval` seconds) fetches `notifications-summarized` and emits only
  notifications not seen before (de-duplicated by Id, ids persisted locally — no content stored).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from . import config
from .client import PortalClient
from .errors import PortalError

log = logging.getLogger(__name__)

SIGNALR_BASE = "https://signalr.ulife.com.br/signalr"
MIN_POLL_INTERVAL = 300.0  # seconds; safety polling floor (push events still arrive in real time)
HUBS = [{"name": "ilanghub"}]


def parse_signalr_frame(raw: str) -> list[tuple[str, str, list[Any]]]:
    """Return [(hub, method, args)] from a SignalR 2.x persistent-connection frame; ignores keep-alives/acks."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(msg, dict):
        return []
    out = []
    for m in msg.get("M") or []:
        if isinstance(m, dict) and "H" in m and "M" in m:
            out.append((str(m["H"]), str(m["M"]), list(m.get("A") or [])))
    return out


class SeenStore:
    """Ids of notifications already emitted (only ids, no content)."""

    def __init__(self, path=None) -> None:  # noqa: ANN001
        # Resolved at runtime (not import time) so config.DATA_DIR overrides are honored.
        self.path = path or (config.DATA_DIR / "notifications_seen.json")
        try:
            self.ids: set[str] = set(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self.ids = set()
        self.initialized = self.path.exists()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.ids)[-5000:]), encoding="utf-8")


class NotificationWatcher:
    def __init__(self, client: PortalClient, on_new: Callable[[dict[str, Any]], None], *, poll_interval: float = 600.0,
                 use_push: bool = True, emit_existing_on_first_run: bool = False) -> None:
        self.client = client
        self.on_new = on_new
        self.poll_interval = max(poll_interval, MIN_POLL_INTERVAL)  # fixed floor: never poll the portal more often
        self.use_push = use_push
        self.emit_existing = emit_existing_on_first_run
        self.seen = SeenStore()
        self._trigger = threading.Event()
        self._stop = threading.Event()
        self.last_count: int | None = None
        self.push_connected = False

    # -- fetching / diffing ------------------------------------------------------------------------------
    def check_now(self) -> list[dict[str, Any]]:
        summary = self.client.notifications_summary()
        items = summary.get("Notifications") or []
        self.last_count = summary.get("UnReadCount")
        new = [n for n in items if str(n.get("Id")) not in self.seen.ids]
        first_run = not self.seen.initialized
        for n in items:
            self.seen.ids.add(str(n.get("Id")))
        self.seen.save()
        self.seen.initialized = True
        if first_run and not self.emit_existing:
            log.info("First run: %d existing notifications marked as seen (not emitted)", len(items))
            return []
        for n in new:
            self.on_new(n)
        return new

    # -- SignalR listener ----------------------------------------------------------------------------------
    def _cookie_header(self) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in self.client.http.session.cookies if c["domain"].endswith("ulife.com.br"))

    async def _listen_once(self) -> None:
        import websockets

        conn_data = json.dumps(HUBS, separators=(",", ":"))
        nego = self.client.http.get_json(f"{SIGNALR_BASE}/negotiate", {"clientProtocol": "1.4", "connectionData": conn_data, "_": int(time.time() * 1000)})
        token = nego["ConnectionToken"]
        qs = urllib.parse.urlencode({"transport": "webSockets", "clientProtocol": "1.4", "connectionToken": token, "connectionData": conn_data, "tid": 1})
        url = f"wss://signalr.ulife.com.br/signalr/connect?{qs}"
        headers = {"Cookie": self._cookie_header(), "Origin": "https://student.ulife.com.br"}
        async with websockets.connect(url, additional_headers=headers, open_timeout=30, ping_interval=None) as ws:
            # SignalR requires /start after the transport is open (the browser does the same).
            await asyncio.to_thread(self.client.http.get_json, f"{SIGNALR_BASE}/start",
                                    {"transport": "webSockets", "clientProtocol": "1.4", "connectionToken": token, "connectionData": conn_data})
            self.push_connected = True
            log.info("SignalR connected (listening only; no hub methods invoked)")
            keepalive = float(nego.get("KeepAliveTimeout") or 20) * 2
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=keepalive)
                except asyncio.TimeoutError:
                    raise ConnectionError("SignalR keep-alive timeout") from None
                for hub, method, args in parse_signalr_frame(raw if isinstance(raw, str) else raw.decode()):
                    log.debug("SignalR %s.%s %s", hub, method, args)
                    if method == "notificationCountChanged":
                        self._trigger.set()

    def _push_thread(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                asyncio.run(self._listen_once())
                backoff = 5.0
            except Exception as exc:  # network/transport errors: reconnect with backoff, polling still runs
                self.push_connected = False
                log.warning("SignalR disconnected (%s: %s); reconnecting in %.0fs", type(exc).__name__, str(exc)[:120], backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300.0)

    # -- main loop -------------------------------------------------------------------------------------------
    def run(self, max_cycles: int | None = None) -> None:
        if self.use_push:
            threading.Thread(target=self._push_thread, name="ulife-signalr", daemon=True).start()
        cycles = 0
        while not self._stop.is_set():
            try:
                self.check_now()
            except PortalError as exc:
                log.error("Notification check failed: %s", exc)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._trigger.wait(self.poll_interval)
            self._trigger.clear()
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        self.client.http.persist()


def _print(n: dict[str, Any]) -> None:
    print(f"[{n.get('Date') or n.get('CreatedDate') or ''}] {n.get('Title')}: {(n.get('Description') or '')[:160]}", flush=True)


if __name__ == "__main__":  # python -m portal_estudante.notifications
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    watcher = NotificationWatcher(PortalClient(), _print)
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
