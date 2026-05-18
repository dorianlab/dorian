"""
dorian/vault/crypto.py
----------------------
Server-side AES-256-GCM decryption that mirrors the browser's Web Crypto API
implementation in ``frontend/lib/vault-crypto.ts``.

Used at pipeline execution time to decrypt user environment variables in-memory.
The decrypted values are injected into the pipeline DAG and immediately forgotten.

Algorithm parameters (must match the frontend):
    - KDF:         PBKDF2-SHA256, 600 000 iterations
    - Salt:        16 random bytes (stored per envelope)
    - Cipher:      AES-256-GCM
    - IV:          12 random bytes (stored per envelope)
    - Key length:  256 bits
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


_PBKDF2_ITERATIONS = 600_000
_KEY_LENGTH_BYTES = 32  # 256 bits


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a passphrase + salt via PBKDF2-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=SHA256(),
        length=_KEY_LENGTH_BYTES,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def decrypt_envelope(envelope: dict, passphrase: str) -> str:
    """Decrypt an encrypted envelope produced by the frontend.

    Parameters
    ----------
    envelope : dict
        ``{"ciphertext": "<b64>", "iv": "<b64>", "salt": "<b64>"}``
    passphrase : str
        The user's vault passphrase (transmitted via nonce at execution time).

    Returns
    -------
    str
        The plaintext env var value.

    Raises
    ------
    cryptography.exceptions.InvalidTag
        If the passphrase is wrong or the ciphertext was tampered with.
    KeyError
        If the envelope is missing required fields.
    """
    salt = base64.b64decode(envelope["salt"])
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["ciphertext"])

    key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(iv, ciphertext, None)

    return plaintext_bytes.decode("utf-8")
