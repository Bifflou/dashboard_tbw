import copy
import unittest

from scripts.lib import project as proj

EVM = "0x" + "ab" * 20
EVM_EXCLUDED = "0x" + "cd" * 20
XRPL_ISSUER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
STELLAR_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
SOL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def line(token_id: str, chain: str, identifier: dict, excluded=None) -> dict:
    return {
        "id": proj.make_line_id(token_id, chain, identifier),
        "chain": chain,
        "identifier": identifier,
        "excluded_addresses": excluded or [],
    }


def valid_config() -> dict:
    return {
        "schema_version": 1,
        "slug": "demo-project",
        "name": "Démo Project",
        "image": None,
        "created_at": "2026-11-23T10:00:00Z",
        "updated_at": "2026-11-23T10:00:00Z",
        "tokens": [
            {
                "id": "eurx",
                "name": "Euro X",
                "symbol": "EURX",
                "logo": None,
                "currency": "EUR",
                "category": "stablecoin",
                "nav_type": None,
                "nav_source": None,
                "launch_date": None,
                "lines": [
                    line("eurx", "ethereum", {"address": EVM}, [EVM_EXCLUDED]),
                    line("eurx", "xrpl", {"issuer": XRPL_ISSUER, "currency": "EUR"}),
                ],
            },
            {
                "id": "fundx",
                "name": "Fund X",
                "symbol": "FUNDX",
                "logo": None,
                "currency": "USD",
                "category": "mmf",
                "nav_type": "accumulating",
                "nav_source": {"type": "chainlink", "chain": "ethereum", "address": EVM},
                "launch_date": "2024-04-30",
                "lines": [
                    line("fundx", "stellar", {"code": "FUNDX", "issuer": STELLAR_ISSUER}),
                    line("fundx", "solana", {"mint": SOL_MINT}),
                ],
            },
        ],
    }


class IdentifierTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(proj.slugify("SG Forge"), "sg-forge")
        self.assertEqual(proj.slugify("  Société Générale — Forge! "), "societe-generale-forge")
        self.assertEqual(proj.slugify("!!!"), "project")

    def test_token_id(self):
        self.assertEqual(proj.token_id_from_symbol("EURCV"), "eurcv")
        self.assertEqual(proj.token_id_from_symbol("eur-CV", {"eurcv"}), "eurcv2")

    def test_normalize(self):
        self.assertEqual(proj.normalize(" 0xABcd ", "lower"), "0xabcd")
        self.assertEqual(proj.normalize("eur", "xrpl_currency"), "eur")
        self.assertEqual(proj.normalize("a" * 40, "xrpl_currency"), "A" * 40)

    def test_pinned_line_ids(self):
        # Reference values: the admin's JavaScript implementation must produce exactly these.
        self.assertEqual(proj.make_line_id("eurx", "ethereum", {"address": EVM}), "eurx_ethereum_70ba31b3")
        self.assertEqual(
            proj.make_line_id("eurx", "xrpl", {"issuer": XRPL_ISSUER, "currency": "EUR"}), "eurx_xrpl_082e01f9"
        )
        self.assertEqual(
            proj.make_line_id("usdx", "stellar", {"code": "USDX", "issuer": STELLAR_ISSUER}), "usdx_stellar_e5ca9c94"
        )
        self.assertEqual(proj.make_line_id("usdx", "solana", {"mint": SOL_MINT}), "usdx_solana_4938fca5")

    def test_pinned_fingerprint(self):
        self.assertEqual(
            proj.fingerprint("ethereum", {"address": EVM}, [EVM_EXCLUDED]),
            "e01a197e5e7903d6c20567cc42afb15cb1409ea18c4aa35cf3d132e40e58d4e7",
        )

    def test_fingerprint_changes_with_exclusions_but_not_their_order(self):
        a = proj.fingerprint("ethereum", {"address": EVM}, [EVM_EXCLUDED, "0x" + "ee" * 20])
        b = proj.fingerprint("ethereum", {"address": EVM}, ["0x" + "ee" * 20, EVM_EXCLUDED])
        c = proj.fingerprint("ethereum", {"address": EVM}, [])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_pricing_mode(self):
        self.assertEqual(proj.pricing_mode({"category": "stablecoin"}), "peg")
        self.assertEqual(proj.pricing_mode({"category": "mmf", "nav_type": "rebase"}), "peg")
        self.assertEqual(proj.pricing_mode({"category": "mmf", "nav_type": "accumulating"}), "nav_feed")


class ValidationTest(unittest.TestCase):
    def assertInvalid(self, config, fragment):
        errors = proj.validate_project(config)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_valid_config(self):
        self.assertEqual(proj.validate_project(valid_config(), expected_slug="demo-project"), [])

    def test_slug_must_match_folder(self):
        self.assertTrue(proj.validate_project(valid_config(), expected_slug="other"))

    def test_bad_currency_and_category(self):
        config = valid_config()
        config["tokens"][0]["currency"] = "JPY"
        config["tokens"][0]["category"] = "gold"
        self.assertInvalid(config, "currency")
        self.assertInvalid(config, "category")

    def test_nav_rules(self):
        config = valid_config()
        config["tokens"][1]["nav_source"] = None
        self.assertInvalid(config, "needs a nav_source")

        config = valid_config()
        config["tokens"][0]["nav_source"] = {"type": "coingecko", "coin_id": "x"}
        self.assertInvalid(config, "only allowed")

        config = valid_config()
        config["tokens"][1]["nav_type"] = "rebase"
        self.assertInvalid(config, "only allowed")

        config = valid_config()
        config["tokens"][1]["nav_source"] = {"type": "chainlink", "chain": "solana", "address": EVM}
        self.assertInvalid(config, "EVM chain")

    def test_identifier_must_be_normalized(self):
        config = valid_config()
        config["tokens"][0]["lines"][0]["identifier"]["address"] = EVM.upper().replace("0X", "0x")
        self.assertInvalid(config, "normalized")

    def test_checksum_typo_detected(self):
        config = valid_config()
        config["tokens"][0]["lines"][1] = line("eurx", "xrpl", {"issuer": XRPL_ISSUER[:-1] + "x", "currency": "EUR"})
        self.assertInvalid(config, "checksum")

    def test_wrong_line_id(self):
        config = valid_config()
        config["tokens"][0]["lines"][0]["id"] = "eurx_ethereum_00000000"
        self.assertInvalid(config, "line id must be")

    def test_duplicate_line(self):
        config = valid_config()
        config["tokens"][0]["lines"].append(copy.deepcopy(config["tokens"][0]["lines"][0]))
        self.assertInvalid(config, "duplicate line")

    def test_unknown_chain_and_identifier_keys(self):
        config = valid_config()
        config["tokens"][0]["lines"][0]["chain"] = "dogechain"
        self.assertInvalid(config, "unknown chain")

        config = valid_config()
        config["tokens"][0]["lines"][1]["identifier"] = {"issuer": XRPL_ISSUER}
        self.assertInvalid(config, "exactly these keys")

    def test_duplicate_excluded_addresses(self):
        config = valid_config()
        config["tokens"][0]["lines"][0]["excluded_addresses"] = [EVM_EXCLUDED, EVM_EXCLUDED]
        self.assertInvalid(config, "duplicate excluded")


if __name__ == "__main__":
    unittest.main()
