from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.diagnostics.discover_instacart_schemas import (
    build_schema_summary,
    schema_observation,
    write_schema_outputs,
)


class InstacartSchemaDiscoveryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.output_directory = Path(self.temporary_directory.name) / "schema-discovery"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_groups_receipts_with_different_text_under_the_same_structure(self) -> None:
        first = schema_observation(
            "message-1",
            '<div class="item-row"><span class="item-name">Bananas</span><span class="item-price">$2.00</span></div>',
        )
        second = schema_observation(
            "message-2",
            '<div class="item-row"><span class="item-name">Milk</span><span class="item-price">$4.00</span></div>',
        )

        self.assertEqual(first["schema_fingerprint"], second["schema_fingerprint"])
        self.assertNotIn("Bananas", first["schema_shape"])
        self.assertNotIn("$2.00", first["schema_shape"])

    def test_groups_repeated_item_rows_under_one_template_fingerprint(self) -> None:
        one_item = schema_observation(
            "message-1",
            '<table><tr class="item-row"><td class="item-name"></td></tr></table>',
        )
        two_items = schema_observation(
            "message-2",
            '<table><tr class="item-row"><td class="item-name"></td></tr><tr class="item-row"><td class="item-name"></td></tr></table>',
        )

        self.assertEqual(one_item["schema_fingerprint"], two_items["schema_fingerprint"])

    def test_summary_counts_parser_outcomes_by_schema(self) -> None:
        parsed = schema_observation("message-1", '<div class="item-row"></div>')
        rejected = schema_observation("message-2", "<table><tr><td>Refund</td></tr></table>")
        parsed["parser_outcome"] = "parsed"
        rejected["parser_outcome"] = "rejected"
        rejected["failure_code"] = "missing_finalized_items"

        summary = build_schema_summary([parsed, rejected])

        self.assertEqual(summary["message_count"], 2)
        self.assertEqual(summary["schema_variant_count"], 2)
        self.assertEqual(summary["parser_outcomes"]["parsed"], 1)
        self.assertEqual(summary["parser_outcomes"]["rejected"], 1)

    def test_writes_schema_only_artifacts_without_receipt_content(self) -> None:
        observation = schema_observation(
            "message-1",
            '<div class="item-row">Private product name</div>',
        )
        observation["parser_outcome"] = "rejected"
        observation["failure_code"] = "missing_finalized_items"

        write_schema_outputs(self.output_directory, [observation])

        saved_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.output_directory / "processed").iterdir()
        )
        self.assertNotIn("Private product name", saved_text)
        self.assertNotIn("<div", saved_text)
        self.assertNotIn("message-1", saved_text)
