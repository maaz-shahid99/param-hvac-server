"""
Notification dispatch — email + SMS.

Email delivery is tried in order, so the same code runs on AWS and on a local /
on-prem box:
    SES (if SES_FROM set)  ->  SMTP (if SMTP_HOST set)  ->  log only.
SMS uses SNS if enabled, else logs. If nothing is configured, messages are
printed rather than sent, so the whole pipeline is testable with no AWS.

Mobile push (SNS platform endpoints / FCM) is intentionally deferred to Phase 2;
the dispatch surface here (notify_email / notify_sms) is where it will slot in.
"""

from __future__ import annotations

import smtplib
import urllib.parse
import urllib.request
from base64 import b64encode
from email.message import EmailMessage

import config

try:
    import boto3  # type: ignore
except ImportError:  # boto3 optional for local dev
    boto3 = None  # type: ignore

_ses = None
_sns = None


def _ses_client():
    global _ses
    if _ses is None and boto3 is not None:
        _ses = boto3.client("ses", region_name=config.AWS_REGION)
    return _ses


def _sns_client():
    global _sns
    if _sns is None and boto3 is not None:
        _sns = boto3.client("sns", region_name=config.AWS_REGION)
    return _sns


def _send_ses(recipients: list[str], subject: str, body: str) -> bool:
    client = _ses_client()
    if client is None or not config.SES_FROM:
        return False
    try:
        client.send_email(
            Source=config.SES_FROM,
            Destination={"ToAddresses": recipients},
            Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
        )
        print(f"[email:sent:ses] {subject} -> {recipients}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[email:error:ses] {exc}")
        return False


def _send_smtp(recipients: list[str], subject: str, body: str) -> bool:
    """Plain SMTP send (Gmail app-password / Office 365 / LAN relay). Lets a
    local deployment send real email with no AWS."""
    if not config.SMTP_HOST:
        return False
    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
            if config.SMTP_STARTTLS:
                smtp.starttls()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASS)
            smtp.send_message(msg)
        print(f"[email:sent:smtp] {subject} -> {recipients}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[email:error:smtp] {exc}")
        return False


def notify_email(to: list[str], subject: str, body: str) -> None:
    """Best-effort send via SES, then SMTP, then log. Never raises."""
    recipients = [r.strip() for r in to if r and r.strip()]
    if not recipients:
        # Say so. An org can genuinely end up with nobody opted in — an admin
        # who was the only recipient leaves or is removed, and the remaining
        # members all have email_enabled=false. Returning quietly meant the
        # alert fired, was stored, and vanished with no trace anywhere.
        print(f"[email:no-recipients] {subject} -> nobody is opted in to receive alerts")
        return
    if _send_ses(recipients, subject, body):
        return
    if _send_smtp(recipients, subject, body):
        return
    print(f"[email:skipped] {subject} -> {recipients}\n{body}")


def _send_sns(number: str, message: str) -> bool:
    client = _sns_client()
    if client is None or not config.SNS_SMS_ENABLED:
        return False
    try:
        client.publish(PhoneNumber=number, Message=message)
        print(f"[sms:sent:sns] -> {number}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[sms:error:sns] {number}: {exc}")
        return False


def _send_twilio(number: str, message: str) -> bool:
    """Send one SMS via Twilio's REST API (stdlib only — no SDK dependency)."""
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM):
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode(
        {"From": config.TWILIO_FROM, "To": number, "Body": message}
    ).encode()
    auth = b64encode(
        f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
        print(f"[sms:{'sent' if ok else 'error'}:twilio] -> {number}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"[sms:error:twilio] {number}: {exc}")
        return False


def notify_sms(numbers: list[str], message: str) -> None:
    """Best-effort SMS via SNS, then Twilio, then log. Never raises."""
    nums = [n.strip() for n in numbers if n and n.strip()]
    if not nums:
        return
    for number in nums:
        if _send_sns(number, message):
            continue
        if _send_twilio(number, message):
            continue
        print(f"[sms:skipped] -> {number}: {message}")
