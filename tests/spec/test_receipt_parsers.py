from pathlib import Path
import unittest

from src.parsers.receipts import (
    ReceiptParseError,
    parse_instacart_refund_email,
    parse_receipt_email,
)



FIXTURES = Path(__file__).parent.parent / "fixtures"


class ReceiptParserContractTests(unittest.TestCase):
    def test_parses_instacart_walmart_final_items_and_total(self) -> None:
        receipt = parse_receipt_email(
            "walmart_online", (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        )

        self.assertEqual(receipt["receipt_id"], "W-100")
        self.assertEqual(receipt["purchase_date"], "2026-09-17")
        self.assertEqual(receipt["payment_method"], "visa")
        self.assertEqual(receipt["payment_last_four"], "4821")
        self.assertEqual(receipt["final_total_cents"], 579)
        self.assertEqual(receipt["items"], [
            {"description": "Organic Bananas", "quantity": 1, "amount_cents": 164},
            {"description": "Whole Milk", "quantity": 1, "amount_cents": 332},
        ])

    def test_parses_costco_final_items_and_total(self) -> None:
        receipt = parse_receipt_email(
            "costco_same_day", (FIXTURES / "costco_receipt.html").read_text(encoding="utf-8")
        )

        self.assertEqual(receipt["receipt_id"], "C-200")
        self.assertEqual(receipt["purchase_date"], "2026-09-13")
        self.assertEqual(receipt["payment_method"], "visa")
        self.assertEqual(receipt["payment_last_four"], "7316")
        self.assertEqual(receipt["final_total_cents"], 3681)
        self.assertEqual(receipt["items"][1], {"description": "Whole Milk Yogurt", "quantity": 3, "amount_cents": 2472})

    def test_marks_paypal_without_inventing_a_card_mapping_key(self) -> None:
        html = (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Your Visa 4821 was charged", "Your PayPal Card was charged"),
        )

        self.assertEqual(receipt["payment_method"], "paypal")
        self.assertIsNone(receipt["payment_last_four"])

    def test_parses_discover_and_mastercard_evidence(self) -> None:
        html = (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")

        discover_receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Your Visa 4821 was charged", "Your Discover 1234 was charged"),
        )
        self.assertEqual(discover_receipt["payment_method"], "discover")
        self.assertEqual(discover_receipt["payment_last_four"], "1234")

        mastercard_receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Your Visa 4821 was charged", "Your Mastercard ending in 5678 was charged"),
        )
        self.assertEqual(mastercard_receipt["payment_method"], "mastercard")
        self.assertEqual(mastercard_receipt["payment_last_four"], "5678")

        master_card_receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Your Visa 4821 was charged", "Your Master Card 4321 was charged"),
        )
        self.assertEqual(master_card_receipt["payment_method"], "mastercard")
        self.assertEqual(master_card_receipt["payment_last_four"], "4321")

        amex_receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Your Visa 4821 was charged", "Your American Express 9012 was charged"),
        )
        self.assertEqual(amex_receipt["payment_method"], "amex")
        self.assertEqual(amex_receipt["payment_last_four"], "9012")

    def test_uses_gmail_message_id_when_current_instacart_template_has_no_order_id(self) -> None:
        html = (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Order ID: # W-100", "Receipt details"),
            fallback_receipt_id="gmail-message-123",
        )

        self.assertEqual(receipt["receipt_id"], "gmail-message-123")

    def test_fails_when_a_receipt_has_no_final_total(self) -> None:
        with self.assertRaises(ReceiptParseError):
            parse_receipt_email("walmart_online", "<html><body>Order ID: # W-1</body></html>")

    def test_parses_instacart_refund_details(self) -> None:
        refund_html = """
        <html>
        <body>
            <p>Your order was placed on September 18th, 2026.</p>
            <p>Order ID: # W-REF-999</p>
            <div class="item-refunded item-row">
                <span class="item-name">Organic Bananas</span>
                <span class="item-price">$1.64</span>
            </div>
            <div>Total refunded: $12.34</div>
        </body>
        </html>
        """
        refund = parse_instacart_refund_email(refund_html, fallback_refund_id="msg-999")
        self.assertEqual(refund["refund_id"], "W-REF-999")
        self.assertEqual(refund["retailer"], "walmart_online")
        self.assertEqual(refund["refund_date"], "2026-09-18")
        self.assertEqual(refund["refund_amount_cents"], 1234)

    def test_refund_parser_fails_when_amount_or_refund_evidence_missing(self) -> None:
        no_refund_evidence_html = """
        <html>
        <body>
            <p>Your order was placed on September 18th, 2026.</p>
            <p>Order ID: # W-REF-999</p>
            <p>Total charged: $12.34</p>
        </body>
        </html>
        """
        with self.assertRaises(ReceiptParseError):
            parse_instacart_refund_email(no_refund_evidence_html)

    def test_parses_walmart_receipt_with_out_of_stock_and_refunded_items(self) -> None:
        html = """
        <html>
        <body>
            <div>Your order from Walmart was placed on September 17th, 2026</div>
            <div>Order ID: # W-101</div>
            <div>Your Visa 4821 was charged</div>
            <div class="item-row item-delivered">
                <div class="item-name">Organic Bananas<br><small>2 lb x $0.74</small></div>
                <div class="item-price"><div class="total">$1.64</div></div>
            </div>
            <div class="item-actually-delivered item-delivered item-row">
                <div class="item-name">Whole Milk<br><small>1 x $3.32</small></div>
                <div class="item-price"><div class="total">$3.32</div></div>
            </div>
            <div class="item-refunded item-row">
                <div class="item-name">Out of Stock Eggs</div>
            </div>
            <div class="item-row item-wanted">
                <div class="item-name">Desired Bread</div>
            </div>
            <div>Total charged</div><div class="amount">$4.96</div>
        </body>
        </html>
        """
        receipt = parse_receipt_email("walmart_online", html)
        self.assertEqual(len(receipt["items"]), 2)
        self.assertEqual(receipt["items"], [
            {"description": "Organic Bananas", "quantity": 1, "amount_cents": 164},
            {"description": "Whole Milk", "quantity": 1, "amount_cents": 332},
        ])
        self.assertEqual(receipt["final_total_cents"], 496)

    def test_parses_walmart_receipt_with_split_tender_and_credits(self) -> None:
        html = """
        <html>
        <body>
            <div>Your order from Walmart was placed on September 20th, 2026</div>
            <div>Order ID: # W-102</div>
            <div class="item-row item-delivered">
                <div class="item-name">Apples<br><small>1 x $6.24</small></div>
                <div class="item-price"><div class="total">$6.24</div></div>
            </div>
            <div>Order Totals</div>
            <div>Total $6.24</div>
            <div>Visa ending in 4592</div>
            <div>Total charged $1.44</div>
            <div>Instacart credits</div>
            <div>Total charged $4.80</div>
        </body>
        </html>
        """
        receipt = parse_receipt_email("walmart_online", html)
        self.assertEqual(receipt["final_total_cents"], 624)
        self.assertEqual(receipt["payment_method"], "visa")
        self.assertEqual(receipt["payment_last_four"], "4592")


