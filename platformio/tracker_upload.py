"""PlatformIO upload through Inspector Watt: `pio run -t upload` sends the build to the tracker, which
flashes the DUT over its USB port while the PPK2 keeps powering it (and wakes it first if it's asleep).

Copy this file into the firmware project and add an environment to platformio.ini:

    [env:xiao_c6_tracker]
    extends = env:seeed_xiao_esp32c6
    upload_protocol = custom
    extra_scripts = post:tracker_upload.py
    custom_tracker_url = http://<tracker host>:8080
    ; custom_tracker_on_recording = ask    ; ask | keep | stop | after | cancel, when a recording is running
    ; custom_tracker_when_asleep = wait    ; wait (then power-cycle) | always (only wait) | cycle (power-cycle now)

The token comes from the environment variable PST_FLASH_TOKEN (the tracker's upload token: Settings, or
GET /api/flash/settings). PST_URL overrides custom_tracker_url.

If a recording is running and on_recording is "ask", answer here (k/s/a/c + Enter) or in the tracker's web UI.
"after" queues the upload until the recording stops: this command then ends right away and the tracker flashes
later (see its web UI). Only the Python standard library is used.
"""

import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

Import("env")  # noqa: F821 (PlatformIO/SCons)

POLL_S = 0.5


def _option(env, name, default=""):
    try:
        return env.GetProjectOption(name, default)
    except Exception:
        return default


def _git(project_dir):
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty", "--tags"], cwd=project_dir,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _firmware_version(env, project_dir):
    """FIRMWARE_VERSION from the build flags, else a #define in include/*.h."""
    for d in env.get("CPPDEFINES", []):
        if isinstance(d, (list, tuple)) and len(d) == 2 and d[0] == "FIRMWARE_VERSION":
            return str(d[1]).strip('\\"')
    inc = os.path.join(project_dir, "include")
    if os.path.isdir(inc):
        for name in sorted(os.listdir(inc)):
            if name.endswith(".h"):
                try:
                    with open(os.path.join(inc, name), encoding="utf-8", errors="replace") as f:
                        m = re.search(r'#define\s+FIRMWARE_VERSION\s+"([^"]+)"', f.read())
                    if m:
                        return m.group(1)
                except OSError:
                    pass
    return None


def _request(method, url, token, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = e.reason
        raise RuntimeError(f"tracker said {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the tracker at {url}: {e.reason}") from None


def _images(env, source):
    """The same images and offsets PlatformIO's esptool upload would write."""
    images = [(env.subst(off), env.subst(path)) for off, path in env.get("FLASH_EXTRA_IMAGES", [])]
    images.append((env.subst("$ESP32_APP_OFFSET"), str(source[0])))
    out = []
    for off, path in images:
        with open(path, "rb") as f:
            out.append({"offset": int(off, 0), "data_b64": base64.b64encode(f.read()).decode(),
                        "_name": os.path.basename(path)})
    return out


def _flash_settings(env):
    board = env.BoardConfig()
    mode = env.subst("${__get_board_flash_mode(__env__)}") or board.get("build.flash_mode", "dio")
    freq = env.subst("${__get_board_f_image(__env__)}") or "keep"
    return {"chip": board.get("build.mcu", "esp32"), "flash_mode": mode, "flash_freq": freq,
            "flash_size": board.get("upload.flash_size", "detect")}


def _ask_in_terminal(state):
    """Read k/s/c from the terminal while the tracker waits; the web UI may answer first."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        line = sys.stdin.readline().strip().lower()
    except Exception:
        return
    state["answer"] = {"k": "keep", "s": "stop", "a": "after", "c": "cancel"}.get(line[:1])


def tracker_upload(source, target, env):
    url = (os.environ.get("PST_URL") or _option(env, "custom_tracker_url")).rstrip("/")
    token = os.environ.get("PST_FLASH_TOKEN", "")
    if not url:
        sys.stderr.write("Set custom_tracker_url in platformio.ini (or PST_URL).\n")
        return 1
    if not token:
        sys.stderr.write("Set PST_FLASH_TOKEN to the tracker's upload token.\n")
        return 1
    project_dir = env.subst("$PROJECT_DIR")
    images = _images(env, source)
    payload = {
        **_flash_settings(env),
        "images": [{k: v for k, v in im.items() if not k.startswith("_")} for im in images],
        "on_recording": _option(env, "custom_tracker_on_recording", "ask") or "ask",
        "when_asleep": _option(env, "custom_tracker_when_asleep", "") or "",
        "meta": {"project": os.path.basename(project_dir), "env": env.subst("$PIOENV"),
                 "version": _firmware_version(env, project_dir), "git": _git(project_dir),
                 "host": socket.gethostname(), "source": "platformio"},
    }
    print(f"Tracker: {url}  chip {payload['chip']}, "
          + ", ".join(f"{im['_name']} @ {im['offset']:#x}" for im in images))
    try:
        job = _request("POST", f"{url}/api/flash", token, payload)
        seen = 0
        asked = False
        state = {"answer": None}
        while True:
            job = _request("GET", f"{url}/api/flash/{job['id']}?after={seen}", token)
            for line in job.get("log", []):
                print("  " + line)
            seen = job.get("log_total", seen)
            if job["status"] == "waiting_choice" and job.get("question"):
                if not asked:
                    asked = True
                    print("  A recording is running. [k] keep it and mark the flash, [s] stop it, [a] flash after it "
                          "stops, [c] cancel (type + Enter here, or answer in the tracker UI)")
                    threading.Thread(target=_ask_in_terminal, args=(state,), daemon=True).start()
                if state["answer"]:
                    try:
                        _request("POST", f"{url}/api/flash/{job['id']}/answer", token, {"choice": state["answer"]})
                    except RuntimeError as e:  # answered in the UI meanwhile
                        print(f"  {e}")
                    state["answer"] = None
            if job["status"] == "waiting_recording":
                print(f"Queued: the tracker flashes when recording '{job.get('waiting_for')}' stops. "
                      "See its web UI for the result (Settings → Firmware upload).")
                return 0
            if job["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(POLL_S)
    except RuntimeError as e:
        sys.stderr.write(f"Upload through the tracker failed: {e}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Stopped watching; the tracker finishes the flash on its own.\n")
        return 1
    if job["status"] != "done":
        sys.stderr.write(f"Flash {job['status']}: {job.get('error') or ''}\n")
        return 1
    print(f"Flashed{' (DUT power-cycled first)' if job.get('power_cycled') else ''}"
          + (f", marked in recording {job['session_id']}" if job.get("session_id") else ""))
    return 0


env.Replace(UPLOADCMD=tracker_upload)  # noqa: F821
