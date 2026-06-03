"""
Threshold engine — the product's core.

Evaluates each incoming reading against the tenant's limits and manages alert
lifecycle with hysteresis + cooldown so a probe hovering at the limit can't
spam the customer:

  - OPEN  when value crosses the limit and no alert of that kind is open.
  - keep open (re-notify only after ALERT_COOLDOWN_S) while it stays over.
  - CLEAR once value falls HYSTERESIS_C below the limit.

Two breach kinds on ingest:
  - high_temp : hottest probe of a sensor >= high_c.
  - delta     : (exhaust - intake) for a server unit >= delta_c.

A separate stale-sensor watchdog (see app.py) opens 'stale' alerts for mapped
sensors that stop reporting — a dead sensor is as dangerous as a hot one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from db import Alert, Reading, SensorMap, Threshold, now
from notifications import notify_email, notify_sms


# --------------------------------------------------------------------------- #
# Threshold resolution: port override > rack override > tenant default > env   #
# --------------------------------------------------------------------------- #

def resolve_threshold(db: Session, tenant_id: str, sm: SensorMap | None) -> tuple[float, float, bool]:
    """Return (high_c, delta_c, enabled) for a sensor, most-specific first."""
    keys: list[tuple[str, str]] = []
    if sm:
        if sm.port_id:
            keys.append(("port", sm.port_id))
        if sm.rack_id:
            keys.append(("rack", sm.rack_id))
    keys.append(("tenant", ""))

    for scope, scope_id in keys:
        t = db.scalar(
            select(Threshold).where(
                Threshold.tenant_id == tenant_id,
                Threshold.scope == scope,
                Threshold.scope_id == scope_id,
            )
        )
        if t:
            return t.high_c, t.delta_c, t.enabled
    return config.DEFAULT_HIGH_C, config.DEFAULT_DELTA_C, True


# --------------------------------------------------------------------------- #
# Alert lifecycle                                                              #
# --------------------------------------------------------------------------- #

def _open_alert(db: Session, tenant_id: str, alert_recipients,
                key: str, kind: str, location: str, value: float, threshold: float) -> None:
    """Open (or re-notify) an alert. `key` is what makes an alert unique per
    kind: a sensor EUI for high_temp/stale, or "unit:<id>" for delta."""
    existing = db.scalar(
        select(Alert).where(
            Alert.tenant_id == tenant_id, Alert.eui == key,
            Alert.kind == kind, Alert.state.in_(("open", "acked")),
        )
    )
    t = now()
    if existing is None:
        alert = Alert(
            tenant_id=tenant_id, eui=key, location=location, kind=kind,
            state="open", value=value, threshold=threshold,
            opened_at=t, last_notified_at=t,
        )
        db.add(alert)
        db.commit()
        _dispatch(alert_recipients, kind, location, value, threshold, opened=True)
    else:
        existing.value = value
        if t - existing.last_notified_at >= config.ALERT_COOLDOWN_S:
            existing.last_notified_at = t
            db.commit()
            _dispatch(alert_recipients, kind, location, value, threshold, opened=False)
        else:
            db.commit()


def _clear_alert(db: Session, tenant_id: str, key: str, kind: str) -> None:
    existing = db.scalar(
        select(Alert).where(
            Alert.tenant_id == tenant_id, Alert.eui == key,
            Alert.kind == kind, Alert.state.in_(("open", "acked")),
        )
    )
    if existing:
        existing.state = "cleared"
        existing.cleared_at = now()
        db.commit()


def _dispatch(recipients, kind: str, location: str, value: float,
              threshold: float, opened: bool) -> None:
    """recipients: (emails: list[str], phones: list[str]) for the tenant."""
    emails, phones = recipients
    label = {"high_temp": "HIGH TEMPERATURE", "delta": "HIGH ΔT", "stale": "SENSOR OFFLINE"}.get(kind, kind)
    verb = "ALERT" if opened else "REMINDER"
    subject = f"[HVAC {verb}] {label} — {location}"
    if kind == "stale":
        body = f"{location} has stopped reporting.\nNo reading for over {config.STALE_AFTER_S:.0f}s."
    else:
        body = (f"{location}\n{label}: {value:.1f}°C "
                f"(limit {threshold:.1f}°C).")
    notify_email(emails, subject, body)
    notify_sms(phones, f"{subject}\n{body}")


# --------------------------------------------------------------------------- #
# Per-reading evaluation (called from the ingest handler)                      #
# --------------------------------------------------------------------------- #

def _latest_max(db: Session, tenant_id: str, eui: str) -> float | None:
    r = db.scalar(
        select(Reading).where(Reading.tenant_id == tenant_id, Reading.eui == eui)
        .order_by(Reading.ts.desc()).limit(1)
    )
    return r.max_c if r else None


def evaluate_reading(db: Session, tenant_id: str, eui: str, max_c: float,
                     recipients) -> None:
    """Run high-temp and ΔT checks for a freshly-stored reading."""
    sm = db.scalar(
        select(SensorMap).where(SensorMap.tenant_id == tenant_id, SensorMap.eui == eui)
    )
    high_c, delta_c, enabled = resolve_threshold(db, tenant_id, sm)
    if not enabled:
        return

    location = sm.label if (sm and sm.label) else f"sensor {eui}"

    # --- high temperature ---
    if max_c >= high_c:
        _open_alert(db, tenant_id, recipients, eui, "high_temp", location, max_c, high_c)
    elif max_c <= high_c - config.HYSTERESIS_C:
        _clear_alert(db, tenant_id, eui, "high_temp")

    # --- delta (exhaust - intake) per unit ---
    if sm and sm.unit_id:
        _evaluate_delta(db, tenant_id, sm.unit_id, delta_c, recipients)


def _evaluate_delta(db: Session, tenant_id: str, unit_id: str,
                    delta_c: float, recipients) -> None:
    members = db.scalars(
        select(SensorMap).where(SensorMap.tenant_id == tenant_id, SensorMap.unit_id == unit_id)
    ).all()
    intake = [m for m in members if m.slot == "A"]
    exhaust = [m for m in members if m.slot == "B"]
    if not intake or not exhaust:
        return  # need both sides to compute a delta

    intake_t = _hottest([_latest_max(db, tenant_id, m.eui) for m in intake])
    exhaust_t = _hottest([_latest_max(db, tenant_id, m.eui) for m in exhaust])
    if intake_t is None or exhaust_t is None:
        return

    delta = exhaust_t - intake_t
    key = f"unit:{unit_id}"
    location = _unit_label(intake + exhaust)
    if delta >= delta_c:
        _open_alert(db, tenant_id, recipients, key, "delta", location, delta, delta_c)
    elif delta <= delta_c - config.HYSTERESIS_C:
        _clear_alert(db, tenant_id, key, "delta")


def _hottest(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _unit_label(members: list[SensorMap]) -> str:
    """Best-effort 'Rack / Unit' label from a member's 'Rack / Unit / Port' label."""
    for m in members:
        if m.label:
            parts = [p.strip() for p in m.label.split("/")]
            if len(parts) >= 2:
                return " / ".join(parts[:2])
            return m.label
    return "unit"
