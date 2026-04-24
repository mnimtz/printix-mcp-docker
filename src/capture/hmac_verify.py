"""
HMAC Signature Verification for Printix Capture Connector
=========================================================
Supports both HMAC-SHA256 and HMAC-SHA512 as per Printix Capture API.

Headers checked:
  x-printix-signature-256  — HMAC-SHA256
  x-printix-signature-512  — HMAC-SHA512

Handles both raw hex digests and prefixed formats (sha256=HEXDIGEST).
"""

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def _strip_prefix(sig: str) -> str:
    """Entfernt optionale Prefixe wie 'sha256=' oder 'sha512='."""
    for prefix in ("sha256=", "sha512=", "SHA256=", "SHA512="):
        if sig.startswith(prefix):
            return sig[len(prefix):]
    return sig


def verify_hmac(body_bytes: bytes, headers: dict, secret_key: str) -> bool:
    """
    Verifies the HMAC signature of an incoming Printix Capture webhook.

    Returns True if valid, False otherwise.
    If no secret_key is configured, signature check is skipped (returns True).
    """
    if not secret_key:
        logger.debug("No secret_key configured — skipping HMAC verification")
        return True

    key_bytes = secret_key.encode("utf-8")

    # Log all signature-related headers for debugging
    sig_headers = {k: v[:20] + "..." for k, v in headers.items()
                   if "signature" in k or "hmac" in k or "printix" in k}
    if sig_headers:
        logger.info("HMAC: Signature headers found: %s", sig_headers)
    else:
        logger.warning("HMAC: No signature/printix headers found in: %s",
                       [k for k in headers.keys() if k.startswith("x-")])

    # Try SHA-256 first
    sig_256 = headers.get("x-printix-signature-256", "")
    if sig_256:
        sig_clean = _strip_prefix(sig_256)
        expected = hmac.new(key_bytes, body_bytes, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig_clean.lower(), expected.lower()):
            return True
        logger.warning("HMAC-SHA256 mismatch: got=%s... expected=%s...",
                       sig_clean[:16], expected[:16])
        return False

    # Try SHA-512
    sig_512 = headers.get("x-printix-signature-512", "")
    if sig_512:
        sig_clean = _strip_prefix(sig_512)
        expected = hmac.new(key_bytes, body_bytes, hashlib.sha512).hexdigest()
        if hmac.compare_digest(sig_clean.lower(), expected.lower()):
            return True
        logger.warning("HMAC-SHA512 mismatch: got=%s... expected=%s...",
                       sig_clean[:16], expected[:16])
        return False

    # No signature header present but secret_key configured.
    # v4.4.1: Printix Capture Connector sendet nicht immer Signatur-Header.
    # Deshalb: Request durchlassen, aber explizit warnen.
    # Wenn alle Requests signiert sein SOLLEN, muss das im Capture-Profil
    # über ein separates "require_signature" Flag erzwungen werden (TODO).
    logger.warning(
        "HMAC: secret_key is configured but request has NO signature header — "
        "allowing request (Printix compatibility mode)"
    )
    return True
