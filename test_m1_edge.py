#!/usr/bin/env python3
"""Offline unit/regression tests for the opt-in M1E paper second-book model.

Run: python -u test_m1_edge.py
No network, pandas, Telegram, or live-scanner imports are required.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from m1_edge.model import (
    AUDIT_MEDIAN_COST_R,
    AUDIT_MEDIAN_RISK_PCT,
    Config,
    SCHEMA,
    SecondBookModel,
    evaluate_if_enabled,
    finalize_if_enabled,
    from_board_payload,
)

ROOT = Path(__file__).resolve().parent


def _payload(bar: str, *, persist: float = 95.0, counter: float = 2.0,
             qualified: bool = True, mv: float = 2.0, second_mv: float = 1.5,
             rank1: float = 80.0, rank2: float = 70.0, pick: str = "WAIT") -> dict:
    return {
        "b": bar,
        "session": "2026-11-25",
        "pick": pick,
        "market": "BULL",
        "sectors": {
            "FMCG": {
                "bull": persist, "bear": counter, "side": 1,
                "gate": int(qualified), "qualified": int(qualified),
                "day": 2.0, "intra": 1.0,
                "n": ["HINDUNILVR", "DABUR", "BRITANNIA"],
                "rank": [rank1, rank2, 50.0],
                "rk": [1, 2, 3],
                "mv": [mv, second_mv, 0.5],
                "s": [90.0, 80.0, 70.0],
                "t": [90.0, 80.0, 70.0],
            },
            "MEDIA": {
                # Below the 90 persistence floor by default, so synthetic tests
                # exercise one book. Individual tests can raise it explicitly.
                "bull": 1.0, "bear": 85.0, "side": -1,
                "gate": 1, "qualified": 1,
                "n": ["SUNTV", "SAREGAMA"],
                "rank": [90.0, 80.0], "rk": [1, 2], "mv": [-4.0, -3.0],
                "s": [90.0, 80.0], "t": [90.0, 80.0],
            },
        },
    }


def _config(tmp: Path, **overrides) -> Config:
    env = {
        "M1E_ENABLED": "1",
        "M1E_MODE": "paper",
        "M1E_STATE_PATH": str(tmp / "edge_state.json"),
        "M1E_JOURNAL_DIR": str(tmp / "journal"),
    }
    env.update({f"M1E_{k}": str(v) for k, v in overrides.items()})
    return Config.from_env(env)


class Sender:
    """Fake telegram.send_bot3. Records the distinct M1E cards."""

    def __init__(self, responses=None):
        self.messages: list[str] = []
        self.calls = 0
        self.responses = list(responses or [])

    def __call__(self, text: str, *, dry_run: bool = False) -> bool:
        self.messages.append(text)
        self.calls += 1
        if self.responses:
            return bool(self.responses.pop(0))
        return True


class TestM1Edge(unittest.TestCase):
    def test_disabled_hook_is_noop_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "edge_state.json"
            env = {"M1E_ENABLED": "0", "M1E_STATE_PATH": str(state),
                   "M1E_JOURNAL_DIR": str(Path(td) / "journal")}
            result = evaluate_if_enabled(_payload("09:35"), env=env)
            self.assertFalse(result["enabled"])
            self.assertEqual(result["new_entries"], [])
            self.assertEqual(result["alerts"], [])
            self.assertFalse(state.exists())
            result2 = finalize_if_enabled(session="2026-11-25", env=env)
            self.assertFalse(result2["enabled"])
            self.assertFalse(state.exists())

    def test_live_mode_is_refused(self):
        with self.assertRaises(ValueError):
            Config.from_env({"M1E_ENABLED": "1", "M1E_MODE": "live"})

    def test_shadow_alias_becomes_paper_and_alerts_on(self):
        cfg = Config.from_env({"M1E_ENABLED": "1", "M1E_MODE": "shadow"})
        self.assertEqual(cfg.mode, "paper")
        self.assertTrue(cfg.alert)

    def test_opt_in_sends_distinct_bot3_alert_and_separate_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _config(tmp)
            sender = Sender()
            original_ledger = tmp / "scan1m_state.json"
            original_ledger.write_text('{"session":"2026-11-25"}', encoding="utf-8")
            before = original_ledger.read_text(encoding="utf-8")
            model = SecondBookModel(cfg, sender=sender, dry_run=True)
            result = model.process(from_board_payload(_payload("09:35")))
            self.assertTrue(result["enabled"])
            self.assertEqual(result["mode"], "paper")
            self.assertEqual(len(result["new_entries"]), 2)
            self.assertEqual([e["name"] for e in result["new_entries"]],
                             ["HINDUNILVR", "DABUR"])
            self.assertEqual(len(result["alerts"]), 1)
            self.assertTrue(result["alerts"][0]["delivered"])
            self.assertFalse(result["alert_delivery_failed"])
            card = sender.messages[0]
            self.assertIn("[M1E · PAPER SECOND BOOK]", card)
            self.assertIn("PAPER", card)
            self.assertIn("Original 1m book unchanged", card)
            self.assertIn("HINDUNILVR", card)
            self.assertEqual(original_ledger.read_text(encoding="utf-8"), before)
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], SCHEMA)
            self.assertEqual(len(state["entries"]), 2)
            self.assertEqual(len(state["alerted"]), 1)
            self.assertEqual(state["pending_alerts"], [])
            self.assertTrue(all(e["mode"] == "paper" and e["status"] == "open"
                                for e in state["entries"]))
            journal = Path(cfg.journal_dir) / "m1_edge_2026-11-25.jsonl"
            self.assertTrue(journal.exists())
            self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 1)

    def test_gates(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(Path(td), MIN_RANK="95")
            model = SecondBookModel(cfg, sender=Sender(), dry_run=True)
            result = model.process(from_board_payload(_payload("09:35")))
            self.assertEqual(result["new_entries"], [])
            self.assertEqual(result["alerts"], [])
            self.assertTrue(any(r["why"] == "rank_below_min" for r in result["rejections"]))

    def test_duplicate_bar_does_not_double_mark_or_realert(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(Path(td))
            sender = Sender()
            model = SecondBookModel(cfg, sender=sender, dry_run=True)
            model.process(from_board_payload(_payload("09:35")))
            duplicate = model.process(from_board_payload(_payload("09:35")))
            self.assertTrue(any(r["why"] == "duplicate_bar" for r in duplicate["rejections"]))
            self.assertEqual(duplicate["new_entries"], [])
            self.assertEqual(duplicate["alerts"], [])
            self.assertEqual(sender.calls, 1)
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertEqual(state["entries"][0]["bars_held"], 0)

    def test_alert_retry_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(Path(td))
            sender = Sender(responses=[False, True])
            model = SecondBookModel(cfg, sender=sender, dry_run=True)
            first = model.process(from_board_payload(_payload("09:35")))
            self.assertTrue(first["alert_delivery_failed"])
            self.assertEqual(first["alerts_pending"], 1)
            retry = model.process(from_board_payload(_payload("09:35")))
            self.assertTrue(any(r["why"] == "duplicate_bar" for r in retry["rejections"]))
            self.assertEqual(len(retry["alerts"]), 1)
            self.assertTrue(retry["alerts"][0]["delivered"])
            self.assertEqual(retry["alerts_pending"], 0)
            self.assertFalse(retry["alert_delivery_failed"])
            self.assertEqual(sender.calls, 2)
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertEqual(len(state["alerted"]), 1)
            self.assertEqual(state["pending_alerts"], [])
            self.assertEqual(state["entries"][0]["bars_held"], 0)

    def test_cap_cooldown_and_last_entry(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(Path(td), MAX_DAY="2", COOLDOWN_MIN="45", LAST_ENTRY="10:00")
            model = SecondBookModel(cfg, sender=Sender(), dry_run=True)
            first = model.process(from_board_payload(_payload("09:35")))
            self.assertEqual(len(first["new_entries"]), 2)
            # Cooldown + day cap both hold; no third entry on a later bar.
            second = model.process(from_board_payload(_payload("10:10")))
            self.assertEqual(second["new_entries"], [])
            # After 14:30 no entries are considered even with a fresh sector.
            third = model.process(from_board_payload(_payload("14:35")))
            self.assertEqual(third["new_entries"], [])
            self.assertTrue(any(r["why"] == "after_last_entry" for r in third["rejections"]))

    def test_stop_eod_and_alerts_are_costed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(Path(td), RISK_PCT="1.0", COST_R="0.14", TARGET_R="0")
            sender = Sender()
            model = SecondBookModel(cfg, sender=sender, dry_run=True)
            model.process(from_board_payload(_payload("09:35", mv=2.0, second_mv=1.0)))
            # FMCG leader falls 1.2pp from its rebased entry -> -1.2R stop;
            # the second leader is +2.0R and remains open.
            marked = model.process(from_board_payload(_payload("09:40", mv=0.8, second_mv=3.0)))
            self.assertEqual(len(marked["closed"]), 1)
            self.assertEqual(marked["closed"][0]["name"], "HINDUNILVR")
            self.assertEqual(marked["closed"][0]["why"], "stop")
            self.assertAlmostEqual(marked["closed"][0]["net_r"], -1.14, places=6)
            self.assertIn("STOP HINDUNILVR", sender.messages[1])
            # Final bar closes the remaining name at +2.5R before cost. EOD is
            # one distinct summary card, not a duplicate per-bar close card.
            final_payload = _payload("15:10", mv=0.8, second_mv=3.5)
            final = model.process(from_board_payload(final_payload, final=True))
            self.assertTrue(final["final"])
            self.assertEqual(len(final["closed"]), 1)
            self.assertEqual(final["closed"][0]["name"], "DABUR")
            self.assertAlmostEqual(final["closed"][0]["net_r"], 2.5 - 0.14, places=6)
            self.assertEqual(final["summary"]["stops"], 1)
            self.assertEqual(final["summary"]["open"], 0)
            self.assertEqual(sender.calls, 3)
            self.assertIn("EOD", sender.messages[2])
            self.assertIn("net +1.220R", sender.messages[2])

    def test_finalize_sends_eod_once(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = _config(tmp)
            sender = Sender()
            model = SecondBookModel(cfg, sender=sender, dry_run=True)
            model.process(from_board_payload(_payload("09:35")))
            out = model.finalize("2026-11-25")
            self.assertEqual(out["summary"]["open"], 0)
            self.assertEqual(out["summary"]["closed"], 2)
            self.assertEqual(sender.calls, 2)  # entries + EOD
            self.assertIn("EOD", sender.messages[1])
            again = model.finalize("2026-11-25")
            self.assertTrue(any(r["why"] == "already_finalized" for r in again["rejections"]))
            self.assertEqual(sender.calls, 2)

    def test_from_board_payload_directional_mv_and_gate_string(self):
        payload = _payload("09:35", qualified=False)
        payload["sectors"]["FMCG"]["gate"] = "0"
        payload["sectors"]["FMCG"]["qualified"] = "0"
        snap = from_board_payload(payload)
        self.assertFalse(snap.sectors["FMCG"].qualified)
        media = {x.name: x for x in snap.leaders if x.sector == "MEDIA"}
        # Raw -4.0pp on a short becomes +4.0pp directional.
        self.assertAlmostEqual(media["SUNTV"].mv, 4.0)
        self.assertAlmostEqual(media["SUNTV"].raw_mv, -4.0)

    def test_audit_fixture_regression_when_raw_report_is_present(self):
        audit = ROOT / "results" / "scan1m_2026-11-25" / "inputs" / "audit-2026-11-25-1m.json"
        if not audit.exists():
            self.skipTest("raw audit fixture is gitignored and not present here")
        from m1_edge.runner import _fixture_payloads
        with tempfile.TemporaryDirectory() as td:
            cfg = _config(Path(td))
            # No Telegram sender in a pure replay; cards stay queued in state.
            model = SecondBookModel(cfg)
            payloads = _fixture_payloads(str(audit), "2026-11-25")
            for i, payload in enumerate(payloads):
                model.process(from_board_payload(payload, final=i == len(payloads) - 1))
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertEqual(len(state["entries"]), 6)
            self.assertEqual([e["name"] for e in state["entries"]],
                             ["SUNTV", "SAREGAMA", "HINDUNILVR", "DABUR",
                              "TATASTEEL", "WELCORP"])
            self.assertAlmostEqual(state["stats"]["net_r_sum"], 14.085325, places=5)
            self.assertAlmostEqual(state["stats"]["mfe_r_sum"], 22.721453, places=5)
            self.assertEqual(state["stats"]["stops"], 2)
            self.assertGreater(state["stats"]["alerts_pending"], 0)
            self.assertAlmostEqual(cfg.risk_pct, AUDIT_MEDIAN_RISK_PCT)
            self.assertAlmostEqual(cfg.cost_r, AUDIT_MEDIAN_COST_R)

    def test_environment_alias_m1_edge_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"M1_EDGE_ENABLED": "1", "M1_EDGE_STATE_PATH": str(Path(td) / "s.json")}
            cfg = Config.from_env(env)
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.mode, "paper")


if __name__ == "__main__":
    unittest.main(verbosity=2)
