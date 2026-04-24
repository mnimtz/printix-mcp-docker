"""
Desktop-Cards-API-Routen (v6.7.89)
===================================
Endpunkte fuer die iOS-Mobile-App, mit denen der aktuell angemeldete User
seine eigenen RFID-Karten verwalten kann. Strikt self-service: es gibt
keine Routen ueber fremde User, die Mobile-App sieht nur die Karten des
Token-Owners.

Alle Routen sind Token-basiert authentifiziert (Authorization: Bearer
<token>) und teilen den Rollen-Gate aus `hasManagementAccess`: Admin oder
User, kein Employee. Der Client zeigt den Karten-Tab entsprechend nur
fuer diese Rollen.

Endpoints:
  GET    /desktop/cards                — Liste meiner Karten
  GET    /desktop/cards/profiles       — Liste aller Transform-Profile
  POST   /desktop/cards/preview        — Dry-Run: zeigt Transformation ohne Speichern
  POST   /desktop/cards                — Neue Karte anlegen (Printix + lokal)
  DELETE /desktop/cards/{mapping_id}   — Karte loeschen (Printix + lokal)

Response-Format: immer JSON. Fehler als `{"error": "…", "code": "…"}`
mit passendem HTTP-Status.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from desktop_auth import validate_token

logger = logging.getLogger("printix.desktop.cards")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _require_token(authorization: Optional[str]) -> Optional[dict]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return validate_token(parts[1].strip())


def _json_error(msg: str, code: str = "error", status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg, "code": code}, status_code=status)


def _has_management_access(user: dict) -> bool:
    """Spiegelt hasManagementAccess aus der iOS-App: admin oder user,
    kein employee. Employees haben keinen eigenen Tenant und werden
    bewusst ausgeschlossen, bis Phase 1 des Cloud-Print-Plans steht."""
    role = (user.get("role_type") or "").strip().lower()
    return role in ("admin", "user")


def _resolve_tenant(user: dict) -> Optional[dict]:
    """Liefert den Tenant-Kontext des Users (Full-Record mit Secrets).

    Für Employees wird ueber parent_user_id aufgeloest — aktuell aber
    ueber _has_management_access() sowieso gesperrt. Die Indirektion
    bleibt drin, damit das spaeter nur ein Gate-Toggle ist.
    """
    from db import get_tenant_full_by_user_id
    from cloudprint.db_extensions import get_parent_user_id
    target_user_id = get_parent_user_id(user["user_id"]) or user["user_id"]
    return get_tenant_full_by_user_id(target_user_id)


def _make_printix_client(tenant: dict):
    """Card-API-faehigen PrintixClient aus Tenant-Credentials bauen.

    Identisch zu _make_printix_client in web/app.py — hier dupliziert,
    damit dieses Modul autark bleibt (die app.py-Version ist als innere
    Funktion in register_web_routes verschachtelt und nicht importierbar).
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from printix_client import PrintixClient
    return PrintixClient(
        tenant_id=tenant.get("printix_tenant_id", ""),
        print_client_id=tenant.get("print_client_id") or None,
        print_client_secret=tenant.get("print_client_secret") or None,
        card_client_id=tenant.get("card_client_id") or None,
        card_client_secret=tenant.get("card_client_secret") or None,
        ws_client_id=tenant.get("ws_client_id") or None,
        ws_client_secret=tenant.get("ws_client_secret") or None,
    )


def _extract_card_id(card_obj) -> str:
    """Printix gibt Karten mit _links.self.href zurueck — daraus ID ziehen."""
    if not isinstance(card_obj, dict):
        return ""
    href = ((card_obj.get("_links") or {}).get("self") or {}).get("href", "")
    if href:
        return href.split("/")[-1]
    return card_obj.get("card_id", "") or card_obj.get("id", "") or ""


def _serialize_mapping(mapping: dict, profile_map: dict) -> dict:
    """Slim JSON-Shape fuer die App. Interne Felder wie search_blob und
    preview_json werden nicht exponiert; die berechneten preview-Felder
    (hex, decimal, base64_text) schon, weil die App sie im Detail anzeigt.
    """
    if not mapping:
        return {}
    profile_id = mapping.get("profile_id") or ""
    profile = profile_map.get(profile_id) if profile_id else None
    preview = mapping.get("preview") or {}
    # v6.7.105: id ist in Legacy-DBs als TEXT-Spalte angelegt worden; die
    # iOS-App erwartet Int. Explizit casten, sonst fallen alle ids im Swift-
    # Decoder auf 0 → Display-Bug in ForEach/NavigationLink.
    raw_id = mapping.get("id")
    try:
        int_id = int(raw_id) if raw_id is not None and raw_id != "" else 0
    except (TypeError, ValueError):
        int_id = 0
    return {
        "id": int_id,
        "printix_card_id": mapping.get("printix_card_id") or "",
        "profile_id": profile_id,
        "profile_name": (profile or {}).get("name", ""),
        "profile_vendor": (profile or {}).get("vendor", ""),
        "profile_reader_model": (profile or {}).get("reader_model", ""),
        "local_value": mapping.get("local_value") or "",
        "final_value": mapping.get("final_value") or "",
        "normalized_value": mapping.get("normalized_value") or "",
        "notes": mapping.get("notes") or "",
        "source": mapping.get("source") or "",
        "created_at": mapping.get("created_at") or "",
        "updated_at": mapping.get("updated_at") or "",
        "preview": {
            "raw": preview.get("raw", ""),
            "normalized": preview.get("normalized", ""),
            "working": preview.get("working", ""),
            "hex": preview.get("hex", ""),
            "hex_reversed": preview.get("hex_reversed", ""),
            "decimal": preview.get("decimal", ""),
            "decimal_reversed": preview.get("decimal_reversed", ""),
            "base64_text": preview.get("base64_text", ""),
            "final_submit_value": preview.get("final_submit_value", ""),
        },
    }


def _build_profile_map(tenant_id: str) -> dict:
    """Schneller Lookup profile_id → profile-dict fuer _serialize_mapping."""
    from cards.store import list_profiles
    out: dict = {}
    for p in list_profiles(tenant_id) or []:
        pid = p.get("id") or ""
        if pid:
            out[pid] = p
    return out


def _apply_profile(tenant_id: str, profile_id: str, raw_value: str) -> dict:
    """Profil anwenden (oder Standard-Transform ohne Profil). Spiegelt
    das Verhalten aus tenant_user_add_card in web/app.py wider."""
    from cards.store import get_profile
    from cards.transform import transform_card_value
    rules: dict = {}
    if profile_id:
        prof = get_profile(profile_id, tenant_id)
        if prof:
            rules_raw = prof.get("rules_json") or {}
            if isinstance(rules_raw, str):
                import json
                try:
                    rules = json.loads(rules_raw or "{}") or {}
                except Exception:
                    rules = {}
            elif isinstance(rules_raw, dict):
                rules = rules_raw
    return transform_card_value(raw_value, **rules)


# ─── Registrierung ───────────────────────────────────────────────────────────

def register_desktop_cards_routes(app: FastAPI) -> None:
    """Registriert alle /desktop/cards-Routen in der FastAPI-App."""

    @app.get("/desktop/cards")
    async def desktop_cards_list(
        request: Request,
        authorization: str = Header(default=""),
    ):
        user = _require_token(authorization)
        if not user:
            return _json_error("token invalid", code="auth_required", status=401)
        if not _has_management_access(user):
            return _json_error("role not permitted", code="forbidden", status=403)

        tenant = _resolve_tenant(user)
        if not tenant:
            return _json_error("tenant not found", code="no_tenant", status=404)

        printix_user_id = (user.get("printix_user_id") or "").strip()
        if not printix_user_id:
            # Ohne Printix-UID haben wir nichts, was wir dem User zuordnen
            # koennten — die App zeigt dann eine leere Liste mit Hinweis.
            logger.info("Cards-List: user='%s' has no printix_user_id",
                        user.get("username"))
            return JSONResponse({"cards": []})

        from cards.store import list_mappings_for_user, init_cards_tables
        init_cards_tables()
        mappings = list_mappings_for_user(tenant["id"], printix_user_id)
        profile_map = _build_profile_map(tenant["id"])
        cards = [_serialize_mapping(m, profile_map) for m in mappings]

        logger.info("Cards-List OK — user='%s' count=%d ids=%s",
                    user.get("username"), len(cards),
                    [c.get("id") for c in cards])
        return JSONResponse({"cards": cards})

    @app.get("/desktop/cards/profiles")
    async def desktop_cards_profiles(
        request: Request,
        authorization: str = Header(default=""),
    ):
        user = _require_token(authorization)
        if not user:
            return _json_error("token invalid", code="auth_required", status=401)
        if not _has_management_access(user):
            return _json_error("role not permitted", code="forbidden", status=403)

        tenant = _resolve_tenant(user)
        if not tenant:
            return _json_error("tenant not found", code="no_tenant", status=404)

        from cards.store import list_profiles, init_cards_tables
        init_cards_tables()
        raw = list_profiles(tenant["id"]) or []
        profiles = [{
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "vendor": p.get("vendor", ""),
            "reader_model": p.get("reader_model", ""),
            "mode": p.get("mode", ""),
            "description": p.get("description", ""),
            "is_builtin": bool(p.get("is_builtin")),
        } for p in raw]
        # Firmen-Default mitliefern, damit iOS den Picker nicht zeigen
        # muss wenn der Admin ein Standard-Profil gesetzt hat. Leere ID
        # = kein Default gesetzt, iOS faellt auf "Ohne Profil" zurueck.
        default_profile_id = tenant.get("default_card_profile_id") or ""
        # TEMP-Debug v6.7.99
        logger.info("Cards-Profiles OK — user='%s' count=%d default='%s'",
                    user.get("username"), len(profiles), default_profile_id)
        return JSONResponse({
            "profiles": profiles,
            "default_profile_id": default_profile_id,
        })

    @app.post("/desktop/cards/preview")
    async def desktop_cards_preview(
        request: Request,
        authorization: str = Header(default=""),
    ):
        user = _require_token(authorization)
        if not user:
            return _json_error("token invalid", code="auth_required", status=401)
        if not _has_management_access(user):
            return _json_error("role not permitted", code="forbidden", status=403)

        tenant = _resolve_tenant(user)
        if not tenant:
            return _json_error("tenant not found", code="no_tenant", status=404)

        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_value = (body.get("raw_value") or "").strip()
        profile_id = (body.get("profile_id") or "").strip()

        if not raw_value:
            return _json_error("raw_value required", code="invalid_input", status=422)

        preview = _apply_profile(tenant["id"], profile_id, raw_value)
        return JSONResponse({"preview": {
            "raw": preview.get("raw", ""),
            "normalized": preview.get("normalized", ""),
            "working": preview.get("working", ""),
            "hex": preview.get("hex", ""),
            "hex_reversed": preview.get("hex_reversed", ""),
            "decimal": preview.get("decimal", ""),
            "decimal_reversed": preview.get("decimal_reversed", ""),
            "base64_text": preview.get("base64_text", ""),
            "final_submit_value": preview.get("final_submit_value", ""),
        }})

    @app.post("/desktop/cards")
    async def desktop_cards_create(
        request: Request,
        authorization: str = Header(default=""),
    ):
        user = _require_token(authorization)
        if not user:
            return _json_error("token invalid", code="auth_required", status=401)
        if not _has_management_access(user):
            return _json_error("role not permitted", code="forbidden", status=403)

        tenant = _resolve_tenant(user)
        if not tenant:
            return _json_error("tenant not found", code="no_tenant", status=404)

        printix_user_id = (user.get("printix_user_id") or "").strip()
        if not printix_user_id:
            return _json_error("user has no linked printix account",
                               code="no_printix_user", status=409)
        # Synthetische IDs (mgr:-Praefix) werden von der Printix Card-API
        # abgelehnt ("Failed to convert 'user' with value: 'mgr:...'"). Das
        # ist keine echte Printix-UUID, sondern unser interner Platzhalter
        # aus cached_printix_users. Fuer Cards brauchen wir die ECHTE UUID
        # des Users — entweder aus der Printix-Admin-URL manuell, oder via
        # First-Submit-Fallback (beim naechsten Druckjob ueber /desktop/send).
        if printix_user_id.startswith("mgr:") or ":" in printix_user_id:
            return _json_error(
                "stored printix user id is a synthetic placeholder, not a "
                "real printix uuid — paste the real uuid from the printix "
                "admin url, or submit a print job first to auto-populate it",
                code="printix_uuid_invalid", status=409,
            )

        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_value = (body.get("raw_value") or "").strip()
        profile_id = (body.get("profile_id") or "").strip()
        notes = (body.get("notes") or "").strip()

        if not raw_value:
            return _json_error("raw_value required", code="invalid_input", status=422)

        # Transform laufen lassen — das gibt uns final_submit_value fuer
        # Printix und normalized/raw fuer die DB.
        try:
            preview = _apply_profile(tenant["id"], profile_id, raw_value)
        except Exception as e:
            logger.error("Cards-Create: transform failed user='%s' err=%s",
                         user.get("username"), e)
            return _json_error("transform failed", code="transform_error", status=422)

        submit_value = preview.get("final_submit_value") or raw_value
        normalized_value = preview.get("normalized") or raw_value

        # An Printix pushen — BEFORE/AFTER-Vergleich, um die neu vergebene
        # card_id zu ermitteln (Printix-API gibt sie im Register-Response
        # nicht zuverlaessig zurueck).
        try:
            client = _make_printix_client(tenant)
        except Exception as e:
            logger.error("Cards-Create: printix client init failed user='%s' err=%s",
                         user.get("username"), e)
            return _json_error("printix client unavailable",
                               code="printix_unavailable", status=502)

        # Helper: Karte via search_card finden und pruefen wer der Owner
        # ist. Liefert (card_id, owner_matches_current_user_bool).
        def _lookup_by_secret(secret: str) -> tuple[str, bool]:
            try:
                card_obj = client.search_card(card_number=secret)
            except Exception as search_err:
                logger.warning("Cards-Create: search fallback failed user='%s' err=%s",
                               user.get("username"), search_err)
                return "", False
            cid = _extract_card_id(card_obj)
            owner_href = ""
            if isinstance(card_obj, dict):
                owner_href = (
                    ((card_obj.get("_links") or {}).get("owner") or {})
                    .get("href", "")
                )
            owner_match = (
                not owner_href or owner_href.endswith("/" + printix_user_id)
            )
            return cid, owner_match

        # Import hier damit PrintixAPIError fuer den 409-Handler verfuegbar ist.
        from printix_client import PrintixAPIError  # type: ignore

        new_card_id = ""
        try:
            before = client.list_user_cards(printix_user_id)
            before_ids = set()
            for c in before.get("cards", before.get("content", [])) or []:
                cid = _extract_card_id(c)
                if cid:
                    before_ids.add(cid)
            try:
                client.register_card(printix_user_id, submit_value)
            except PrintixAPIError as reg_err:
                msg = (reg_err.message or "").lower()
                is_duplicate = (
                    reg_err.status_code == 409
                    or "already exist" in msg
                    or "already registered" in msg
                    or "card secret already" in msg
                )
                if not is_duplicate:
                    raise
                # Karte existiert bereits — idempotent behandeln: Owner
                # pruefen und denselben card_id nehmen, statt mit 502 zu
                # sterben. Passiert z.B. wenn der User denselben NFC-Chip
                # zweimal registriert (Web + iOS).
                logger.info("Cards-Create: printix 409 duplicate — trying lookup user='%s'",
                            user.get("username"))
                existing_id, owner_match = _lookup_by_secret(submit_value)
                if existing_id and owner_match:
                    new_card_id = existing_id
                    logger.info("Cards-Create: duplicate resolved to own card_id=%s", existing_id)
                elif existing_id and not owner_match:
                    return _json_error(
                        "this card is already registered to another user in printix",
                        code="card_already_registered", status=409,
                    )
                else:
                    return _json_error(
                        "card already exists in printix but lookup failed",
                        code="card_duplicate_lookup_failed", status=409,
                    )

            if not new_card_id:
                after = client.list_user_cards(printix_user_id)
                after_cards = after.get("cards", after.get("content", [])) or []
                for c in after_cards:
                    cid = _extract_card_id(c)
                    if cid and cid not in before_ids:
                        new_card_id = cid
                        break
            if not new_card_id:
                # Fallback ueber Suche nach card_number
                candidate_id, owner_match = _lookup_by_secret(submit_value)
                if candidate_id and owner_match:
                    new_card_id = candidate_id
        except Exception as e:
            logger.error("Cards-Create: printix register failed user='%s' err=%s",
                         user.get("username"), e)
            return _json_error("printix register failed",
                               code="printix_error", status=502)

        if not new_card_id:
            logger.error("Cards-Create: printix registered card but id lookup failed user='%s'",
                         user.get("username"))
            return _json_error(
                "card created in printix but local mapping failed",
                code="card_id_missing", status=500,
            )

        # Lokales Mapping speichern — selber Shape wie der Web-UI-Pfad.
        from cards.store import save_mapping, get_mapping_by_card, init_cards_tables
        init_cards_tables()
        mapping_id = save_mapping(
            tenant_id=tenant["id"],
            printix_user_id=printix_user_id,
            printix_card_id=new_card_id,
            local_value=raw_value,
            final_value=submit_value,
            normalized_value=normalized_value,
            source="mobile_app_add_card",
            notes=notes,
            profile_id=profile_id or "",
            preview=preview,
            printix_secret_value=submit_value,
        )

        saved = get_mapping_by_card(tenant["id"], printix_user_id, new_card_id)
        profile_map = _build_profile_map(tenant["id"])
        logger.info("Cards-Create OK — user='%s' card_id=%s profile=%s",
                    user.get("username"), new_card_id, profile_id or "-")
        return JSONResponse({"card": _serialize_mapping(saved, profile_map)},
                            status_code=201)

    @app.delete("/desktop/cards/{mapping_id}")
    async def desktop_cards_delete(
        mapping_id: int,
        request: Request,
        authorization: str = Header(default=""),
    ):
        user = _require_token(authorization)
        if not user:
            return _json_error("token invalid", code="auth_required", status=401)
        if not _has_management_access(user):
            return _json_error("role not permitted", code="forbidden", status=403)

        tenant = _resolve_tenant(user)
        if not tenant:
            return _json_error("tenant not found", code="no_tenant", status=404)

        printix_user_id = (user.get("printix_user_id") or "").strip()

        # Mapping erst laden, damit wir die Printix-card_id bekommen UND
        # verifizieren dass es wirklich die Karte des aufrufenden Users ist.
        from cards.store import (
            list_mappings_for_user, delete_mapping, init_cards_tables,
        )
        init_cards_tables()
        mappings = list_mappings_for_user(tenant["id"], printix_user_id)
        # v6.7.106: id kann aus der Legacy-TEXT-Spalte als String kommen —
        # int-Cast, sonst schlaegt "22" == 22 fehl und wir liefern 404 obwohl
        # die Karte existiert.
        def _as_int(v):
            try:
                return int(v) if v is not None and v != "" else None
            except (TypeError, ValueError):
                return None
        mapping = next((m for m in mappings if _as_int(m.get("id")) == mapping_id), None)
        if not mapping:
            return _json_error("card not found", code="not_found", status=404)

        printix_card_id = (mapping.get("printix_card_id") or "").strip()

        # Erst Printix — wenn das fehlschlaegt, lokalen Eintrag behalten,
        # damit der User nochmal loeschen kann.
        if printix_card_id:
            try:
                client = _make_printix_client(tenant)
                client.delete_card(printix_card_id, user_id=printix_user_id)
            except Exception as e:
                logger.warning("Cards-Delete: printix delete failed user='%s' card=%s err=%s",
                               user.get("username"), printix_card_id, e)
                return _json_error("printix delete failed",
                                   code="printix_error", status=502)

        delete_mapping(mapping_id, tenant["id"])
        logger.info("Cards-Delete OK — user='%s' mapping_id=%s card_id=%s",
                    user.get("username"), mapping_id, printix_card_id or "-")
        return JSONResponse({"ok": True})
