import unittest

from scripts.lib import chains
from scripts.lib.addresses import (
    is_valid_solana_address,
    is_valid_stellar_account,
    is_valid_xrpl_address,
)
from scripts.lib.paths import ROOT


class RegistryTest(unittest.TestCase):
    def test_registry_is_valid(self):
        registry = chains.load_registry()
        self.assertEqual(chains.validate_registry(registry), [])

    def test_base_chains_present(self):
        self.assertEqual(
            {c: chains.family_of(c) for c in ("ethereum", "solana", "xrpl", "stellar")},
            {"ethereum": "evm", "solana": "solana", "xrpl": "xrpl", "stellar": "stellar"},
        )

    def test_chain_logos_exist(self):
        for chain_id in chains.chain_ids():
            logo = ROOT / chains.get_chain(chain_id)["logo"]
            self.assertTrue(logo.exists(), f"missing logo for {chain_id}: {logo}")

    def test_invalid_registry_is_reported(self):
        errors = chains.validate_registry({"families": {}, "chains": {"Bad": {"family": "nope"}}})
        self.assertTrue(errors)


class AddressChecksumTest(unittest.TestCase):
    # Well-known public addresses.
    def test_solana(self):
        self.assertTrue(is_valid_solana_address("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        self.assertTrue(is_valid_solana_address("So11111111111111111111111111111111111111112"))
        self.assertFalse(is_valid_solana_address("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt"))

    def test_xrpl(self):
        self.assertTrue(is_valid_xrpl_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"))
        self.assertTrue(is_valid_xrpl_address("rrrrrrrrrrrrrrrrrrrrrhoLvTp"))
        self.assertFalse(is_valid_xrpl_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTx"))  # one char changed

    def test_stellar(self):
        self.assertTrue(is_valid_stellar_account("GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"))
        self.assertFalse(is_valid_stellar_account("GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVM"))
        self.assertFalse(is_valid_stellar_account("GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZV"))


if __name__ == "__main__":
    unittest.main()
