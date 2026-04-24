"""
Crypto — Verschlüsselung für sensible Felder in der Datenbank
=============================================================
Verwendet Fernet (symmetrische Verschlüsselung) für Printix API Keys,
SQL-Passwörter und Mail API Keys.

Der Fernet-Key wird einmalig generiert und in /data/mcp_secrets.json
gespeichert (gleiches Persistenz-File wie Bearer Token + OAuth Secret).

Passwörter werden mit bcrypt gehasht (nicht reversibel).
"""

import base64
import logging
import os

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet
    FERNET_AVAILABLE = True
except ImportError:
    FERNET_AVAILABLE = False
    logger.warning("cryptography nicht installiert — Feldverschlüsselung deaktiviert")

try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False
    logger.warning("bcrypt nicht installiert — Passwort-Hashing deaktiviert")


def _get_fernet() -> "Fernet":
    key = os.environ.get("FERNET_KEY", "")
    if not key:
        # Fallback: direkt aus /data/fernet.key lesen (z.B. bei manuellem Neustart via docker exec)
        try:
            with open("/data/fernet.key", "r") as _f:
                key = _f.read().strip()
            if key:
                os.environ["FERNET_KEY"] = key  # für spätere Aufrufe cachen
        except Exception:
            pass
    if not key:
        raise RuntimeError(
            "FERNET_KEY nicht gesetzt. run.sh muss den Key aus /data/fernet.key laden."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Verschlüsselt einen String. Gibt leeren String zurück wenn Input leer."""
    if not plaintext:
        return ""
    if not FERNET_AVAILABLE:
        logger.warning("Fernet nicht verfügbar — Wert wird unverschlüsselt gespeichert")
        return plaintext
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Entschlüsselt einen verschlüsselten String. Gibt leeren String zurück wenn Input leer."""
    if not ciphertext:
        return ""
    if not FERNET_AVAILABLE:
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception as e:
        logger.error("Entschlüsselungsfehler: %s", e)
        return ""


def hash_password(password: str) -> str:
    """Hasht ein Passwort mit bcrypt."""
    if not BCRYPT_AVAILABLE:
        raise RuntimeError("bcrypt nicht installiert")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Prüft ein Passwort gegen einen bcrypt-Hash."""
    if not BCRYPT_AVAILABLE:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def generate_fernet_key() -> str:
    """Generiert einen neuen Fernet-Key (Base64-URL-encoded, 32 Bytes)."""
    return Fernet.generate_key().decode()
