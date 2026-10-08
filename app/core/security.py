"""Password hashing, CSRF tokens and safe redirects."""

import hmac
import secrets
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
# Verified against when the account does not exist, so login timing does not reveal accounts.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")

PASSWORD_MIN = 8
PASSWORD_MAX = 128


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def password_problem(password: str) -> str | None:
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters."
    if password.isdigit():
        return "Use letters as well as numbers."
    return None


def session_fingerprint(password_hash: str, secret_key: str) -> str:
    """Stored in the session. Changing the password changes it, signing out other devices."""
    return hmac.new(secret_key.encode(), password_hash.encode(), "sha256").hexdigest()[:24]


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def tokens_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow same-site relative paths, so ?next= cannot redirect to another site."""
    if not url:
        return default
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not url.startswith("/") or url.startswith("//"):
        return default
    if "\\" in url:
        return default
    return url
