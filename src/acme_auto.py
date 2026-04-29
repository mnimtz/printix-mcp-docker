"""
Automatic HTTPS via sslip.io + Let's Encrypt (v7.2.36).

For users who have a fixed public IP but no own domain. Generates a
hostname like `52-143-121-45.sslip.io` from the public IP, requests a
Let's Encrypt certificate via certbot's standalone HTTP-01 challenge,
saves cert + key to /data/tls/, sets `tls_enabled=1`. A daemon thread
polls daily and runs `certbot renew` when the cert is within 30 days
of expiry — no admin action needed.

Why sslip.io: their wildcard DNS turns any public IPv4 into a
cert-eligible hostname (`<dashed-ip>.sslip.io`). Let's Encrypt accepts
sslip.io for HTTP-01 challenge requests. Free, no account, no DNS
config.

Requirements at runtime:
- Public IP reachable from the internet on port 80 (during the ~30 s
  ACME challenge; can be closed afterwards).
- /data writable (cert storage)
- certbot binary installed in the image (provided since v7.2.36).
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("printix.acme")

CERTBOT_DIR = "/data/letsencrypt"
TLS_CERT_PATH = "/data/tls/cert.pem"
TLS_KEY_PATH  = "/data/tls/key.pem"

SETTING_ENABLED  = "auto_tls_enabled"
SETTING_HOSTNAME = "auto_tls_hostname"
SETTING_EMAIL    = "auto_tls_email"
SETTING_PUBLIC_IP = "auto_tls_public_ip"
SETTING_LAST_RUN = "auto_tls_last_run"
SETTING_NEXT_DUE = "auto_tls_next_due"
SETTING_LAST_ERROR = "auto_tls_last_error"


# ─── Public IP detection ─────────────────────────────────────────────────────

def detect_public_ip(timeout: float = 5.0) -> str:
    """Asks api.ipify.org for the public IP this VM is reachable from.

    Returns an empty string on any error — caller should handle that.
    """
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org",
                                     timeout=timeout) as resp:
            ip = resp.read().decode("ascii", errors="ignore").strip()
            socket.inet_aton(ip)  # validates IPv4
            return ip
    except Exception as e:
        logger.warning("public IP detection failed: %s", e)
        return ""


def hostname_for_ip(ip: str) -> str:
    """Returns the canonical sslip.io hostname for a given IPv4."""
    if not ip:
        return ""
    return ip.replace(".", "-") + ".sslip.io"


# ─── Cert management ─────────────────────────────────────────────────────────

def _certbot_args(hostname: str, email: str, force: bool = False) -> list[str]:
    """Builds the certbot CLI args. Persists state under /data so it
    survives container restarts."""
    args = [
        "certbot", "certonly",
        "--standalone",
        "--preferred-challenges", "http",
        "-d", hostname,
        "--email", email,
        "-n",
        "--agree-tos",
        "--no-eff-email",
        "--config-dir", os.path.join(CERTBOT_DIR, "config"),
        "--work-dir",   os.path.join(CERTBOT_DIR, "work"),
        "--logs-dir",   os.path.join(CERTBOT_DIR, "logs"),
        "--key-type", "ecdsa",
        "--elliptic-curve", "secp384r1",
    ]
    if force:
        args.append("--force-renewal")
    return args


def _copy_cert_to_tls(hostname: str) -> tuple[bool, str]:
    """Copies certbot's output to /data/tls/{cert,key}.pem so uvicorn
    finds it via the existing TLS-import mechanism."""
    src_dir = os.path.join(CERTBOT_DIR, "config", "live", hostname)
    src_cert = os.path.join(src_dir, "fullchain.pem")
    src_key  = os.path.join(src_dir, "privkey.pem")
    if not (os.path.isfile(src_cert) and os.path.isfile(src_key)):
        return False, f"certbot output missing under {src_dir}"
    os.makedirs("/data/tls", exist_ok=True)
    shutil.copyfile(src_cert, TLS_CERT_PATH)
    shutil.copyfile(src_key, TLS_KEY_PATH)
    os.chmod(TLS_CERT_PATH, 0o644)
    os.chmod(TLS_KEY_PATH, 0o600)
    return True, "cert copied to /data/tls/"


def request_cert(email: str, public_ip: str = "") -> dict:
    """One-shot: detect IP if not given → request cert → copy → enable.

    Returns a dict with status, hostname, and any error.
    """
    if not email or "@" not in email:
        return {"ok": False, "error": "valid email is required"}

    ip = (public_ip or "").strip() or detect_public_ip()
    if not ip:
        return {"ok": False, "error": "could not detect public IP"}
    hostname = hostname_for_ip(ip)

    if shutil.which("certbot") is None:
        return {"ok": False, "error": "certbot binary not present in this image"}

    logger.info("ACME: requesting cert for %s (IP %s, email %s)",
                hostname, ip, email)
    started = time.time()

    try:
        proc = subprocess.run(
            _certbot_args(hostname, email),
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "certbot timed out (>180 s)"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"certbot not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"certbot failed: {e}"}

    if proc.returncode != 0:
        # Limit error to last ~20 lines; certbot logs are noisy
        tail = "\n".join(proc.stderr.strip().splitlines()[-20:])
        logger.warning("certbot rc=%s output:\n%s", proc.returncode, tail)
        return {
            "ok": False,
            "error": f"certbot exited with code {proc.returncode}",
            "details": tail,
        }

    ok, msg = _copy_cert_to_tls(hostname)
    if not ok:
        return {"ok": False, "error": msg}

    # Persist settings + activate TLS
    try:
        from db import set_setting
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        set_setting(SETTING_ENABLED, "1")
        set_setting(SETTING_HOSTNAME, hostname)
        set_setting(SETTING_EMAIL, email)
        set_setting(SETTING_PUBLIC_IP, ip)
        set_setting(SETTING_LAST_RUN, now)
        set_setting(SETTING_LAST_ERROR, "")
        # Tell the existing TLS-import flag to load cert at next uvicorn start
        set_setting("tls_enabled", "1")
        # public_url base — use sslip.io hostname so Connect-Center reflects it
        set_setting("public_url", f"https://{hostname}")
    except Exception as e:
        logger.warning("ACME: settings persist failed: %s", e)

    elapsed = time.time() - started
    logger.info("ACME: cert for %s acquired in %.1f s", hostname, elapsed)
    return {
        "ok": True,
        "hostname": hostname,
        "public_ip": ip,
        "elapsed_seconds": elapsed,
        "next_step": (
            "Restart the container so uvicorn picks up the new certificate. "
            "Auto-renewal runs daily; no further action needed."
        ),
    }


def renew_if_due(force: bool = False) -> dict:
    """Run `certbot renew`; idempotent — only acts if cert is <30 days
    from expiry (or `force=True`). Updates /data/tls/{cert,key}.pem
    if a renewal happened."""
    if shutil.which("certbot") is None:
        return {"ok": False, "error": "certbot not present"}

    args = [
        "certbot", "renew",
        "--config-dir", os.path.join(CERTBOT_DIR, "config"),
        "--work-dir",   os.path.join(CERTBOT_DIR, "work"),
        "--logs-dir",   os.path.join(CERTBOT_DIR, "logs"),
        "-n",
    ]
    if force:
        args.append("--force-renewal")

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-20:])
        return {"ok": False, "error": f"rc={proc.returncode}", "details": tail}

    # Pick up renewed cert (only one cert in our setup, parse hostname from settings)
    try:
        from db import get_setting, set_setting
        hostname = get_setting(SETTING_HOSTNAME, "")
        if hostname:
            ok, msg = _copy_cert_to_tls(hostname)
            if ok:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                set_setting(SETTING_LAST_RUN, now)
    except Exception as e:
        logger.warning("ACME renew: post-step failed: %s", e)

    return {
        "ok": True,
        "stdout_tail": "\n".join(proc.stdout.strip().splitlines()[-10:]),
    }


# ─── Renewal scheduler (daemon thread) ───────────────────────────────────────

_renewal_thread: Optional[threading.Thread] = None
_renewal_stop_event: Optional[threading.Event] = None


def start_renewal_scheduler() -> None:
    """Starts a daemon thread that wakes every 24h and runs renew_if_due.

    First wake-up is delayed by 1 h after process start so a freshly
    booted container doesn't burn CPU during startup.
    """
    global _renewal_thread, _renewal_stop_event
    if _renewal_thread and _renewal_thread.is_alive():
        return
    _renewal_stop_event = threading.Event()

    def _loop():
        # Wait an hour before first run, then daily.
        first_delay = 60 * 60
        if _renewal_stop_event.wait(first_delay):
            return
        while not _renewal_stop_event.is_set():
            try:
                from db import get_setting
                if get_setting(SETTING_ENABLED, "0") == "1":
                    logger.info("ACME: scheduled renewal check")
                    result = renew_if_due()
                    if not result.get("ok"):
                        logger.warning("ACME scheduled renewal: %s",
                                       result.get("error"))
            except Exception as e:
                logger.warning("ACME renewal loop: %s", e)
            # 24 h until next check
            if _renewal_stop_event.wait(24 * 60 * 60):
                return

    _renewal_thread = threading.Thread(target=_loop, daemon=True,
                                        name="acme-renewal")
    _renewal_thread.start()
    logger.info("ACME: renewal scheduler started (24h cadence)")


# ─── Status helpers ──────────────────────────────────────────────────────────

def status() -> dict:
    """Reads current state from settings + parses the on-disk cert
    if present. Used by the admin UI."""
    try:
        from db import get_setting
        enabled = get_setting(SETTING_ENABLED, "0") == "1"
        hostname = get_setting(SETTING_HOSTNAME, "")
        email = get_setting(SETTING_EMAIL, "")
        public_ip = get_setting(SETTING_PUBLIC_IP, "")
        last_run = get_setting(SETTING_LAST_RUN, "")
        last_error = get_setting(SETTING_LAST_ERROR, "")
    except Exception:
        enabled = False
        hostname = email = public_ip = last_run = last_error = ""

    cert_info: dict = {}
    if os.path.isfile(TLS_CERT_PATH):
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            with open(TLS_CERT_PATH, "rb") as fh:
                cert = x509.load_pem_x509_certificate(fh.read(), default_backend())
            now = datetime.now(timezone.utc)
            expires = cert.not_valid_after_utc
            cert_info = {
                "subject":   cert.subject.rfc4514_string(),
                "issuer":    cert.issuer.rfc4514_string(),
                "expires":   expires.isoformat(timespec="seconds"),
                "days_remaining": (expires - now).days,
                "expired":   expires < now,
            }
        except Exception as e:
            cert_info = {"parse_error": str(e)}

    return {
        "enabled": enabled,
        "hostname": hostname,
        "email": email,
        "public_ip": public_ip,
        "last_run": last_run,
        "last_error": last_error,
        "cert": cert_info,
        "binary_present": shutil.which("certbot") is not None,
    }
