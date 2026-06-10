"""Quick SMTP sanity check — sends one email through the SAME path the server uses
(SES -> SMTP -> log), so you can verify Gmail/Workspace config without waiting for
a real alert.

    python send_test_email.py [recipient@example.com]

Reads the .env next to config.py. Watch the printed `[email:sent:smtp]` (good) or
`[email:error:smtp] ...` (shows the exact SMTP error) line.
"""
import sys

import config
from notifications import notify_email

to = sys.argv[1] if len(sys.argv) > 1 else config.SMTP_USER
print(f"SMTP_HOST={config.SMTP_HOST!r}  PORT={config.SMTP_PORT}  STARTTLS={config.SMTP_STARTTLS}")
print(f"SMTP_USER={config.SMTP_USER!r}  MAIL_FROM={config.MAIL_FROM!r}")
if not config.SMTP_HOST:
    print("\nSMTP_HOST is empty — set the SMTP_* values in .env first (see .env.example).")
    sys.exit(1)
if not to:
    print("\nNo recipient — pass one: python send_test_email.py you@example.com")
    sys.exit(1)

print(f"\nSending a test email to {to} ...")
notify_email([to], "HVAC Cloud — test email", "If you received this, email alerts are working. ✅")
print("Done — check the [email:...] line above and the recipient inbox.")
