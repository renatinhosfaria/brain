import contextvars
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet


class AuthError(Exception):
    """Token de autenticação ausente ou inválido."""


@dataclass(frozen=True)
class Principal:
    type: str  # "curator" | "client"
    slug: str
    name: str


_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "brain_current_principal", default=None
)


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


def generate_client_token(slug: str) -> str:
    return f"brain_client_{slug}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def encrypt_token(token: str, key: str) -> str:
    return Fernet(key.encode("utf-8")).encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted: str, key: str) -> str:
    return Fernet(key.encode("utf-8")).decrypt(encrypted.encode("utf-8")).decode("utf-8")


def set_current_principal(principal: Principal):
    return _current_principal.set(principal)


def reset_current_principal(token) -> None:
    _current_principal.reset(token)


def get_current_principal() -> Principal:
    principal = _current_principal.get()
    if principal is None:
        raise AuthError("Principal ausente")
    return principal


async def resolve_principal(session, settings, bearer_token: str) -> Principal:
    curator_token = settings.brain_curator_token
    if not curator_token:
        raise AuthError("Token de curador nao configurado")
    if curator_token and hmac.compare_digest(bearer_token, curator_token):
        return Principal("curator", settings.brain_curator_slug, settings.brain_curator_name)

    from brain.storage import repositories as repo

    client = await repo.get_agent_client_by_token_hash(session, hash_token(bearer_token))
    if client is not None and client.status == "active":
        await repo.touch_agent_client_seen(session, client.slug)
        return Principal("client", client.slug, client.name)

    raise AuthError("Token invalido")
