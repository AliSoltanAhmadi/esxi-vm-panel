import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOCK_PATH = DATA_DIR / "switch.lock"
KNOWN_HOSTS = BASE_DIR / "known_hosts"

VM_LINE = re.compile(
    r"^(?P<vmid>\d+)\s+(?P<name>.*?)\s+\[(?P<datastore>[^\]]+)\]\s+"
    r"(?P<file>.*?)\s+(?P<guest>\S+)\s+(?P<version>vmx-\S+)\s*(?P<annotation>.*)$"
)
POWER_COMMAND = re.compile(r"^vim-cmd vmsvc/power\.(getstate|on|off|shutdown) [1-9][0-9]*$")
LIST_COMMAND = (
    "vim-cmd vmsvc/getallvms; "
    "echo '---RUNNING---'; "
    "esxcli vm process list"
)


class EsxiError(Exception):
    pass


def load_env_file(path):
    if not path.exists():
        raise EsxiError(f"Missing {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise EsxiError(f"Set {name} in /opt/app/.env")
    return value


def ssh(command, timeout=120):
    if command != LIST_COMMAND and not POWER_COMMAND.fullmatch(command):
        raise EsxiError("That ESXi command is not allowed")
    password = os.environ.get("ESXI_PASSWORD", "")
    if not password:
        raise EsxiError("Set ESXI_PASSWORD in /opt/app/.env")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    script = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="askpass-",
        suffix=".sh",
        dir=DATA_DIR,
        delete=False,
        encoding="utf-8",
    )
    script_path = script.name
    try:
        script.write("#!/usr/bin/env python3\nprint(%s)\n" % repr(password))
        script.close()
        os.chmod(script_path, 0o700)
        process_env = os.environ.copy()
        process_env["SSH_ASKPASS"] = script_path
        process_env["SSH_ASKPASS_REQUIRE"] = "force"
        process_env["DISPLAY"] = "none"
        process_env.pop("SSH_AUTH_SOCK", None)
        completed = subprocess.run(
            [
                "setsid",
                "ssh",
                "-T",
                "-o", "BatchMode=no",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
                "-o", "PreferredAuthentications=keyboard-interactive,password",
                "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "ConnectTimeout=20",
                "-o", "LogLevel=ERROR",
                "-p", env("ESXI_PORT"),
                f"{env('ESXI_USER')}@{env('ESXI_HOST')}",
                command,
            ],
            env=process_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    output = completed.stdout.decode("utf-8", "replace").replace("\r", "")
    error = completed.stderr.decode("utf-8", "replace").replace("\r", "").strip()
    if completed.returncode != 0:
        raise EsxiError(error or output.strip() or f"ESXi SSH failed ({completed.returncode})")
    return output


def parse_vms(output):
    listing, marker, running_blob = output.partition("---RUNNING---")
    if not marker:
        raise EsxiError("ESXi did not return the running VM list")

    running_names = set()
    running_files = []
    for line in running_blob.splitlines():
        stripped = line.strip()
        if stripped.startswith("Display Name:"):
            running_names.add(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Config File:"):
            running_files.append(stripped.split(":", 1)[1].strip())

    vms = []
    for line in listing.splitlines():
        match = VM_LINE.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip()
        vmx = match.group("file").strip()
        powered_on = name in running_names or any(
            path.endswith("/" + vmx) or path.endswith(vmx) for path in running_files
        )
        vms.append(
            {
                "vmid": int(match.group("vmid")),
                "name": name,
                "guest": match.group("guest"),
                "datastore": match.group("datastore"),
                "power": "Powered on" if powered_on else "Powered off",
            }
        )

    vms.sort(key=lambda item: item["name"].lower())
    return vms


def list_vms():
    return parse_vms(ssh(LIST_COMMAND))


def power_state(vmid):
    output = ssh(f"vim-cmd vmsvc/power.getstate {int(vmid)}", timeout=30)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise EsxiError(f"No power state returned for VM {vmid}")
    return lines[-1]


def wait_for_power(vmid, expected, timeout=90):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = power_state(vmid)
        if last == expected:
            return
        time.sleep(2)
    raise EsxiError(f"VM {vmid} is still '{last}', not '{expected}'")


def set_power(vmid, action):
    if action not in ("on", "off"):
        raise EsxiError("Choose power on or power off")
    expected = "Powered on" if action == "on" else "Powered off"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        vms = parse_vms(ssh(LIST_COMMAND))
        selected = next((vm for vm in vms if vm["vmid"] == vmid), None)
        if selected is None:
            raise EsxiError("That VM is not on this ESXi host")
        if selected["power"] != expected:
            if action == "on":
                ssh(f"vim-cmd vmsvc/power.on {int(vmid)}", timeout=60)
                wait_for_power(vmid, expected)
                selected["power"] = expected
                return f"Powered on {selected['name']}.", vms
            try:
                ssh(f"vim-cmd vmsvc/power.shutdown {int(vmid)}", timeout=60)
            except EsxiError as exc:
                detail = str(exc).lower()
                tools_missing = "tools" in detail or "shutdownguest" in detail
                if not tools_missing:
                    raise
                ssh(f"vim-cmd vmsvc/power.off {int(vmid)}", timeout=60)
                wait_for_power(vmid, expected)
                selected["power"] = expected
                return (
                    f"VMware Tools is not running in {selected['name']}, so it was powered off.",
                    vms,
                )
            try:
                wait_for_power(vmid, expected, timeout=180)
            except EsxiError as exc:
                raise EsxiError(
                    f"Guest shutdown was sent to {selected['name']}, but it is still running. {exc}"
                ) from exc
            selected["power"] = expected
            return f"Shut down {selected['name']}.", vms
    if action == "on":
        return f"{selected['name']} is already powered on.", vms
    return f"{selected['name']} is already powered off.", vms


def payload_vms():
    return {"ok": True, "host": env("ESXI_HOST"), "vms": list_vms()}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = (BASE_DIR / "templates" / "index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body, cache=False)
            return
        if path == "/api/vms":
            try:
                payload = payload_vms()
            except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
                return
            self._send_json(payload)
            return
        self._send_json({"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/power", "/api/select"):
            self._send_json({"ok": False, "error": "Not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            incoming = json.loads(raw.decode("utf-8"))
            vmid = int(incoming.get("vmid"))
            if path == "/api/select":
                action = "on"
            else:
                action = str(incoming.get("action") or "")
            if action not in ("on", "off"):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "Choose a VM, then power on or power off."}, 400)
            return
        try:
            message, vms = set_power(vmid, action)
            payload = {"ok": True, "host": env("ESXI_HOST"), "vms": vms}
        except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        payload["message"] = message
        self._send_json(payload)

    def _send(self, status, content_type, body, cache=True):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=200):
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode(), cache=False)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    load_env_file(BASE_DIR / ".env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((env("APP_HOST"), int(env("APP_PORT"))), Handler)
    print(f"VM panel listening on {env('APP_HOST')}:{env('APP_PORT')}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
