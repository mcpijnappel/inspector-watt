"""The Arduino OTA endpoint against an uploader that speaks like espota.py (Arduino-ESP32 3.3.1+)."""

import asyncio
import hashlib
import socket

from api import ota

TOKEN = "s3cret-token"


class Uploader:
    """espota.py's side, step by step: invitation, SHA256 challenge, TCP server, chunks, result."""

    def __init__(self, ota_port: int, image: bytes, password: str = TOKEN, command: int = ota.FLASH):
        self.ota = ("127.0.0.1", ota_port)
        self.image, self.password, self.command = image, password, command
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.server.settimeout(5)

    def udp(self, text: str) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(5)
        s.sendto(text.encode(), self.ota)
        try:
            return s.recv(69).decode()
        finally:
            s.close()

    def run(self) -> str:
        md5 = hashlib.md5(self.image).hexdigest()
        port = self.server.getsockname()[1]
        answer = self.udp(f"{self.command} {port} {len(self.image)} {md5}\n")
        if not answer.startswith("AUTH"):
            return answer
        nonce = answer.split()[1]
        cnonce = hashlib.sha256(f"firmware.bin{len(self.image)}{md5}127.0.0.1".encode()).hexdigest()
        answer = self.udp(f"{ota.AUTH} {cnonce} {ota.auth_response(self.password, nonce, cnonce)}\n")
        if answer != "OK":
            return answer
        conn, _ = self.server.accept()
        conn.settimeout(5)
        try:
            for i in range(0, len(self.image), 1024):
                conn.sendall(self.image[i:i + 1024])
                ack = conn.recv(10).decode()
                last = i + 1024 >= len(self.image)
                # espota: an OK in the last chunk's answer is success right away (the final OK can arrive in the
                # same read when the flash is quick); an OK in an earlier answer would end the upload too soon
                assert ack and (last or "OK" not in ack)
                if last and "OK" in ack:
                    return "result: OK"
            return "result: " + conn.recv(32).decode()
        except ConnectionResetError:
            return "reset"
        except socket.timeout:
            return "no answer"
        finally:
            conn.close()
            self.server.close()


def run(image: bytes, accept=True, flash_ok=True, token=TOKEN, **kw):
    flashed = []

    async def get_token():
        return token

    async def submit(data, ip):
        flashed.append(data)
        return 1 if accept else None

    async def result(job_id):
        return flash_ok

    async def main():
        transport, _ = await ota.start(get_token, submit, result, host="127.0.0.1", port=0)
        port = transport.get_extra_info("sockname")[1]
        try:
            return await asyncio.to_thread(Uploader(port, image, **kw).run)
        finally:
            transport.close()

    return asyncio.run(main()), flashed


IMAGE = bytes(range(256)) * 10 + b"tail"  # not a multiple of 1024


def test_upload_flashes_and_answers_ok():
    result, flashed = run(IMAGE)
    assert result == "result: OK" and flashed == [IMAGE]


def test_refused_upload_resets_before_the_last_chunk_is_answered():
    """espota fails at once only while it still sends chunks: a reset after the last chunk reads as success."""
    result, flashed = run(IMAGE, accept=False)
    assert result == "reset" and flashed == [IMAGE]


def test_failed_flash_gets_no_answer():
    """Accepted but the flash failed: silence, so espota ends with "No response from device" (exit 1)."""
    result, _ = run(IMAGE, flash_ok=False)
    assert result == "no answer"


def test_wrong_password_and_refusals():
    assert run(IMAGE, password="nope")[0] == "Authentication Failed"
    assert run(IMAGE, token=None)[0].startswith("ERR")
    assert run(IMAGE, command=ota.SPIFFS)[0].startswith("ERR")


def test_app_only_job():
    job = ota.app_only_job(b"\xe9abc", "10.0.0.5")
    assert job["app_only"] and job["images"][0]["offset"] is None and job["meta"]["source"] == "arduino-ota"
