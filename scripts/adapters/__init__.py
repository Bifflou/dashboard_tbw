"""Chain adapters: one per chain family, all with the interface of adapters.base.Adapter."""

from scripts.lib import chains

from .base import Adapter, Transfer


def get_adapter(line: dict) -> Adapter:
    family = chains.family_of(line["chain"])
    if family == "evm":
        from .evm import EvmAdapter
        return EvmAdapter(line)
    if family == "xrpl":
        from .xrpl import XrplAdapter
        return XrplAdapter(line)
    if family == "stellar":
        from .stellar import StellarAdapter
        return StellarAdapter(line)
    if family == "solana":
        from .solana import SolanaAdapter
        return SolanaAdapter(line)
    raise NotImplementedError(f"No adapter yet for the {family} family (SPEC §11)")


__all__ = ["Adapter", "Transfer", "get_adapter"]
