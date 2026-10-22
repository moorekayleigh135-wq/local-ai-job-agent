"""Application-layer encryption for sensitive JSON stored in MySQL."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

from app.config import settings

ENCRYPTED_PREFIX = "enc:v1:"
ASSOCIATED_DATA = b"jobharbor:personal-json:v1"
LEGACY_ASSOCIATED_DATA = b"runway:personal-json:v1"


class EncryptionConfigurationError(ValueError):
    """The local data-encryption key is missing or invalid."""


def encryption_key() -> bytes:
    value = settings.profile_encryption_key.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise EncryptionConfigurationError(
            "PROFILE_ENCRYPTION_KEY must be a 64-character hexadecimal key."
        )
    return bytes.fromhex(value)


def validate_encryption_key() -> None:
    encryption_key()


def encrypt_json(value: object) -> str:
    """Encrypt JSON with AES-256-GCM and a fresh 96-bit nonce."""
    nonce = os.urandom(12)
    cleartext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(encryption_key()).encrypt(nonce, cleartext, ASSOCIATED_DATA)
    encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return ENCRYPTED_PREFIX + encoded


def decrypt_json(value: str) -> object:
    if not value.startswith(ENCRYPTED_PREFIX):
        raise ValueError("Personal data is not encrypted with the current format.")
    try:
        payload = base64.urlsafe_b64decode(value[len(ENCRYPTED_PREFIX):].encode("ascii"))
        nonce, ciphertext = payload[:12], payload[12:]
        cipher = AESGCM(encryption_key())
        try:
            cleartext = cipher.decrypt(nonce, ciphertext, ASSOCIATED_DATA)
        except InvalidTag:
            # Preserve readability for data encrypted during an earlier in-progress build.
            cleartext = cipher.decrypt(nonce, ciphertext, LEGACY_ASSOCIATED_DATA)
        return json.loads(cleartext.decode("utf-8"))
    except (InvalidTag, ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error, IndexError) as exc:
        raise EncryptionConfigurationError(
            "Personal data could not be decrypted. Check that PROFILE_ENCRYPTION_KEY has not changed."
        ) from exc


class EncryptedJSON(TypeDecorator):
    """Store JSON as authenticated ciphertext while exposing normal Python data."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: object, dialect) -> str | None:
        return None if value is None else encrypt_json(value)

    def process_result_value(self, value: object, dialect) -> object:
        if isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX):
            return decrypt_json(value)
        # Plain JSON is accepted only so startup migration can encrypt older rows.
        return value
