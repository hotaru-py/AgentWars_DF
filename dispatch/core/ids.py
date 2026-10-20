"""Prefixed, sortable ids: ord_01J..., veh_01J... (time-ordered, URL-safe)."""

import os
import time

PREFIXES = {
    "depot": "dep", "vehicle": "veh", "driver": "drv", "customer": "cus", "order": "ord",
    "plan": "pln", "incident": "inc", "decision": "dec", "message": "msg", "policy": "pol",
    "signal": "sig", "replay": "rep",
}

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32


def _b32(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        n, r = divmod(n, 32)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def new_id(kind: str) -> str:
    """new_id("order") -> "ord_01J9Z3K4M7QH2X5RTV8WBN6C0D". 48-bit ms timestamp + 80 random bits."""
    prefix = PREFIXES[kind]
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return f"{prefix}_{_b32(ms, 10)}{_b32(rand, 16)}"


def kind_of(id_: str) -> str | None:
    prefix = id_.split("_", 1)[0]
    return next((k for k, p in PREFIXES.items() if p == prefix), None)
