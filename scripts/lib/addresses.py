"""Checksum validation for chain addresses (catches typos that a regex lets through).

EVM addresses are only checked by regex: EIP-55 needs keccak-256, which the standard library does not provide.
"""

import base64
import hashlib

BITCOIN_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
XRPL_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"

STELLAR_ACCOUNT_VERSION = 6 << 3  # "G..." public keys


def b58decode(value: str, alphabet: str) -> bytes | None:
    number = 0
    for char in value:
        index = alphabet.find(char)
        if index < 0:
            return None
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(value) - len(value.lstrip(alphabet[0]))
    return b"\x00" * leading_zeros + body


def is_valid_solana_address(value: str) -> bool:
    decoded = b58decode(value, BITCOIN_ALPHABET)
    return decoded is not None and len(decoded) == 32


def is_valid_xrpl_address(value: str) -> bool:
    decoded = b58decode(value, XRPL_ALPHABET)
    if decoded is None or len(decoded) != 25 or decoded[0] != 0:
        return False
    payload, checksum = decoded[:21], decoded[21:]
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def is_valid_stellar_account(value: str) -> bool:
    if len(value) != 56:
        return False
    try:
        decoded = base64.b32decode(value)
    except (ValueError, base64.binascii.Error):
        return False
    if len(decoded) != 35 or decoded[0] != STELLAR_ACCOUNT_VERSION:
        return False
    body, checksum = decoded[:33], decoded[33:]
    return _crc16_xmodem(body).to_bytes(2, "little") == checksum


# family -> identifier key (or "excluded") -> checksum validator
CHECKSUMS = {
    "solana": {"mint": is_valid_solana_address, "excluded": is_valid_solana_address},
    "xrpl": {"issuer": is_valid_xrpl_address, "excluded": is_valid_xrpl_address},
    "stellar": {"issuer": is_valid_stellar_account, "excluded": is_valid_stellar_account},
}


def checksum_ok(family: str, key: str, value: str) -> bool:
    validator = CHECKSUMS.get(family, {}).get(key)
    return validator(value) if validator else True
