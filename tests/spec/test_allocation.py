import unittest

from src.domain.allocate import AllocationError, allocate_receipt, _split_cents


PARTICIPANTS = ["himanshu", "krishna", "nitish"]


class AllocationContractTests(unittest.TestCase):
    def test_allocates_a_raw_item_equally_after_inclusive_absence_filtering(self) -> None:
        result = allocate_receipt(
            receipt={
                "purchase_date": "2026-09-17",
                "final_total_cents": 900,
                "items": [{"description": "Rice", "amount_cents": 900}],
            },
            config={
                "participant_ids": PARTICIPANTS,
                "absences": [
                    {
                        "participant_id": "himanshu",
                        "starts_on": "2026-09-17",
                        "ends_on": "2026-09-17",
                    }
                ],
                "rules": [],
            },
        )

        self.assertEqual(result["items"][0]["shares_cents"], {"krishna": 450, "nitish": 450})
        self.assertEqual(result["total_allocated_cents"], 900)

    def test_include_only_replaces_the_base_set_and_excludes_then_refine_it(self) -> None:
        result = allocate_receipt(
            receipt={
                "purchase_date": "2026-09-17",
                "final_total_cents": 1001,
                "items": [{"description": "Almonds", "amount_cents": 1001}],
            },
            config={
                "participant_ids": PARTICIPANTS,
                "absences": [],
                "rules": [
                    {"match_description": "almonds", "include_only": ["krishna", "nitish"]},
                    {"match_description": "almonds", "exclude": ["nitish"]},
                ],
            },
        )

        self.assertEqual(result["items"][0]["shares_cents"], {"krishna": 1001})

    def test_allocates_residual_cents_among_present_people_in_configured_order(self) -> None:
        result = allocate_receipt(
            receipt={
                "purchase_date": "2026-09-17",
                "final_total_cents": 1001,
                "items": [{"description": "Rice", "amount_cents": 1000}],
            },
            config={"participant_ids": PARTICIPANTS, "absences": [], "rules": []},
        )

        self.assertEqual(
            result["items"][0]["shares_cents"],
            {"himanshu": 334, "krishna": 333, "nitish": 333},
        )
        self.assertEqual(
            result["residual_shares_cents"], {"himanshu": 1, "krishna": 0, "nitish": 0})
        self.assertEqual(result["total_allocated_cents"], 1001)

    def test_fails_closed_when_no_rule_eligible_participant_remains(self) -> None:
        with self.assertRaises(AllocationError):
            allocate_receipt(
                receipt={
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 500,
                    "items": [{"description": "Milk", "amount_cents": 500}],
                },
                config={
                    "participant_ids": PARTICIPANTS,
                    "absences": [],
                    "rules": [{"match_description": "milk", "exclude": PARTICIPANTS}],
                },
            )

    def test_split_cents_rotates_remainder_by_offset(self) -> None:
        self.assertEqual(
            _split_cents(1000, ["a", "b", "c"], offset=0),
            {"a": 334, "b": 333, "c": 333},
        )
        self.assertEqual(
            _split_cents(1000, ["a", "b", "c"], offset=1),
            {"a": 333, "b": 334, "c": 333},
        )
        self.assertEqual(
            _split_cents(1000, ["a", "b", "c"], offset=2),
            {"a": 333, "b": 333, "c": 334},
        )

    def test_allocates_receipt_with_receipt_id_rotation(self) -> None:
        # 'test-receipt-2' sum(ord(c)) is 1336, 1336 % 3 == 1 (krishna)
        result = allocate_receipt(
            receipt={
                "receipt_id": "test-receipt-2",
                "purchase_date": "2026-09-17",
                "final_total_cents": 1001,
                "items": [{"description": "Rice", "amount_cents": 1000}],
            },
            config={"participant_ids": PARTICIPANTS, "absences": [], "rules": []},
        )
        self.assertEqual(
            result["items"][0]["shares_cents"],
            {"himanshu": 333, "krishna": 334, "nitish": 333},
        )
        self.assertEqual(
            result["residual_shares_cents"],
            {"himanshu": 0, "krishna": 1, "nitish": 0},
        )
        self.assertEqual(result["total_allocated_cents"], 1001)

    def test_allocates_multi_item_receipt_with_round_robin_item_offsets(self) -> None:
        result = allocate_receipt(
            receipt={
                "purchase_date": "2026-09-17",
                "final_total_cents": 3000,
                "items": [
                    {"description": "Item 1", "amount_cents": 1000},
                    {"description": "Item 2", "amount_cents": 1000},
                    {"description": "Item 3", "amount_cents": 1000},
                ],
            },
            config={"participant_ids": PARTICIPANTS, "absences": [], "rules": []},
        )
        self.assertEqual(
            result["items"][0]["shares_cents"],
            {"himanshu": 334, "krishna": 333, "nitish": 333},
        )
        self.assertEqual(
            result["items"][1]["shares_cents"],
            {"himanshu": 333, "krishna": 334, "nitish": 333},
        )
        self.assertEqual(
            result["items"][2]["shares_cents"],
            {"himanshu": 333, "krishna": 333, "nitish": 334},
        )
        participant_totals = {p: 0 for p in PARTICIPANTS}
        for item in result["items"]:
            for p, cents in item["shares_cents"].items():
                participant_totals[p] += cents
        self.assertEqual(
            participant_totals,
            {"himanshu": 1000, "krishna": 1000, "nitish": 1000},
        )
        self.assertEqual(result["total_allocated_cents"], 3000)
