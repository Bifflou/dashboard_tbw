"""Project configuration (projects/<slug>/project.json): identifiers, validation, loading.

The admin page (JavaScript) implements the same normalisation, line_id and fingerprint rules.
Reference values are pinned in tests/test_project.py so both sides stay identical.
"""

import hashlib
import re
import unicodedata

from . import chains
from .addresses import checksum_ok
from .dates import is_valid_day
from .jsonio import load_json
from .paths import PROJECTS_DIR, project_config_file

SCHEMA_VERSION = 1
CURRENCIES = ("EUR", "USD", "CHF", "GBP")
CATEGORIES = ("stablecoin", "mmf")
NAV_TYPES = ("rebase", "accumulating")
NAV_SOURCE_TYPES = ("chainlink", "coingecko")

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TOKEN_ID_RE = re.compile(r"^[a-z0-9]{1,32}$")
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
COINGECKO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ── Identifiers ──────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:48].strip("-") or "project"


def token_id_from_symbol(symbol: str, taken: set[str] | None = None) -> str:
    taken = taken or set()
    base = re.sub(r"[^a-z0-9]", "", symbol.lower())[:28] or "token"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}{n}", n + 1
    return candidate


def normalize(value: str, rule: str) -> str:
    value = value.strip()
    if rule == "lower":
        return value.lower()
    if rule == "upper":
        return value.upper()
    if rule == "xrpl_currency":
        return value.upper() if len(value) == 40 else value
    return value


def canonical_identifier(chain_id: str, identifier: dict) -> str:
    fields = chains.identifier_fields(chain_id)
    values = [normalize(str(identifier[f["key"]]), f["normalize"]) for f in fields]
    return "|".join([chain_id, *values])


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_line_id(token_id: str, chain_id: str, identifier: dict) -> str:
    return f"{token_id}_{chain_id}_{_sha256_hex(canonical_identifier(chain_id, identifier))[:8]}"


def fingerprint(chain_id: str, identifier: dict, excluded_addresses: list[str]) -> str:
    rule = chains.excluded_address_spec(chain_id)["normalize"]
    excluded = sorted({normalize(a, rule) for a in excluded_addresses})
    return _sha256_hex(canonical_identifier(chain_id, identifier) + "#" + ",".join(excluded))


def pricing_mode(token: dict) -> str:
    """'peg' (price = 1 unit of the token currency) or 'nav_feed' (price = NAV of the day)."""
    if token.get("category") == "mmf" and token.get("nav_type") == "accumulating":
        return "nav_feed"
    return "peg"


# ── Validation ───────────────────────────────────────────────────────────────────

def validate_project(config: dict, expected_slug: str | None = None) -> list[str]:
    """Return a list of human-readable errors (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["project.json must be an object"]

    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    slug = config.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        errors.append(f"invalid slug {slug!r}")
    elif expected_slug is not None and slug != expected_slug:
        errors.append(f"slug {slug!r} does not match folder name {expected_slug!r}")
    if not _non_empty_str(config.get("name")):
        errors.append("name is required")
    if config.get("image") is not None and not _non_empty_str(config.get("image")):
        errors.append("image must be a path or null")

    tokens = config.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        errors.append("at least one token is required")
        return errors

    token_ids: set[str] = set()
    line_ids: set[str] = set()
    for t_index, token in enumerate(tokens):
        where = f"token #{t_index + 1}"
        if not isinstance(token, dict):
            errors.append(f"{where}: must be an object")
            continue
        token_id = token.get("id")
        if not isinstance(token_id, str) or not TOKEN_ID_RE.match(token_id):
            errors.append(f"{where}: invalid id {token_id!r}")
        elif token_id in token_ids:
            errors.append(f"{where}: duplicate id {token_id!r}")
        else:
            token_ids.add(token_id)
            where = f"token {token_id}"
        errors.extend(_validate_token(token, where))

        lines = token.get("lines")
        if not isinstance(lines, list) or not lines:
            errors.append(f"{where}: at least one line is required")
            continue
        for l_index, line in enumerate(lines):
            line_errors, line_id = _validate_line(line, token_id, f"{where} line #{l_index + 1}")
            errors.extend(line_errors)
            if line_id:
                if line_id in line_ids:
                    errors.append(f"{where}: duplicate line {line_id} (same chain and identifier twice)")
                line_ids.add(line_id)
    return errors


def _validate_token(token: dict, where: str) -> list[str]:
    errors: list[str] = []
    for key in ("name", "symbol"):
        if not _non_empty_str(token.get(key)):
            errors.append(f"{where}: {key} is required")
    if token.get("logo") is not None and not _non_empty_str(token.get("logo")):
        errors.append(f"{where}: logo must be a path or null")
    if token.get("currency") not in CURRENCIES:
        errors.append(f"{where}: currency must be one of {', '.join(CURRENCIES)}")
    category = token.get("category")
    if category not in CATEGORIES:
        errors.append(f"{where}: category must be one of {', '.join(CATEGORIES)}")

    nav_type, nav_source = token.get("nav_type"), token.get("nav_source")
    if category == "stablecoin" and nav_type is not None:
        errors.append(f"{where}: a stablecoin has no nav_type")
    if category == "mmf" and nav_type not in NAV_TYPES:
        errors.append(f"{where}: an mmf needs nav_type 'rebase' or 'accumulating'")
    if nav_type == "accumulating":
        errors.extend(_validate_nav_source(nav_source, where))
    elif nav_source is not None:
        errors.append(f"{where}: nav_source is only allowed when nav_type is 'accumulating'")

    launch_date = token.get("launch_date")
    if launch_date is not None and not is_valid_day(launch_date):
        errors.append(f"{where}: launch_date must be YYYY-MM-DD or null")
    return errors


def _validate_nav_source(source: object, where: str) -> list[str]:
    if not isinstance(source, dict):
        return [f"{where}: an accumulating mmf needs a nav_source"]
    kind = source.get("type")
    if kind == "chainlink":
        chain_id = source.get("chain")
        if chain_id not in chains.chain_ids() or chains.family_of(chain_id) != "evm":
            return [f"{where}: chainlink nav_source needs an EVM chain"]
        if not isinstance(source.get("address"), str) or not EVM_ADDRESS_RE.match(source["address"]):
            return [f"{where}: chainlink nav_source address must be a lowercase 0x address"]
        return []
    if kind == "coingecko":
        if not isinstance(source.get("coin_id"), str) or not COINGECKO_ID_RE.match(source["coin_id"]):
            return [f"{where}: coingecko nav_source needs a coin_id"]
        return []
    return [f"{where}: nav_source.type must be one of {', '.join(NAV_SOURCE_TYPES)}"]


def _validate_line(line: object, token_id: object, where: str) -> tuple[list[str], str | None]:
    if not isinstance(line, dict):
        return [f"{where}: must be an object"], None
    chain_id = line.get("chain")
    if chain_id not in chains.chain_ids():
        return [f"{where}: unknown chain {chain_id!r}"], None
    family = chains.family_of(chain_id)
    where = f"{where} ({chain_id})"
    errors: list[str] = []

    identifier = line.get("identifier")
    fields = chains.identifier_fields(chain_id)
    if not isinstance(identifier, dict) or set(identifier) != {f["key"] for f in fields}:
        expected = ", ".join(f["key"] for f in fields)
        return [f"{where}: identifier must have exactly these keys: {expected}"], None
    for field in fields:
        errors.extend(_check_value(identifier[field["key"]], field, family, field["key"], f"{where} {field['label']}"))

    excluded = line.get("excluded_addresses")
    if not isinstance(excluded, list):
        errors.append(f"{where}: excluded_addresses must be a list (possibly empty)")
    else:
        spec = chains.excluded_address_spec(chain_id)
        for address in excluded:
            errors.extend(_check_value(address, spec, family, "excluded", f"{where} excluded address"))
        if len(set(excluded)) != len(excluded):
            errors.append(f"{where}: duplicate excluded addresses")

    if errors or not isinstance(token_id, str):
        return errors, None
    expected_id = make_line_id(token_id, chain_id, identifier)
    if line.get("id") != expected_id:
        errors.append(f"{where}: line id must be {expected_id!r}, got {line.get('id')!r}")
    return errors, expected_id


def _check_value(value: object, spec: dict, family: str, key: str, where: str) -> list[str]:
    if not isinstance(value, str) or not re.fullmatch(spec["pattern"], value):
        return [f"{where}: invalid format {value!r}"]
    if value != normalize(value, spec["normalize"]):
        return [f"{where}: must be normalized as {normalize(value, spec['normalize'])!r}"]
    if not checksum_ok(family, key, value):
        return [f"{where}: checksum mismatch for {value!r} (typo?)"]
    return []


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ── Loading ──────────────────────────────────────────────────────────────────────

def list_project_slugs() -> list[str]:
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.parent.name for p in PROJECTS_DIR.glob("*/project.json"))


def load_project(slug: str) -> dict:
    return load_json(project_config_file(slug))


def iter_lines(config: dict):
    """Yield (token, line) pairs."""
    for token in config.get("tokens", []):
        for line in token.get("lines", []):
            yield token, line
