# ESXi VM Panel

A small web panel for **one** VMware ESXi host. It lists every virtual machine and lets you **power on** or **shut down** a VM one at a time. Other VMs are left unchanged.

No vSphere / vCenter client required — only SSH to ESXi and a browser.

## Features

- List all VMs with power state, guest type, and datastore
- Search by name, guest OS type, or VM id
- Power on a single VM
- Guest shutdown via VMware Tools (`power.shutdown`); if Tools is missing, falls back to hard power off
- Dark / light theme (remembered in the browser)
- Concurrent power actions without freezing the UI
- Strict allowlist of ESXi commands (`power.on`, `power.shutdown`, `power.off`, `power.getstate` + list)

## Requirements

- A Linux host that can reach the ESXi SSH port (usually TCP 22)
- Docker Engine (recommended), **or** Python 3 + OpenSSH client
- An ESXi account that can run `vim-cmd` and `esxcli`
- VMware Tools inside the guest for a clean shutdown (optional but recommended)

## Quick start

### 1. Configure

```bash
cp .env.example .env
# Edit .env: ESXI_HOST, ESXI_USER, ESXI_PASSWORD, ports
```

Fetch the ESXi SSH host key into `known_hosts` (required; StrictHostKeyChecking is on):

```bash
ssh-keyscan -t rsa YOUR_ESXI_IP > known_hosts
```

### 2. Run with Docker Compose (online build)

```bash
docker compose up -d --build
```

Open `http://SERVER_IP:8080` (or the port you mapped).

### 3. Check

```bash
docker ps --filter name=vm-panel
curl -sS http://127.0.0.1:8080/api/vms
```

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
  Dockerfile
  docker-compose.yml
  build-offline.sh  # optional: air-gapped image build
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
3. **Power on** runs `vim-cmd vmsvc/power.on <vmid>` and waits until state is powered on.
4. **Shut down** prefers `vim-cmd vmsvc/power.shutdown <vmid>`. If Tools is not available, it uses `power.off` and reports that.
5. Power changes use a file lock under `data/` so concurrent actions stay orderly. SSH passwords are passed with a short-lived askpass script under `data/` that is deleted after each command.

The panel does **not** change VM settings, disks, or snapshots.

## API

| Method | Path          | Body                         | Purpose        |
|--------|---------------|------------------------------|----------------|
| `GET`  | `/`           | —                            | HTML UI        |
| `GET`  | `/api/vms`    | —                            | List VMs       |
| `POST` | `/api/power`  | `{"vmid":N,"action":"on\|off"}` | Power on/off |
| `POST` | `/api/select` | `{"vmid":N}`                 | Power on (alias) |

## Security notes

- Keep `.env` and `known_hosts` private; never push them to a public repo.
- Prefer a dedicated ESXi user with only the rights you need.
- Bind the panel to a trusted network or put it behind reverse proxy / VPN / auth.
- Rotate the ESXi password if it was ever committed or shared.

## License

Use and adapt as you need for your own infrastructure.
