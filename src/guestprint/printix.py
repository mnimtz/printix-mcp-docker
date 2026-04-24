"""Printix-Guest-Provisionierung fuer Guest-Print.

Bruecke zwischen der Allowlist (guestprint_guest-Tabelle) und der Printix-
Cloud. Legt GUEST_USER an, mit expirationTimestamp ("Timebomb"), und
loescht sie beim Entfernen aus der Allowlist wieder.

Idempotent: Wenn die Mailadresse in Printix bereits existiert, wird der
existierende User nicht ueberschrieben — Printix kennt keinen Update-
Endpoint fuer Expiration, ein Re-Create wuerde den User plus alle Karten-
bindungen neu erzeugen. Stattdessen: existing.id uebernehmen, die lokale
expires_at als "unbekannt" markieren.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def iso_expiration(days: int) -> str:
    """ISO-8601 UTC Timestamp 'days' in der Zukunft (Printix-Format).
    days <= 0 -> leerer String = unbegrenzt."""
    if days <= 0:
        return ""
    ts = datetime.now(timezone.utc) + timedelta(days=int(days))
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_user_by_email(client, email: str) -> Optional[dict]:
    """Sucht einen Printix-User anhand der E-Mail (exact, case-insensitive)."""
    if not email:
        return None
    needle = email.strip().lower()
    try:
        # list_users(query=...) macht server-side substring match; wir filtern
        # nachtraeglich auf Gleichheit, falls es Treffer wie 'a@x' und 'ab@x'
        # gibt.
        resp = client.list_users(query=needle, page=0, page_size=50)
    except Exception as e:
        logger.warning("Printix list_users(query=%s) fehlgeschlagen: %s", needle, e)
        return None
    items: list[dict] = []
    if isinstance(resp, dict):
        items = resp.get("users") or resp.get("content") or resp.get("items") or []
    elif isinstance(resp, list):
        items = resp
    for u in items:
        if not isinstance(u, dict):
            continue
        u_email = (u.get("email") or "").strip().lower()
        if u_email == needle:
            return u
    return None


def provision_guest(
    client,
    sender_email: str,
    full_name: str = "",
    expiration_days: int = 7,
    pin: Optional[str] = None,
    id_code: Optional[str] = None,
    send_welcome_email: bool = False,
    send_expiration_email: bool = True,
) -> dict:
    """Stellt sicher, dass in Printix ein GUEST_USER fuer sender_email existiert.

    Ablauf:
      1. Mail existiert schon als Printix-User -> bestehenden uebernehmen.
      2. Sonst: GUEST_USER anlegen, mit expirationTimestamp = now + days.
         days <= 0 = unbegrenzt (kein expirationTimestamp gesetzt).

    Returns:
        {
          "printix_user_id":     str,
          "printix_guest_email": str,
          "expires_at":          str,   # ISO-8601 oder "" bei unbegrenzt/unbekannt
          "created":             bool,  # True = wir haben neu angelegt
          "existing":            bool,  # True = war schon da
          "warning":             str,   # optional (z.B. "existing user is USER not GUEST")
        }

    Raises:
        Exception bei Netzwerk-/API-Fehler im Create-Call (der Caller
        entscheidet, was er loggt und wie er fortfaehrt).
    """
    email = (sender_email or "").strip().lower()
    if not email:
        raise ValueError("sender_email leer")
    display = (full_name or email).strip()

    existing = _find_user_by_email(client, email)
    if existing:
        pxid = existing.get("id", "") or ""
        role = (existing.get("role") or "").upper()
        warning = ""
        if role and role != "GUEST_USER":
            warning = f"existing Printix user has role {role}, not GUEST_USER"
        return {
            "printix_user_id":     pxid,
            "printix_guest_email": (existing.get("email") or email).strip().lower(),
            "expires_at":          existing.get("expirationTimestamp", "") or "",
            "created":             False,
            "existing":            True,
            "warning":             warning,
        }

    expires_at = iso_expiration(expiration_days)
    resp = client.create_user(
        email=email,
        display_name=display,
        role="GUEST_USER",
        pin=pin,
        id_code=id_code,
        expiration_timestamp=expires_at or None,
        send_welcome_email=bool(send_welcome_email),
        send_expiration_email=bool(send_expiration_email and expires_at),
    )
    created = client.extract_created_user(resp)
    pxid = (created.get("id") or "").strip()
    if not pxid:
        raise RuntimeError(f"Printix create_user lieferte keine user id: {resp}")
    logger.info(
        "Printix GUEST_USER angelegt: %s (id=%s, expires=%s)",
        email, pxid, expires_at or "unbegrenzt",
    )
    return {
        "printix_user_id":     pxid,
        "printix_guest_email": (created.get("email") or email).strip().lower(),
        "expires_at":          expires_at,
        "created":             True,
        "existing":            False,
        "warning":             "",
    }


def delete_guest(client, printix_user_id: str) -> bool:
    """Loescht einen Printix-User (idempotent: 404 wird als True gezaehlt,
    weil das Ziel erreicht ist)."""
    if not printix_user_id:
        return False
    try:
        client.delete_user(printix_user_id)
        return True
    except Exception as e:
        msg = str(e).lower()
        if "404" in msg or "not found" in msg:
            logger.info("Printix-User %s existierte nicht mehr (OK)",
                         printix_user_id)
            return True
        logger.warning("Printix delete_user(%s) fehlgeschlagen: %s",
                        printix_user_id, e)
        return False


def verify_guest_exists(client, printix_user_id: str) -> bool:
    """Prueft, ob ein Printix-User noch existiert (z.B. nach Expiration-Ablauf).
    Dient der Admin-UI als "Status"-Indikator."""
    if not printix_user_id:
        return False
    try:
        resp = client.get_user(printix_user_id)
        return isinstance(resp, dict) and bool(resp.get("id"))
    except Exception as e:
        msg = str(e).lower()
        if "404" in msg or "not found" in msg:
            return False
        logger.warning("Printix get_user(%s) fehlgeschlagen: %s",
                        printix_user_id, e)
        return False
