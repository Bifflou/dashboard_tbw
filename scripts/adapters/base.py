"""Adapter interface. The engine (scripts/lib/engine.py) only talks to adapters through this class."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Transfer:
    """One balance movement of the token, in chronological order.

    sender = None means a mint, receiver = None means a burn.
    `cursor` is the chain-specific position to resume from *after* this transfer (e.g. {"block": n + 1}).
    All transfers sharing a cursor must share a day, so resuming never splits a day.
    """

    day: str
    sender: str | None
    receiver: str | None
    amount: int
    cursor: dict


class Adapter:
    family = ""

    def __init__(self, line: dict) -> None:
        self.line = line
        self.chain_id = line["chain"]

    def decimals(self) -> int:
        raise NotImplementedError

    def start_position(self) -> dict:
        """Position to fetch from for a full initialization."""
        raise NotImplementedError

    def fetch_transfers(self, position: dict) -> list[Transfer]:
        """Every transfer from `position` up to now (including today), in chronological order."""
        raise NotImplementedError

    def onchain_supply_raw(self) -> int | None:
        """Current total supply read on-chain (before exclusions), for reconciliation. None if unavailable."""
        return None

    def onchain_balances_raw(self) -> dict[str, int] | None:
        """Every address balance at the same moment as onchain_supply_raw, when the chain lists them cheaply
        (XRPL trust lines, Stellar trustlines...). None when unavailable or too expensive (EVM)."""
        return None

    def non_holder_addresses(self) -> set[str]:
        """Addresses never counted as holders (e.g. the issuer on XRPL/Stellar). Excluded addresses are added by the engine."""
        return set()
