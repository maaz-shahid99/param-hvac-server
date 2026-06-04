# AWS production setup — Cloud Server

The code is production-ready; these are the **AWS-account actions** that can't be
done from the repo. Do them in order. Everything is driven by env vars (see
`../.env.example`), supplied in production via `/etc/hvac-cloud.env` (read by
`hvac-cloud.service`).

---

## 1. RDS Postgres (Blocker #2 — ops side)
1. Create an **RDS PostgreSQL** instance (Multi-AZ for prod), in a private subnet;
   allow inbound 5432 only from the app's security group.
2. Set the connection string:
   ```
   DATABASE_URL=postgresql+psycopg://USER:PASS@your-db.xxxx.rds.amazonaws.com:5432/hvac
   ```
3. Apply the schema **before** starting the app (the systemd unit does this via
   `ExecStartPre`):
   ```bash
   alembic upgrade head
   ```
4. Enable automated backups + a retention window.

## 2. TLS / domain (Blocker #1 — ops side)
The C3 firmware and app already speak `https://`. You just need a cert:

**Option A — Nginx + Let's Encrypt** (single EC2):
```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp deploy/nginx.conf /etc/nginx/sites-available/hvac && sudo ln -s ... && sudo nginx -t
sudo certbot --nginx -d api.yourdomain.com        # auto-installs + renews the cert
```
**Option B — ALB + ACM** (scales horizontally): request an ACM cert for
`api.yourdomain.com`, attach to an HTTPS listener, target group → instances `:8002`.
Skip Nginx; uvicorn still binds `127.0.0.1:8002` per instance.

Then both the **app cloud URL** and the gateway **PROVISION `cloud`** field are
`https://api.yourdomain.com` (no port). If you pin a CA in `Bridge.ino`
(`CLOUD_ROOT_CA`) instead of `setInsecure()`, the gateway must also have correct
time — add an NTP sync (or use its DS1307 RTC) so cert validity checks pass.

## 3. SES email — out of sandbox + domain auth (Blocker #3)
Without this, OTP and alert emails are **logged, not sent** (and even once sent,
unauthenticated mail lands in spam). All AWS-console / DNS work:

1. **Verify your sending domain** in SES (Configuration → Identities → Create
   identity → Domain → `yourdomain.com`). SES gives you DKIM CNAME records.
2. **Add DNS records** at your DNS host:
   - the 3 **DKIM** CNAMEs SES generated,
   - an **SPF** TXT on the sending subdomain: `v=spf1 include:amazonses.com -all`,
   - a **DMARC** TXT at `_dmarc.yourdomain.com`: `v=DMARC1; p=quarantine; rua=mailto:dmarc@yourdomain.com`.
3. **Request production access** (SES → Account dashboard → *Request production
   access*). Until granted, SES is in **sandbox** and can only send to verified
   addresses. Describe the use case (transactional: password-reset codes +
   overheat alerts) and expected volume.
4. Set env:
   ```
   SES_FROM=alerts@yourdomain.com      # must be on the verified domain
   AWS_REGION=us-east-1                # the region where the identity is verified
   ```
5. Give the instance an **IAM role** allowing `ses:SendEmail` (don't bake keys).
6. Test: trigger a password reset in the app → a real email should arrive;
   `notifications.py` logs `[email:sent]` instead of `[email:skipped]`.

## 4. SNS SMS (optional)
1. Move SNS SMS out of its sandbox (verify destination numbers first, then
   request production), set spend limit + sender ID.
2. IAM role: allow `sns:Publish`. Env: `SNS_SMS_ENABLED=1`.
3. Alert recipients' phones are set per-tenant via `PUT /v1/recipients`.

## 5. App secrets (do not ship the dev defaults)
```
JWT_SECRET=<64+ random chars>           # openssl rand -hex 48
BOOTSTRAP_TOKEN=<random>                 # or leave empty to disable self-register
```

## Deploy order (every release)
1. `git pull` to `/opt/hvac`.
2. `pip install -r requirements.txt` into the venv.
3. `alembic upgrade head` (the systemd `ExecStartPre` does this automatically).
4. `systemctl restart hvac-cloud`.
5. Smoke check: `curl https://api.yourdomain.com/health` → `{"ok":true}`.
