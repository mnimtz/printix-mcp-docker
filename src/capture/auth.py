"""
Capture Authentication — Multi-Method Verification (v4.6.7)
===========================================================
Supports the Printix/Tungsten Capture Connector authentication model:

1. Printix Capture Signature (x-printix-signature) — v4.6.7 FIXED
   — Exact Printix Capture Connector formula:
     StringToSign = "{RequestId}.{Timestamp}.{method}.{RequestPath}.{Body}"
     HMAC-SHA-256 with Base64-decoded key, result Base64-encoded
   — RequestId from x-printix-request-id header
   — Timestamp from x-printix-timestamp header (Unix epoch seconds)
   — method = HTTP verb in LOWERCASE (e.g. "post")
   — RequestPath from x-printix-request-path header (URL path)
   — Body = raw request body as UTF-8 string
   — Supports comma-separated multi-signatures (key rotation)
   — Fallback: body-only and legacy canonical formats
2. HMAC Signature (x-printix-signature-256 / x-printix-signature-512)
   — Body-only HMAC, multiple secrets for key rotation
3. Hub Signature (x-hub-signature-256)
   — GitHub-style webhook signature
4. Connector Token (Authorization: Bearer <token> / x-connector-token)
   — Multiple tokens per profile for rotation
5. require_signature flag per profile
   — Enforces that at least one auth method must succeed

Diagnostic logging (v4.6.5+):
  — Full received signature logged (not truncated)
  — Every candidate (key × format) logged with full expected Base64
  — Key material described (utf8/b64dec, length, preview)
  — Body SHA-256 hash logged for independent verification
"""

import base64
import hashlib
import hmac as _hmac
import logging
from dataclasses import dataclass

logger = logging.getLogger("printix.capture.auth")


@dataclass
class AuthResult:
    """Result of capture request authentication."""
    success: bool
    method: str = ""
    detail: str = ""
    secret_index: int = -1


def _strip_prefix(sig: str) -> str:
    """Remove optional prefix like 'sha256=' or 'sha512='."""
    for prefix in ("sha256=", "sha512=", "SHA256=", "SHA512="):
        if sig.startswith(prefix):
            return sig[len(prefix):]
    return sig


def _parse_multi(value: str) -> list[str]:
    """Parse multi-value field (newline-separated) into list of non-empty strings."""
    if not value:
        return []
    return [v.strip() for v in value.split("\n") if v.strip()]


def _try_hmac(body_bytes: bytes, sig_value: str, secrets: list[str], algo) -> tuple[bool, int]:
    """Try HMAC verification against all secrets. Returns (matched, secret_index)."""
    sig_clean = _strip_prefix(sig_value).lower()
    for i, secret in enumerate(secrets):
        expected = _hmac.new(secret.encode("utf-8"), body_bytes, algo).hexdigest()
        if _hmac.compare_digest(sig_clean, expected.lower()):
            return True, i
    return False, -1


def _get_key_variants(secret: str) -> list[tuple[bytes, str]]:
    """
    Get all plausible key byte representations for a secret string.
    Returns list of (key_bytes, description).
    """
    variants: list[tuple[bytes, str]] = []

    # 1. Raw UTF-8 (most common for simple secrets)
    raw = secret.encode("utf-8")
    variants.append((raw, f"utf8({len(raw)}B)"))

    # 2. Base64-decoded (common for Azure/Tungsten APIs)
    try:
        decoded = base64.b64decode(secret)
        if decoded != raw:
            variants.append((decoded, f"b64dec({len(decoded)}B)"))
    except Exception:
        pass

    # 3. Base64 URL-safe decoded
    try:
        decoded = base64.urlsafe_b64decode(secret)
        if decoded != raw:
            existing = [v[0] for v in variants]
            if decoded not in existing:
                variants.append((decoded, f"b64url({len(decoded)}B)"))
    except Exception:
        pass

    return variants


def _compare_sig(expected_bytes: bytes, received_str: str) -> tuple[bool, str]:
    """
    Compare HMAC digest bytes against received signature string.
    Tries Base64 and hex encodings.
    Returns (matched, encoding_name).
    """
    received = _strip_prefix(received_str)

    # Base64 standard (case-sensitive)
    expected_b64 = base64.b64encode(expected_bytes).decode("ascii")
    if _hmac.compare_digest(received, expected_b64):
        return True, "base64"

    # Base64 with/without padding
    expected_b64_nopad = expected_b64.rstrip("=")
    received_nopad = received.rstrip("=")
    if _hmac.compare_digest(received_nopad, expected_b64_nopad):
        return True, "base64-nopad"

    # Base64 URL-safe
    expected_b64url = base64.urlsafe_b64encode(expected_bytes).decode("ascii")
    if _hmac.compare_digest(received, expected_b64url):
        return True, "base64url"

    # Hex (case-insensitive)
    expected_hex = expected_bytes.hex()
    if _hmac.compare_digest(received.lower(), expected_hex.lower()):
        return True, "hex"

    return False, ""


def _parse_comma_sigs(sig_value: str) -> list[str]:
    """
    Parse potentially comma-separated signatures (key rotation).
    Returns list of individual signature strings.
    """
    parts = [s.strip() for s in sig_value.split(",") if s.strip()]
    return parts if parts else [sig_value]


def _detect_algo_from_sig(sig_value: str) -> str:
    """
    Detect likely HMAC algorithm from Base64 signature length.
      44 chars (32 bytes) → SHA-256
      28 chars (20 bytes) → SHA-1
      88 chars (64 bytes) → SHA-512
    """
    sig = _strip_prefix(sig_value).rstrip("=")
    # Estimate raw byte length from Base64
    raw_len = len(sig) * 3 // 4
    if raw_len == 32:
        return "sha256"
    if raw_len == 64:
        return "sha512"
    if raw_len == 20:
        return "sha1"
    return "unknown"


def _build_canonical_payloads(
    body_bytes: bytes,
    timestamp: str,
    request_path: str,
    request_id: str,
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> list[tuple[bytes, str]]:
    """
    Build all canonical payload variants for HMAC computation (v4.6.7).

    Priority order:
      1. Printix Capture Connector format (v4.6.7 — exact documented format)
      2. Body-only (Printix Cloud Print API format)
      3. Legacy canonical strings (fallback)
    """
    payloads: list[tuple[bytes, str]] = []
    ts = timestamp
    path = request_path
    rid = request_id
    body_text = body_bytes.decode("utf-8", errors="replace")

    # ── 0. PRINTIX CAPTURE FORMAT (v4.6.7) — HIGHEST PRIORITY ─────────────
    # Exact formula: "{RequestId}.{Timestamp}.{method}.{RequestPath}.{Body}"
    # - method is LOWERCASE
    # - entire StringToSign is UTF-8 encoded
    # - key is Base64-decoded
    # - output is Base64-encoded
    # Source: Tungsten Capture Connector API documentation
    if rid and ts and path:
        method_lower = method.lower()
        sts = f"{rid}.{ts}.{method_lower}.{path}.{body_text}"
        payloads.append((sts.encode("utf-8"), "printix-capture(rid.ts.method.path.body)"))

        # Variation: uppercase method (paranoia fallback)
        sts_upper = f"{rid}.{ts}.{method.upper()}.{path}.{body_text}"
        payloads.append((sts_upper.encode("utf-8"), "printix-capture(rid.ts.METHOD.path.body)"))

        # Variation: swapped rid/ts order
        sts_swap = f"{ts}.{rid}.{method_lower}.{path}.{body_text}"
        payloads.append((sts_swap.encode("utf-8"), "printix-capture(ts.rid.method.path.body)"))

    # Partial: if only ts+path (no rid), try 4-component format
    if ts and path and not rid:
        method_lower = method.lower()
        sts4 = f"{ts}.{method_lower}.{path}.{body_text}"
        payloads.append((sts4.encode("utf-8"), "printix-capture-4(ts.method.path.body)"))

    # Partial: if only rid+ts (no path), try without path
    if rid and ts and not path:
        method_lower = method.lower()
        sts_nopath = f"{rid}.{ts}.{method_lower}.{body_text}"
        payloads.append((sts_nopath.encode("utf-8"), "printix-capture-nopath(rid.ts.method.body)"))

    # ── 1. Body variants (Printix Cloud Print API / simple HMAC) ──────────
    payloads.append((body_bytes, "body-only"))

    # Cloud Print API format: timestamp.body (SHA-512, hex — different API)
    if ts:
        payloads.append((f"{ts}.{body_text}".encode("utf-8"), "ts.body(str)"))
        payloads.append((f"{ts}.".encode() + body_bytes, "ts.body(bytes)"))

    # Body with \r\n → \n normalization (proxy may alter line endings)
    body_lf = body_bytes.replace(b"\r\n", b"\n")
    if body_lf != body_bytes:
        payloads.append((body_lf, "body(crlf→lf)"))

    # Body stripped of trailing whitespace/newline
    body_stripped = body_bytes.rstrip()
    if body_stripped != body_bytes:
        payloads.append((body_stripped, "body(rstrip)"))

    # ── 2. Timestamp + path + body (legacy fallbacks) ──────────────────────
    if ts and path:
        payloads.append((f"{ts}.{path}.".encode() + body_bytes, "ts.path.body"))
        payloads.append((f"{path}.{ts}.".encode() + body_bytes, "path.ts.body"))

    # ── 3. With request ID (legacy order variants) ─────────────────────────
    if ts and path and rid:
        payloads.append((f"{ts}.{path}.{rid}.".encode() + body_bytes, "ts.path.rid.body"))
        payloads.append((f"{ts}.{rid}.{path}.".encode() + body_bytes, "ts.rid.path.body"))
        payloads.append((f"{rid}.{ts}.{path}.".encode() + body_bytes, "rid.ts.path.body"))

    return payloads


def _try_printix_native(
    body_bytes: bytes,
    sig_value: str,
    secrets: list[str],
    timestamp: str,
    request_path: str,
    request_id: str,
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> tuple[bool, int, str]:
    """
    Printix Capture Connector signature verification (v4.6.7).

    Tries the exact Printix Capture format FIRST:
      StringToSign = "{RequestId}.{Timestamp}.{method}.{RequestPath}.{Body}"
    Then falls back to body-only and legacy canonical formats.

    Returns (matched, secret_index, description_of_match).
    """
    sig_candidates = _parse_comma_sigs(sig_value)
    payloads = _build_canonical_payloads(
        body_bytes, timestamp, request_path, request_id, headers, method,
    )

    # Detect expected algo from signature length
    detected_algo = _detect_algo_from_sig(sig_candidates[0])

    # Order algos: detected first, then others
    all_algos: list[tuple[str, type]] = [
        ("sha256", hashlib.sha256),
        ("sha512", hashlib.sha512),
        ("sha1", hashlib.sha1),
    ]
    if detected_algo == "sha512":
        all_algos = [("sha512", hashlib.sha512), ("sha256", hashlib.sha256), ("sha1", hashlib.sha1)]
    elif detected_algo == "sha1":
        all_algos = [("sha1", hashlib.sha1), ("sha256", hashlib.sha256), ("sha512", hashlib.sha512)]
    # else sha256 first (default) — matches 44-char sig

    for sig in sig_candidates:
        sig_clean = _strip_prefix(sig)

        for secret_idx, secret in enumerate(secrets):
            for key_bytes, key_desc in _get_key_variants(secret):
                for algo_name, algo_cls in all_algos:
                    for payload, fmt_desc in payloads:
                        mac = _hmac.new(key_bytes, payload, algo_cls)
                        matched, enc = _compare_sig(mac.digest(), sig_clean)
                        if matched:
                            desc = (f"key={key_desc}, algo={algo_name}, "
                                    f"format={fmt_desc}, encoding={enc}")
                            logger.info("Auth: MATCH — %s", desc)
                            return True, secret_idx, desc

    return False, -1, ""


def _diagnostic_log(
    body_bytes: bytes,
    sig_value: str,
    secrets: list[str],
    timestamp: str,
    request_path: str,
    request_id: str,
    headers: dict[str, str] | None = None,
    method: str = "POST",
):
    """
    v4.6.7: Detailed diagnostic log for signature mismatch.
    Logs at INFO level — shows full values, raw body analysis,
    and every candidate with full expected Base64.
    Now includes the correct Printix Capture format as first candidate.
    """
    sig_clean = _strip_prefix(sig_value)
    detected_algo = _detect_algo_from_sig(sig_value)
    body_hash = hashlib.sha256(body_bytes).hexdigest()

    logger.info("┏━━━ SIGNATURE DIAGNOSTIC (v4.6.7) ━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("┃ received_sig  = %s", sig_clean)
    logger.info("┃ sig_len       = %d chars → detected algo: %s", len(sig_clean), detected_algo)
    logger.info("┃ method        = %s", method)
    logger.info("┃ timestamp     = %s", timestamp or "(none)")
    logger.info("┃ request_path  = %s", request_path or "(none)")
    logger.info("┃ request_id    = %s", request_id or "(none)")

    # ── Expected Printix Capture format ────────────────────────────────────
    if timestamp and request_path and request_id:
        body_text = body_bytes.decode("utf-8", errors="replace")
        sts = f"{request_id}.{timestamp}.{method.lower()}.{request_path}.{body_text}"
        logger.info("┃")
        logger.info("┃ ── PRINTIX CAPTURE StringToSign ──")
        logger.info("┃ format        = {rid}.{ts}.{method}.{path}.{body}")
        logger.info("┃ StringToSign  = %s...%s (len=%d)",
                     sts[:80], sts[-40:] if len(sts) > 120 else "", len(sts))
    else:
        missing = []
        if not timestamp:
            missing.append("x-printix-timestamp")
        if not request_path:
            missing.append("x-printix-request-path")
        if not request_id:
            missing.append("x-printix-request-id")
        logger.info("┃")
        logger.info("┃ ⚠ MISSING HEADERS for Printix Capture format: %s", ", ".join(missing))
        logger.info("┃   Without these headers, signature verification cannot work!")
        logger.info("┃   Check if a reverse proxy (HA Ingress, nginx) strips custom headers.")

    # ── Raw body analysis ──────────────────────────────────────────────────
    logger.info("┃")
    logger.info("┃ ── RAW BODY ANALYSIS ──")
    logger.info("┃ body_len      = %d bytes", len(body_bytes))
    logger.info("┃ body_sha256   = %s", body_hash)
    logger.info("┃ body_first32  = %s", body_bytes[:32].hex())
    logger.info("┃ body_last32   = %s", body_bytes[-32:].hex() if len(body_bytes) >= 32 else body_bytes.hex())
    logger.info("┃ body_has_crlf = %s", b"\r\n" in body_bytes)
    logger.info("┃ body_ends_nl  = %s (last byte: 0x%s)",
                body_bytes.endswith(b"\n"),
                body_bytes[-1:].hex() if body_bytes else "??")
    logger.info("┃ body_has_bom  = %s", body_bytes[:3] == b"\xef\xbb\xbf")

    # ── ALL request headers (v4.6.7 — full dump for debugging) ─────────────
    if headers:
        logger.info("┃")
        logger.info("┃ ── ALL REQUEST HEADERS ──")
        for k, v in sorted(headers.items()):
            logger.info("┃   %s = %s", k, v[:120] if len(v) > 120 else v)

    # ── Secret analysis ────────────────────────────────────────────────────
    logger.info("┃")
    logger.info("┃ ── SECRET & CANDIDATE ANALYSIS ──")
    logger.info("┃ secrets       = %d configured", len(secrets))

    if not secrets:
        logger.info("┗━━━ NO SECRETS — cannot compute candidates")
        return

    # Focus on detected algo (sha256 for 44-char sig)
    if detected_algo == "sha256":
        focus_algos = [("sha256", hashlib.sha256)]
    elif detected_algo == "sha512":
        focus_algos = [("sha512", hashlib.sha512)]
    else:
        focus_algos = [("sha256", hashlib.sha256), ("sha512", hashlib.sha512)]

    payloads = _build_canonical_payloads(
        body_bytes, timestamp, request_path, request_id, headers, method,
    )

    for s_idx, secret in enumerate(secrets):
        key_variants = _get_key_variants(secret)
        logger.info("┃")
        logger.info("┃ Secret #%d: %d key variants", s_idx + 1, len(key_variants))

        for key_bytes, key_desc in key_variants:
            logger.info("┃   Key: %s (preview: %s...)", key_desc, key_bytes[:6].hex())

            for algo_name, algo_cls in focus_algos:
                for payload, fmt_desc in payloads:
                    mac = _hmac.new(key_bytes, payload, algo_cls)
                    expected_b64 = base64.b64encode(mac.digest()).decode("ascii")
                    match_marker = "✓ MATCH" if _hmac.compare_digest(sig_clean, expected_b64) else "✗"
                    logger.info("┃     %s %s [%s] → %s",
                                match_marker, algo_name, fmt_desc, expected_b64)

    logger.info("┗━━━ END DIAGNOSTIC ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def verify_capture_auth(
    body_bytes: bytes,
    headers: dict[str, str],
    profile: dict,
    method: str = "POST",
) -> AuthResult:
    """
    Verify incoming Capture webhook request against profile auth settings.

    Supports (in priority order):
      1. x-printix-signature — Printix Capture format (v4.6.7):
         StringToSign = "{RequestId}.{Timestamp}.{method}.{RequestPath}.{Body}"
      2. x-printix-signature-256 / x-hub-signature-256 (HMAC-SHA256, body only)
      3. x-printix-signature-512 (HMAC-SHA512, body only)
      4. Connector tokens via Authorization: Bearer / x-connector-token header
      5. require_signature enforcement per profile
    """
    secrets = _parse_multi(profile.get("secret_key", ""))
    tokens = _parse_multi(profile.get("connector_token", ""))
    require_sig = bool(profile.get("require_signature", False))

    has_secrets = bool(secrets)
    has_tokens = bool(tokens)

    # Log all auth-related headers for diagnostics
    auth_headers = {k: v for k, v in headers.items()
                    if any(x in k for x in ("signature", "hmac", "printix",
                                            "authorization", "connector-token"))}
    if auth_headers:
        logger.info("Auth: Relevant headers: %s",
                    {k: v[:40] + "..." if len(v) > 40 else v for k, v in auth_headers.items()})

    logger.debug("Auth config: secrets=%d, tokens=%d, require_signature=%s",
                 len(secrets), len(tokens), require_sig)

    # ── 1. Try HMAC/Signature methods ──────────────────────────────────────
    if has_secrets:
        # ── 1a. Printix Native: x-printix-signature ────────────────────────
        sig_native = headers.get("x-printix-signature", "")
        if sig_native:
            timestamp = headers.get("x-printix-timestamp", "")
            request_path = headers.get("x-printix-request-path", "")
            request_id = headers.get("x-printix-request-id", "")

            detected_algo = _detect_algo_from_sig(sig_native)
            n_sigs = len(_parse_comma_sigs(sig_native))
            sig_len = len(_strip_prefix(sig_native))

            logger.info("Auth: x-printix-signature detected — "
                        "len=%d → %s, sigs=%d, ts=%s, path=%s",
                        sig_len, detected_algo, n_sigs,
                        timestamp or "(none)", request_path or "(none)")

            matched, idx, desc = _try_printix_native(
                body_bytes, sig_native, secrets, timestamp, request_path, request_id,
                headers, method,
            )
            if matched:
                detail = (f"Printix signature verified "
                          f"(secret #{idx + 1}/{len(secrets)}, {desc})")
                logger.info("Auth: %s", detail)
                return AuthResult(True, "printix-native", detail, idx)

            # Mismatch — full diagnostic dump at INFO level
            logger.warning("Auth: Printix signature mismatch — running full diagnostic")
            _diagnostic_log(body_bytes, sig_native, secrets,
                            timestamp, request_path, request_id, headers, method)

            return AuthResult(False, "printix-native",
                              f"Signature mismatch (sig_len={sig_len}, "
                              f"algo={detected_algo}, secrets={len(secrets)})")

        # ── 1b. x-printix-signature-256 / x-hub-signature-256 ─────────────
        sig_256 = headers.get("x-printix-signature-256", "") or headers.get("x-hub-signature-256", "")
        if sig_256:
            matched, idx = _try_hmac(body_bytes, sig_256, secrets, hashlib.sha256)
            if matched:
                return AuthResult(True, "hmac-sha256",
                                  f"HMAC-SHA256 verified (secret #{idx + 1}/{len(secrets)})",
                                  idx)
            logger.warning("Auth: HMAC-SHA256 mismatch (tried %d secrets)", len(secrets))
            return AuthResult(False, "hmac-sha256",
                              f"HMAC-SHA256 signature mismatch (tried {len(secrets)} secrets)")

        # ── 1c. x-printix-signature-512 ───────────────────────────────────
        sig_512 = headers.get("x-printix-signature-512", "")
        if sig_512:
            matched, idx = _try_hmac(body_bytes, sig_512, secrets, hashlib.sha512)
            if matched:
                return AuthResult(True, "hmac-sha512",
                                  f"HMAC-SHA512 verified (secret #{idx + 1}/{len(secrets)})",
                                  idx)
            logger.warning("Auth: HMAC-SHA512 mismatch (tried %d secrets)", len(secrets))
            return AuthResult(False, "hmac-sha512",
                              f"HMAC-SHA512 signature mismatch (tried {len(secrets)} secrets)")

        # No signature header found
        if not has_tokens:
            if require_sig:
                logger.warning("Auth: No signature header found and require_signature=True. "
                               "Expected one of: x-printix-signature, x-printix-signature-256, "
                               "x-printix-signature-512, x-hub-signature-256")
                return AuthResult(False, "none",
                                  "No signature header in request (require_signature is enabled)")
            logger.warning("Auth: No signature header — allowing (compatibility mode)")
            return AuthResult(True, "skipped",
                              "No signature header but require_signature=False (compatibility mode)")

    # ── 2. Try Connector Token ──────────────────────────────────────────────
    if has_tokens:
        auth_header = headers.get("authorization", "")
        req_token = ""
        if auth_header.lower().startswith("bearer "):
            req_token = auth_header[7:].strip()
        if not req_token:
            req_token = headers.get("x-connector-token", "").strip()

        if req_token:
            for i, valid_token in enumerate(tokens):
                if _hmac.compare_digest(req_token, valid_token):
                    return AuthResult(True, "connector-token",
                                      f"Connector token verified (token #{i + 1}/{len(tokens)})",
                                      i)
            logger.warning("Auth: Connector token mismatch (tried %d tokens)", len(tokens))
            return AuthResult(False, "connector-token",
                              f"Invalid connector token (tried {len(tokens)} tokens)")

        if not has_secrets:
            if require_sig:
                return AuthResult(False, "none",
                                  "No connector token in request (require_signature is enabled)")
            return AuthResult(True, "skipped",
                              "No connector token but require_signature=False (compatibility mode)")

    # ── 3. Neither secrets nor tokens configured ────────────────────────────
    if not has_secrets and not has_tokens:
        if require_sig:
            return AuthResult(False, "none",
                              "require_signature enabled but no secrets or tokens configured")
        return AuthResult(True, "skipped", "No authentication configured on profile")

    # ── 4. Both configured but neither present in request ───────────────────
    if require_sig:
        return AuthResult(False, "none",
                          "No signature header or connector token in request")
    return AuthResult(True, "skipped",
                      "No auth credentials in request (compatibility mode)")
