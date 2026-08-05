# Local / on-prem setup — no AWS

Run the whole product on a **PC or a small node at the rack** (a mini-PC or
Raspberry Pi — the ESP32 gateway itself can't run the server). No RDS, SES, SNS,
or ALB required: SQLite for storage, SMTP for email, plain HTTP on the LAN.

This is a real **production** deployment, just not on AWS — `ENV=onprem` still
enforces strong secrets (see `config.py`'s `validate_startup()`), it just
allows SQLite + HTTP since a trusted LAN doesn't need Postgres/TLS/CORS
allowlists.

> The "master node" must be a real computer with Python. Pick one box on the
> same LAN as the gateways and phones; note its LAN IP (`<NODE-IP>`) — that's
> what gateways and the app point at.

These steps assume you've **already cloned the repo somewhere** (your home
directory, `/srv`, wherever — it does *not* need to be `/opt`). Everything
below is written to work from whatever path you cloned into.

---

## 0. Note your paths

```bash
cd /path/to/param-hvac-server/cloud-server   # wherever you cloned it
APP_DIR=$(pwd)
echo $APP_DIR
```
You'll reuse `$APP_DIR` in a couple of the steps below (shell variables don't
survive between separate terminal sessions — re-set it, or just substitute
the real path by hand each time).

## 1. Python environment

```bash
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
```
Run `pip install` **from inside `cloud-server/`** (i.e. `$APP_DIR`) — that's
where `requirements.txt` actually lives, not the repo root.

## 2. Create a system user to run the service (recommended)

```bash
sudo useradd --system --shell /usr/sbin/nologin hvac
sudo chown -R hvac:hvac "$APP_DIR"
```
`chown` here covers the venv you just created and the SQLite DB file the app
will create on first run.

## 3. Configure — `/etc/hvac-cloud.env`

Everything has a safe default; the only thing you really need for a *usable*
deployment is **email** (so password-reset codes and alerts actually arrive).

```bash
sudo tee /etc/hvac-cloud.env <<'EOF'
ENV=onprem                       # enforce strong secrets, allow SQLite + HTTP
JWT_SECRET=REPLACE_ME            # openssl rand -hex 48
BOOTSTRAP_TOKEN=REPLACE_ME       # used once to create the org/admin, then rotate/blank it
DATABASE_URL=sqlite:///cloud.db  # relative to WorkingDirectory — fine for a single site

# --- email via SMTP (pick one) ---
# Gmail (use a Google App Password, not your login password):
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=youraccount@gmail.com
SMTP_PASS=REPLACE_ME
MAIL_FROM=youraccount@gmail.com
# Office 365:  SMTP_HOST=smtp.office365.com  SMTP_PORT=587
# LAN relay:   SMTP_HOST=<relay-ip> SMTP_PORT=25 SMTP_STARTTLS=0  (no user/pass)
EOF
sudo chmod 600 /etc/hvac-cloud.env
```
Leave `SMTP_HOST` empty to keep email **log-only** (fine for testing — the OTP
code prints to the server console instead of sending).

Generate the two required secrets instead of typing your own:
```bash
openssl rand -hex 48   # -> JWT_SECRET
openssl rand -hex 16   # -> BOOTSTRAP_TOKEN
```

## 4. Install the systemd service

The template in `deploy/hvac-cloud.service` uses `__APP_DIR__` / `__VENV_DIR__`
placeholders — substitute your real path and install it:

```bash
cd "$APP_DIR/deploy"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__VENV_DIR__#$APP_DIR/venv#g" \
    hvac-cloud.service | sudo tee /etc/systemd/system/hvac-cloud.service
sudo systemctl daemon-reload
sudo systemctl enable --now hvac-cloud
```
`ExecStartPre` runs `alembic upgrade head` automatically before every start —
migrations are applied on every deploy/restart with no extra step from you.

## 5. Firewall — LAN only

Since this box only needs to answer devices on your own network, restrict it:
```bash
sudo ufw allow from 192.168.1.0/24 to any port 8002 proto tcp   # adjust to your subnet
sudo ufw enable
```

## 6. Verify

```bash
sudo systemctl status hvac-cloud
journalctl -u hvac-cloud -f              # confirm alembic ran clean, then app started
curl http://127.0.0.1:8002/health        # {"ok":true}
```
Then from another device on the LAN:
```bash
curl http://<NODE-IP>:8002/health
```

## 7. Rotate `BOOTSTRAP_TOKEN` after first use

It's a shared secret that lets anyone create a new tenant/admin — once you've
created your org through the app, disable it:
```bash
sudo sed -i 's/^BOOTSTRAP_TOKEN=.*/BOOTSTRAP_TOKEN=/' /etc/hvac-cloud.env
sudo systemctl restart hvac-cloud
```
Empty disables self-registration entirely; re-set it temporarily only if you
need to add another tenant later.

## 8. Point the gateway + app at the node

- **App:** cloud URL = `http://<NODE-IP>:8002`. Create the org with your
  `BOOTSTRAP_TOKEN` (before you rotate it away in step 7).
- **Gateway (Router Setup):** `cloud = http://<NODE-IP>:8002`, mint the API key —
  that's it. **Leave `disc` blank:** discovery is served by this same server at
  `/discovery`, and the gateway derives `http://<NODE-IP>:8002/discovery` from the
  cloud URL by itself. Only fill `disc` in if you deliberately run the standalone
  `discovery-server` on its own port.

## 9. Storage & backups

SQLite lives at `$APP_DIR/cloud.db`. Automate a daily backup rather than
relying on manual copies:
```bash
sudo mkdir -p /var/backups/hvac
sudo tee /etc/cron.daily/hvac-backup <<EOF
#!/bin/sh
sqlite3 "$APP_DIR/cloud.db" ".backup /var/backups/hvac/cloud-\$(date +%Y%m%d).db"
find /var/backups/hvac -mtime +14 -delete
EOF
sudo chmod +x /etc/cron.daily/hvac-backup
```
To migrate to Postgres later, just change `DATABASE_URL` and run
`alembic upgrade head` — no code change needed.

## HTTPS (optional on a trusted LAN)

Plain HTTP is acceptable inside a closed LAN. If you want TLS anyway, run a
self-signed cert behind `deploy/nginx.conf` (and switch `ExecStart`'s
`--host` back to `127.0.0.1` in the service file, since Nginx becomes the
only thing exposed) — the C3 gateway already speaks TLS to `https://` URLs
via `setInsecure()` (no CA needed for self-signed). See `SECURITY.md`.

## What you DON'T need locally

RDS/Postgres, SES, SNS, ALB, a domain, certbot, IAM roles. Those are only for
the AWS deployment in `AWS_SETUP.md`.
