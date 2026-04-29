"""
Cloudflare Tunnel manager (v7.2.32).

Wraps the bundled `cloudflared` binary and exposes:
  - get_manager()   — singleton
  - .start_quick(port)        — anonymous *.trycloudflare.com tunnel
  - .start_named(token, host) — persistent named tunnel via CF dashboard
  - .stop()
  - .status()

Persists state in the `settings` table so the admin's last choice
survives container restarts. On import the module reads those settings
and auto-starts the configured tunnel.

The manager runs cloudflared as a subprocess inside the same container
as the web/MCP services. A reader thread captures stdout and parses
the *.trycloudflare.com URL out of cloudflared's status messages so we
can display it in the admin UI and write it to the `public_url` setting
(which the rest of the app already consults as the canonical base URL).

Security note: named-tunnel tokens are stored Fernet-encrypted via the
existing `_enc` / `_dec` helpers — same pattern as Printix client
secrets and the user OAuth secret.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger("printix.tunnel")

# Quick Tunnel writes a line like:
#   2024-04-29T12:34:56Z INF +-----------------------------------------+
#   2024-04-29T12:34:56Z INF |  https://random-words.trycloudflare.com  |
_QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Settings keys
SETTING_MODE        = "tunnel_mode"          # off | quick | named
SETTING_TOKEN_ENC   = "tunnel_named_token"   # encrypted CF tunnel token
SETTING_NAMED_HOST  = "tunnel_named_host"    # public hostname configured in CF dashboard
SETTING_LAST_URL    = "tunnel_last_url"      # last detected URL (for display only)


class TunnelManager:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._mode: str = "off"
        self._url: str = ""
        self._configured_host: str = ""
        self._started_at: float = 0.0
        self._last_error: str = ""
        self._log_buffer: list[str] = []
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return {
                "binary_present":    self._binary_path() is not None,
                "mode":              self._mode if running else "off",
                "running":           running,
                "url":               self._url if running else "",
                "configured_host":   self._configured_host,
                "started_at":        self._started_at if running else 0,
                "uptime_seconds":    (time.time() - self._started_at) if (running and self._started_at) else 0,
                "last_error":        self._last_error,
                "logs":              list(self._log_buffer[-30:]),
            }

    @staticmethod
    def _binary_path() -> Optional[str]:
        """Returns absolute path to cloudflared binary, or None if missing."""
        return shutil.which("cloudflared")

    # ── Control ───────────────────────────────────────────────────────────

    def stop(self) -> dict:
        with self._lock:
            self._stop_locked()
            self._mode = "off"
            self._url = ""
            self._configured_host = ""
            try:
                from db import set_setting
                set_setting(SETTING_MODE, "off")
                set_setting(SETTING_LAST_URL, "")
            except Exception:
                pass
        return self.status()

    def _stop_locked(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def start_quick(self, target_port: int = 8080) -> dict:
        """Start an anonymous *.trycloudflare.com tunnel pointing at the
        given local port (defaults to the web UI port). Returns once the
        URL has been captured (or after 12 s timeout)."""
        bin_path = self._binary_path()
        if not bin_path:
            err = "cloudflared binary not present in this image"
            logger.warning(err)
            return {"error": err, "status": self.status()}

        with self._lock:
            self._stop_locked()
            try:
                self._proc = subprocess.Popen(
                    [bin_path, "tunnel", "--no-autoupdate",
                     "--url", f"http://localhost:{target_port}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._mode = "quick"
                self._started_at = time.time()
                self._url = ""
                self._configured_host = ""
                self._last_error = ""
                self._log_buffer.clear()
                self._reader_thread = threading.Thread(
                    target=self._reader, args=(self._proc,), daemon=True,
                )
                self._reader_thread.start()
            except Exception as e:
                logger.exception("quick tunnel start failed")
                self._last_error = str(e)
                self._proc = None
                return {"error": str(e), "status": self.status()}

        # Wait up to 12 s for the URL to appear in the cloudflared output
        deadline = time.time() + 12
        while time.time() < deadline:
            time.sleep(0.25)
            with self._lock:
                if self._url:
                    self._persist_active_url(self._url)
                    break
        # Persist mode either way — even if we didn't capture URL, tunnel may still come up
        try:
            from db import set_setting
            set_setting(SETTING_MODE, "quick")
        except Exception:
            pass
        return self.status()

    def start_named(self, token: str, public_host: str = "") -> dict:
        """Start a persistent named tunnel using the user's CF tunnel
        token. The public hostname is the one the user has configured
        in the Cloudflare Zero Trust dashboard — we cannot derive it
        from the token, so the user supplies it for the public_url
        setting."""
        bin_path = self._binary_path()
        if not bin_path:
            err = "cloudflared binary not present in this image"
            return {"error": err, "status": self.status()}

        token = (token or "").strip()
        if not token:
            return {"error": "token is required", "status": self.status()}

        with self._lock:
            self._stop_locked()
            try:
                self._proc = subprocess.Popen(
                    [bin_path, "tunnel", "--no-autoupdate", "run",
                     "--token", token],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._mode = "named"
                self._started_at = time.time()
                self._url = ""
                self._configured_host = public_host.strip().rstrip("/")
                self._last_error = ""
                self._log_buffer.clear()
                self._reader_thread = threading.Thread(
                    target=self._reader, args=(self._proc,), daemon=True,
                )
                self._reader_thread.start()
            except Exception as e:
                logger.exception("named tunnel start failed")
                self._last_error = str(e)
                self._proc = None
                return {"error": str(e), "status": self.status()}

        # Persist token (encrypted) + mode + host
        try:
            from db import set_setting, _enc
            set_setting(SETTING_MODE, "named")
            set_setting(SETTING_TOKEN_ENC, _enc(token))
            set_setting(SETTING_NAMED_HOST, self._configured_host)
            if self._configured_host:
                # Public URL is what the user set up at CF — write it as
                # canonical base URL so links/Connect-Center pick it up.
                normalised = self._configured_host
                if not normalised.startswith("http"):
                    normalised = "https://" + normalised
                set_setting("public_url", normalised)
                set_setting(SETTING_LAST_URL, normalised)
                with self._lock:
                    self._url = normalised
        except Exception as e:
            logger.warning("tunnel: could not persist named-tunnel settings: %s", e)
        return self.status()

    # ── Internal ──────────────────────────────────────────────────────────

    def _persist_active_url(self, url: str) -> None:
        try:
            from db import set_setting
            set_setting("public_url", url)
            set_setting(SETTING_LAST_URL, url)
            logger.info("Tunnel: public_url setting updated → %s", url)
        except Exception as e:
            logger.warning("tunnel: could not persist public_url: %s", e)

    def _reader(self, proc) -> None:
        """Runs in a background thread — drains cloudflared stdout/stderr
        and detects the trycloudflare.com URL for Quick Tunnels."""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                with self._lock:
                    self._log_buffer.append(line)
                    if len(self._log_buffer) > 100:
                        self._log_buffer = self._log_buffer[-100:]
                    if self._mode == "quick" and not self._url:
                        m = _QUICK_URL_RE.search(line)
                        if m:
                            self._url = m.group(0)
                            logger.info("Quick Tunnel URL detected: %s", self._url)
                            # Persist outside the lock to avoid contention
                            url_to_persist = self._url
                            self._persist_active_url(url_to_persist)
        except Exception as e:
            logger.warning("tunnel reader stopped: %s", e)
        finally:
            with self._lock:
                if self._proc is proc and proc.poll() is not None:
                    rc = proc.returncode
                    self._last_error = f"cloudflared exited with code {rc}"
                    logger.warning("cloudflared exited (rc=%s)", rc)


_singleton: Optional[TunnelManager] = None


def get_manager() -> TunnelManager:
    global _singleton
    if _singleton is None:
        _singleton = TunnelManager()
    return _singleton


def auto_start_from_settings() -> None:
    """Called once at web-app startup. If the admin previously enabled a
    tunnel, restart it here so the public URL works again after a
    container restart without manual intervention."""
    try:
        from db import get_setting, _dec
        mode = (get_setting(SETTING_MODE, "off") or "off").strip().lower()
        if mode == "off":
            return
        m = get_manager()
        if mode == "quick":
            target_port = int(os.environ.get("WEB_PORT", "8080"))
            logger.info("Tunnel auto-start (quick) on port %d", target_port)
            m.start_quick(target_port)
        elif mode == "named":
            token_enc = get_setting(SETTING_TOKEN_ENC, "")
            if not token_enc:
                logger.warning("Tunnel auto-start (named): token missing")
                return
            try:
                token = _dec(token_enc)
            except Exception:
                logger.warning("Tunnel auto-start (named): token decrypt failed")
                return
            host = get_setting(SETTING_NAMED_HOST, "")
            logger.info("Tunnel auto-start (named) for %s", host or "(no host configured)")
            m.start_named(token, host)
    except Exception as e:
        logger.warning("auto_start_from_settings failed: %s", e)
