import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOCK_PATH = DATA_DIR / "switch.lock"
KNOWN_HOSTS = BASE_DIR / "known_hosts"

VM_LINE = re.compile(
    r"^(?P<vmid>\d+)\s+(?P<name>.*?)\s+\[(?P<datastore>[^\]]+)\]\s+"
    r"(?P<file>.*?)\s+(?P<guest>\S+)\s+(?P<version>vmx-\S+)\s*(?P<annotation>.*)$"
)
POWER_COMMAND = re.compile(
    r"^vim-cmd vmsvc/(?:power\.(?:getstate|on|off|shutdown)|get\.(?:guest|summary)) [1-9][0-9]*$"
)
GUEST_BATCH_COMMAND = re.compile(
    r"^(?:echo '---GUEST [1-9][0-9]*---'; vim-cmd vmsvc/get\.(?:guest|summary) [1-9][0-9]*)"
    r"(?:; echo '---GUEST [1-9][0-9]*---'; vim-cmd vmsvc/get\.(?:guest|summary) [1-9][0-9]*)*$"
)
LIST_COMMAND = (
    "vim-cmd vmsvc/getallvms; "
    "echo '---RUNNING---'; "
    "esxcli vm process list"
)
HOST_SUMMARY_COMMAND = "vim-cmd hostsvc/hostsummary"
STORAGE_LIST_COMMAND = "esxcli storage filesystem list"

TOOLS_READY = {"guestToolsRunning"}
TOOLS_OK = {"toolsOk", "toolsOld"}
IP_CACHE_TTL_SEC = 60
_ip_cache = {}  # vmid -> (ip, expires_at)
_details_cache = {}  # vmid -> (details_dict, expires_at)
_host_cache = {"data": None, "expires": 0}


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


def command_allowed(command):
    return (
        command == LIST_COMMAND
        or command == HOST_SUMMARY_COMMAND
        or command == STORAGE_LIST_COMMAND
        or bool(POWER_COMMAND.fullmatch(command))
        or bool(GUEST_BATCH_COMMAND.fullmatch(command))
    )


def ssh(command, timeout=120):
    if not command_allowed(command):
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


def _field(blob, name):
    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', blob)
    if match:
        return match.group(1)
    match = re.search(rf"\b{re.escape(name)}\s*=\s*'([^']*)'", blob)
    if match:
        return match.group(1)
    return ""


def _num(blob, name):
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(-?\d+)", blob)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_guest_ip(blob):
    ip_address = _field(blob, "ipAddress")
    if ip_address not in ("", "<unset>", "null"):
        return ip_address
    ips = re.findall(r'ipAddress\s*=\s*"(\d+\.\d+\.\d+\.\d+)"', blob)
    return ips[0] if ips else ""


def format_uptime(seconds):
    if seconds is None or seconds < 0:
        return ""
    days = seconds / 86400.0
    if days >= 1:
        return f"{days:.2f} days"
    hours = seconds / 3600.0
    if hours >= 1:
        return f"{hours:.1f} hours"
    minutes = max(1, int(seconds / 60))
    return f"{minutes} min"


def format_gb_from_mb(mb):
    if mb is None:
        return ""
    return f"{mb / 1024.0:.1f} GB"


def format_gb_from_bytes(num):
    if num is None:
        return ""
    return f"{num / (1024.0 ** 3):.2f} GB"


def tools_label(tools_status, tools_running, powered_on):
    if not powered_on:
        return "NotRunning"
    status = (tools_status or "").lower()
    running = (tools_running or "").lower()
    if "notinstalled" in status or status == "toolsnotinstalled":
        return "NotInstalled"
    if running == "guesttoolsrunning" or status in ("toolsok", "toolsold"):
        return "Ok"
    if "notrunning" in running or status == "toolsnotrunning":
        return "NotRunning"
    return tools_status or tools_running or "Unknown"


def parse_summary_details(blob, powered_on=False):
    memory_mb = _num(blob, "memorySizeMB")
    num_cpu = _num(blob, "numCpu")
    committed = _num(blob, "committed")
    uptime = _num(blob, "uptimeSeconds")
    guest_full = _field(blob, "guestFullName")
    tools_status = _field(blob, "toolsStatus")
    tools_running = _field(blob, "toolsRunningStatus")
    ip_address = parse_guest_ip(blob)
    return {
        "guest_full_name": guest_full,
        "cpu_cores": num_cpu,
        "memory_mb": memory_mb,
        "memory_gb": round((memory_mb or 0) / 1024.0, 1) if memory_mb is not None else None,
        "disk_bytes": committed,
        "disk_gb": round((committed or 0) / (1024.0 ** 3), 2) if committed is not None else None,
        "uptime_seconds": uptime if powered_on else 0,
        "uptime": format_uptime(uptime) if powered_on and uptime else "",
        "ip": ip_address if powered_on else "",
        "tools": tools_label(tools_status, tools_running, powered_on),
        "tools_status": tools_status or "unknown",
        "tools_running_status": tools_running or "unknown",
        "snapshots": None,
    }


def fetch_vm_details(vms):
    """Batch get.summary for VM hardware / tools / IP details."""
    now = time.time()
    result = {}
    wanted = []
    by_id = {int(vm["vmid"]): vm for vm in vms}
    for vm in vms:
        vmid = int(vm["vmid"])
        cached = _details_cache.get(vmid)
        if cached and cached[1] > now:
            result[vmid] = cached[0]
            continue
        wanted.append(vmid)
    if wanted:
        command = "; ".join(
            f"echo '---GUEST {vmid}---'; vim-cmd vmsvc/get.summary {vmid}" for vmid in wanted
        )
        try:
            output = ssh(command, timeout=max(90, 15 * len(wanted)))
        except EsxiError:
            output = ""
        current = None
        chunks = {}
        for line in (output or "").splitlines():
            marker = re.match(r"^---GUEST ([1-9][0-9]*)---\s*$", line.strip())
            if marker:
                current = int(marker.group(1))
                chunks[current] = []
                continue
            if current is not None:
                chunks[current].append(line)
        expires = time.time() + IP_CACHE_TTL_SEC
        for vmid in wanted:
            powered_on = by_id.get(vmid, {}).get("power") == "Powered on"
            details = parse_summary_details("\n".join(chunks.get(vmid, [])), powered_on=powered_on)
            result[vmid] = details
            _details_cache[vmid] = (details, expires)
            if details.get("ip"):
                _ip_cache[vmid] = (details["ip"], expires)
    return result


def host_info(use_cache=True):
    now = time.time()
    if use_cache and _host_cache["data"] and _host_cache["expires"] > now:
        return _host_cache["data"]

    summary = ssh(HOST_SUMMARY_COMMAND, timeout=60)
    storage = ssh(STORAGE_LIST_COMMAND, timeout=60)

    cpu_mhz = _num(summary, "cpuMhz") or 0
    num_cores = _num(summary, "numCpuCores") or 0
    overall_cpu = _num(summary, "overallCpuUsage") or 0
    memory_bytes = _num(summary, "memorySize") or 0
    overall_mem_mb = _num(summary, "overallMemoryUsage") or 0
    uptime = _num(summary, "uptime") or 0
    version = _field(summary, "version") or ""
    full_name = _field(summary, "fullName") or "VMware ESXi"
    host_name = _field(summary, "name") or env("ESXI_HOST")

    cpu_total_mhz = cpu_mhz * num_cores
    cpu_used_ghz = overall_cpu / 1000.0
    cpu_total_ghz = cpu_total_mhz / 1000.0
    mem_used_gb = overall_mem_mb / 1024.0
    mem_total_gb = memory_bytes / (1024.0 ** 3)

    datastores = []
    for line in storage.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        # Mount Point, Volume Name, UUID, Mounted, Type, Size, Free
        # Volume name may contain spaces - parse from the right.
        try:
            size_b = int(parts[-2])
            free_b = int(parts[-1])
        except ValueError:
            continue
        fs_type = parts[-3]
        mounted = parts[-4]
        uuid = parts[-5]
        mount = parts[0]
        volume = " ".join(parts[1:-5]).strip() or uuid
        used_b = max(0, size_b - free_b)
        datastores.append(
            {
                "name": volume,
                "type": fs_type,
                "mounted": mounted.lower() == "true",
                "size_bytes": size_b,
                "free_bytes": free_b,
                "used_bytes": used_b,
                "size_gb": round(size_b / (1024.0 ** 3), 2),
                "used_gb": round(used_b / (1024.0 ** 3), 2),
                "free_gb": round(free_b / (1024.0 ** 3), 2),
                "percent": round((used_b / size_b) * 100, 1) if size_b else 0,
            }
        )

    primary = datastores[0] if datastores else None
    payload = {
        "name": host_name,
        "product": full_name,
        "version": version,
        "ip": env("ESXI_HOST"),
        "uptime_seconds": uptime,
        "uptime": format_uptime(uptime),
        "cpu": {
            "used_ghz": round(cpu_used_ghz, 1),
            "total_ghz": round(cpu_total_ghz, 1),
            "percent": round((overall_cpu / cpu_total_mhz) * 100, 1) if cpu_total_mhz else 0,
            "cores": num_cores,
        },
        "memory": {
            "used_gb": round(mem_used_gb, 1),
            "total_gb": round(mem_total_gb, 1),
            "percent": round((overall_mem_mb * 1024 * 1024 / memory_bytes) * 100, 1) if memory_bytes else 0,
        },
        "datastores": datastores,
        "datastore": primary,
    }
    _host_cache["data"] = payload
    _host_cache["expires"] = time.time() + 30
    return payload


def apply_cached_ips(vms):
    now = time.time()
    for vm in vms:
        vmid = int(vm["vmid"])
        cached = _ip_cache.get(vmid)
        if cached and cached[1] > now and vm.get("power") == "Powered on":
            vm["ip"] = cached[0]
        else:
            vm.setdefault("ip", "")
            if vm.get("power") != "Powered on":
                vm["ip"] = ""
                _ip_cache.pop(vmid, None)
    return vms


def fetch_guest_ips(vmids, use_cache=True):
    """Return {vmid: ip} for powered-on guests via get.summary (lighter than get.guest)."""
    now = time.time()
    wanted = []
    result = {}
    for raw in vmids:
        vmid = int(raw)
        if use_cache:
            cached = _ip_cache.get(vmid)
            if cached and cached[1] > now:
                result[vmid] = cached[0]
                continue
        wanted.append(vmid)
    if not wanted:
        return result

    command = "; ".join(
        f"echo '---GUEST {vmid}---'; vim-cmd vmsvc/get.summary {vmid}" for vmid in wanted
    )
    try:
        output = ssh(command, timeout=max(60, 12 * len(wanted)))
    except EsxiError:
        for vmid in wanted:
            result.setdefault(vmid, "")
        return result

    current = None
    chunks = {}
    for line in output.splitlines():
        marker = re.match(r"^---GUEST ([1-9][0-9]*)---\s*$", line.strip())
        if marker:
            current = int(marker.group(1))
            chunks[current] = []
            continue
        if current is not None:
            chunks[current].append(line)

    expires = time.time() + IP_CACHE_TTL_SEC
    for vmid in wanted:
        ip = parse_guest_ip("\n".join(chunks.get(vmid, [])))
        result[vmid] = ip
        _ip_cache[vmid] = (ip, expires)
    return result


def list_vms(include_ips=False):
    """Fast list by default. IPs come from cache and/or /api/ips."""
    vms = apply_cached_ips(parse_vms(ssh(LIST_COMMAND)))
    if include_ips:
        powered_ids = [vm["vmid"] for vm in vms if vm.get("power") == "Powered on"]
        ips = fetch_guest_ips(powered_ids, use_cache=True)
        for vm in vms:
            if vm.get("power") == "Powered on":
                vm["ip"] = ips.get(int(vm["vmid"]), vm.get("ip") or "")
            else:
                vm["ip"] = ""
    return vms


def power_state(vmid):
    output = ssh(f"vim-cmd vmsvc/power.getstate {int(vmid)}", timeout=30)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise EsxiError(f"No power state returned for VM {vmid}")
    return lines[-1]


def guest_info(vmid):
    """Read guest/tools readiness from ESXi (vim-cmd vmsvc/get.guest)."""
    blob = ssh(f"vim-cmd vmsvc/get.guest {int(vmid)}", timeout=45)
    tools_running = _field(blob, "toolsRunningStatus")
    tools_status = _field(blob, "toolsStatus")
    guest_state = _field(blob, "guestState")
    ip_address = parse_guest_ip(blob)

    tools_running_ok = tools_running in TOOLS_READY
    tools_status_ok = tools_status in TOOLS_OK or tools_status == ""
    guest_running = guest_state in ("", "running")
    ready = bool(tools_running_ok and tools_status_ok and guest_running)

    if ready and ip_address:
        percent = 100
        stage = "guest_ready"
        detail = f"VMware Tools running, IP {ip_address}"
    elif ready:
        percent = 95
        stage = "tools_running"
        detail = "VMware Tools running (no guest IP yet)"
    elif tools_running_ok:
        percent = 90
        stage = "tools_running"
        detail = f"Tools running ({tools_status or 'status unknown'})"
    elif guest_state == "running":
        percent = 70
        stage = "waiting_tools"
        detail = "Guest reported running; waiting for VMware Tools"
    else:
        percent = 55
        stage = "waiting_tools"
        detail = "Waiting for VMware Tools / guest OS"

    return {
        "tools_running_status": tools_running or "unknown",
        "tools_status": tools_status or "unknown",
        "guest_state": guest_state or "unknown",
        "ip_address": ip_address,
        "ready": ready,
        "percent": percent,
        "stage": stage,
        "detail": detail,
    }


def wait_for_power(vmid, expected, timeout=90):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = power_state(vmid)
        if last == expected:
            return
        time.sleep(2)
    raise EsxiError(f"VM {vmid} is still '{last}', not '{expected}'")


def wait_for_guest_ready(vmid, timeout=300, on_progress=None):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            info = guest_info(vmid)
        except EsxiError as exc:
            info = {
                "ready": False,
                "percent": 60,
                "stage": "waiting_tools",
                "detail": f"Waiting for guest info ({exc})",
                "tools_running_status": "unknown",
                "tools_status": "unknown",
                "guest_state": "unknown",
                "ip_address": "",
            }
        last = info
        if on_progress:
            on_progress(info)
        if info.get("ready"):
            return info
        time.sleep(3)
    if last is None:
        raise EsxiError(f"No guest status returned for VM {vmid}")
    last = dict(last)
    last["ready"] = False
    last["timed_out"] = True
    last["percent"] = max(int(last.get("percent") or 0), 85)
    last["stage"] = "tools_timeout"
    last["detail"] = (
        "VM is powered on, but VMware Tools did not become ready in time. "
        "Guest OS may still be booting, or Tools may be missing."
    )
    if on_progress:
        on_progress(last)
    return last


def set_power(vmid, action, on_progress=None):
    if action not in ("on", "off"):
        raise EsxiError("Choose power on or power off")
    expected = "Powered on" if action == "on" else "Powered off"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    def emit(percent, stage, detail, **extra):
        if on_progress:
            payload = {"percent": percent, "stage": stage, "detail": detail}
            payload.update(extra)
            on_progress(payload)

    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        vms = parse_vms(ssh(LIST_COMMAND))
        selected = next((vm for vm in vms if vm["vmid"] == vmid), None)
        if selected is None:
            raise EsxiError("That VM is not on this ESXi host")
        name = selected["name"]

        if selected["power"] == expected:
            if action == "on":
                emit(40, "powered_on", f"{name} is already powered on; checking guest readiness…", vmid=vmid, name=name)
                guest = wait_for_guest_ready(vmid, timeout=120, on_progress=on_progress)
                vms = list_vms()
                if guest.get("ready"):
                    msg = f"{name} is powered on and the guest is ready"
                    if guest.get("ip_address"):
                        msg += f" ({guest['ip_address']})"
                        _ip_cache[int(vmid)] = (
                            guest["ip_address"],
                            time.time() + IP_CACHE_TTL_SEC,
                        )
                    msg += "."
                else:
                    msg = f"{name} is powered on, but VMware Tools is not ready yet."
                return msg, vms, guest
            return f"{name} is already powered off.", vms, None

        if action == "on":
            emit(5, "starting", f"Starting power on for {name}…", vmid=vmid, name=name)
            ssh(f"vim-cmd vmsvc/power.on {int(vmid)}", timeout=60)
            emit(25, "power_command", "Power-on command sent; waiting for VM power state…", vmid=vmid, name=name)
            wait_for_power(vmid, expected)
            emit(45, "powered_on", "VM is powered on; waiting for VMware Tools / guest OS…", vmid=vmid, name=name)
            # Release lock before long guest wait so other VMs stay controllable.
            fcntl.flock(lock, fcntl.LOCK_UN)

    if action == "on":
        guest = wait_for_guest_ready(vmid, timeout=300, on_progress=on_progress)
        vms = list_vms()
        if guest.get("ready"):
            msg = f"Powered on {name}; guest is ready"
            if guest.get("ip_address"):
                msg += f" ({guest['ip_address']})"
                _ip_cache[int(vmid)] = (
                    guest["ip_address"],
                    time.time() + IP_CACHE_TTL_SEC,
                )
            msg += "."
        else:
            msg = (
                f"Powered on {name}, but VMware Tools did not report ready in time. "
                "The guest may still be booting."
            )
        return msg, vms, guest

    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        vms = parse_vms(ssh(LIST_COMMAND))
        selected = next((vm for vm in vms if vm["vmid"] == vmid), None)
        if selected is None:
            raise EsxiError("That VM is not on this ESXi host")
        name = selected["name"]
        if selected["power"] == "Powered off":
            return f"{name} is already powered off.", vms, None
        emit(20, "shutdown", f"Asking {name} to shut down…", vmid=vmid, name=name)
        try:
            ssh(f"vim-cmd vmsvc/power.shutdown {int(vmid)}", timeout=60)
        except EsxiError as exc:
            detail = str(exc).lower()
            tools_missing = "tools" in detail or "shutdownguest" in detail
            if not tools_missing:
                raise
            emit(50, "power_off", "VMware Tools missing; powering off…", vmid=vmid, name=name)
            ssh(f"vim-cmd vmsvc/power.off {int(vmid)}", timeout=60)
            wait_for_power(vmid, expected)
            emit(100, "done", f"{name} powered off.", vmid=vmid, name=name)
            selected["power"] = expected
            return (
                f"VMware Tools is not running in {name}, so it was powered off.",
                vms,
                None,
            )
        try:
            wait_for_power(vmid, expected, timeout=180)
        except EsxiError as exc:
            raise EsxiError(
                f"Guest shutdown was sent to {name}, but it is still running. {exc}"
            ) from exc
        emit(100, "done", f"{name} shut down.", vmid=vmid, name=name)
        selected["power"] = expected
        return f"Shut down {name}.", vms, None


def payload_vms(include_ips=False):
    return {"ok": True, "host": env("ESXI_HOST"), "vms": list_vms(include_ips=include_ips)}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            body = (BASE_DIR / "templates" / "index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body, cache=False)
            return
        if path == "/api/host":
            try:
                self._send_json({"ok": True, "host": host_info(use_cache=True)})
            except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/vms":
            qs = parse_qs(parsed.query)
            include_ips = (qs.get("ips") or ["0"])[0] in ("1", "true", "yes")
            try:
                payload = payload_vms(include_ips=include_ips)
            except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
                return
            self._send_json(payload)
            return
        if path == "/api/details":
            qs = parse_qs(parsed.query)
            raw_ids = (qs.get("vmids") or [""])[0]
            try:
                vms = list_vms(include_ips=False)
                if raw_ids.strip():
                    wanted = {int(part) for part in raw_ids.split(",") if part.strip()}
                    selected = [vm for vm in vms if int(vm["vmid"]) in wanted]
                else:
                    selected = vms
                details = fetch_vm_details(selected)
                enriched = []
                for vm in selected:
                    item = dict(vm)
                    extra = details.get(int(vm["vmid"]), {})
                    item.update(extra)
                    if extra.get("ip"):
                        item["ip"] = extra["ip"]
                    enriched.append(item)
                self._send_json({"ok": True, "host": env("ESXI_HOST"), "vms": enriched})
            except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/ips":
            qs = parse_qs(parsed.query)
            raw_ids = (qs.get("vmids") or [""])[0]
            try:
                if raw_ids.strip():
                    vmids = [int(part) for part in raw_ids.split(",") if part.strip()]
                else:
                    vmids = [
                        int(vm["vmid"])
                        for vm in list_vms(include_ips=False)
                        if vm.get("power") == "Powered on"
                    ]
                if any(vmid < 1 for vmid in vmids):
                    raise ValueError
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "Provide valid vmids."}, 400)
                return
            try:
                ips = fetch_guest_ips(vmids, use_cache=True)
                self._send_json(
                    {
                        "ok": True,
                        "host": env("ESXI_HOST"),
                        "ips": {str(k): v for k, v in ips.items()},
                    }
                )
            except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/guest":
            qs = parse_qs(parsed.query)
            try:
                vmid = int((qs.get("vmid") or [""])[0])
                if vmid < 1:
                    raise ValueError
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "Provide a valid vmid."}, 400)
                return
            try:
                power = power_state(vmid)
                guest = guest_info(vmid) if power == "Powered on" else {
                    "ready": False,
                    "percent": 0,
                    "stage": "powered_off",
                    "detail": "VM is powered off",
                    "tools_running_status": "unknown",
                    "tools_status": "unknown",
                    "guest_state": "notRunning",
                    "ip_address": "",
                }
                self._send_json({"ok": True, "host": env("ESXI_HOST"), "vmid": vmid, "power": power, "guest": guest})
            except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
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
            stream = bool(incoming.get("stream")) or action == "on"
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "Choose a VM, then power on or power off."}, 400)
            return

        if stream:
            self._stream_power(vmid, action)
            return

        try:
            message, vms, guest = set_power(vmid, action)
            payload = {"ok": True, "host": env("ESXI_HOST"), "vms": vms, "message": message}
            if guest is not None:
                payload["guest"] = guest
        except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        self._send_json(payload)

    def _stream_power(self, vmid, action):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(event):
            line = json.dumps(event, ensure_ascii=True) + "\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        try:
            def on_progress(info):
                event = {"ok": True, "type": "progress", "vmid": vmid, "action": action}
                event.update(info)
                emit(event)

            message, vms, guest = set_power(vmid, action, on_progress=on_progress)
            final = {
                "ok": True,
                "type": "done",
                "vmid": vmid,
                "action": action,
                "message": message,
                "host": env("ESXI_HOST"),
                "vms": vms,
                "percent": 100,
                "stage": "done",
                "detail": message,
            }
            if guest is not None:
                final["guest"] = guest
                if not guest.get("ready"):
                    final["percent"] = int(guest.get("percent") or 85)
                    final["stage"] = guest.get("stage") or "tools_timeout"
            emit(final)
        except (EsxiError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            emit({"ok": False, "type": "error", "vmid": vmid, "action": action, "error": str(exc)})
        except BrokenPipeError:
            return

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
