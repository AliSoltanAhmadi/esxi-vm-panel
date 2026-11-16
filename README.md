# ESXi VM Panel

A small web panel for **one** VMware ESXi host. It lists every virtual machine and lets you **power on** or **shut down** a VM one at a time. Other VMs are left unchanged.

No vSphere / vCenter client required — only SSH to ESXi and a browser.

## Features

- List all VMs with power state, guest IP (from VMware Tools), guest type, and datastore
- Search by name, guest OS type, or VM id
- Power on a single VM with a **live progress bar** until the guest is ready
- Guest readiness uses ESXi `vim-cmd vmsvc/get.guest` (VMware Tools running / `toolsOk`, guest state, IP when available)
- Guest shutdown via VMware Tools (`power.shutdown`); if Tools is missing, the UI asks before hard power off
- Dark / light theme (remembered in the browser)
- Concurrent power actions without freezing the UI
- Strict allowlist of ESXi commands (`power.on`, `power.shutdown`, `power.off`, `power.getstate`, `get.guest` + list)

## Requirements

- A Linux host that can reach the ESXi SSH port (usually TCP 22)
- Docker Engine (recommended), **or** Python 3 + OpenSSH client
- An ESXi account that can run `vim-cmd` and `esxcli`
- **ESXi SSH service (`TSM-SSH`) enabled** — see below
- VMware Tools inside the guest for a clean shutdown (optional but recommended)

## Enable ESXi SSH (TSM-SSH)

This panel talks to ESXi **only over SSH**. If TCP 22 is closed (`Connection refused`), start the **TSM-SSH** service on the host.

### Option A — ESXi Host Client (UI)

1. Open `https://YOUR_ESXI_IP` in a browser and sign in.
2. Go to **Host** → **Manage** → **Services** (or **Navigator** → **Manage** → **Services**).
3. Select **TSM-SSH** (SSH).
4. Click **Start**.
5. Optional but recommended: **Actions** → **Policy** → **Start and stop with host** so SSH survives reboot.

### Option B — ESXi Direct Console (DCUI)

1. On the ESXi console: **F2** → log in.
2. **Troubleshooting Options** → **Enable ESXi Shell** (if needed) → **Enable SSH**.
3. Exit. SSH should listen on TCP 22.

### Option C — From an existing shell on the host

If Shell/SSH is already available:

```bash
vim-cmd hostsvc/enable_ssh
vim-cmd hostsvc/start_ssh
# optional: keep SSH running after reboot
vim-cmd hostsvc/enable_ssh
chkconfig SSH on
```

Verify from the panel server:

```bash
nc -vz YOUR_ESXI_IP 22
# or: timeout 3 bash -c 'echo >/dev/tcp/YOUR_ESXI_IP/22' && echo open
```

## Quick start

### 1. Configure

```bash
cp .env.example .env
# Edit .env: ESXI_HOST, ESXI_USER, ESXI_PASSWORD, ports
```

Confirm **TSM-SSH** is running on ESXi, then fetch the SSH host key into `known_hosts` (required; StrictHostKeyChecking is on):

```bash
ssh-keyscan -t rsa,ecdsa,ed25519 YOUR_ESXI_IP > known_hosts
```

### 2. Run with Docker Compose (online build)

```bash
docker compose up -d --build
```

Open `http://SERVER_IP:8080` (or the port you mapped).

If the build fails because Docker Hub is slow or blocked, use the mirror fallback below, then run `docker compose up -d --build` again.

### 3. Check

```bash
docker ps --filter name=vm-panel
curl -sS http://127.0.0.1:8080/api/vms
```

## If Docker Hub fails — use mirrors

Typical errors: `connection reset by peer`, `TLS handshake timeout`, `failed to copy`, or hanging on `FROM python:3.12-slim-bookworm`.

You need **two** mirrors:

| What fails | File to create / change | Purpose |
|------------|-------------------------|---------|
| Pulling the base image (`python:3.12-slim-bookworm`) | **`/etc/docker/daemon.json`** on the **host** (not in this repo) | Docker registry mirror for Docker Hub |
| `apt-get` inside the build (`openssh-client`) | **`Dockerfile`** in this project | Debian apt mirror during `docker build` |

`docker-compose.yml`, `.env`, and `app.py` do **not** need mirror changes.

### Step 1 — Docker Hub registry mirror (host)

Create or edit `/etc/docker/daemon.json` (backup first if it already exists):

```json
{
  "registry-mirrors": [
    "https://docker.arvancloud.ir",
    "https://hub.hamdocker.ir"
  ]
}
```

Apply and verify:

```bash
sudo systemctl restart docker
docker info | grep -A5 "Registry Mirrors"
```

Docker will try these mirrors when Hub is unreachable. No project file change is required for this step.

### Step 2 — Debian apt mirror (Dockerfile)

This repo’s `Dockerfile` already rewrites apt sources to `mirror.iranserver.com` before `apt-get install openssh-client`.

If you need another apt mirror, edit **only** the `sed` lines at the top of `Dockerfile`:

```dockerfile
RUN sed -i 's|deb.debian.org|mirror.iranserver.com|g; s|security.debian.org|mirror.iranserver.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirror.iranserver.com|g; s|security.debian.org|mirror.iranserver.com|g' /etc/apt/sources.list 2>/dev/null || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 ...
```

Replace `mirror.iranserver.com` with another reachable Debian mirror if needed.

### Step 3 — Rebuild the app

```bash
docker compose down
docker compose up -d --build
docker ps --filter name=vm-panel
curl -sS http://127.0.0.1:8080/api/vms
```

### Still blocked?

Use the offline build (`build-offline.sh`) in the next section — it does not pull from Docker Hub.

## Configuration

| Variable        | Description                                      | Example        |
|-----------------|--------------------------------------------------|----------------|
| `ESXI_HOST`     | ESXi hostname or IP                              | `192.168.1.10` |
| `ESXI_PORT`     | SSH port                                         | `22`           |
| `ESXI_USER`     | SSH user with `vim-cmd` / `esxcli`               | `root`         |
| `ESXI_PASSWORD` | SSH password (quote if it contains `#`)          | `"secret"`     |
| `APP_HOST`      | Bind address inside the app                      | `0.0.0.0`      |
| `APP_PORT`      | Listen port                                      | `8080`         |

`.env` and `known_hosts` are **mounted** into the container. They are not baked into the image. Do not commit them.

Layout:

```text
esxi-vm-panel/
  app.py
  templates/index.html
  .env.example      # publish this
  .env              # local only — gitignored
  known_hosts       # local only — gitignored
  data/             # lock + short-lived askpass files
  Dockerfile        # apt mirror for build (Iran-friendly by default)
  docker-compose.yml
  build-offline.sh  # optional: air-gapped image build (no Docker Hub)

Host only (not in git):
  /etc/docker/daemon.json   # registry-mirrors when Docker Hub fails
```

## Offline / air-gapped build

If the server cannot pull from Docker Hub, use the host Python and OpenSSH to build a local image:

```bash
# Place the project under /opt/app (or adjust paths in build-offline.sh)
sudo bash build-offline.sh
sudo docker rm -f vm-panel
sudo docker run -d --name vm-panel --restart unless-stopped \
  -p 8080:8080 \
  -v "$(pwd)/.env:/opt/app/.env:ro" \
  -v "$(pwd)/data:/opt/app/data" \
  -v "$(pwd)/known_hosts:/opt/app/known_hosts:ro" \
  vm-panel:local
```

After editing `app.py` or the template on an offline host, rebuild with `build-offline.sh` and recreate the container.

## How it works

1. The browser loads the panel and calls `/api/vms`.
2. The app opens **one SSH session** to ESXi and runs:
   - `vim-cmd vmsvc/getallvms`
   - `esxcli vm process list`  
   A VM that appears in the process list is treated as powered on.
3. **Power on** runs `vim-cmd vmsvc/power.on <vmid>`, waits until the VM is powered on, then polls `vim-cmd vmsvc/get.guest` until VMware Tools is running (or times out). Progress is streamed to the browser as NDJSON (`/api/power` with `stream: true`) and shown as a progress bar.
4. **Shut down** prefers `vim-cmd vmsvc/power.shutdown <vmid>`. If Tools is not available, the panel asks for confirmation, then runs `power.off` only if you accept.
5. Power changes use a file lock under `data/` for the power command; the long Tools wait runs after the lock is released so other VMs stay controllable. SSH passwords are passed with a short-lived askpass script under `data/` that is deleted after each command.

The panel does **not** change VM settings, disks, or snapshots.

## API

| Method | Path          | Body                         | Purpose        |
|--------|---------------|------------------------------|----------------|
| `GET`  | `/`           | —                            | HTML UI        |
| `GET`  | `/api/vms`    | —                            | List VMs       |
| `GET`  | `/api/guest`  | `?vmid=N`                    | Power + Tools/guest readiness |
| `POST` | `/api/power`  | `{"vmid":N,"action":"on\|off","stream":true}` | Power on/off (NDJSON progress when streaming) |
| `POST` | `/api/select` | `{"vmid":N}`                 | Power on (alias) |

Progress stages for power on: `starting` → `power_command` → `powered_on` → `waiting_tools` → `tools_running` / `guest_ready` (100%). If Tools never becomes ready, the bar stops near 85% with a timeout message; the VM may still be booting.

## Security notes

- Keep `.env` and `known_hosts` private; never push them to a public repo.
- Prefer a dedicated ESXi user with only the rights you need.
- Bind the panel to a trusted network or put it behind reverse proxy / VPN / auth.
- Rotate the ESXi password if it was ever committed or shared.

## License

Use and adapt as you need for your own infrastructure.
