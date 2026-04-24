"""
Email Recipient Parser & Validator
===================================
Robustes Parsen und Validieren von E-Mail-Empfänger-Listen für Report-Versand.

Unterstützte Eingabeformate (gemischt, kommasepariert oder semikolongetrennt):
  - plain:          max@firma.de
  - mit Name:       Max Mustermann <max@firma.de>
  - quoted name:    "Nimtz, Marcus" <marcus@firma.de>
  - Liste:          max@firma.de, "Erika M." <erika@firma.de>; test@x.de

Die Ausgabe von parse_recipient_list() ist immer eine Liste von normalisierten
Strings im Format, das Resend akzeptiert:
  - "email@domain"                  (wenn kein Name vorhanden)
  - "Name <email@domain>"           (wenn Name vorhanden, ggf. quoted)

validate_recipients() prüft JEDEN Eintrag und liefert (ok_list, errors).
Ist errors nicht leer, darf der Mail-Versand NICHT stattfinden — sonst
gibt Resend einen 422-Fehler zurück.
"""

import re
from email.utils import parseaddr, formataddr
from typing import Tuple, List

# RFC-5322-light: erlaubt die gängigen lokalen Teile + Domain mit TLD.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)

# Zeichen, die einen Quoted Display Name erzwingen (RFC 5322 §3.2.3)
_NAME_SPECIALS = set(',"<>@;:()[]\\')


def _split_outside_quotes(s: str) -> List[str]:
    """
    Splittet an ',' oder ';', aber respektiert doppelte Anführungszeichen
    und eckige Klammern <...> (damit 'Nimtz, Marcus <x@y.de>' intakt bleibt).
    """
    parts: List[str] = []
    buf: List[str] = []
    in_quotes = False
    in_angle = False

    for ch in s:
        if ch == '"' and not in_angle:
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == '<' and not in_quotes:
            in_angle = True
            buf.append(ch)
        elif ch == '>' and not in_quotes:
            in_angle = False
            buf.append(ch)
        elif ch in (',', ';') and not in_quotes and not in_angle:
            token = "".join(buf).strip()
            if token:
                parts.append(token)
            buf = []
        else:
            buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    return parts


def _needs_quoting(name: str) -> bool:
    """Prüft ob ein Display-Name in Anführungszeichen gesetzt werden muss."""
    return any(c in _NAME_SPECIALS for c in name)


def _format_recipient(name: str, email: str) -> str:
    """
    Baut einen Resend-kompatiblen Empfänger-String.
      - ohne Name:       "max@firma.de"
      - mit Name:        "Max Mustermann <max@firma.de>"
      - mit Sonderzeich: '"Nimtz, Marcus" <marcus@firma.de>'
    """
    name = (name or "").strip().strip('"').strip()
    email = (email or "").strip().strip("<>").strip()
    if not name:
        return email
    if _needs_quoting(name):
        # Escape interne Anführungszeichen
        safe = name.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{safe}" <{email}>'
    return f"{name} <{email}>"


def parse_recipient_list(raw: str) -> List[str]:
    """
    Parst einen Roh-String (z.B. aus einem Formularfeld) in eine Liste von
    normalisierten, Resend-kompatiblen Empfänger-Strings.

    Wirft KEINE Exception bei ungültigen Einträgen — diese kommen als
    Rohstring durch, damit validate_recipients() sie später als Fehler
    meldet. Leere Strings werden entfernt.

    Beispiele:
      'max@x.de, Erika <erika@x.de>' -> ['max@x.de', 'Erika <erika@x.de>']
      '"Nimtz, Marcus" <m@x.de>'     -> ['"Nimtz, Marcus" <m@x.de>']
      'max@x.de; foo@y.de'           -> ['max@x.de', 'foo@y.de']
    """
    if not raw or not raw.strip():
        return []

    tokens = _split_outside_quotes(raw)
    result: List[str] = []

    for tok in tokens:
        # parseaddr versteht sowohl 'a@b' als auch 'Name <a@b>' und '"X Y" <a@b>'
        name, email = parseaddr(tok)
        if email and _EMAIL_RE.match(email):
            result.append(_format_recipient(name, email))
        else:
            # Unparsbarer Input — als roher Token zurückgeben, damit
            # validate_recipients() ihn später sauber als Fehler meldet.
            result.append(tok.strip())

    return result


def validate_recipient(entry: str) -> Tuple[bool, str]:
    """
    Prüft einen einzelnen Empfänger-String.

    Returns:
        (True, "")       wenn gültig
        (False, reason)  wenn ungültig (reason ist Deutsch, user-facing)
    """
    if not entry or not entry.strip():
        return False, "leerer Eintrag"

    name, email = parseaddr(entry)
    if not email:
        return False, f"keine E-Mail-Adresse gefunden in '{entry}'"

    if not _EMAIL_RE.match(email):
        return False, (
            f"ungültige E-Mail-Adresse '{email}' — "
            f"erwartet Format: name@firma.de oder Name <name@firma.de>"
        )

    return True, ""


def validate_recipients(entries: List[str]) -> Tuple[List[str], List[str]]:
    """
    Validiert eine Liste von Empfänger-Strings.

    Returns:
        (ok_list, errors)
          ok_list: alle gültigen Empfänger, Resend-kompatibel normalisiert
          errors:  Liste von Fehlermeldungen (Deutsch, user-facing)

    Der Mail-Versand darf nur stattfinden, wenn errors leer ist UND
    ok_list nicht leer ist.
    """
    ok: List[str] = []
    errors: List[str] = []

    for entry in entries:
        valid, reason = validate_recipient(entry)
        if valid:
            name, email = parseaddr(entry)
            ok.append(_format_recipient(name, email))
        else:
            errors.append(reason)

    return ok, errors


def parse_and_validate(raw: str) -> Tuple[List[str], List[str]]:
    """
    Convenience: parst UND validiert in einem Schritt.
    Returns (ok_list, errors). Wenn errors leer ist, ist ok_list bereit
    für send_report().
    """
    parsed = parse_recipient_list(raw)
    return validate_recipients(parsed)
