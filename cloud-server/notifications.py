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
        return
    if _send_ses(recipients, subject, body):
        return
    if _send_smtp(recipients, subject, body):
        return
    print(f"[email:skipped] {subject} -> {recipients}\n{body}")


def notify_sms(numbers: list[str], message: str) -> None:
    """Best-effort SNS SMS send. Never raises into the caller."""
    nums = [n.strip() for n in numbers if n and n.strip()]
    if not nums:
        return
    client = _sns_client()
    if client is None or not config.SNS_SMS_ENABLED:
        print(f"[sms:skipped] -> {nums}: {message}")
        return
    for number in nums:
        try:
            client.publish(PhoneNumber=number, Message=message)
            print(f"[sms:sent] -> {number}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sms:error] {number}: {exc}")
