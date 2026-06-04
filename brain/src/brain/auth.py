import hmac


class AuthError(Exception):
    """Token de autenticação ausente ou inválido."""


def verify_bearer_token(authorization_header: str | None, *, expected: str) -> None:
    if not authorization_header:
        raise AuthError("Authorization header ausente")
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        raise AuthError("Formato esperado: 'Bearer <token>'")
    token = authorization_header[len(prefix):]
    # Comparação em tempo constante para evitar timing attacks
    if not hmac.compare_digest(token, expected):
        raise AuthError("Token inválido")
