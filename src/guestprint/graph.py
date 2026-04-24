"""Microsoft Graph Mail-Client fuer Guest-Print.

Minimaler Wrapper um die Endpoints, die wir wirklich brauchen. App-Only-
Token (client_credentials) — in Azure muessen die folgenden Application
Permissions mit Admin-Consent aktiv sein:

    Mail.ReadWrite    — Inbox lesen + Nachrichten in Subfolder verschieben
    (User.Read.All ist NICHT noetig, solange wir mit UPN direkt arbeiten)

Rate-Limits: Graph erlaubt ~10k Requests / 10min / App pro Mailbox; fuer
MVP-Polling reicht das mit Abstand. 429-Retry-After wird respektiert.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Optional

import requests

from . import config as gp_config

logger = logging.getLogger(__name__)

_GRAPH_BASE   = "https://graph.microsoft.com/v1.0"
_TOKEN_URL    = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_SCOPE_APP    = "https://graph.microsoft.com/.default"
_HTTP_TIMEOUT = 30


class GraphError(Exception):
    def __init__(self, status: int, message: str, detail: str = ""):
        super().__init__(f"{status}: {message}")
        self.status  = status
        self.message = message
        self.detail  = detail


# ─── Token-Manager ───────────────────────────────────────────────────────────

class _TokenCache:
    """In-memory App-Only-Token mit 60s-Sicherheitspuffer vor Ablauf."""

    def __init__(self) -> None:
        self._token: str   = ""
        self._expires: float = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token

        cfg = gp_config.get_config()
        if not (cfg["tenant_id"] and cfg["client_id"] and cfg["client_secret"]):
            raise GraphError(0, "Guest-Print Entra-App nicht konfiguriert")

        url = _TOKEN_URL.format(tenant=cfg["tenant_id"])
        resp = requests.post(
            url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "scope":         _SCOPE_APP,
            },
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            raise GraphError(
                resp.status_code,
                "Token-Fetch fehlgeschlagen",
                resp.text[:500],
            )
        data = resp.json()
        self._token   = data.get("access_token", "")
        self._expires = time.time() + float(data.get("expires_in", 3600))
        if not self._token:
            raise GraphError(0, "Token-Response ohne access_token")
        logger.info("Guest-Print Graph-Token OK (expires_in=%ss)",
                     int(data.get("expires_in", 3600)))
        return self._token

    def invalidate(self) -> None:
        self._token   = ""
        self._expires = 0.0


_tokens = _TokenCache()


# ─── HTTP-Primitiven ─────────────────────────────────────────────────────────

def _request(method: str, path: str, *, json: Any = None,
             params: Optional[dict] = None, raw: bool = False) -> Any:
    """Fuehrt einen Graph-Request mit automatischem Token-Refresh +
    429-Retry aus. path beginnt mit '/' und ist relativ zu _GRAPH_BASE.
    """
    url = path if path.startswith("http") else _GRAPH_BASE + path
    last_err: Optional[Exception] = None
    for attempt in range(3):
        token = _tokens.get()
        headers = {"Authorization": f"Bearer {token}"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, json=json,
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            last_err = e
            time.sleep(1 + attempt)
            continue

        # Token abgelaufen trotz Puffer (Clock-Skew o.ae.) -> einmal refresh
        if resp.status_code == 401 and attempt == 0:
            _tokens.invalidate()
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2") or "2")
            logger.warning("Graph 429, warte %ds (attempt %d)", wait, attempt + 1)
            time.sleep(min(wait, 10))
            continue

        if resp.status_code >= 400:
            raise GraphError(resp.status_code, resp.reason, resp.text[:500])

        if raw:
            return resp.content
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    raise GraphError(0, f"Graph-Request nach 3 Versuchen fehlgeschlagen: {last_err}")


def _get(path: str, params: Optional[dict] = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, payload: Any = None) -> Any:
    return _request("POST", path, json=payload)


def _get_raw(path: str, params: Optional[dict] = None) -> bytes:
    return _request("GET", path, params=params, raw=True)


# ─── Mail-Operationen ────────────────────────────────────────────────────────

def _user_path(upn: str) -> str:
    # Graph erlaubt UPN direkt im Pfad; wir URL-encoden den '@' nicht,
    # Graph toleriert das. Bei Sonderzeichen quote() waere sicherer.
    return f"/users/{upn}"


def list_unread_with_attachments(upn: str, top: int = 50) -> list[dict]:
    """Listet ungelesene Inbox-Nachrichten mit Anhaengen.

    Returns: list[{id, subject, from_email, received_at, has_attachments}]
    """
    params = {
        "$filter":  "hasAttachments eq true and isRead eq false",
        "$select":  "id,subject,from,receivedDateTime,hasAttachments",
        "$top":     str(int(top)),
        "$orderby": "receivedDateTime asc",
    }
    resp = _get(f"{_user_path(upn)}/mailFolders/inbox/messages", params)
    out: list[dict] = []
    for m in resp.get("value", []) or []:
        frm = (m.get("from") or {}).get("emailAddress") or {}
        out.append({
            "id":               m.get("id", ""),
            "subject":          m.get("subject", "") or "",
            "from_email":       (frm.get("address") or "").strip().lower(),
            "from_name":        frm.get("name", "") or "",
            "received_at":      m.get("receivedDateTime", "") or "",
            "has_attachments":  bool(m.get("hasAttachments")),
        })
    return out


def list_attachments(upn: str, message_id: str) -> list[dict]:
    """Listet die Attachment-Metadaten einer Nachricht (ohne contentBytes).

    Returns: list[{id, name, content_type, size, is_inline, odata_type}]
    """
    params = {
        "$select": "id,name,contentType,size,isInline",
    }
    resp = _get(f"{_user_path(upn)}/messages/{message_id}/attachments", params)
    out: list[dict] = []
    for a in resp.get("value", []) or []:
        out.append({
            "id":            a.get("id", ""),
            "name":          a.get("name", "") or "",
            "content_type":  a.get("contentType", "") or "",
            "size":          int(a.get("size") or 0),
            "is_inline":     bool(a.get("isInline")),
            "odata_type":    a.get("@odata.type", ""),
        })
    return out


def download_attachment(upn: str, message_id: str,
                         attachment_id: str) -> tuple[str, str, bytes]:
    """Holt einen Anhang inkl. Content. Funktioniert zuverlaessig fuer
    fileAttachment <= 3 MB via $value (Binaer-Stream). Groessere Anhaenge
    via contentBytes (base64) im JSON-Body — beides hier unterstuetzt.

    Returns: (name, content_type, bytes)
    """
    # Metadaten zuerst (Name/Content-Type)
    meta = _get(
        f"{_user_path(upn)}/messages/{message_id}/attachments/{attachment_id}",
        params={"$select": "id,name,contentType,size,contentBytes"},
    )
    name = meta.get("name", "") or ""
    ctype = meta.get("contentType", "") or ""
    b64 = meta.get("contentBytes")
    if b64:
        try:
            return name, ctype, base64.b64decode(b64)
        except Exception as e:
            raise GraphError(0, f"Base64-Decode fehlgeschlagen: {e}")
    # Fallback: Binaer-Stream ($value)
    content = _get_raw(
        f"{_user_path(upn)}/messages/{message_id}/attachments/{attachment_id}/$value"
    )
    return name, ctype, content or b""


def move_message(upn: str, message_id: str, destination_folder_id: str) -> str:
    """Verschiebt eine Nachricht in einen Ordner. Returns: neue message_id
    (Graph erzeugt beim Move eine neue ID im Ziel-Ordner)."""
    resp = _post(
        f"{_user_path(upn)}/messages/{message_id}/move",
        {"destinationId": destination_folder_id},
    )
    return resp.get("id", "") if isinstance(resp, dict) else ""


# ─── Ordner-Handling ─────────────────────────────────────────────────────────

def _list_child_folders(upn: str, parent_folder_id: str) -> list[dict]:
    resp = _get(
        f"{_user_path(upn)}/mailFolders/{parent_folder_id}/childFolders",
        params={"$select": "id,displayName", "$top": "100"},
    )
    return [
        {"id": f.get("id", ""), "name": f.get("displayName", "")}
        for f in (resp.get("value") or [])
    ]


def _create_child_folder(upn: str, parent_folder_id: str, name: str) -> str:
    resp = _post(
        f"{_user_path(upn)}/mailFolders/{parent_folder_id}/childFolders",
        {"displayName": name},
    )
    fid = resp.get("id", "") if isinstance(resp, dict) else ""
    if not fid:
        raise GraphError(0, f"Folder-Create lieferte keine id fuer '{name}'")
    return fid


def ensure_folder_path(upn: str, path: str, parent: str = "inbox") -> str:
    """Stellt sicher, dass ein (ggf. geschachtelter) Ordner existiert.
    Pfad ist slash-separiert, relativ zum `parent` (well-known name oder
    Folder-ID). Gibt die Folder-ID des Blatts zurueck.

    Leerer Pfad -> gibt die Parent-ID zurueck (nach Aufloesung).
    """
    # Parent aufloesen — well-known aliases wie 'inbox' gehen direkt,
    # ansonsten nehmen wir das Argument als Folder-ID an.
    parent_id = parent
    parts = [p.strip() for p in (path or "").split("/") if p.strip()]
    if not parts:
        # Parent-ID in echter ID aufloesen (falls 'inbox' gegeben)
        resp = _get(f"{_user_path(upn)}/mailFolders/{parent}",
                    params={"$select": "id"})
        return resp.get("id", "") or parent

    for part in parts:
        children = _list_child_folders(upn, parent_id)
        hit = next(
            (c for c in children
             if (c["name"] or "").strip().lower() == part.lower()),
            None,
        )
        if hit:
            parent_id = hit["id"]
        else:
            parent_id = _create_child_folder(upn, parent_id, part)
    return parent_id


# ─── Smoke-Test-Helper (wird von Admin-UI aufgerufen) ────────────────────────

def test_connection(upn: str) -> dict:
    """Pruef-Call: holt 1 ungelesene Mail und liefert Diagnose-Info.
    Wird vom Admin-UI beim Speichern der Postfach-Config benutzt.
    """
    try:
        _tokens.invalidate()
        _ = _tokens.get()
        msgs = _get(
            f"{_user_path(upn)}/mailFolders/inbox/messages",
            params={"$select": "id", "$top": "1"},
        )
        return {
            "ok":     True,
            "token":  "ok",
            "sample": len((msgs or {}).get("value", []) or []),
        }
    except GraphError as e:
        return {
            "ok":     False,
            "status": e.status,
            "error":  str(e),
            "detail": e.detail,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
