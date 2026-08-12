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

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from db import Alert, Reading, SensorMap, Tenant, Threshold, now
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


def _clear_alert(db: Session, tenant_id: str, key: str, kind: str,
                 recipients=None, location: str = "") -> None:
    """Clear an open/acked alert. For an 'offline' (stale) alert, passing
    `recipients` also sends a BACK-ONLINE recovery email (online/offline pairing).
    Other kinds clear silently, as before."""
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
        if kind == "stale" and recipients is not None:
            _dispatch(recipients, kind, location or existing.location, 0.0, 0.0,
                      opened=False, recovered=True)


def _dispatch(recipients, kind: str, location: str, value: float,
              threshold: float, opened: bool, recovered: bool = False) -> None:
    """recipients: (emails: list[str], phones: list[str]) for the tenant.
    opened=new alert · recovered=back-to-normal (offline->online) · else a
    reminder while the condition persists."""
    emails, phones = recipients
    if recovered:
        # Only 'stale' (offline) currently sends a recovery/back-online notice.
        subject = f"[HVAC RECOVERED] BACK ONLINE — {location}"
        body = f"{location} is reporting again."
    else:
        # 'stale' covers both sensors and routers, so keep the label generic.
        label = {"high_temp": "HIGH TEMPERATURE", "delta": "HIGH ΔT", "stale": "OFFLINE",
                 "hum_high": "HIGH HUMIDITY", "hum_low": "LOW HUMIDITY"}.get(kind, kind)
        verb = "ALERT" if opened else "REMINDER"
        subject = f"[HVAC {verb}] {label} — {location}"
        if kind == "stale":
            body = f"{location} has stopped reporting.\nNo data for over {config.STALE_AFTER_S:.0f}s."
        elif kind in ("hum_high", "hum_low"):
            # Humidity is a band, so say which end was crossed and in which
            # direction — "%RH: 78 (limit 70)" alone doesn't tell you whether
            # that limit was a floor or a ceiling.
            side = "above the maximum" if kind == "hum_high" else "below the minimum"
            body = (f"{location}\nRelative humidity {value:.1f}%RH is {side} "
                    f"of {threshold:.1f}%RH.")
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


def _latest_probe_temp(db: Session, tenant_id: str, eui: str, rom: str) -> float | None:
    """Latest temperature of one specific probe (by ROM) of a sensor."""
    r = db.scalar(
        select(Reading).where(Reading.tenant_id == tenant_id, Reading.eui == eui)
        .order_by(Reading.ts.desc()).limit(1)
    )
    if not r:
        return None
    try:
        for p in json.loads(r.probes):
            if isinstance(p, dict) and p.get("rom") == rom:
                return p.get("c")
    except (ValueError, TypeError):
        pass
    return None


def _member_temp(db: Session, tenant_id: str, m: SensorMap) -> float | None:
    """A unit member's temperature for ΔT: its specific probe if mapped per-probe,
    else the sensor's hottest probe (legacy whole-sensor mapping)."""
    if m.probe_rom:
        return _latest_probe_temp(db, tenant_id, m.eui, m.probe_rom)
    return _latest_max(db, tenant_id, m.eui)


def evaluate_reading(db: Session, tenant_id: str, eui: str, probes: list[dict],
                     max_c: float, recipients) -> None:
    """Run high-temp and ΔT checks for a freshly-stored reading, honoring the
    tenant's alert granularity:
      - 'sensor' (default): one high_temp alert per sensor on its hottest probe.
      - 'probe': each mapped probe alerts independently at its own exhaust, keyed
        '<eui>:<rom>'.
    `probes` is the parsed [{"rom","c"}] list for this reading."""
    granularity = _granularity(db, tenant_id)
    sms = db.scalars(
        select(SensorMap).where(SensorMap.tenant_id == tenant_id, SensorMap.eui == eui)
    ).all()

    if granularity == "probe":
        temp_by_rom = {p["rom"]: p["c"] for p in probes if isinstance(p, dict)}
        units: set[str] = set()
        for sm in sms:
            val = temp_by_rom.get(sm.probe_rom) if sm.probe_rom else max_c
            if val is None:
                continue
            high_c, _delta_c, enabled = resolve_threshold(db, tenant_id, sm)
            if not enabled:
                continue
            key = f"{eui}:{sm.probe_rom}" if sm.probe_rom else eui
            location = sm.label or f"sensor {eui}"
            if val >= high_c:
                _open_alert(db, tenant_id, recipients, key, "high_temp", location, val, high_c)
            elif val <= high_c - config.HYSTERESIS_C:
                _clear_alert(db, tenant_id, key, "high_temp")
            if sm.unit_id:
                units.add(sm.unit_id)
        for unit_id in units:
            member = next((m for m in sms if m.unit_id == unit_id), None)
            _, delta_c, enabled = resolve_threshold(db, tenant_id, member)
            if enabled:
                _evaluate_delta(db, tenant_id, unit_id, delta_c, recipients)
        return

    # --- sensor mode (legacy): hottest probe, one alert per sensor ---
    sm = sms[0] if sms else None
    high_c, delta_c, enabled = resolve_threshold(db, tenant_id, sm)
    if not enabled:
        return
    location = sm.label if (sm and sm.label) else f"sensor {eui}"
    if max_c >= high_c:
        _open_alert(db, tenant_id, recipients, eui, "high_temp", location, max_c, high_c)
    elif max_c <= high_c - config.HYSTERESIS_C:
        _clear_alert(db, tenant_id, eui, "high_temp")
    if sm and sm.unit_id:
        _evaluate_delta(db, tenant_id, sm.unit_id, delta_c, recipients)


def evaluate_humidity(db: Session, tenant_id: str, eui: str, hum: float | None,
                      recipients, label: str = "") -> None:
    """Evaluate one router/gateway BME humidity sample against the tenant band.

    Called from the /v1/env ingest path. Humidity differs from temperature in
    three ways that shape this:

      * it is a BAND — too dry is an ESD risk, too damp risks condensation — so
        there are two independent alert kinds rather than one ceiling;
      * it is measured per DEVICE (the BME on a router/gateway), not per rack
        probe, so only the tenant-scope row applies — a rack/port override would
        have nothing to attach to;
      * it is OPT-IN (`hum_enabled`). A site upgrading to this build has never
        chosen a humidity limit, and inventing one for them would mean emailing
        about a condition they never asked to watch.

    hum_low and hum_high are separate kinds so each clears on its own side of
    the band. They cannot both be open at once — a reading can't be under the
    floor and over the ceiling — so the (tenant, eui, kind) dedup still holds.
    """
    if hum is None:
        return
    try:
        h = float(hum)
    except (TypeError, ValueError):
        return
    # A BME that has failed or been disconnected commonly reports exactly 0 or a
    # wild value; alerting "LOW HUMIDITY 0%" on a dead sensor is noise, not signal.
    if not (0.0 < h <= 100.0):
        return

    t = db.scalar(select(Threshold).where(
        Threshold.tenant_id == tenant_id, Threshold.scope == "tenant"))
    if t is None or not getattr(t, "hum_enabled", False):
        return                                   # opt-in; nothing configured yet
    lo = t.hum_min if t.hum_min is not None else config.DEFAULT_HUM_MIN
    hi = t.hum_max if t.hum_max is not None else config.DEFAULT_HUM_MAX
    if lo >= hi:
        return                                   # nonsensical band; treat as off

    loc = label or f"sensor {eui}"
    key_hi, key_lo = f"{eui}:hum_high", f"{eui}:hum_low"

    if h >= hi:
        _open_alert(db, tenant_id, recipients, key_hi, "hum_high", loc, h, hi)
    elif h <= hi - config.HYSTERESIS_RH:
        _clear_alert(db, tenant_id, key_hi, "hum_high")

    if h <= lo:
        _open_alert(db, tenant_id, recipients, key_lo, "hum_low", loc, h, lo)
    elif h >= lo + config.HYSTERESIS_RH:
        _clear_alert(db, tenant_id, key_lo, "hum_low")


def _granularity(db: Session, tenant_id: str) -> str:
    t = db.get(Tenant, tenant_id)
    return t.alert_granularity if t else "sensor"


def _evaluate_delta(db: Session, tenant_id: str, unit_id: str,
                    delta_c: float, recipients) -> None:
    members = db.scalars(
        select(SensorMap).where(SensorMap.tenant_id == tenant_id, SensorMap.unit_id == unit_id)
    ).all()
    intake = [m for m in members if m.slot == "A"]
    exhaust = [m for m in members if m.slot == "B"]
    if not intake or not exhaust:
        return  # need both sides to compute a delta

    intake_t = _hottest([_member_temp(db, tenant_id, m) for m in intake])
    exhaust_t = _hottest([_member_temp(db, tenant_id, m) for m in exhaust])
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
