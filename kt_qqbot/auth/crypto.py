"""AES-256-GCM helpers for the QR bind protocol."""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kt_qqbot.errors import BindError

KEY_BYTES = 32
IV_BYTES = 12
TAG_BYTES = 16


def new_key() -> bytes:
    """Return a fresh random 32-byte key."""
    return os.urandom(KEY_BYTES)


def encode_key(key: bytes) -> str:
    """Return the base64 form sent to ``create_bind_task``."""
    return base64.b64encode(key).decode("ascii")


def encrypt_secret(plaintext: str, key: bytes, iv: bytes | None = None) -> str:
    """Encrypt to ``base64(IV | ciphertext | tag)``; the inverse of :func:`decrypt_secret`."""
    iv = iv or os.urandom(IV_BYTES)
    sealed = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv + sealed).decode("ascii")


def decrypt_secret(blob_b64: str, key: bytes) -> str:
    """Decrypt ``bot_encrypt_secret`` from ``poll_bind_result``."""
    try:
        blob = base64.b64decode(blob_b64)
    except ValueError as exc:
        raise BindError(f"bind secret is not valid base64: {exc}") from exc
    if len(blob) < IV_BYTES + TAG_BYTES + 1:
        raise BindError("bind secret is too short")
    iv, sealed = blob[:IV_BYTES], blob[IV_BYTES:]
    try:
        plain = AESGCM(key).decrypt(iv, sealed, None)
    except Exception as exc:
        raise BindError("bind secret failed authentication") from exc
    return plain.decode("utf-8")
