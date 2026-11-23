"""Parsing of XRPL metadata, Stellar effects and Solana token balances (no network)."""

import unittest

from scripts.adapters.solana import SolanaAdapter
from scripts.adapters.stellar import StellarAdapter
from scripts.adapters.xrpl import RIPPLE_EPOCH, XrplAdapter, to_raw
from scripts.lib.project import make_line_id

XRPL_ISSUER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
CURRENCY = "EUR"
H1, H2, H3, H4 = "rHolder1", "rHolder2", "rHolder3", "rHolder4"


def ripple_state(kind, low, high, final=None, previous=None, currency=CURRENCY):
    fields = {"LowLimit": {"issuer": low}, "HighLimit": {"issuer": high}}
    body = {"LedgerEntryType": "RippleState"}
    if final is not None:
        fields["Balance"] = {"currency": currency, "value": final}
    if kind == "CreatedNode":
        body["NewFields"] = fields
    else:
        body["FinalFields"] = fields
        if previous is not None:
            body["PreviousFields"] = {"Balance": {"currency": currency, "value": previous}}
    return {kind: body}


class XrplParsingTest(unittest.TestCase):
    def setUp(self):
        identifier = {"issuer": XRPL_ISSUER, "currency": CURRENCY}
        self.adapter = XrplAdapter({"id": make_line_id("eurx", "xrpl", identifier), "chain": "xrpl",
                                    "identifier": identifier, "excluded_addresses": []})

    def test_balance_changes_from_metadata(self):
        meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": [
            ripple_state("ModifiedNode", H1, XRPL_ISSUER, "15", "10"),        # issuer high: holder = low, +5
            ripple_state("ModifiedNode", XRPL_ISSUER, H2, "-1", "-3"),        # issuer low: holder = high, 3 -> 1
            ripple_state("CreatedNode", H3, XRPL_ISSUER, "7"),                # new trust line with 7
            ripple_state("DeletedNode", H4, XRPL_ISSUER, "0", "4"),           # closed after sending 4
            ripple_state("ModifiedNode", H1, XRPL_ISSUER, "9", "1", "USD"),   # other currency
            ripple_state("ModifiedNode", H1, "rOtherIssuer", "9", "1"),       # other issuer
            {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {}}},
        ]}
        changes = self.adapter._balance_changes(meta)
        self.assertEqual(changes, {H1: to_raw("5"), H2: -to_raw("2"), H3: to_raw("7"), H4: -to_raw("4")})

    def test_transfers_of_transaction(self):
        entry = {"tx": {"ledger_index": 100, "date": 86_400},
                 "meta": {"TransactionResult": "tesSUCCESS",
                          "AffectedNodes": [ripple_state("ModifiedNode", H1, XRPL_ISSUER, "3", "1")]}}
        (transfer,) = self.adapter._transfers_of(entry)
        self.assertEqual((transfer.sender, transfer.receiver, transfer.amount), (None, H1, to_raw("2")))
        self.assertEqual(transfer.cursor, {"ledger": 101})
        self.assertEqual(transfer.day, "2000-01-02")
        self.assertEqual(RIPPLE_EPOCH, 946_684_800)

    def test_failed_transaction_ignored(self):
        entry = {"tx": {"ledger_index": 1, "date": 0}, "meta": {"TransactionResult": "tecPATH_DRY"}}
        self.assertEqual(self.adapter._transfers_of(entry), [])


class StellarParsingTest(unittest.TestCase):
    def setUp(self):
        identifier = {"code": "EURX", "issuer": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"}
        self.adapter = StellarAdapter({"id": make_line_id("eurx", "stellar", identifier), "chain": "stellar",
                                       "identifier": identifier, "excluded_addresses": []})
        self.asset = {"asset_type": "credit_alphanum4", "asset_code": "EURX", "asset_issuer": identifier["issuer"]}

    def test_effects(self):
        delta = self.adapter._delta
        self.assertEqual(delta({"type": "account_credited", "amount": "1.5", **self.asset}), 15_000_000)
        self.assertEqual(delta({"type": "account_debited", "amount": "2", **self.asset}), -20_000_000)
        self.assertEqual(delta({"type": "account_credited", "amount": "9", "asset_type": "native"}), 0)
        trade = {"type": "trade", "sold_amount": "3", "sold_asset_code": "EURX",
                 "sold_asset_issuer": self.asset["asset_issuer"], "bought_amount": "4", "bought_asset_type": "native"}
        self.assertEqual(delta(trade), -30_000_000)
        deposit = {"type": "liquidity_pool_deposited",
                   "reserves_deposited": [{"asset": f"EURX:{self.asset['asset_issuer']}", "amount": "1"},
                                          {"asset": "native", "amount": "5"}]}
        self.assertEqual(delta(deposit), -10_000_000)


class SolanaParsingTest(unittest.TestCase):
    MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def setUp(self):
        self.adapter = SolanaAdapter({"id": make_line_id("usdx", "solana", {"mint": self.MINT}), "chain": "solana",
                                      "identifier": {"mint": self.MINT}, "excluded_addresses": []})

    def fake_tx(self, err=None):
        def balance(index, owner, amount, mint=self.MINT):
            return {"accountIndex": index, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount)}}
        return {
            "slot": 42, "blockTime": 1767225600,
            "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in ("payer", "ta1", "ta2", "ta3", "ta4")]}},
            "meta": {"err": err,
                     "preTokenBalances": [balance(1, "alice", 100), balance(2, "bob", 5), balance(3, "alice", 0),
                                          balance(4, "carol", 7, mint="OtherMint")],
                     "postTokenBalances": [balance(1, "alice", 60), balance(2, "bob", 45), balance(3, "alice", 0),
                                           balance(4, "carol", 1, mint="OtherMint")]},
        }

    def test_owner_deltas(self):
        self.adapter._rpc = lambda method, params: self.fake_tx()
        parsed = self.adapter._read_transaction("sig")
        self.assertEqual(parsed["owners"], {"alice": -40, "bob": 40})
        self.assertEqual(sorted(parsed["token_accounts"]), ["ta1", "ta2", "ta3"])
        self.assertEqual(parsed["slot"], 42)

    def test_closed_account(self):
        tx = self.fake_tx()
        tx["meta"]["postTokenBalances"] = [b for b in tx["meta"]["postTokenBalances"] if b["accountIndex"] != 1]
        self.adapter._rpc = lambda method, params: tx
        self.assertEqual(self.adapter._read_transaction("sig")["owners"], {"alice": -100, "bob": 40})

    def test_failed_transaction(self):
        self.adapter._rpc = lambda method, params: self.fake_tx(err={"InstructionError": [0, "Custom"]})
        self.assertIsNone(self.adapter._read_transaction("sig"))


if __name__ == "__main__":
    unittest.main()
