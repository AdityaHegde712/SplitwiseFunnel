import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.app.config import (
    ConfigError,
    append_participant,
    append_participant_and_payment_mapping,
    append_payment_mapping,
    load_config,
    resolve_payer,
)
from src.app.refund_links import load_refund_links, save_refund_link
from src.app.results import (
    aggregate_run_results,
    build_result,
    render_markdown_run_summary,
    render_markdown_summary,
    write_result_files,
)
from src.domain.allocate import allocate_receipt, get_present_participants, _split_cents



VALID_CONFIG = {
    "participant_ids": ["himanshu", "krishna", "nitish"],
    "payment_mappings": [
        {
            "last_four": "4821",
            "payer_id": "krishna",
        }
    ],
    "absences": [],
    "rules": [],
}


class ConfigAndResultContractTests(unittest.TestCase):
    def test_load_config_rejects_a_payment_mapping_to_an_unknown_participant(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            invalid_config = {
                **VALID_CONFIG,
                "payment_mappings": [
                    {
                        "last_four": "4821",
                        "payer_id": "unknown",
                    }
                ],
            }
            config_path.write_text(json.dumps(invalid_config), encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_resolve_payer_uses_household_wide_last_four_or_explicit_manual_payer(self) -> None:
        self.assertEqual(
            resolve_payer(
                payment_last_four="4821",
                explicit_payer_id=None,
                config=VALID_CONFIG,
            ),
            ("krishna", "card_last_four"),
        )
        self.assertEqual(
            resolve_payer(
                payment_last_four=None,
                explicit_payer_id="nitish",
                config=VALID_CONFIG,
            ),
            ("nitish", "manual"),
        )

    def test_appends_an_unknown_visa_mapping_to_the_private_config(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "household.json"
            config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

            updated_config = append_payment_mapping(
                config_path,
                last_four="7192",
                payer_id="himanshu",
            )

            self.assertIn(
                {
                    "last_four": "7192",
                    "payer_id": "himanshu",
                },
                updated_config["payment_mappings"],
            )
            self.assertEqual(load_config(config_path), updated_config)

    def test_creates_a_participant_and_its_unknown_visa_mapping(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "household.json"
            config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

            updated_config = append_participant_and_payment_mapping(
                config_path,
                last_four="9001",
                participant_id="aditya_hegde",
            )

            self.assertIn("aditya_hegde", updated_config["participant_ids"])
            self.assertEqual(
                resolve_payer("9001", None, updated_config),
                ("aditya_hegde", "card_last_four"),
            )

    def test_appends_a_participant_without_creating_a_payment_mapping(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "household.json"
            config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

            updated_config = append_participant(config_path, "kushagra_bainsla")

            self.assertIn("kushagra_bainsla", updated_config["participant_ids"])
            self.assertEqual(updated_config["payment_mappings"], VALID_CONFIG["payment_mappings"])

    def test_result_files_are_auditable_and_summary_totals_reconcile(self) -> None:
        receipt = {
            "receipt_id": "walmart-123",
            "retailer": "walmart_online",
            "purchase_date": "2026-09-17",
            "payment_last_four": "4821",
            "final_total_cents": 1001,
            "items": [{"description": "Rice", "amount_cents": 1000}],
        }
        allocation = allocate_receipt(receipt, VALID_CONFIG)
        result = build_result(
            receipt=receipt,
            payer_id="krishna",
            payer_resolution="card_last_four",
            allocation=allocation,
        )

        self.assertEqual(result["participant_totals_cents"], {"himanshu": 335, "krishna": 333, "nitish": 333})
        self.assertEqual(result["allocation"]["total_allocated_cents"], receipt["final_total_cents"])
        self.assertIn("$10.01", render_markdown_summary(result))
        self.assertIn("Krishna", render_markdown_summary(result))
        self.assertIn("## Item allocation", render_markdown_summary(result))
        self.assertIn("Rice — Himanshu $3.34, Krishna $3.33, Nitish $3.33", render_markdown_summary(result))
        self.assertIn("## Receipt-level residual", render_markdown_summary(result))

        with TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            json_path, markdown_path = write_result_files(result, output_directory, "20260917_120000")

            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), result)
            self.assertIn("Per-person totals", markdown_path.read_text(encoding="utf-8"))

    def test_aggregates_all_receipts_into_one_copy_ready_run_summary(self) -> None:
        results = [
            {
                "receipt": {"receipt_id": "one", "final_total_cents": 1001},
                "participant_totals_cents": {"himanshu": 335, "krishna": 333, "nitish": 333},
            },
            {
                "receipt": {"receipt_id": "two", "final_total_cents": 500},
                "participant_totals_cents": {"himanshu": 100, "krishna": 200, "nitish": 200},
            },
        ]

        aggregate = aggregate_run_results(results)

        self.assertEqual(aggregate["receipt_count"], 2)
        self.assertEqual(aggregate["total_cents"], 1501)
        self.assertEqual(
            aggregate["participant_totals_cents"],
            {"himanshu": 435, "krishna": 533, "nitish": 533},
        )
        summary = render_markdown_run_summary(aggregate)
        self.assertIn("# Run total", summary)
        self.assertIn("$15.01", summary)
        self.assertIn("Himanshu: $4.35", summary)

    def test_get_present_participants_respects_purchase_date_absences(self) -> None:
        participant_ids = ["himanshu", "krishna", "nitish"]
        absences = [
            {"participant_id": "nitish", "starts_on": "2026-09-10", "ends_on": "2026-09-20"}
        ]

        present_during_absence = get_present_participants(
            participant_ids=participant_ids,
            purchase_date="2026-09-15",
            absences=absences,
        )
        self.assertEqual(present_during_absence, ["himanshu", "krishna"])

        present_after_absence = get_present_participants(
            participant_ids=participant_ids,
            purchase_date="2026-09-25",
            absences=absences,
        )
        self.assertEqual(present_after_absence, ["himanshu", "krishna", "nitish"])

    def test_negative_cent_split_is_deterministic_and_exact(self) -> None:
        split = _split_cents(-1001, ["a", "b", "c", "d", "e"])
        self.assertEqual(
            split,
            {"a": -201, "b": -200, "c": -200, "d": -200, "e": -200},
        )
        self.assertEqual(sum(split.values()), -1001)

    def test_build_result_with_refund_adjustment_preserves_original_total_and_reconciles_net_total(
        self,
    ) -> None:
        receipt = {
            "receipt_id": "walmart-123",
            "retailer": "walmart_online",
            "purchase_date": "2026-09-17",
            "payment_last_four": "4821",
            "final_total_cents": 4500,
            "items": [{"description": "Groceries", "amount_cents": 4500}],
        }
        allocation = allocate_receipt(receipt, VALID_CONFIG)
        refund_details = {
            "refund_id": "ref-1",
            "refund_date": "2026-09-18",
            "refund_amount_cents": 1000,
            "shares_cents": {"himanshu": -334, "krishna": -333, "nitish": -333},
        }

        result = build_result(
            receipt=receipt,
            payer_id="krishna",
            payer_resolution="card_last_four",
            allocation=allocation,
            refund_adjustment_cents=-1000,
            refund_details=refund_details,
        )

        self.assertEqual(result["original_total_cents"], 4500)
        self.assertEqual(result["refund_adjustment_cents"], -1000)
        self.assertEqual(result["net_total_cents"], 3500)
        self.assertEqual(
            result["participant_totals_cents"],
            {"himanshu": 1166, "krishna": 1167, "nitish": 1167},
        )
        self.assertEqual(sum(result["participant_totals_cents"].values()), 3500)

        markdown = render_markdown_summary(result)
        self.assertIn("Original total: $45.00", markdown)
        self.assertIn("Refund adjustment: -$10.00", markdown)
        self.assertIn("Net total: $35.00", markdown)

    def test_aggregate_run_results_reconciles_net_totals(self) -> None:
        results = [
            {
                "receipt": {"receipt_id": "one", "final_total_cents": 4500},
                "original_total_cents": 4500,
                "refund_adjustment_cents": -1000,
                "net_total_cents": 3500,
                "participant_totals_cents": {"himanshu": 1166, "krishna": 1167, "nitish": 1167},
            },
            {
                "receipt": {"receipt_id": "two", "final_total_cents": 500},
                "original_total_cents": 500,
                "refund_adjustment_cents": 0,
                "net_total_cents": 500,
                "participant_totals_cents": {"himanshu": 100, "krishna": 200, "nitish": 200},
            },
        ]

        aggregate = aggregate_run_results(results)
        self.assertEqual(aggregate["receipt_count"], 2)
        self.assertEqual(aggregate["total_cents"], 4000)
        self.assertEqual(
            aggregate["participant_totals_cents"],
            {"himanshu": 1266, "krishna": 1367, "nitish": 1367},
        )
        self.assertEqual(
            sum(aggregate["participant_totals_cents"].values()), aggregate["total_cents"]
        )

    def test_refund_link_store_persists_and_loads_mappings_idempotently(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store_path = Path(temporary_directory) / "refund_links.json"
            self.assertEqual(load_refund_links(store_path), {})

            save_refund_link(
                store_path,
                refund_id="msg-ref-1",
                original_receipt_id="walmart-123",
                refund_amount_cents=1000,
                refund_date="2026-09-18",
            )
            loaded = load_refund_links(store_path)
            self.assertIn("msg-ref-1", loaded)
            self.assertEqual(loaded["msg-ref-1"]["original_receipt_id"], "walmart-123")
            self.assertEqual(loaded["msg-ref-1"]["refund_amount_cents"], 1000)

            # Idempotent re-save
            save_refund_link(
                store_path,
                refund_id="msg-ref-1",
                original_receipt_id="walmart-123",
                refund_amount_cents=1000,
                refund_date="2026-09-18",
            )
            self.assertEqual(load_refund_links(store_path), loaded)

    def test_aggregate_run_results_and_summary_with_manual_receipts(self) -> None:
        results = [
            {
                "receipt": {"final_total_cents": 1000},
                "participant_totals_cents": {"himanshu": 500, "krishna": 500},
            }
        ]
        manual_receipts = [
            {
                "message_id": "manual-msg-1",
                "receipt_id": "W-999",
                "retailer": "walmart_online",
                "purchase_date": "2026-09-17",
                "reason": "unsupported_receipt_format",
            }
        ]

        aggregate = aggregate_run_results(results, manual_receipts=manual_receipts)
        self.assertEqual(aggregate["automated_count"], 1)
        self.assertEqual(aggregate["manual_count"], 1)
        self.assertEqual(aggregate["total_count"], 2)
        self.assertEqual(aggregate["receipt_count"], 1)
        self.assertEqual(len(aggregate["manual_receipts"]), 1)

        summary = render_markdown_run_summary(aggregate)
        self.assertIn("# Run total", summary)
        self.assertIn("Manual Processing Required", summary)
        self.assertIn("W-999", summary)
        self.assertIn("2026-09-17", summary)
        self.assertIn("Walmart Online", summary)
        self.assertIn("unsupported_receipt_format", summary)

    def test_aggregate_run_results_produces_payer_aggregates_and_summary_blocks(self) -> None:
        results = [
            {
                "receipt": {"receipt_id": "W-100", "final_total_cents": 1000},
                "payer": {"participant_id": "krishna", "resolution": "card_last_four"},
                "net_total_cents": 1000,
                "participant_totals_cents": {"himanshu": 500, "krishna": 500},
            },
            {
                "receipt": {"receipt_id": "W-101", "final_total_cents": 2000},
                "payer": {"participant_id": "himanshu", "resolution": "card_last_four"},
                "net_total_cents": 2000,
                "participant_totals_cents": {"himanshu": 1000, "krishna": 1000},
            },
            {
                "receipt": {"receipt_id": "C-200", "final_total_cents": 1500},
                "payer": {"participant_id": "krishna", "resolution": "card_last_four"},
                "net_total_cents": 1500,
                "participant_totals_cents": {"himanshu": 750, "krishna": 750},
            },
        ]
        manual_receipts = [
            {
                "message_id": "manual-1",
                "receipt_id": "W-999",
                "retailer": "walmart_online",
                "purchase_date": "2026-09-17",
                "reason": "unsupported_receipt_format",
            }
        ]

        aggregate = aggregate_run_results(results, manual_receipts=manual_receipts)

        self.assertIn("payer_aggregates", aggregate)
        payer_aggregates = aggregate["payer_aggregates"]
        self.assertIn("krishna", payer_aggregates)
        self.assertIn("himanshu", payer_aggregates)

        krishna_agg = payer_aggregates["krishna"]
        self.assertEqual(krishna_agg["total_cents"], 2500)
        self.assertEqual(krishna_agg["receipt_count"], 2)
        self.assertEqual(krishna_agg["receipt_ids"], ["W-100", "C-200"])
        self.assertEqual(krishna_agg["participant_shares_cents"], {"himanshu": 1250, "krishna": 1250})
        self.assertIn("## Paid by Krishna", krishna_agg["markdown_summary"])
        self.assertIn("$25.00", krishna_agg["markdown_summary"])

        himanshu_agg = payer_aggregates["himanshu"]
        self.assertEqual(himanshu_agg["total_cents"], 2000)
        self.assertEqual(himanshu_agg["receipt_count"], 1)
        self.assertEqual(himanshu_agg["receipt_ids"], ["W-101"])
        self.assertEqual(himanshu_agg["participant_shares_cents"], {"himanshu": 1000, "krishna": 1000})
        self.assertIn("## Paid by Himanshu", himanshu_agg["markdown_summary"])
        self.assertIn("$20.00", himanshu_agg["markdown_summary"])

        summary = render_markdown_run_summary(aggregate)
        self.assertIn("# Run total", summary)
        self.assertIn("## Paid by Krishna", summary)
        self.assertIn("## Paid by Himanshu", summary)
        self.assertIn("## Manual Processing Required", summary)


