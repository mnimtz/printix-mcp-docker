import base64
from typing import Optional

from printix_client import PrintixClient, PrintixAPIError


def _is_base64(s: str) -> bool:
    try:
        return base64.b64encode(base64.b64decode(s)).decode() == s
    except Exception:
        return False


def _b64_text(s: str) -> str:
    return base64.b64encode((s or '').encode('utf-8')).decode('ascii')


def _decode_b64(s: str) -> Optional[str]:
    try:
        return base64.b64decode(s).decode('utf-8')
    except Exception:
        return None


def _candidates(value: str) -> list[str]:
    raw = (value or '').strip()
    if not raw:
        return []

    out: list[str] = []

    def add(v: Optional[str]) -> None:
        if v and v not in out:
            out.append(v)

    add(raw)
    norm = raw.replace(' ', '').replace(':', '').replace('-', '')
    add(norm)

    stripped = norm.lstrip('0') or '0'
    add(stripped)
    add('0' + stripped)

    if _is_base64(raw):
        dec = _decode_b64(raw)
        add(dec)
        if dec:
            dnorm = dec.replace(' ', '').replace(':', '').replace('-', '')
            add(dnorm)
            add(dnorm.lstrip('0') or '0')
            add('0' + (dnorm.lstrip('0') or '0'))

    for item in list(out):
        add(_b64_text(item))

    return out


_original_search_card = PrintixClient.search_card


def _patched_search_card(self, card_id=None, card_number=None):
    if card_id:
        return _original_search_card(self, card_id=card_id, card_number=None)
    if not card_number:
        raise ValueError('Either card_id or card_number must be provided.')

    tried: list[str] = []
    last_error = None

    for candidate in _candidates(card_number):
        tried.append(candidate)
        try:
            result = _original_search_card(self, card_id=None, card_number=candidate)
            if isinstance(result, dict):
                result.setdefault('_lookup', {
                    'input': card_number,
                    'matched_candidate': candidate,
                    'tried_candidates': tried,
                })
            return result
        except PrintixAPIError as e:
            if getattr(e, 'status_code', None) == 404:
                last_error = e
                continue
            raise

    raise PrintixAPIError(
        404,
        f"Card not found for input '{card_number}'. Tried candidates: {', '.join(tried)}",
        getattr(last_error, 'error_id', ''),
    )


PrintixClient.search_card = _patched_search_card
