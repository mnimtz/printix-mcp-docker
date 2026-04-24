"""
Paperless-ngx Plugin — Dokumente an Paperless-ngx weiterleiten (v4.4.12)
========================================================================
Lädt das Dokument von der Azure Blob SAS URL herunter und sendet es
an die Paperless-ngx REST API: POST /api/documents/post_document/

Die API erwartet IDs (integers) für Tags, Correspondent und Document Type.
Dieses Plugin löst konfigurierte Namen automatisch in IDs auf und legt
fehlende Einträge bei Bedarf automatisch an.

Konfiguration:
  - paperless_url: Base-URL der Paperless-Instanz (z.B. http://192.168.1.10:8000)
  - paperless_token: API-Token für die Authentifizierung
  - default_tags: Komma-getrennte Tag-Namen (optional) — werden zu IDs aufgelöst
  - default_correspondent: Korrespondent-Name (optional) — wird zu ID aufgelöst
  - default_document_type: Dokumenttyp-Name (optional) — wird zu ID aufgelöst
"""

import logging
import mimetypes
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

from capture.base_plugin import CapturePlugin, register_plugin


async def _resolve_tag_id(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: dict,
    tag_name: str,
) -> int | None:
    """Resolve a tag name to its ID. Creates the tag if it doesn't exist."""
    # Search by exact name (case-insensitive)
    url = f"{base_url}/api/tags/?name__iexact={tag_name}"
    async with session.get(url, headers=headers) as resp:
        if resp.status == 200:
            data = await resp.json()
            results = data.get("results", [])
            if results:
                tag_id = results[0]["id"]
                logger.debug("Tag '%s' → ID %d", tag_name, tag_id)
                return tag_id

    # Tag doesn't exist — create it
    logger.info("Creating tag '%s' in Paperless-ngx", tag_name)
    create_url = f"{base_url}/api/tags/"
    async with session.post(
        create_url, json={"name": tag_name}, headers=headers
    ) as resp:
        if resp.status in (200, 201):
            data = await resp.json()
            tag_id = data["id"]
            logger.info("Created tag '%s' → ID %d", tag_name, tag_id)
            return tag_id
        else:
            text = await resp.text()
            logger.warning("Failed to create tag '%s': HTTP %d — %s", tag_name, resp.status, text[:200])
            return None


async def _resolve_correspondent_id(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: dict,
    name: str,
) -> int | None:
    """Resolve a correspondent name to its ID. Creates if not found."""
    url = f"{base_url}/api/correspondents/?name__iexact={name}"
    async with session.get(url, headers=headers) as resp:
        if resp.status == 200:
            data = await resp.json()
            results = data.get("results", [])
            if results:
                cid = results[0]["id"]
                logger.debug("Correspondent '%s' → ID %d", name, cid)
                return cid

    logger.info("Creating correspondent '%s' in Paperless-ngx", name)
    create_url = f"{base_url}/api/correspondents/"
    async with session.post(
        create_url, json={"name": name}, headers=headers
    ) as resp:
        if resp.status in (200, 201):
            data = await resp.json()
            cid = data["id"]
            logger.info("Created correspondent '%s' → ID %d", name, cid)
            return cid
        else:
            text = await resp.text()
            logger.warning("Failed to create correspondent '%s': HTTP %d — %s", name, resp.status, text[:200])
            return None


async def _resolve_document_type_id(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: dict,
    name: str,
) -> int | None:
    """Resolve a document type name to its ID. Creates if not found."""
    url = f"{base_url}/api/document_types/?name__iexact={name}"
    async with session.get(url, headers=headers) as resp:
        if resp.status == 200:
            data = await resp.json()
            results = data.get("results", [])
            if results:
                dtid = results[0]["id"]
                logger.debug("Document type '%s' → ID %d", name, dtid)
                return dtid

    logger.info("Creating document type '%s' in Paperless-ngx", name)
    create_url = f"{base_url}/api/document_types/"
    async with session.post(
        create_url, json={"name": name}, headers=headers
    ) as resp:
        if resp.status in (200, 201):
            data = await resp.json()
            dtid = data["id"]
            logger.info("Created document type '%s' → ID %d", name, dtid)
            return dtid
        else:
            text = await resp.text()
            logger.warning("Failed to create document type '%s': HTTP %d — %s", name, resp.status, text[:200])
            return None


@register_plugin
class PaperlessNgxPlugin(CapturePlugin):
    plugin_id = "paperless_ngx"
    plugin_name = "Paperless-ngx"
    plugin_icon = "📋"
    plugin_description = "Send scanned documents to Paperless-ngx for archival and OCR processing."
    plugin_color = "#16a34a"

    def config_schema(self) -> list[dict]:
        return [
            {
                "key": "paperless_url",
                "label": "Paperless-ngx URL",
                "type": "url",
                "required": True,
                "hint": "e.g. http://192.168.1.10:8000",
                "default": "",
            },
            {
                "key": "paperless_token",
                "label": "API Token",
                "type": "password",
                "required": True,
                "hint": "Settings → API Token in Paperless-ngx",
                "default": "",
            },
            {
                "key": "default_tags",
                "label": "Default Tags",
                "type": "text",
                "required": False,
                "hint": "Comma-separated tag names (e.g. printix,scan) — auto-created if missing",
                "default": "printix",
            },
            {
                "key": "default_correspondent",
                "label": "Default Correspondent",
                "type": "text",
                "required": False,
                "hint": "Correspondent name — auto-created if missing",
                "default": "",
            },
            {
                "key": "default_document_type",
                "label": "Default Document Type",
                "type": "text",
                "required": False,
                "hint": "Document type name — auto-created if missing",
                "default": "",
            },
        ]

    async def ingest_bytes(
        self,
        data: bytes,
        filename: str,
        metadata: dict[str, Any],
    ) -> tuple[bool, str]:
        """Direkt-Upload aus Desktop-Send/Web-Upload ohne Azure-Blob-Zwischenstation."""
        if not data:
            return False, "Empty document"
        paperless_url = self.config.get("paperless_url", "").rstrip("/")
        token = self.config.get("paperless_token", "")
        if not paperless_url or not token:
            return False, "Paperless-ngx URL or API token not configured"
        api_headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                return await self._upload_bytes_to_paperless(
                    session, paperless_url, api_headers, data, filename, metadata,
                )
        except Exception as e:
            logger.exception("Paperless-ngx ingest_bytes error: %s", e)
            return False, f"Error: {e}"

    async def process_document(
        self,
        document_url: str,
        filename: str,
        metadata: dict[str, Any],
        event_data: dict,
    ) -> tuple[bool, str]:
        """Downloads the document from Azure Blob and uploads to Paperless-ngx (v4.4.12)."""
        paperless_url = self.config.get("paperless_url", "").rstrip("/")
        token = self.config.get("paperless_token", "")

        if not paperless_url or not token:
            return False, "Paperless-ngx URL or API token not configured"

        if not document_url:
            return False, "No document URL provided in webhook"

        api_headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                # 1. Download document from Azure Blob SAS URL
                logger.info("Downloading document from: %s", document_url[:80])
                async with session.get(document_url) as dl_resp:
                    if dl_resp.status != 200:
                        return False, f"Download failed: HTTP {dl_resp.status}"
                    doc_bytes = await dl_resp.read()
                    if not doc_bytes:
                        return False, "Downloaded document is empty"
                    logger.info("Downloaded %d bytes", len(doc_bytes))

                return await self._upload_bytes_to_paperless(
                    session, paperless_url, api_headers, doc_bytes, filename, metadata,
                )
        except Exception as e:
            logger.exception("Paperless-ngx plugin error: %s", e)
            return False, f"Error: {e}"

    async def _upload_bytes_to_paperless(
        self,
        session: "aiohttp.ClientSession",
        paperless_url: str,
        api_headers: dict,
        doc_bytes: bytes,
        filename: str,
        metadata: dict[str, Any],
    ) -> tuple[bool, str]:
        """Gemeinsamer Upload-Pfad: Bytes → Paperless mit Tag-/Corr-/Type-Resolution.

        Die Session wird vom Aufrufer gemanaged (offen übergeben, nicht hier
        geschlossen), damit beide Pfade — Webhook-Download und Direkt-Ingest —
        denselben Code teilen können.
        """
        # Resolve names → IDs for tags, correspondent, document_type
        tag_ids: list[int] = []
        tags_cfg = self.config.get("default_tags", "")
        if tags_cfg:
            for tag_name in tags_cfg.split(","):
                tag_name = tag_name.strip()
                if tag_name:
                    tid = await _resolve_tag_id(session, paperless_url, api_headers, tag_name)
                    if tid is not None:
                        tag_ids.append(tid)

        correspondent_id: int | None = None
        correspondent_name = self.config.get("default_correspondent", "").strip()
        if correspondent_name:
            correspondent_id = await _resolve_correspondent_id(
                session, paperless_url, api_headers, correspondent_name
            )

        doc_type_id: int | None = None
        doc_type_name = self.config.get("default_document_type", "").strip()
        if doc_type_name:
            doc_type_id = await _resolve_document_type_id(
                session, paperless_url, api_headers, doc_type_name
            )

        # Build multipart form for Paperless-ngx
        upload_url = f"{paperless_url}/api/documents/post_document/"

        _fn = filename or "scan.pdf"
        _ct = mimetypes.guess_type(_fn)[0] or "application/pdf"

        form = aiohttp.FormData()
        form.add_field("document", doc_bytes, filename=_fn, content_type=_ct)

        title = metadata.get("title") or metadata.get("Title") or filename or ""
        if title:
            form.add_field("title", title)
        for tid in tag_ids:
            form.add_field("tags", str(tid))
        if correspondent_id is not None:
            form.add_field("correspondent", str(correspondent_id))
        if doc_type_id is not None:
            form.add_field("document_type", str(doc_type_id))

        logger.info("Uploading to Paperless-ngx: %s (tags=%s, corr=%s, dtype=%s, size=%d)",
                    upload_url, tag_ids, correspondent_id, doc_type_id, len(doc_bytes))
        async with session.post(upload_url, data=form, headers=api_headers) as resp:
            resp_text = await resp.text()
            if resp.status in (200, 201, 202):
                logger.info("Paperless-ngx accepted document: %s", resp_text[:200])
                return True, f"Document uploaded successfully (HTTP {resp.status})"
            logger.error("Paperless-ngx rejected: HTTP %d — %s", resp.status, resp_text[:300])
            return False, f"Paperless-ngx error: HTTP {resp.status} — {resp_text[:200]}"

    async def test_connection(self) -> tuple[bool, str]:
        """Tests connection to Paperless-ngx by querying /api/documents/ (v4.4.11)."""
        import aiohttp

        paperless_url = self.config.get("paperless_url", "").rstrip("/")
        token = self.config.get("paperless_token", "")

        if not paperless_url:
            return False, "Paperless-ngx URL not configured"
        if not token:
            return False, "API token not configured"

        headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with aiohttp.ClientSession() as session:
                # Use /api/documents/?page_size=1 — lightweight, reliable,
                # works with DRF + Cloudflare/reverse proxies.
                # The /api/ root can return 406 with format negotiation.
                url = f"{paperless_url}/api/documents/?page_size=1"
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("content-type", "")
                        if "text/html" in ct:
                            return False, (
                                f"Server returned HTML instead of JSON — "
                                f"check URL (proxy/login page?): {paperless_url}"
                            )
                        try:
                            data = await resp.json()
                        except Exception:
                            return False, f"Response is not valid JSON (Content-Type: {ct})"

                        # Extract document count for status info
                        doc_count = data.get("count", "?")

                        # Try to get version from /api/ui_settings/
                        version = ""
                        try:
                            async with session.get(
                                f"{paperless_url}/api/ui_settings/",
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as vr:
                                if vr.status == 200:
                                    vdata = await vr.json()
                                    version = vdata.get("version", "")
                        except Exception:
                            pass

                        msg = "Connection successful"
                        if version:
                            msg += f" (Paperless-ngx {version})"
                        msg += f" — {doc_count} documents"
                        return True, msg
                    elif resp.status == 401:
                        return False, "Authentication failed — check API token"
                    elif resp.status == 403:
                        return False, "Access forbidden — check API token permissions"
                    else:
                        return False, f"Unexpected response: HTTP {resp.status}"

        except aiohttp.ClientConnectorError:
            return False, f"Cannot connect to {paperless_url} — check URL and network"
        except Exception as e:
            return False, f"Connection error: {e}"
