"""Normalização tolerante de datas usadas nos schemas da API.

O frontend pode enviar uma data ISO completa (`2024-05-01`), apenas ano/mês
(`2024`, `2024-05`) ou string vazia quando a fonte externa não tem data
confiável. Tudo vira `datetime` (meia-noite) ou `None`.
"""

import re
from datetime import date, datetime, time

_PARTIAL_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")


def parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str):
        raise ValueError("Data inválida. Use o formato AAAA-MM-DD.")

    raw = value.strip()
    if not raw:
        return None

    match = _PARTIAL_DATE_RE.match(raw)
    if match:
        year = int(match.group(1))
        month = int(match.group(2) or 1)
        day = int(match.group(3) or 1)
        try:
            return datetime(year, month, day)
        except ValueError as exc:
            raise ValueError("Data inválida. Verifique ano, mês e dia.") from exc

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(
            "Data inválida. Use AAAA-MM-DD (ou envie vazio para omitir a data)."
        ) from exc
