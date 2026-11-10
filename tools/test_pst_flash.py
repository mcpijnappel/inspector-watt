"""Tests for pst_flash.py (standard library only, like the script): python -m unittest discover -s tools"""

import base64
import io
import json
import os
import struct
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pst_flash


def esp_image(chip_id=13, app=True, size=0x2000):
    """A minimal ESP image: header with the chip id at 12; an app also has esp_app_desc_t after the headers."""
    hdr = bytes([0xE9, 1, 2, 0x2F]) + struct.pack("<I", 0x42000000) + bytes([0xEE, 0, 0, 0]) + struct.pack("<H", chip_id)
    hdr += b"\0" * (24 - len(hdr)) + struct.pack("<II", 0x3C000020, 256)
    if app:
        hdr += struct.pack("<I", 0xABCD5432)
    return hdr.ljust(size, b"\x22")


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
        return path


class Readers(Files):
    def test_esp_idf_build_folder(self):
        self.write("bootloader/bootloader.bin", esp_image(app=False))
        self.write("partition_table/partition-table.bin", b"\xaa\x50" + b"\0" * 30)
        self.write("blink.bin", esp_image())
        self.write("flasher_args.json", json.dumps({
            "flash_settings": {"flash_mode": "dio", "flash_size": "4MB", "flash_freq": "80m"},
            "flash_files": {"0x10000": "blink.bin", "0x0": "bootloader/bootloader.bin",
                            "0x8000": "partition_table/partition-table.bin"},
            "extra_esptool_args": {"after": "hard_reset", "before": "default_reset", "stub": True, "chip": "esp32c6"}}))
        self.write("project_description.json", json.dumps({"project_name": "blink", "project_version": "1.0.2",
                                                           "target": "esp32c6", "project_path": self.dir}))
        u = pst_flash.from_sources([self.dir])
        self.assertEqual([off for off, _, _ in u.images], [0x0, 0x8000, 0x10000])  # sorted
        self.assertEqual(u.chip, "esp32c6")
        self.assertEqual(u.options, {"flash_mode": "dio", "flash_size": "4MB", "flash_freq": "80m"})
        self.assertEqual((u.meta["project"], u.meta["version"], u.meta["source"]), ("blink", "1.0.2", "esp-idf"))
        self.assertEqual(pst_flash.from_sources([os.path.join(self.dir, "flasher_args.json")]).chip, "esp32c6")

    def test_merged_image_is_split_along_its_partition_table(self):
        def entry(t, st, off, size, label):
            return b"\xaa\x50" + struct.pack("<BBII", t, st, off, size) + label.ljust(16, b"\0") + b"\0" * 4
        merged = bytearray(b"\xff" * 0x30000)
        merged[0:0x1800] = esp_image(chip_id=9, app=False, size=0x1800)  # bootloader: 1.5 sectors
        merged[0x8000:0x8080] = entry(1, 2, 0x9000, 0x5000, b"nvs") + entry(1, 0, 0xE000, 0x2000, b"otadata") + \
            entry(0, 0x10, 0x10000, 0x10000, b"app0") + entry(1, 0x82, 0x20000, 0x10000, b"spiffs")
        merged[0xE000:0xE020] = b"\x01" * 32  # boot_app0
        merged[0x10000:0x12000] = esp_image(chip_id=9, size=0x2000)
        path = self.write("out/blink.ino.merged.bin", bytes(merged))
        u = pst_flash.from_sources([os.path.dirname(path)])  # an arduino-cli --export-binaries folder
        # nvs and spiffs are erased in the merged image: left out, so the board keeps its settings
        self.assertEqual([(off, len(d)) for off, d, _ in u.images],
                         [(0x0, 0x2000), (0x8000, 0x1000), (0xE000, 0x1000), (0x10000, 0x2000)])
        self.assertEqual((u.chip, u.options, u.images[3][2]), ("esp32s3", {}, "blink.ino.merged.bin: app0"))
        no_table = self.write("plain.bin", esp_image(app=False, size=0x9000))
        self.assertEqual([off for off, _, _ in pst_flash.from_sources([no_table]).images], [0x0])

    def test_app_and_pairs(self):
        app = self.write("firmware.bin", esp_image(chip_id=5))
        u = pst_flash.from_sources([app])
        self.assertEqual(([off for off, _, _ in u.images], u.chip, u.options), ([None], "esp32c3", {"app_only": True}))
        boot = self.write("boot.bin", esp_image(chip_id=13, app=False))
        u = pst_flash.from_sources(["0x10000", app, "0x0", boot])
        self.assertEqual(([off for off, _, _ in u.images], u.chip), ([0x0, 0x10000], "esp32c6"))

    def test_bad_sources(self):
        junk = self.write("junk.bin", b"hello")
        for words in ([junk], [self.dir], ["0x0"], ["zero", junk], [os.path.join(self.dir, "missing.bin")]):
            with self.assertRaises(pst_flash.UploadError, msg=words):
                pst_flash.from_sources(words)

    def test_payload(self):
        u = pst_flash.Upload([(None, b"app", "a.bin")], "esp32c6", options={"app_only": True}, source="app image")
        p = u.payload("keep", "cycle", True)
        self.assertEqual(p["images"], [{"offset": None, "data_b64": base64.b64encode(b"app").decode()}])
        self.assertEqual((p["family"], p["chip"], p["app_only"], p["on_recording"], p["when_asleep"], p["full_flash"]),
                         ("esp32", "esp32c6", True, "keep", "cycle", True))
        self.assertEqual(p["meta"]["source"], "app image")

    def test_dry_run(self):
        app = self.write("firmware.bin", esp_image())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(pst_flash.main(["--dry-run", "--chip", "esp32c6", app]), 0)
        self.assertIn("firmware.bin (app partition)", out.getvalue())


class FakeTracker(BaseHTTPRequestHandler):
    """POST /api/flash -> a job; GET /api/flash/<id> -> done, with the first image already on the board."""
    received = []

    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer tok":
            return self.reply({"detail": "flash token missing or wrong"}, 401)
        FakeTracker.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.reply({"id": 7, "status": "queued"})

    def do_GET(self):
        self.reply({"id": 7, "status": "done", "log": ["Hash of data verified."], "log_total": 1,
                    "power_cycled": False, "images": [{"skipped": True}, {"skipped": False}]})


class Send(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTracker)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        FakeTracker.received.clear()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_upload_and_follow(self):
        u = pst_flash.Upload([(0, b"boot", "b.bin"), (0x10000, b"app", "a.bin")], "esp32c6", source="files")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(pst_flash.send(u, self.url + "/", "tok", "keep"), 0)
        self.assertEqual(FakeTracker.received[0]["on_recording"], "keep")
        self.assertIn("1 of 2 image(s) already on the board", out.getvalue())

    def test_wrong_token(self):
        u = pst_flash.Upload([(0, b"boot", "b.bin")], "esp32c6")
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pst_flash.send(u, self.url, "wrong"), 1)
        self.assertIn("401", err.getvalue())


if __name__ == "__main__":
    unittest.main()
