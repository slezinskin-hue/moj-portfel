"""Wspolne narzedzia importu: konwersje liczb, kanoniczny hash wiersza, maskowanie danych wrazliwych."""
import hashlib, json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


class ImportFailed(Exception):
    """Blad blokujacy import (struktura pliku, nowe konto bez potwierdzenia itp.). Komunikat po polsku, partia jest wycofywana."""


def blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def clean(v):
    """'' i None -> None; teksty bez bialych znakow na brzegach."""
    if blank(v):
        return None
    return v.strip() if isinstance(v, str) else v


def dec(v, places=None):
    """float/int/str -> Decimal bez bledow zmiennoprzecinkowych (przez repr tekstowy), opcjonalnie zaokraglony do `places`."""
    if blank(v):
        return None
    d = Decimal(str(v).strip())
    if places is not None:
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return d


def canon(d):
    """Postac kanoniczna Decimal (bez koncowych zer) do skrotow."""
    if d is None:
        return None
    s = format(d.normalize(), "f")
    return s


def as_utc(dt):
    """Znaczniki czasu z eksportu XTB sa bez strefy; kolumna mowi UTC (Z-08): dopisujemy UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def iso_ms(dt):
    return as_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.") + f"{as_utc(dt).microsecond // 1000:03d}Z"


def row_hash(fields):
    """SHA-256 z kanonicznego JSON-a znaczacych pol (dokument 7 §7.3)."""
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def jsonable(obj):
    """Do zapisu w JSONB: Decimal -> tekst, datetime -> ISO, reszta bez zmian."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return iso_ms(obj)
    return obj


def mask_identifier(value, keep_start=2, keep_end=4):
    """Maskowanie identyfikatorow w logach i raportach (np. IBAN): PL12...3629."""
    if not value or len(value) <= keep_start + keep_end:
        return "***"
    return f"{value[:keep_start + 2]}…{value[-keep_end:]}"
