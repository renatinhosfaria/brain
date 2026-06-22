"""Rate limiting in-memory por principal (token bucket).

O estado vive no processo da API; é adequado ao deploy de instância única do MCP.
Cada principal (curador/cliente) tem seu próprio balde, reabastecido a uma taxa
constante até um teto (capacidade = burst permitido).
"""

import time
from collections.abc import Callable


class RateLimitExceeded(Exception):
    """O principal excedeu o limite de requisições permitido."""


class RateLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = float(capacity)
        self._refill = refill_per_second
        self._clock = clock
        # key -> (tokens disponíveis, instante da última atualização)
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        """Consome um token de `key`. Retorna False se não houver token disponível."""
        now = self._clock()
        tokens, last = self._state.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + max(0.0, now - last) * self._refill)
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True
