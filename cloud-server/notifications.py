"""
Notification dispatch — AWS SES (email) and SNS (SMS).

Mirrors the discovery server's "log instead of send when unconfigured" idiom:
if boto3 is missing or SES_FROM / SNS are not configured, messages are printed
rather than sent, so the whole alert pipeline is testable locally with no AWS.

Mobile push (SNS platform endpoints / FCM) is intentionally deferred to Phase 2;
the dispatch surface here (notify_email / notify_sms) is where it will slot in.
"""

from __future__ import annotations

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


def notify_email(to: list[str], subject: str, body: str) -> None:
    """Best-effort SES send. Never raises into the caller."""
    recipients = [r.strip() for r in to if r and r.strip()]
    if not recipients:
        return
    client = _ses_client()
    if client is None or not config.SES_FROM:
        print(f"[email:skipped] {subject} -> {recipients}\n{body}")
        return
    try:
        client.send_email(
            Source=config.SES_FROM,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        print(f"[email:sent] {subject} -> {recipients}")
    except Exception as exc:  # noqa: BLE001
        print(f"[email:error] {exc}")


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
