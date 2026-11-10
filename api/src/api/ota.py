"""Arduino OTA endpoint: the tracker answers like an ESP32 running ArduinoOTA, so PlatformIO's (and the Arduino IDE's)
network upload works without a script: upload_protocol = espota, upload_port = <tracker>, upload_flags = --auth=<token>.

The protocol (espota.py, Arduino-ESP32 3.3.1+):
1. UDP invitation to port 3232: "0 <host port> <size> <md5>".  We answer "AUTH <nonce>" (the upload token is the
   OTA password), the uploader answers "200 <cnonce> <response>" (PBKDF2-HMAC-SHA256), we answer "OK".
2. We connect back over TCP to <uploader>:<host port>; it sends the app image in 1 KB chunks and waits for an
   answer after each (anything without "OK").
3. It then waits up to 10 x 30 s for "OK". Meanwhile the image goes to capture as an app-only flash job (the
   partition table on the chip says where it goes) and we answer "OK" when that's done.
Failing so that espota notices (it calls an upload with any answer after the last chunk, even a reset, "may still be
successful", and exits 0): the last chunk's answer waits until capture accepted the job. Refused (not an app image,
MD5 mismatch, another flash running): the connection is reset mid-upload and espota fails at once. Accepted but the
flash fails: no answer at all, so espota gives up with "No response from device" (slow, but a failure).
Only the app goes over OTA (no bootloader or partition table); filesystem images are refused.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import socket
import struct
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("api.ota")

FLASH, SPIFFS, AUTH = 0, 100, 200
OTA_PORT = 3232
CHUNK = 1024
CONNECT_TIMEOUT_S = 8.0  # the uploader accepts within 10 s
INVITE_TTL_S = 30.0
MAX_SIZE = 16 * 2**20
POLL_S = 0.5
RESULT_TIMEOUT_S = 290.0  # the uploader waits 10 x 30 s for the result
HOLD_S = 330.0  # a failed flash: stay silent until the uploader gives up and closes


def auth_response(password: str, nonce: str, cnonce: str) -> str:
    """What the uploader must send for this nonce (Arduino-ESP32 3.3.1+: SHA256 of the password, PBKDF2)."""
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    key = hashlib.pbkdf2_hmac("sha256", password_hash.encode(), f"{nonce}:{cnonce}".encode(), 10000).hex()
    return hashlib.sha256(f"{key}:{nonce}:{cnonce}".encode()).hexdigest()


class Invitation:
    def __init__(self, ip: str, port: int, size: int, md5: str):
        self.ip, self.port, self.size, self.md5 = ip, port, size, md5
        self.nonce = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        self.at = time.monotonic()


class OtaEndpoint(asyncio.DatagramProtocol):
    """get_token() -> the upload token (None: OTA refused); submit(image, ip) -> a job id, None if refused;
    result(job id) -> True once flashed."""

    def __init__(self, get_token: Callable[[], Awaitable[str | None]],
                 submit: Callable[[bytes, str], Awaitable[int | None]], result: Callable[[int], Awaitable[bool]]):
        self.get_token = get_token
        self.submit = submit
        self.result = result
        self.transport: asyncio.DatagramTransport | None = None
        self.invites: dict[str, Invitation] = {}
        self.busy = False
        self.tasks: set[asyncio.Task] = set()

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._spawn(self._handle(data, addr))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _reply(self, text: str, addr) -> None:
        if self.transport:
            self.transport.sendto(text.encode(), addr)

    async def _handle(self, data: bytes, addr) -> None:
        try:
            parts = data.decode(errors="replace").split()
            cmd = int(parts[0])
        except (ValueError, IndexError):
            return
        ip = addr[0]
        if cmd == AUTH and len(parts) == 3:
            inv = self.invites.pop(ip, None)
            token = await self.get_token()
            if inv is None or time.monotonic() - inv.at > INVITE_TTL_S or not token:
                self._reply("Authentication Failed", addr)
                return
            if not secrets.compare_digest(parts[2], auth_response(token, inv.nonce, parts[1])):
                log.warning("OTA upload from %s: wrong password (the tracker's upload token)", ip)
                self._reply("Authentication Failed", addr)
                return
            if self.busy:
                self._reply("ERR another upload is running", addr)
                return
            self.busy = True
            self._reply("OK", addr)
            self._spawn(self._receive(inv))
        elif cmd in (FLASH, SPIFFS) and len(parts) == 4:
            if cmd == SPIFFS:
                self._reply("ERR only firmware, no filesystem images", addr)
                return
            try:
                port, size = int(parts[1]), int(parts[2])
            except ValueError:
                return
            if not 0 < size <= MAX_SIZE or not 0 < port < 65536:
                self._reply("ERR bad size", addr)
                return
            if not await self.get_token():
                self._reply("ERR no upload token on this tracker", addr)
                return
            inv = Invitation(ip, port, size, parts[3].lower())
            self.invites[ip] = inv
            self._reply(f"AUTH {inv.nonce}", addr)

    async def _receive(self, inv: Invitation) -> None:
        writer = None
        state = "reset"  # how the connection ends: reset (mid-upload: espota fails), ok, or silent
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(inv.ip, inv.port), CONNECT_TIMEOUT_S)
            data = bytearray()
            last = 0
            while len(data) < inv.size:
                last = min(CHUNK, inv.size - len(data))
                data += await asyncio.wait_for(reader.readexactly(last), 15)
                if len(data) < inv.size:
                    writer.write(str(last).encode())  # the uploader waits for an answer after each chunk
                    await writer.drain()
            if hashlib.md5(data).hexdigest() != inv.md5:
                log.warning("OTA upload from %s: MD5 mismatch", inv.ip)
                return
            job_id = await self.submit(bytes(data), inv.ip)
            if job_id is None:
                return  # refused: reset while the uploader waits for the last chunk's answer
            writer.write(str(last).encode())
            await writer.drain()
            log.info("OTA upload from %s: %d bytes, flash job %d", inv.ip, len(data), job_id)
            state = "silent"
            if await asyncio.wait_for(self.result(job_id), RESULT_TIMEOUT_S):
                writer.write(b"OK")
                await writer.drain()
                state = "ok"
            else:
                await asyncio.wait_for(reader.read(), HOLD_S)  # until the uploader gives up and closes
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            log.warning("OTA upload from %s: %s", inv.ip, e or type(e).__name__)
        except Exception:
            log.exception("OTA upload from %s failed", inv.ip)
        finally:
            self.busy = False
            if writer is not None:
                if state == "reset":  # no FIN: the uploader's next read fails while it still sends chunks
                    sock = writer.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.close()


async def start(get_token, submit, result, host: str = "0.0.0.0", port: int = OTA_PORT):
    """Listen for invitations. Returns (transport, endpoint); transport.close() stops it."""
    loop = asyncio.get_running_loop()
    return await loop.create_datagram_endpoint(lambda: OtaEndpoint(get_token, submit, result), local_addr=(host, port))


def app_only_job(image: bytes, ip: str) -> dict:
    """The capture flash request for an OTA upload: the app image alone; capture finds its partition."""
    return {"family": "esp32", "app_only": True, "images": [{"offset": None, "data_b64": base64.b64encode(image).decode()}],
            "on_recording": "ask", "meta": {"source": "arduino-ota", "host": ip}}
