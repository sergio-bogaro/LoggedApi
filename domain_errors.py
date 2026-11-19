"""Erros de domínio que devem virar 400 (e não 500).

Herda de `ValueError` de propósito: existem vários `except ValueError` no código
(parsers, matchers, providers) que continuam funcionando sem alteração. A
diferença é que agora conseguimos distinguir um erro esperado de um bug:

- `DomainError` → 400 com a mensagem para o usuário;
- qualquer outra exceção → 500, com traceback no log.
"""

from __future__ import annotations


class DomainError(ValueError):
    """Entrada inválida ou regra de negócio violada (mensagem é segura ao usuário)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
