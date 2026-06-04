# Local / on-prem setup — no AWS

Run the whole product on a **PC or a small node at the rack** (a mini-PC or
Raspberry Pi — the ESP32 gateway itself can't run the server). No RDS, SES, SNS,
or ALB required: SQLite for storage, SMTP for email, plain HTTP on the LAN.

> The "master node" must be a real computer with Python. Pick one box on the
> same LAN as the gateways and phones; note its LAN IP (`<NODE-IP>`) — that's
> what gateways and the app point at.

---

## 1. Configure (`/etc/hvac-cloud.env`, or set env vars)
Everything has a safe default; the only thing you really need for a *usable*
deployment is **email** (so password-reset codes and alerts actually arrive).

```bash
ENV=onprem                       # enforce strong secrets, allow SQLite + HTTP
JWT_SECRET=<64 random chars>     # openssl rand -hex 48   (REQUIRED in onprem)
BOOTSTRAP_TOKEN=<your-token>     # REQUIRED; used once to create the org/admin
DATABASE_URL=sqlite:///cloud.db  # fine for a single site

# --- email via SMTP (pick one) ---
# Gmail (use a Google App Password, not your login password):
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=youraccount@gmail.com
SMTP_PASS=<app-password>
MAIL_FROM=youraccount@gmail.com
# Office 365:  SMTP_HOST=smtp.office365.com  SMTP_PORT=587
# LAN relay:   SMTP_HOST=<relay-ip> SMTP_PORT=25 SMTP_STARTTLS=0  (no user/pass)
```
Leave `SMTP_HOST` empty to keep email **log-only** (fine for testing — the OTP
code prints to the server console).

## 2. Run the servers
You need the **Cloud Server** (alerts/accounts) and the **display node** (LAN 3D
dashboard). The discovery server is optional on a single site — you can provision
the gateway with the node URL directly instead.

**Windows (PowerShell):**
```powershell
cd "C:\path\to\Cloud Server"
$env:ENV="onprem"; $env:JWT_SECRET="...."; $env:BOOTSTRAP_TOKEN="...."
$env:SMTP_HOST="smtp.gmail.com"; $env:SMTP_USER="..."; $env:SMTP_PASS="..."
conda run -n alpr_dev python -m uvicorn app:app --host 0.0.0.0 --port 8002
```

**Linux / Raspberry Pi:** put the vars in `/etc/hvac-cloud.env` and use the
provided service unit:
```bash
sudo cp deploy/hvac-cloud.service /etc/systemd/system/
sudo systemctl enable --now hvac-cloud      # ExecStartPre runs alembic upgrade head
```

## 3. Survive reboot (run as a service)
- **Linux/Pi:** the `systemd` unit above already does this.
- **Windows:** wrap uvicorn with **NSSM** (`nssm install HVACCloud ...`) or a
  **Task Scheduler** task set to "run at startup". Same for `display_node.py`.

## 4. Point the gateway + app at the node
- **App:** cloud URL = `http://<NODE-IP>:8002`. Create the org with your
  `BOOTSTRAP_TOKEN`.
- **Gateway (Router Setup):** `cloud = http://<NODE-IP>:8002`,
  `disc = http://<NODE-IP>:8000` (or skip discovery and let it use the node
  directly), mint the API key.

## 5. Storage & backups
SQLite lives in `cloud.db` next to the app. Back up that single file (a nightly
copy / the node's own backup). To migrate to Postgres later, just set
`DATABASE_URL` and run `alembic upgrade head` — no code change.

## HTTPS (optional on a trusted LAN)
Plain HTTP is acceptable inside a closed LAN. If you want TLS, run a self-signed
cert behind `deploy/nginx.conf`; the C3 gateway already speaks TLS to `https://`
URLs via `setInsecure()` (no CA needed for self-signed) — see `SECURITY.md`.

## What you DON'T need locally
RDS/Postgres, SES, SNS, ALB, a domain, certbot, IAM roles. Those are only for the
AWS deployment in `AWS_SETUP.md`.
