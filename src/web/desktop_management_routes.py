"""
Desktop-Management-API (v6.7.66)
================================
Read-only Live-Endpunkte für den iOS-"Management"-Tab (Admin/User-Rolle).
Kein Cache, keine DB — jeder Aufruf fragt Printix live ab. Parallelisierung
via asyncio.gather + asyncio.to_thread, damit ein Overview-Refresh nicht
sequenziell 3–5 s dauert.

Endpoints (alle Bearer-Token via Authorization-Header):
  GET /desktop/management/stats        — Zähler für Printer/User/Workstation
  GET /desktop/management/printers     — Druckerliste (id, name, status …)
  GET /desktop/management/printers/{id}— Details einzelner Drucker/Queue
  GET /desktop/management/users        — Benutzerliste
  GET /desktop/management/workstations — Workstation-Liste

Rollen:
  - admin / user → Zugriff erlaubt
  - employee    → 403 (employees haben keinen Tenant → keine Printix-API)

Response-Format: JSON, Fehlerstruktur kompatibel mit desktop_routes:
  `{"error": "...", "code": "...", ...}`
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from web.desktop_routes import _require_token, _json_error, _log_req

logger = logging.getLogger("printix.desktop.mgmt")

_HREF_QUEUE_RE = re.compile(r"/printers/([^/]+)/queues/([^/?]+)")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _require_mgmt_user(authorization: Optional[str]) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Token prüfen + Rolle auf admin/user beschränken.

    Employees dürfen Management nicht sehen (kein Tenant, kein Sinn).
    Returns (user, None) bei Erfolg, (None, error_response) sonst.
    """
    user = _require_token(authorization)
    if not user:
        return None, _json_error("token invalid", code="auth_required", status=401)
    role = (user.get("role_type") or "user").lower()
    if role not in ("admin", "user"):
        return None, _json_error(
            "management is only available for admin/user roles",
            code="role_forbidden", status=403,
        )
    return user, None


def _load_tenant_for_user(user: dict) -> Optional[dict]:
    """Tenant-Full-Record mit Secrets für den aktuellen User (oder Parent)."""
    import sys, os
    src_dir = os.path.dirname(os.path.dirname(__file__))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from db import get_tenant_full_by_user_id
    try:
        from cloudprint.db_extensions import get_parent_user_id
        parent_id = get_parent_user_id(user["user_id"])
    except Exception:
        parent_id = user["user_id"]
    return get_tenant_full_by_user_id(parent_id or user["user_id"])


def _make_client(tenant: dict):
    """Baut einen PrintixClient mit allen verfügbaren Credential-Sets."""
    import sys, os
    src_dir = os.path.dirname(os.path.dirname(__file__))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from printix_client import PrintixClient
    return PrintixClient(
        tenant_id=tenant.get("printix_tenant_id", ""),
        print_client_id=tenant.get("print_client_id") or None,
        print_client_secret=tenant.get("print_client_secret") or None,
        card_client_id=tenant.get("card_client_id") or None,
        card_client_secret=tenant.get("card_client_secret") or None,
        ws_client_id=tenant.get("ws_client_id") or None,
        ws_client_secret=tenant.get("ws_client_secret") or None,
        um_client_id=tenant.get("um_client_id") or None,
        um_client_secret=tenant.get("um_client_secret") or None,
        shared_client_id=tenant.get("shared_client_id") or None,
        shared_client_secret=tenant.get("shared_client_secret") or None,
    )


def _api_enabled(tenant: dict, prefix: str) -> bool:
    """Heuristik: ein API-Set ist aktiv wenn client_id+secret gesetzt sind.

    prefix ∈ {'print', 'card', 'ws', 'um'}.
    """
    cid = (tenant.get(f"{prefix}_client_id") or "").strip()
    sec = (tenant.get(f"{prefix}_client_secret") or "").strip()
    return bool(cid and sec)


# ─── Shape-Helpers (Printix Response → flache JSON-Struktur für iOS) ─────────

def _extract_printers(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("printers") or raw.get("content") \
             or (raw.get("_embedded") or {}).get("printers") or []
    else:
        items = []
    out: list[dict] = []
    seen_pids: set[str] = set()
    for p in items if isinstance(items, list) else []:
        if not isinstance(p, dict):
            continue
        href = (p.get("_links") or {}).get("self", {}).get("href", "")
        m = _HREF_QUEUE_RE.search(href)
        pid = m.group(1) if m else (p.get("id") or "")
        qid = m.group(2) if m else (p.get("queueId") or "")
        if not pid:
            continue
        key = f"{pid}:{qid}"
        if key in seen_pids:
            continue
        seen_pids.add(key)
        status = (p.get("connectionStatus") or p.get("status") or "").lower()
        name = (p.get("name") or p.get("modelName") or p.get("displayName") or "").strip()
        location = (p.get("location") or p.get("locationName") or "").strip()
        model = (p.get("modelName") or p.get("model") or "").strip()
        out.append({
            "id": pid,
            "queue_id": qid,
            "name": name or pid,
            "model": model,
            "location": location,
            "status": status or "unknown",
            "is_online": status in ("connected", "online", "ok"),
        })
    return out


def _extract_users(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        items = raw.get("users") or raw.get("content") \
             or (raw.get("_embedded") or {}).get("users") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out: list[dict] = []
    for u in items if isinstance(items, list) else []:
        if not isinstance(u, dict):
            continue
        uid = u.get("id") or ""
        if not uid:
            href = (u.get("_links") or {}).get("self", {}).get("href", "")
            uid = href.rsplit("/", 1)[-1] if href else ""
        email = (u.get("email") or u.get("username") or "").strip()
        name  = (u.get("fullName") or u.get("name") or u.get("displayName") or "").strip()
        role  = (u.get("role") or "").strip()
        out.append({
            "id": uid,
            "email": email,
            "name": name or email,
            "role": role,
        })
    return out


def _extract_workstations(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        items = raw.get("workstations") or raw.get("content") \
             or (raw.get("_embedded") or {}).get("workstations") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out: list[dict] = []
    for w in items if isinstance(items, list) else []:
        if not isinstance(w, dict):
            continue
        wid = w.get("id") or ""
        if not wid:
            href = (w.get("_links") or {}).get("self", {}).get("href", "")
            wid = href.rsplit("/", 1)[-1] if href else ""
        hostname = (w.get("hostname") or w.get("computerName")
                    or w.get("name") or "").strip()
        user_email = (w.get("userEmail") or w.get("lastUserEmail") or "").strip()
        last_seen = w.get("lastActiveTime") or w.get("lastSeen") or ""
        online = bool(w.get("active") or w.get("online"))
        out.append({
            "id": wid,
            "hostname": hostname or wid,
            "user_email": user_email,
            "last_seen": last_seen or "",
            "is_online": online,
        })
    return out


# ─── Registrierung ───────────────────────────────────────────────────────────

def register_desktop_management_routes(app: FastAPI) -> None:
    """Registriert alle /desktop/management/*-Routen in der FastAPI-App."""

    @app.get("/desktop/management/stats")
    async def mgmt_stats(request: Request,
                         authorization: str = Header(default="")):
        ci = _log_req(request, "GET /management/stats")
        user, err = _require_mgmt_user(authorization)
        if err:
            return err

        tenant = _load_tenant_for_user(user)
        if not tenant:
            return _json_error("no tenant configured", code="no_tenant", status=404)

        has_print = _api_enabled(tenant, "print")
        has_card  = _api_enabled(tenant, "card")
        has_ws    = _api_enabled(tenant, "ws")

        client = _make_client(tenant)

        async def _printers() -> dict:
            if not has_print:
                return {"total": 0, "online": 0, "available": False}
            try:
                raw = await asyncio.to_thread(lambda: client.list_printers(size=200))
                ps = _extract_printers(raw)
                return {
                    "total": len(ps),
                    "online": sum(1 for p in ps if p["is_online"]),
                    "available": True,
                }
            except Exception as e:
                logger.warning("mgmt stats printers: %s", e)
                return {"total": 0, "online": 0, "available": False,
                        "error": str(e)[:200]}

        async def _users() -> dict:
            if not has_card:
                return {"total": 0, "available": False}
            try:
                raw = await asyncio.to_thread(
                    lambda: client.list_users(role="USER,GUEST_USER", page_size=200))
                us = _extract_users(raw)
                return {"total": len(us), "available": True}
            except Exception as e:
                logger.warning("mgmt stats users: %s", e)
                return {"total": 0, "available": False, "error": str(e)[:200]}

        async def _workstations() -> dict:
            if not has_ws:
                return {"total": 0, "online": 0, "available": False}
            try:
                raw = await asyncio.to_thread(lambda: client.list_workstations(size=200))
                ws = _extract_workstations(raw)
                return {
                    "total": len(ws),
                    "online": sum(1 for w in ws if w["is_online"]),
                    "available": True,
                }
            except Exception as e:
                logger.warning("mgmt stats workstations: %s", e)
                return {"total": 0, "online": 0, "available": False,
                        "error": str(e)[:200]}

        printers, users, workstations = await asyncio.gather(
            _printers(), _users(), _workstations())

        logger.info(
            "Desktop-Mgmt stats OK — user='%s' printers=%s users=%s ws=%s peer=%s",
            user.get("username"),
            printers.get("total"), users.get("total"), workstations.get("total"),
            ci["peer"],
        )
        return JSONResponse({
            "printers":     printers,
            "users":        users,
            "workstations": workstations,
            "tenant": {
                "id":   tenant.get("id"),
                "name": tenant.get("tenant_name") or tenant.get("name") or "",
            },
        })

    @app.get("/desktop/management/printers")
    async def mgmt_printers(request: Request,
                            authorization: str = Header(default="")):
        ci = _log_req(request, "GET /management/printers")
        user, err = _require_mgmt_user(authorization)
        if err:
            return err
        tenant = _load_tenant_for_user(user)
        if not tenant:
            return _json_error("no tenant configured", code="no_tenant", status=404)
        if not _api_enabled(tenant, "print"):
            return JSONResponse({"printers": [], "available": False})
        try:
            client = _make_client(tenant)
            raw = await asyncio.to_thread(lambda: client.list_printers(size=200))
            items = _extract_printers(raw)
            logger.info(
                "Desktop-Mgmt printers OK — user='%s' count=%d peer=%s",
                user.get("username"), len(items), ci["peer"],
            )
            return JSONResponse({"printers": items, "available": True})
        except Exception as e:
            logger.warning("mgmt printers: %s", e)
            return _json_error(str(e)[:200], code="printix_error", status=502)

    @app.get("/desktop/management/printers/{printer_id}")
    async def mgmt_printer_detail(printer_id: str, request: Request,
                                    queue_id: str = "",
                                    authorization: str = Header(default="")):
        ci = _log_req(request, "GET /management/printers/{id}",
                      f"printer_id={printer_id} queue_id={queue_id or '-'}")
        user, err = _require_mgmt_user(authorization)
        if err:
            return err
        tenant = _load_tenant_for_user(user)
        if not tenant:
            return _json_error("no tenant configured", code="no_tenant", status=404)
        if not _api_enabled(tenant, "print"):
            return _json_error("print api not configured",
                               code="print_api_unavailable", status=404)
        try:
            client = _make_client(tenant)
            # Wenn der Client keine queue_id mitgegeben hat, versuchen wir sie
            # aus list_printers zu resolven.
            qid = queue_id
            if not qid:
                raw = await asyncio.to_thread(lambda: client.list_printers(size=200))
                for p in _extract_printers(raw):
                    if p["id"] == printer_id:
                        qid = p["queue_id"]
                        break
            if not qid:
                return _json_error("queue id not found",
                                   code="queue_unknown", status=404)
            detail = await asyncio.to_thread(
                lambda: client.get_printer(printer_id, qid))
            logger.info(
                "Desktop-Mgmt printer-detail OK — user='%s' pid=%s qid=%s peer=%s",
                user.get("username"), printer_id, qid, ci["peer"],
            )
            return JSONResponse({"printer": detail,
                                 "id": printer_id, "queue_id": qid})
        except Exception as e:
            logger.warning("mgmt printer-detail: %s", e)
            return _json_error(str(e)[:200], code="printix_error", status=502)

    @app.get("/desktop/management/users")
    async def mgmt_users(request: Request,
                         authorization: str = Header(default="")):
        ci = _log_req(request, "GET /management/users")
        user, err = _require_mgmt_user(authorization)
        if err:
            return err
        tenant = _load_tenant_for_user(user)
        if not tenant:
            return _json_error("no tenant configured", code="no_tenant", status=404)
        if not _api_enabled(tenant, "card"):
            return JSONResponse({"users": [], "available": False})
        try:
            client = _make_client(tenant)
            raw = await asyncio.to_thread(
                lambda: client.list_users(role="USER,GUEST_USER", page_size=200))
            items = _extract_users(raw)
            logger.info(
                "Desktop-Mgmt users OK — user='%s' count=%d peer=%s",
                user.get("username"), len(items), ci["peer"],
            )
            return JSONResponse({"users": items, "available": True})
        except Exception as e:
            logger.warning("mgmt users: %s", e)
            return _json_error(str(e)[:200], code="printix_error", status=502)

    @app.get("/desktop/management/workstations")
    async def mgmt_workstations(request: Request,
                                authorization: str = Header(default="")):
        ci = _log_req(request, "GET /management/workstations")
        user, err = _require_mgmt_user(authorization)
        if err:
            return err
        tenant = _load_tenant_for_user(user)
        if not tenant:
            return _json_error("no tenant configured", code="no_tenant", status=404)
        if not _api_enabled(tenant, "ws"):
            return JSONResponse({"workstations": [], "available": False})
        try:
            client = _make_client(tenant)
            raw = await asyncio.to_thread(lambda: client.list_workstations(size=200))
            items = _extract_workstations(raw)
            logger.info(
                "Desktop-Mgmt workstations OK — user='%s' count=%d peer=%s",
                user.get("username"), len(items), ci["peer"],
            )
            return JSONResponse({"workstations": items, "available": True})
        except Exception as e:
            logger.warning("mgmt workstations: %s", e)
            return _json_error(str(e)[:200], code="printix_error", status=502)
