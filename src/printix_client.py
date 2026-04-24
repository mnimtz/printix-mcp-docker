"""
Printix API Client
Handles OAuth2 token management and all Printix Cloud Print API calls.

Printix stellt pro API-Bereich eigene Client-Credentials aus:
  - Print API        → PRINTIX_PRINT_CLIENT_ID / PRINTIX_PRINT_CLIENT_SECRET
  - Card Management  → PRINTIX_CARD_CLIENT_ID  / PRINTIX_CARD_CLIENT_SECRET
  - Workstation Mon. → PRINTIX_WS_CLIENT_ID    / PRINTIX_WS_CLIENT_SECRET

Auth:     https://auth.printix.net/oauth/token
API Base: https://api.printix.net/cloudprint
"""

import logging
import time
import base64
import requests
from typing import Optional, Any

logger = logging.getLogger("printix_client")


class PrintixAPIError(Exception):
    """Raised when the Printix API returns an error."""
    def __init__(self, status_code: int, message: str, error_id: str = ""):
        self.status_code = status_code
        self.message = message
        self.error_id = error_id
        super().__init__(f"Printix API Error {status_code}: {message} (ErrorID: {error_id})")


def _is_base64(s: str) -> bool:
    try:
        return base64.b64encode(base64.b64decode(s)).decode() == s
    except Exception:
        return False


class _TokenManager:
    """OAuth2 Client Credentials token cache for one credential pair."""

    AUTH_URL = "https://auth.printix.net/oauth/token"

    def __init__(self, client_id: str, client_secret: str, label: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.label = label
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._session = requests.Session()

    def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        logger.debug("OAuth token request: %s", self.label)
        resp = self._session.post(
            self.AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if not resp.ok:
            raise PrintixAPIError(resp.status_code,
                                  f"Token request failed for '{self.label}': {resp.text}")
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        logger.info("OAuth token OK: %s (expires_in=%ss)", self.label, data.get("expires_in", 3600))
        return self._token


class PrintixClient:
    """
    Printix Cloud Print API Client.

    Supports separate credentials per API area (Print / Card / Workstation).
    Falls back to shared credentials if area-specific ones are not set.

    Rate limit: 100 req/min per user.
    """

    BASE_URL = "https://api.printix.net/cloudprint"

    def __init__(
        self,
        tenant_id: str,
        # Print API credentials
        print_client_id: Optional[str] = None,
        print_client_secret: Optional[str] = None,
        # Card Management credentials
        card_client_id: Optional[str] = None,
        card_client_secret: Optional[str] = None,
        # Workstation Monitoring credentials
        ws_client_id: Optional[str] = None,
        ws_client_secret: Optional[str] = None,
        # User Management credentials
        um_client_id: Optional[str] = None,
        um_client_secret: Optional[str] = None,
        # Shared fallback credentials (used if area-specific ones are missing)
        shared_client_id: Optional[str] = None,
        shared_client_secret: Optional[str] = None,
    ):
        self.tenant_id = tenant_id
        self._session = requests.Session()

        def _tm(cid, csec, label):
            effective_id = cid or shared_client_id
            effective_sec = csec or shared_client_secret
            if not effective_id or not effective_sec:
                return None
            return _TokenManager(effective_id, effective_sec, label)

        self._print_tm = _tm(print_client_id, print_client_secret, "Print API")
        self._card_tm = _tm(card_client_id, card_client_secret, "Card Management")
        self._ws_tm = _tm(ws_client_id, ws_client_secret, "Workstation Monitoring")
        self._um_tm = _tm(um_client_id, um_client_secret, "User Management")

    def _require_tm(self, tm: Optional[_TokenManager], area: str) -> _TokenManager:
        if tm is None:
            raise PrintixAPIError(
                0,
                f"No credentials configured for '{area}'. "
                f"Please set the corresponding environment variables.",
            )
        return tm

    def _user_lookup_tm(self) -> _TokenManager:
        """Best-effort credentials for read-only user lookups.

        Prefer User Management (new, broadest scope), then Card Management,
        then Print API so LPR/cloud-print flows can still resolve owner hints.
        """
        return self._require_tm(
            self._um_tm or self._card_tm or self._print_tm,
            "User Management, Card Management or Print API",
        )

    def _user_management_tm(self) -> _TokenManager:
        """Credentials for write user operations (create/delete regular USER).

        Prefer User Management API when available — it supports role=USER
        create/delete. Falls back to Card Management which historically
        only supported GUEST_USER.
        """
        return self._require_tm(
            self._um_tm or self._card_tm,
            "User Management or Card Management",
        )

    def _headers(self, tm: _TokenManager, extra: Optional[dict] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {tm.get_token()}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        return f"{self.BASE_URL}/tenants/{self.tenant_id}{path}"

    def _handle_response(self, resp: requests.Response) -> Any:
        if resp.status_code == 429:
            retry = resp.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
            logger.warning("Rate limit hit (retry-after=%ss): %s", retry, resp.url)
            raise PrintixAPIError(429, f"Rate limit exceeded. Retry after {retry}s.")
        if not resp.ok:
            try:
                err = resp.json()
                # v4.6.8: Printix uses errorText/message, not description
                msg = (err.get("errorText")
                       or err.get("message")
                       or err.get("description")
                       or resp.text)
                err_id = err.get("printix-errorId") or err.get("errorId") or ""
            except Exception:
                msg = resp.text
                err_id = ""
            logger.error("API %s %s → %s: %s", resp.request.method if resp.request else '?', resp.url, resp.status_code, msg)
            raise PrintixAPIError(resp.status_code, msg, err_id)
        if resp.status_code == 204 or not resp.content:
            return {"success": True}
        logger.debug("API %s %s → %s", resp.request.method if resp.request else '?', resp.url, resp.status_code)
        return resp.json()

    def _get(self, tm: _TokenManager, path: str, params: Optional[dict] = None) -> Any:
        resp = self._session.get(self._url(path), headers=self._headers(tm),
                                 params=params, timeout=30)
        return self._handle_response(resp)

    def _post(self, tm: _TokenManager, path: str, json: Optional[dict] = None,
              data: Optional[dict] = None, content_type: Optional[str] = None,
              params: Optional[dict] = None) -> Any:
        extra = {"Content-Type": content_type} if content_type else None
        resp = self._session.post(self._url(path), headers=self._headers(tm, extra),
                                  json=json, data=data, params=params, timeout=60)
        return self._handle_response(resp)

    def _put(self, tm: _TokenManager, path: str, json: Optional[dict] = None) -> Any:
        resp = self._session.put(self._url(path), headers=self._headers(tm),
                                 json=json, timeout=30)
        return self._handle_response(resp)

    def _patch(self, tm: _TokenManager, path: str, json: Optional[dict] = None) -> Any:
        resp = self._session.patch(self._url(path), headers=self._headers(tm),
                                   json=json, timeout=30)
        return self._handle_response(resp)

    def _delete(self, tm: _TokenManager, path: str) -> Any:
        resp = self._session.delete(self._url(path), headers=self._headers(tm), timeout=30)
        return self._handle_response(resp)

    # ─── Token Status ──────────────────────────────────────────────────────────

    def get_credential_status(self) -> dict:
        """Returns which credential areas are configured."""
        return {
            "print_api": self._print_tm is not None,
            "card_management": self._card_tm is not None,
            "workstation_monitoring": self._ws_tm is not None,
            "tenant_id": self.tenant_id,
        }

    # ─── Print Queues / Printers ───────────────────────────────────────────────

    def list_printers(self, search: Optional[str] = None,
                       page: int = 0, size: int = 50) -> Any:
        """List all print queues/printers for the tenant."""
        tm = self._require_tm(self._print_tm, "Print API")
        params: dict = {"page": page, "pageSize": size}
        if search:
            params["query"] = search
        return self._get(tm, "/printers", params=params)

    def get_printer(self, printer_id: str, queue_id: str) -> Any:
        """Get details and capabilities of a specific print queue.
        Endpoint: GET /printers/{printer_id}/queues/{queue_id}
        Both IDs can be extracted from the _links.self.href in list_printers output."""
        tm = self._require_tm(self._print_tm, "Print API")
        return self._get(tm, f"/printers/{printer_id}/queues/{queue_id}", params={"page": 0})

    # ─── Print Jobs ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_submit_pdl(pdl: Optional[str]) -> Optional[str]:
        """Map MIME-style format detection to the Printix submit PDL enum."""
        if not pdl:
            return None
        normalized = pdl.strip().upper()
        mime_map = {
            "APPLICATION/PDF": "PDF",
            "APPLICATION/POSTSCRIPT": "POSTSCRIPT",
            "APPLICATION/VND.HP-PCL": "PCL5",
            "APPLICATION/PCL": "PCL5",
            "APPLICATION/PCLXL": "PCLXL",
            "APPLICATION/VND.HP-PCLXL": "PCLXL",
            "TEXT/PLAIN": "TEXT",
            "APPLICATION/OCTET-STREAM": None,
        }
        return mime_map.get(normalized, normalized)

    def submit_print_job(self, printer_id: str, queue_id: str, title: str,
                          user: Optional[str] = None,
                          pdl: Optional[str] = None,
                          release_immediately: bool = True,
                          color: Optional[bool] = None,
                          duplex: Optional[str] = None,
                          copies: Optional[int] = None,
                          paper_size: Optional[str] = None,
                          orientation: Optional[str] = None,
                          scaling: Optional[str] = None) -> Any:
        """Submit a new print job (API v1.1).
        Endpoint: POST /printers/{printer_id}/queues/{queue_id}/submit?title=...
        Requires header 'version: 1.1' for the structured body format.
        color: True=color, False=monochrome (boolean, not string).
        duplex: NONE | SHORT_EDGE | LONG_EDGE.
        scaling: NOSCALE | SHRINK | FIT.
        paper_size: A4 | A3 | LETTER | LEGAL | A0–A5 | B4–B5 etc.
        orientation: PORTRAIT | LANDSCAPE | AUTO.
        pdl: PCL5 | PCLXL | POSTSCRIPT | UFRII | TEXT | XPS."""
        tm = self._require_tm(self._print_tm, "Print API")
        params: dict = {"title": title, "releaseImmediately": str(release_immediately).lower()}
        if user:
            params["user"] = user
        normalized_pdl = self._normalize_submit_pdl(pdl)
        if normalized_pdl:
            params["PDL"] = normalized_pdl
        body: dict = {}
        if color is not None:
            body["color"] = color
        if duplex:
            body["duplex"] = duplex
        if copies:
            body["copies"] = copies
        if paper_size:
            body["media_size"] = paper_size
        if orientation:
            body["page_orientation"] = orientation
        if scaling:
            body["scaling"] = scaling
        extra_headers = {"version": "1.1", "Content-Type": "application/json"}
        resp = self._session.post(
            self._url(f"/printers/{printer_id}/queues/{queue_id}/submit"),
            headers=self._headers(tm, extra_headers),
            params=params,
            json=body if body else {},
            timeout=60,
        )
        return self._handle_response(resp)

    def upload_file_to_url(self, upload_url: str, file_bytes: bytes,
                            content_type: str = "application/pdf",
                            extra_headers: Optional[dict] = None) -> Any:
        """Upload a file to the cloud storage URL returned from submit_print_job.
        Note: This call goes directly to cloud storage, NOT the Printix API."""
        headers = {"Content-Type": content_type}
        if extra_headers:
            headers.update({k: str(v) for k, v in extra_headers.items() if v is not None})
        resp = requests.put(upload_url, data=file_bytes,
                            headers=headers, timeout=120)
        if not resp.ok:
            raise PrintixAPIError(resp.status_code, f"File upload failed: {resp.text}")
        return {"success": True}

    def complete_upload(self, job_id: str) -> Any:
        """Signal that file upload is complete; this triggers printing.
        Endpoint: POST /jobs/{job_id}/completeUpload (application/x-www-form-urlencoded, no body)"""
        tm = self._require_tm(self._print_tm, "Print API")
        return self._post(tm, f"/jobs/{job_id}/completeUpload",
                          content_type="application/x-www-form-urlencoded")

    def change_job_owner(self, job_id: str, user_email: str) -> Any:
        """v6.7.15: Ownership eines gesubmitteten Print-Jobs auf einen anderen
        User übertragen.

        Hintergrund: Der `user=`-Parameter beim `submit_print_job` wird von
        Printix IGNORIERT — jeder via OAuth-App gesubmittete Job bekommt
        automatisch den App-Owner (typisch: System-Manager) als ownerId.
        Um Delegate-Print / Cloud Print Port sinnvoll zu machen, muss der
        Owner nach dem Submit explizit gewechselt werden via separatem
        Endpoint:
          POST /jobs/{job_id}/changeOwner?userEmail=<email>

        Das lief als `changeOwner`-Link im jedem Submit-Response (templated).

        Args:
            job_id:     Printix-Job-UUID (aus submit-Response)
            user_email: Ziel-Email (muss ein registrierter Printix-User sein)
        """
        tm = self._require_tm(self._print_tm, "Print API")
        extra_headers = {"version": "1.1", "Content-Type": "application/json"}
        resp = self._session.post(
            self._url(f"/jobs/{job_id}/changeOwner"),
            headers=self._headers(tm, extra_headers),
            params={"userEmail": user_email},
            json={},
            timeout=30,
        )
        return self._handle_response(resp)

    def list_print_jobs(self, queue_id: Optional[str] = None,
                         page: int = 0, size: int = 50) -> Any:
        """List print jobs, optionally filtered by queue ID."""
        tm = self._require_tm(self._print_tm, "Print API")
        params: dict = {"page": page, "pageSize": size}
        if queue_id:
            params["printQueueId"] = queue_id
        return self._get(tm, "/jobs", params=params)

    def get_print_job(self, job_id: str) -> Any:
        """Get status and details of a specific print job.
        Note: A job that moved to CONVERTING after complete_upload without an actual file
        may be removed by the backend immediately, causing a 404. This is expected behaviour."""
        tm = self._require_tm(self._print_tm, "Print API")
        try:
            return self._get(tm, f"/jobs/{job_id}")
        except PrintixAPIError as e:
            if e.status_code == 404:
                raise PrintixAPIError(
                    404,
                    f"Job '{job_id}' nicht gefunden. "
                    "Mögliche Ursache: Der Auftrag wurde nach complete_upload ohne echten "
                    "Datei-Upload vom Backend entfernt oder ist bereits abgeschlossen.",
                    e.error_id,
                ) from e
            raise

    def delete_print_job(self, job_id: str) -> Any:
        """Delete a submitted or failed print job.
        Endpoint: POST /jobs/{job_id}/delete  (POST with /delete suffix, not DELETE verb)
        Note: Jobs that completed or were removed after complete_upload return 404 — treated
        as success since the job no longer exists."""
        tm = self._require_tm(self._print_tm, "Print API")
        try:
            return self._post(tm, f"/jobs/{job_id}/delete",
                              content_type="application/x-www-form-urlencoded")
        except PrintixAPIError as e:
            if e.status_code == 404:
                return {
                    "success": True,
                    "note": f"Job '{job_id}' nicht mehr vorhanden (bereits verarbeitet oder entfernt).",
                }
            raise

    def change_job_owner(self, job_id: str, new_owner_email: str) -> Any:
        """Transfer ownership of a print job to another user.
        Endpoint: POST /jobs/{job_id}/changeOwner
        Parameter: userEmail (form-urlencoded) — note: NOT ownerEmail, NOT JSON body."""
        tm = self._require_tm(self._print_tm, "Print API")
        return self._post(
            tm,
            f"/jobs/{job_id}/changeOwner",
            data={"userEmail": new_owner_email},
            content_type="application/x-www-form-urlencoded",
        )

    # ─── Card Management ───────────────────────────────────────────────────────

    def register_card(self, user_id: str, card_number: str) -> Any:
        """Register (associate) a card with a user.
        The card_number is base64-encoded and sent as 'secret' field per API spec.
        Endpoint: POST /users/{user_id}/cards"""
        tm = self._require_tm(self._card_tm, "Card Management")
        encoded = base64.b64encode(card_number.encode()).decode() \
            if not _is_base64(card_number) else card_number
        return self._post(tm, f"/users/{user_id}/cards", json={"secret": encoded})

    def list_user_cards(self, user_id: str) -> Any:
        """List all cards associated with a specific user.
        Endpoint: GET /users/{user_id}/cards"""
        tm = self._require_tm(self._card_tm, "Card Management")
        return self._get(tm, f"/users/{user_id}/cards")

    def search_card(self, card_id: Optional[str] = None,
                     card_number: Optional[str] = None) -> Any:
        """Fetch a single card by its ID or base64-encoded card number.
        Endpoint: GET /cards/{card_id_or_b64_number}"""
        tm = self._require_tm(self._card_tm, "Card Management")
        if card_id:
            return self._get(tm, f"/cards/{card_id}")
        elif card_number:
            encoded = base64.b64encode(card_number.encode()).decode() \
                if not _is_base64(card_number) else card_number
            return self._get(tm, f"/cards/{encoded}")
        else:
            raise ValueError("Either card_id or card_number must be provided.")

    def delete_card(self, card_id: str, user_id: Optional[str] = None) -> Any:
        """Remove a card association.
        Uses DELETE /cards/{card_id} (global Card API endpoint).
        The user-scoped /users/{uid}/cards/{cid} endpoint returns 405 Method Not Allowed.
        user_id parameter kept for backward-compat but is intentionally ignored."""
        tm = self._require_tm(self._card_tm, "Card Management")
        return self._delete(tm, f"/cards/{card_id}")

    # ─── User Management ───────────────────────────────────────────────────────
    # Prefers the new User Management API credentials when set (role=USER
    # create/delete, list both roles), falls back to Card Management which
    # historically only supported GUEST_USER.
    #
    # Verified against the live API (Printix Tenant bee07…):
    #   list: role=USER,GUEST_USER (comma-separated) returns both in one call
    #   create: response is {"users":[{"id":…,"pin":…,"idCode":…,"password":…}]}
    #   delete: POST /users/{id}/delete (NOT DELETE verb — that returns 405)
    #   roles beyond USER/GUEST_USER are rejected with "Invalid role: …"
    #   GET /users/{id} returns 404 with UM creds — needs Card Management

    _ROLE_ALL = "USER,GUEST_USER"

    def create_user(self, email: str, display_name: str,
                     role: str = "GUEST_USER",
                     pin: Optional[str] = None,
                     password: Optional[str] = None,
                     expiration_timestamp: Optional[str] = None,
                     send_welcome_email: bool = False,
                     send_expiration_email: bool = False,
                     id_code: Optional[str] = None) -> Any:
        """Create a user account.

        role:
            'USER'       — regular e-mail user (needs User Management creds).
            'GUEST_USER' — guest with expiration (default, works with Card Mgmt).

        Endpoint: POST /users/create

        The API auto-generates pin/idCode/password if not supplied. The
        generated values are ONLY returned in this create response — they
        cannot be retrieved later. Callers should persist them if needed.

        Response shape (live API):
            {
              "tenantId": "...",
              "success": true,
              "message": "OK",
              "users": [
                {"id": "...", "email": "...", "fullName": "...", "role": "USER",
                 "pin": "4963", "idCode": "723132", "password": "Vi8s3pas",
                 "sendWelcomeEmail": false, "sendExpirationEmail": false}
              ],
              "page": {"size": 1, "totalElements": 1, ...}
            }

        Note: Entra-ID / federated-user creation is NOT supported via this
        endpoint — extra fields like identityProvider/externalId are silently
        dropped by the API and a plain local user is created.
        """
        tm = self._user_management_tm()
        role_upper = (role or "GUEST_USER").upper()
        payload: dict = {
            "email": email,
            "fullName": display_name,
            "role": role_upper,
            "sendWelcomeEmail": bool(send_welcome_email),
        }
        if send_expiration_email:
            payload["sendExpirationEmail"] = True
        if pin:
            payload["pin"] = pin
        if password:
            payload["password"] = password
        if id_code:
            payload["idCode"] = id_code
        if expiration_timestamp:
            payload["expirationTimestamp"] = expiration_timestamp
        return self._post(tm, "/users/create", json=payload)

    def list_users(self, role: Optional[str] = None,
                    query: Optional[str] = None,
                    page: int = 0, page_size: int = 50) -> Any:
        """List users.

        role:
            None          — returns BOTH USER and GUEST_USER in one call
                            (sends role=USER,GUEST_USER which the API accepts).
            'USER'        — only regular users.
            'GUEST_USER'  — only guests (what the raw API default returns).
            'USER,GUEST_USER' — explicit combined listing.

        Only USER and GUEST_USER are accepted; system/site/kiosk manager
        accounts are not exposed through this API (API returns 400 for other
        role values).

        query: server-side name/email substring match (NOT card data).

        Prefers User Management creds when available.
        """
        tm = self._user_lookup_tm()
        params: dict = {"page": page, "pageSize": page_size}
        # Default: list both roles via comma-list so a single call returns
        # every user the API can see — matches what the Printix admin UI shows
        # for USER and GUEST filters combined.
        params["role"] = role if role else self._ROLE_ALL
        if query:
            params["query"] = query
        return self._get(tm, "/users", params=params)

    def list_all_users(self, query: Optional[str] = None,
                        page_size: int = 200,
                        max_pages: int = 20) -> list[dict]:
        """Fetches every user across all pages in a single flat list.

        Uses the combined role=USER,GUEST_USER listing and walks pages until
        fewer than page_size items come back (or max_pages is reached as a
        safety net). Returns the raw user dicts as provided by the API.
        """
        out: list[dict] = []
        seen: set[str] = set()
        for pg in range(max_pages):
            data = self.list_users(query=query, page=pg, page_size=page_size)
            users = []
            if isinstance(data, dict):
                users = data.get("users") or data.get("content") or []
                if not isinstance(users, list):
                    users = []
            for u in users:
                uid = u.get("id", "")
                if uid and uid not in seen:
                    seen.add(uid)
                    out.append(u)
            if len(users) < page_size:
                break
        return out

    def get_user(self, user_id: str) -> Any:
        """Get details of a specific user.

        Note: with User Management credentials this endpoint returns 404 for
        existing users — the API scope is list-only for the UM application.
        Card Management credentials (self._card_tm) must be used instead for
        detail lookups. We keep preferring Card Management here.
        """
        tm = self._require_tm(
            self._card_tm or self._um_tm or self._print_tm,
            "Card Management, User Management or Print API",
        )
        return self._get(tm, f"/users/{user_id}")

    def delete_user(self, user_id: str) -> Any:
        """Delete a user (USER or GUEST_USER).
        Endpoint: POST /users/{user_id}/delete  (POST — DELETE verb returns 405)

        With the new User Management API this works for regular users too,
        not just guests. System/Site/Kiosk managers cannot be deleted
        through the API (they are not listable either)."""
        tm = self._user_management_tm()
        return self._post(tm, f"/users/{user_id}/delete")

    def generate_id_code(self, user_id: str) -> Any:
        """Generate a new 6-digit ID code for a user.
        Endpoint: POST /users/{user_id}/idCode  (camelCase — case-sensitive)"""
        tm = self._require_tm(
            self._card_tm or self._um_tm,
            "Card Management or User Management",
        )
        return self._post(tm, f"/users/{user_id}/idCode")

    @staticmethod
    def extract_created_user(create_response: Any) -> dict:
        """Unwrap the user object from a /users/create response.

        The create endpoint returns a page-wrapper shape:
            {"users": [{...}], "success": true, "page": {...}}
        This helper picks the first user dict, or returns {} if not found.
        """
        if not isinstance(create_response, dict):
            return {}
        users = create_response.get("users")
        if isinstance(users, list) and users:
            first = users[0]
            if isinstance(first, dict):
                return first
        return {}

    # ─── Group Management ──────────────────────────────────────────────────────

    def create_group(self, name: str, external_id: str,
                      identity_provider: Optional[str] = None,
                      description: Optional[str] = None) -> Any:
        """Create a group.
        external_id: Required ID of the group in the external directory (e.g. Azure AD group ID)."""
        tm = self._require_tm(self._print_tm, "Print API")
        payload: dict = {"name": name, "externalId": external_id}
        if identity_provider:
            payload["identityProvider"] = identity_provider
        if description:
            payload["description"] = description
        return self._post(tm, "/groups", json=payload)

    def list_groups(self, search: Optional[str] = None,
                     page: int = 0, size: int = 50) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        params: dict = {"page": page, "pageSize": size}
        if search:
            params["query"] = search
        return self._get(tm, "/groups", params=params)

    def get_group(self, group_id: str) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        return self._get(tm, f"/groups/{group_id}")

    def delete_group(self, group_id: str) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        return self._delete(tm, f"/groups/{group_id}")

    # ─── Workstation Monitoring ────────────────────────────────────────────────

    def list_workstations(self, search: Optional[str] = None,
                           site_id: Optional[str] = None,
                           page: int = 0, size: int = 50) -> Any:
        """List workstations, optionally filtered by site or search term."""
        tm = self._require_tm(self._ws_tm, "Workstation Monitoring")
        params: dict = {"page": page, "pageSize": size}
        if search:
            params["query"] = search
        if site_id:
            params["siteId"] = site_id
        return self._get(tm, "/workstations", params=params)

    def get_workstation(self, workstation_id: str) -> Any:
        """Get details of a specific workstation."""
        tm = self._require_tm(self._ws_tm, "Workstation Monitoring")
        return self._get(tm, f"/workstations/{workstation_id}")

    # ─── Sites ─────────────────────────────────────────────────────────────────

    def list_sites(self, search: Optional[str] = None,
                    page: int = 0, size: int = 50) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        params: dict = {"page": page, "pageSize": size}
        if search:
            params["query"] = search
        return self._get(tm, "/sites", params=params)

    def get_site(self, site_id: str) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        return self._get(tm, f"/sites/{site_id}")

    def create_site(self, name: str, path: str,
                     admin_group_ids: Optional[list] = None,
                     network_ids: Optional[list] = None) -> Any:
        """Create a site.
        path: Required path for the site, e.g. '/Europe/Germany/Munich'
        admin_group_ids: Optional list of admin group IDs.
        network_ids: Optional list of network IDs to assign to the site."""
        tm = self._require_tm(self._print_tm, "Print API")
        payload: dict = {
            "name": name,
            "path": path,
            "adminGroupIds": admin_group_ids or [],
            "networkIds": network_ids or [],
        }
        return self._post(tm, "/sites", json=payload)

    def update_site(self, site_id: str, name: Optional[str] = None,
                     path: Optional[str] = None,
                     admin_group_ids: Optional[list] = None,
                     network_ids: Optional[list] = None) -> Any:
        """Update a site.
        path: Required by the API even for updates (e.g. '/Europe/Germany/Munich').
        All fields are optional but path must be provided to avoid VALIDATION_FAILED."""
        tm = self._require_tm(self._print_tm, "Print API")
        payload: dict = {}
        if name is not None:
            payload["name"] = name
        if path is not None:
            payload["path"] = path
        if admin_group_ids is not None:
            payload["adminGroupIds"] = admin_group_ids
        if network_ids is not None:
            payload["networkIds"] = network_ids
        return self._put(tm, f"/sites/{site_id}", json=payload)

    def delete_site(self, site_id: str) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        return self._delete(tm, f"/sites/{site_id}")

    # ─── Networks ──────────────────────────────────────────────────────────────

    def list_networks(self, site_id: Optional[str] = None,
                       page: int = 0, size: int = 50) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        params: dict = {"page": page, "pageSize": size}
        if site_id:
            params["siteId"] = site_id
        return self._get(tm, "/networks", params=params)

    def get_network(self, network_id: str) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        return self._get(tm, f"/networks/{network_id}")

    def create_network(self, name: str,
                        home_office: bool = False,
                        client_migrate_print_queues: str = "GLOBAL_SETTING",
                        air_print: bool = False,
                        site_id: Optional[str] = None,
                        gateway_mac: Optional[str] = None,
                        gateway_ip: Optional[str] = None) -> Any:
        """Create a network.
        client_migrate_print_queues: 'GLOBAL_SETTING', 'YES', or 'NO'.
        gateway_mac/ip: Optional gateway MAC and IP address."""
        tm = self._require_tm(self._print_tm, "Print API")
        payload: dict = {
            "name": name,
            "homeOffice": home_office,
            "clientMigratePrintQueues": client_migrate_print_queues,
            "airPrint": air_print,
            "siteId": site_id,
            "gateways": [],
        }
        if gateway_mac:
            gw: dict = {"mac": gateway_mac}
            if gateway_ip:
                gw["ip"] = gateway_ip
            payload["gateways"] = [gw]
        return self._post(tm, "/networks", json=payload)

    def update_network(self, network_id: str,
                        name: Optional[str] = None,
                        subnet: Optional[str] = None,
                        home_office: Optional[bool] = None,
                        client_migrate_print_queues: Optional[str] = None,
                        air_print: Optional[bool] = None,
                        site_id: Optional[str] = None) -> Any:
        """Update a network.
        The PUT endpoint requires homeOffice, clientMigratePrintQueues and airPrint to be
        present even for partial updates. This method GETs the current network first and
        merges provided values over the existing ones to avoid VALIDATION_FAILED.
        client_migrate_print_queues: 'GLOBAL_SETTING', 'YES', or 'NO'."""
        tm = self._require_tm(self._print_tm, "Print API")
        # Fetch current state — all existing fields must be carried over to avoid data loss
        current = self._get(tm, f"/networks/{network_id}")
        payload: dict = {
            "name": name if name is not None else current.get("name", ""),
            "homeOffice": home_office if home_office is not None
                          else current.get("homeOffice", False),
            "clientMigratePrintQueues": client_migrate_print_queues
                                        if client_migrate_print_queues is not None
                                        else current.get("clientMigratePrintQueues",
                                                         "GLOBAL_SETTING"),
            "airPrint": air_print if air_print is not None
                        else current.get("airPrint", False),
            # Preserve existing gateways — omitting them clears the list silently
            "gateways": current.get("gateways", []),
            # Preserve existing siteId
            "siteId": site_id if site_id is not None else current.get("siteId"),
        }
        if subnet is not None:
            payload["subnet"] = subnet
        # Remove None siteId to avoid sending null
        if payload["siteId"] is None:
            del payload["siteId"]
        return self._put(tm, f"/networks/{network_id}", json=payload)

    def delete_network(self, network_id: str) -> Any:
        tm = self._require_tm(self._print_tm, "Print API")
        return self._delete(tm, f"/networks/{network_id}")

    # ─── SNMP Configurations ───────────────────────────────────────────────────

    def list_snmp_configs(self, page: int = 0, size: int = 50) -> Any:
        """List SNMP configurations. Endpoint: GET /snmp"""
        tm = self._require_tm(self._print_tm, "Print API")
        return self._get(tm, "/snmp", params={"page": page, "pageSize": size})

    def get_snmp_config(self, config_id: str) -> Any:
        """Get a single SNMP configuration. Endpoint: GET /snmp/{id}"""
        tm = self._require_tm(self._print_tm, "Print API")
        return self._get(tm, f"/snmp/{config_id}")

    def create_snmp_config(self, name: str,
                            get_community_name: Optional[str] = None,
                            set_community_name: Optional[str] = None,
                            tenant_default: Optional[bool] = None,
                            security_level: Optional[str] = None,
                            version: Optional[str] = None,
                            username: Optional[str] = None,
                            context_name: Optional[str] = None,
                            authentication: Optional[str] = None,
                            authentication_key: Optional[str] = None,
                            privacy: Optional[str] = None,
                            privacy_key: Optional[str] = None,
                            network_ids: Optional[list] = None) -> Any:
        """Create an SNMP configuration.
        Endpoint: POST /snmp
        version: V1 | V2C | V3  (uppercase).
        security_level: NO_AUTH_NO_PRIVACY | AUTH_NO_PRIVACY | AUTH_PRIVACY (V3 only).
        authentication: MD5 | SHA | SHA256 | SHA384 | SHA512 (V3 only).
        privacy: DES | AES | AES192 | AES256 (V3 only).

        IMPORTANT: For V1/V2C only send name, community strings, tenantDefault and version.
        Sending V3-only fields (privacy, authentication, securityLevel, username) with
        V1/V2C causes BAD_REQUEST from the backend."""
        tm = self._require_tm(self._print_tm, "Print API")
        is_v3 = (version or "").upper() == "V3"
        payload: dict = {"name": name}
        # Fields valid for all versions
        for k, v in [
            ("version", version),
            ("tenantDefault", tenant_default),
            ("networkIds", network_ids),
        ]:
            if v is not None:
                payload[k] = v
        # Community strings: V1 and V2C only
        if not is_v3:
            for k, v in [
                ("getCommunityName", get_community_name),
                ("setCommunityName", set_community_name),
            ]:
                if v is not None:
                    payload[k] = v
        # V3-only fields: only include when version is V3
        if is_v3:
            for k, v in [
                ("securityLevel", security_level),
                ("username", username),
                ("contextName", context_name),
                ("authentication", authentication),
                ("authenticationKey", authentication_key),
                ("privacy", privacy),
                ("privacyKey", privacy_key),
            ]:
                if v is not None:
                    payload[k] = v
        return self._post(tm, "/snmp", json=payload)

    def update_snmp_config(self, config_id: str,
                            name: Optional[str] = None,
                            get_community_name: Optional[str] = None,
                            set_community_name: Optional[str] = None,
                            tenant_default: Optional[bool] = None,
                            security_level: Optional[str] = None,
                            version: Optional[str] = None,
                            username: Optional[str] = None,
                            context_name: Optional[str] = None,
                            authentication: Optional[str] = None,
                            authentication_key: Optional[str] = None,
                            privacy: Optional[str] = None,
                            privacy_key: Optional[str] = None,
                            network_ids: Optional[list] = None) -> Any:
        """Update an SNMP configuration. Endpoint: PUT /snmp/{id}"""
        tm = self._require_tm(self._print_tm, "Print API")
        payload = {}
        for k, v in [
            ("name", name),
            ("getCommunityName", get_community_name),
            ("setCommunityName", set_community_name),
            ("tenantDefault", tenant_default),
            ("securityLevel", security_level),
            ("version", version),
            ("username", username),
            ("contextName", context_name),
            ("authentication", authentication),
            ("authenticationKey", authentication_key),
            ("privacy", privacy),
            ("privacyKey", privacy_key),
            ("networkIds", network_ids),
        ]:
            if v is not None:
                payload[k] = v
        return self._put(tm, f"/snmp/{config_id}", json=payload)

    def delete_snmp_config(self, config_id: str) -> Any:
        """Delete an SNMP configuration. Endpoint: DELETE /snmp/{id}"""
        tm = self._require_tm(self._print_tm, "Print API")
        return self._delete(tm, f"/snmp/{config_id}")
