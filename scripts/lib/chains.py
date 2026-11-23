"""Chain registry (config/chains.json)."""

import re
from functools import lru_cache

from .jsonio import load_json
from .paths import CHAINS_FILE

NORMALIZE_RULES = {"lower", "upper", "none", "xrpl_currency"}


class RegistryError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_registry() -> dict:
    registry = load_json(CHAINS_FILE)
    errors = validate_registry(registry)
    if errors:
        raise RegistryError("Invalid config/chains.json:\n- " + "\n- ".join(errors))
    return registry


def validate_registry(registry: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(registry, dict):
        return ["root must be an object"]
    families = registry.get("families")
    chains = registry.get("chains")
    if not isinstance(families, dict) or not families:
        errors.append("'families' must be a non-empty object")
        families = {}
    if not isinstance(chains, dict) or not chains:
        errors.append("'chains' must be a non-empty object")
        chains = {}

    for family_id, family in families.items():
        fields = family.get("identifier")
        if not isinstance(fields, list) or not fields:
            errors.append(f"family {family_id}: 'identifier' must be a non-empty list")
            continue
        specs = list(fields) + [family.get("excluded_address") or {}]
        for spec in specs:
            for key in ("label", "pattern", "normalize"):
                if key not in spec:
                    errors.append(f"family {family_id}: field spec missing '{key}'")
            if spec.get("normalize") not in NORMALIZE_RULES:
                errors.append(f"family {family_id}: unknown normalize rule {spec.get('normalize')!r}")
            try:
                re.compile(spec.get("pattern", ""))
            except re.error as exc:
                errors.append(f"family {family_id}: invalid pattern ({exc})")

    for chain_id, chain in chains.items():
        if not re.fullmatch(r"[a-z0-9]+", chain_id):
            errors.append(f"chain id {chain_id!r} must be lowercase alphanumeric")
        family = chain.get("family")
        if family not in families:
            errors.append(f"chain {chain_id}: unknown family {family!r}")
        for key in ("name", "logo", "explorer_token_url", "explorer_address_url"):
            if not chain.get(key):
                errors.append(f"chain {chain_id}: missing '{key}'")
        if family == "evm" and not isinstance(chain.get("evm", {}).get("etherscan_chainid"), int):
            errors.append(f"chain {chain_id}: evm chains need evm.etherscan_chainid")
    return errors


def chain_ids() -> list[str]:
    return list(load_registry()["chains"].keys())


def get_chain(chain_id: str) -> dict:
    chains = load_registry()["chains"]
    if chain_id not in chains:
        raise KeyError(f"Unknown chain {chain_id!r}")
    return chains[chain_id]


def family_of(chain_id: str) -> str:
    return get_chain(chain_id)["family"]


def identifier_fields(chain_id: str) -> list[dict]:
    return load_registry()["families"][family_of(chain_id)]["identifier"]


def excluded_address_spec(chain_id: str) -> dict:
    return load_registry()["families"][family_of(chain_id)]["excluded_address"]
