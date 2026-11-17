"""A TCP proxy whose target can be switched atomically, for zero-downtime store swaps.

The sync process listens on the public SPARQL port and forwards each connection to the
server currently in service. A new release is served by a new server on another internal
port; once it answers, the proxy switches to it, waits for the connections still open on
the old server to finish, and only then is the old server stopped.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter

log = logging.getLogger(__name__)

UNAVAILABLE = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\nRetry-After: 30\r\n"
               b"Content-Length: 28\r\nConnection: close\r\n\r\nThe store is being loaded.\r\n")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(1 << 16):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


class SwitchProxy:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.target: tuple[str, int] | None = None
        self.active: Counter[tuple[str, int]] = Counter()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        ready = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            try:
                self._server = self._loop.run_until_complete(
                    asyncio.start_server(self._handle, self.host, self.port, reuse_address=True))
            except BaseException as err:  # noqa: BLE001 - reported to the caller
                errors.append(err)
                ready.set()
                return
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="switch-proxy", daemon=True)
        self._thread.start()
        ready.wait()
        if errors:
            raise errors[0]
        if self.port == 0 and self._server is not None:
            self.port = self._server.sockets[0].getsockname()[1]
        log.info("SPARQL proxy listening on %s:%s", self.host, self.port)

    def switch(self, target: tuple[str, int] | None) -> None:
        """New connections go to ``target`` (``None``: answer 503)."""
        self.target = target
        log.info("SPARQL proxy now forwards to %s", f"{target[0]}:{target[1]}" if target else "nothing (503)")

    def drain(self, target: tuple[str, int], timeout: float) -> bool:
        """Wait until no connection to ``target`` is open; False if ``timeout`` expired."""
        deadline = time.monotonic() + timeout
        while self.active[target] > 0:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.1)
        return True

    def stop(self) -> None:
        if self._loop is None:
            return

        async def close() -> None:
            if self._server is not None:
                self._server.close()
            self._loop.stop()

        asyncio.run_coroutine_threadsafe(close(), self._loop)
        if self._thread is not None:
            self._thread.join(5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target = self.target
        if target is None:
            writer.write(UNAVAILABLE)
            await self._close(writer)
            return
        self.active[target] += 1
        try:
            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(*target)
            except OSError:
                writer.write(UNAVAILABLE)
                return
            try:
                await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
            finally:
                await self._close(upstream_writer)
        finally:
            self.active[target] -= 1
            await self._close(writer)

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
