"""Fail-closed parsers for the supported finalized receipt email formats."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import logging
import re
from typing import Any, Final

from src.observability.logging import log_event



LOGGER = logging.getLogger("splitwise_funnel.parsers.receipts")


class ReceiptParseError(ValueError):
    """Raised when a supported receipt cannot be parsed without ambiguity."""


_SUPPORTED_VENDORS: Final[frozenset[str]] = frozenset(
    {"walmart_online", "costco_same_day"}
)
_CENTS_PER_DOLLAR: Final[Decimal] = Decimal("100")
_CURRENCY_QUANTUM: Final[Decimal] = Decimal("0.01")


@dataclass
class _Node:
    tag: str
    attributes: dict[str, str]
    children: list[_Node] = field(default_factory=list)
    content: list[str | _Node] = field(default_factory=list)

    def text(self) -> str:
        """Return whitespace-normalized text without retaining markup."""
        parts: list[str] = [
            entry if isinstance(entry, str) else entry.text() for entry in self.content
        ]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()


class _ReceiptHtmlParser(HTMLParser):
    """Create a minimal text tree so nested receipt markup stays parseable."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: _Node = _Node(tag="root", attributes={})
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: _Node = _Node(
            tag=tag.casefold(),
            attributes={key.casefold(): value or "" for key, value in attrs},
        )
        self._stack[-1].children.append(node)
        self._stack[-1].content.append(node)
        if tag.casefold() not in {"br", "hr", "img", "meta", "link", "input"}:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag: str = tag.casefold()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].content.append(data)


def parse_receipt_email(
    vendor_id: str,
    html: str,
    fallback_receipt_id: str | None = None,
) -> dict[str, object]:
    """Parse one supported finalized receipt email into exact-cent receipt data."""
    if vendor_id not in _SUPPORTED_VENDORS:
        log_event(LOGGER, "receipt_parse_failed", vendor_id=vendor_id, status="unsupported_vendor")
        raise ReceiptParseError(f"Unsupported receipt vendor: {vendor_id!r}.")
    if not isinstance(html, str) or not html.strip():
        log_event(LOGGER, "receipt_parse_failed", vendor_id=vendor_id, status="missing_body")
        raise ReceiptParseError("Receipt email must contain a supported HTML body.")

    log_event(LOGGER, "receipt_parse_started", vendor_id=vendor_id)
    parser: _ReceiptHtmlParser = _ReceiptHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except ValueError as error:
        log_event(LOGGER, "receipt_parse_failed", vendor_id=vendor_id, status="invalid_html")
        raise ReceiptParseError("Receipt email HTML could not be parsed.") from error

    if vendor_id == "walmart_online":
        receipt: dict[str, object] = _parse_walmart_receipt(parser.root, fallback_receipt_id)
    else:
        receipt = _parse_costco_receipt(parser.root)
    items: object = receipt.get("items", [])
    log_event(
        LOGGER,
        "receipt_parse_completed",
        vendor_id=vendor_id,
        item_count=len(items) if isinstance(items, list) else 0,
        total_cents=receipt.get("final_total_cents"),
    )
    return receipt


def parse_instacart_refund_email(
    html: str,
    fallback_refund_id: str | None = None,
) -> dict[str, Any]:
    """Parse one Instacart Walmart refund email into safe, value-checked refund data."""
    if not isinstance(html, str) or not html.strip():
        log_event(LOGGER, "refund_parse_failed", status="missing_body")
        raise ReceiptParseError("Refund email must contain a supported HTML body.")

    log_event(LOGGER, "refund_parse_started", vendor_id="walmart_online")
    parser: _ReceiptHtmlParser = _ReceiptHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except ValueError as error:
        log_event(LOGGER, "refund_parse_failed", status="invalid_html")
        raise ReceiptParseError("Refund email HTML could not be parsed.") from error

    full_text: str = parser.root.text()
    has_refund_text = bool(re.search(r"\brefund(?:ed)?\b", full_text, flags=re.IGNORECASE))
    has_refund_class = any(
        "item-refunded" in node.attributes.get("class", "").split()
        for node in _walk_nodes(parser.root)
    )
    if not (has_refund_text or has_refund_class):
        log_event(LOGGER, "refund_parse_failed", status="missing_refund_evidence")
        raise ReceiptParseError("Receipt email does not contain refund evidence.")

    refund_id: str = _extract_order_id(full_text, fallback_refund_id)
    refund_date: str
    try:
        refund_date = _extract_date(
            full_text,
            r"was placed on ([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4})",
            "%B %d, %Y",
        )
    except ReceiptParseError:
        try:
            refund_date = _extract_date(
                full_text,
                r"delivered your order on ([A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
                "%b %d, %Y",
            )
        except ReceiptParseError:
            iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", full_text)
            if iso_match:
                refund_date = iso_match.group(1)
            else:
                log_event(LOGGER, "refund_parse_failed", status="missing_date")
                raise ReceiptParseError("Refund email is missing a valid date.")

    amount_cents: int | None = None
    match = re.search(
        r"(?:Total\s+refunded|Refund\s+total|Refund\s+amount|Refunded|Refund)\s*:?\s*-?(\$[\d,]+(?:\.\d{2})?)",
        full_text,
        flags=re.IGNORECASE,
    )
    if match is not None:
        amount_cents = _money_to_cents(match.group(1))
    else:
        refunded_cents = 0
        for node in _walk_nodes(parser.root):
            classes = set(node.attributes.get("class", "").split())
            if "item-refunded" in classes:
                price_text = _find_descendant_text(node, "item-price")
                if price_text:
                    refunded_cents += _extract_money(price_text)
        if refunded_cents > 0:
            amount_cents = refunded_cents

    if amount_cents is None or amount_cents <= 0:
        log_event(LOGGER, "refund_parse_failed", status="missing_amount")
        raise ReceiptParseError("Refund email is missing an explicit positive refund amount.")

    result: dict[str, Any] = {
        "refund_id": refund_id,
        "retailer": "walmart_online",
        "refund_date": refund_date,
        "refund_amount_cents": amount_cents,
    }
    log_event(
        LOGGER,
        "refund_parse_completed",
        refund_id=refund_id,
        refund_amount_cents=amount_cents,
        refund_date=refund_date,
    )
    return result



def _parse_walmart_receipt(root: _Node, fallback_receipt_id: str | None) -> dict[str, object]:
    full_text: str = root.text()
    receipt_id: str = _extract_order_id(full_text, fallback_receipt_id)
    purchase_date: str = _extract_date(
        full_text,
        r"was placed on ([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4})",
        "%B %d, %Y",
    )
    items: list[dict[str, object]] = []
    for node in _walk_nodes(root):
        css_classes: set[str] = set(node.attributes.get("class", "").split())
        if "item-row" not in css_classes:
            continue
        if "item-delivered" not in css_classes or "item-refunded" in css_classes:
            continue
        item_name: str | None = _find_descendant_text(node, "item-name")
        item_amount: str | None = _find_descendant_text(node, "item-price")
        if item_name is None or item_amount is None:
            raise ReceiptParseError("Walmart receipt contains an incomplete item row.")
        description: str = _first_line(item_name)
        amount_cents: int = _extract_money(item_amount)
        quantity: int = _extract_quantity(item_name)
        items.append(
            {
                "description": description,
                "quantity": quantity,
                "amount_cents": amount_cents,
            }
        )

    total_cents: int = _extract_walmart_total(full_text)
    return _build_receipt(
        "walmart_online", receipt_id, purchase_date, total_cents, items, full_text
    )



def _parse_costco_receipt(root: _Node) -> dict[str, object]:
    full_text: str = root.text()
    receipt_id: str = _extract_order_id(full_text)
    purchase_date: str = _extract_date(
        full_text,
        r"delivered your order on ([A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
        "%b %d, %Y",
    )
    items: list[dict[str, object]] = []
    for node in _walk_nodes(root):
        if node.tag != "table":
            continue
        row_text: str = node.text()
        match: re.Match[str] | None = re.fullmatch(
            r"\s*(\d+)\s*x\s*(.+?)\s*(\$[\d,]+(?:\.\d{2})?)\s*", row_text
        )
        if match is None:
            continue
        quantity: int = int(match.group(1))
        description: str = _clean_description(match.group(2))
        items.append(
            {
                "description": description,
                "quantity": quantity,
                "amount_cents": _money_to_cents(match.group(3)),
            }
        )

    total_cents: int = _extract_money_after_label(full_text, "Total:")
    return _build_receipt(
        "costco_same_day", receipt_id, purchase_date, total_cents, items, full_text
    )


def _build_receipt(
    retailer: str,
    receipt_id: str,
    purchase_date: str,
    final_total_cents: int,
    items: list[dict[str, object]],
    full_text: str,
) -> dict[str, object]:
    if not items:
        raise ReceiptParseError("Receipt does not contain any finalized item rows.")
    payment_method, payment_last_four = _extract_payment_evidence(full_text)
    return {
        "receipt_id": receipt_id,
        "retailer": retailer,
        "purchase_date": purchase_date,
        "final_total_cents": final_total_cents,
        "items": items,
        "payment_method": payment_method,
        "payment_last_four": payment_last_four,
    }


def _walk_nodes(node: _Node) -> list[_Node]:
    nodes: list[_Node] = [node]
    for child in node.children:
        nodes.extend(_walk_nodes(child))
    return nodes


def _find_descendant_text(node: _Node, css_class: str) -> str | None:
    for descendant in _walk_nodes(node):
        classes: set[str] = set(descendant.attributes.get("class", "").split())
        if css_class in classes:
            return descendant.text()
    return None


def _extract_order_id(text: str, fallback_receipt_id: str | None = None) -> str:
    match: re.Match[str] | None = re.search(r"Order ID:\s*#\s*([^\s]+)", text)
    if match is not None:
        return match.group(1)
    if isinstance(fallback_receipt_id, str) and fallback_receipt_id.strip():
        return fallback_receipt_id
    raise ReceiptParseError("Receipt is missing an order ID.")


def _extract_payment_evidence(text: str) -> tuple[str | None, str | None]:
    card_patterns: list[tuple[str, str]] = [
        ("visa", r"\bvisa\s+(?:ending\s+in\s+|••••\s*|\*{4}\s*)?(\d{4})\b"),
        ("discover", r"\bdiscover\s+(?:ending\s+in\s+|••••\s*|\*{4}\s*)?(\d{4})\b"),
        ("mastercard", r"\b(?:mastercard|master\s+card)\s+(?:ending\s+in\s+|••••\s*|\*{4}\s*)?(\d{4})\b"),
        ("amex", r"\b(?:amex|american\s+express)\s+(?:ending\s+in\s+|••••\s*|\*{4}\s*)?(\d{4})\b"),
    ]
    for brand, pattern in card_patterns:
        match: re.Match[str] | None = re.search(pattern, text, flags=re.IGNORECASE)
        if match is not None:
            return brand, match.group(1)

    paypal_match: re.Match[str] | None = re.search(
        r"\bpaypal(?:\s+card)?\b", text, flags=re.IGNORECASE
    )
    if paypal_match is not None:
        return "paypal", None

    fallback_brands: list[tuple[str, str]] = [
        ("visa", r"\bvisa(?:\s+card)?\b"),
        ("discover", r"\bdiscover(?:\s+card)?\b"),
        ("mastercard", r"\b(?:mastercard|master\s+card)\b"),
        ("amex", r"\b(?:amex|american\s+express)(?:\s+card)?\b"),
    ]
    for brand, pattern in fallback_brands:
        if re.search(pattern, text, flags=re.IGNORECASE) is not None:
            return brand, None

    return None, None


def _extract_date(text: str, pattern: str, date_format: str) -> str:
    match: re.Match[str] | None = re.search(pattern, text)
    if match is None:
        raise ReceiptParseError("Receipt is missing a finalized purchase date.")
    date_text: str = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", match.group(1))
    try:
        return datetime.strptime(date_text, date_format).date().isoformat()
    except ValueError as error:
        raise ReceiptParseError("Receipt contains an invalid purchase date.") from error


def _extract_walmart_total(full_text: str) -> int:
    order_total_match: re.Match[str] | None = re.search(
        r"(?:Order\s+Totals?\b.*?\bTotal|Order\s+Total)\s*:?\s*(\$[\d,]+(?:\.\d{2})?)",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    overall_total_cents: int | None = (
        _money_to_cents(order_total_match.group(1))
        if order_total_match is not None
        else None
    )

    charged_matches: list[str] = re.findall(
        r"Total charged\s*:?\s*(\$[\d,]+(?:\.\d{2})?)",
        full_text,
        flags=re.IGNORECASE,
    )
    charged_cents: list[int] = [_money_to_cents(m) for m in charged_matches]

    if overall_total_cents is not None:
        if len(charged_cents) > 1 and sum(charged_cents) != overall_total_cents:
            raise ReceiptParseError(
                f"Split tender charges sum (${sum(charged_cents)/100:.2f}) does not match order total (${overall_total_cents/100:.2f})."
            )
        return overall_total_cents

    if charged_cents:
        return sum(charged_cents) if len(charged_cents) > 1 else charged_cents[0]

    fallback_total_match: re.Match[str] | None = re.search(
        r"\b(?:Total|Order Total)\s*:?\s*(\$[\d,]+(?:\.\d{2})?)",
        full_text,
        flags=re.IGNORECASE,
    )
    if fallback_total_match is not None:
        return _money_to_cents(fallback_total_match.group(1))

    raise ReceiptParseError("Receipt is missing a final total.")



def _extract_money_after_label(text: str, label: str) -> int:
    label_pattern: str = re.escape(label)
    match: re.Match[str] | None = re.search(
        rf"{label_pattern}\s*(\$[\d,]+(?:\.\d{{2}})?)", text
    )
    if match is None:
        raise ReceiptParseError(f"Receipt is missing a final total labeled {label!r}.")
    return _money_to_cents(match.group(1))


def _extract_money(text: str) -> int:
    match: re.Match[str] | None = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    if match is None:
        raise ReceiptParseError("Receipt item is missing an exact dollar amount.")
    return _money_to_cents(match.group(0))


def _money_to_cents(value: str) -> int:
    normalized_value: str = value.replace("$", "").replace(",", "").strip()
    try:
        dollar_amount: Decimal = Decimal(normalized_value)
    except InvalidOperation as error:
        raise ReceiptParseError("Receipt contains an invalid currency amount.") from error
    if dollar_amount < 0 or dollar_amount.quantize(_CURRENCY_QUANTUM) != dollar_amount:
        raise ReceiptParseError("Receipt currency amounts must be non-negative exact cents.")
    return int((dollar_amount * _CENTS_PER_DOLLAR).to_integral_value(ROUND_HALF_UP))


def _extract_quantity(item_text: str) -> int:
    match: re.Match[str] | None = re.search(r"(?:^|\s)(\d+)\s*x\s*\$", item_text)
    if match is None:
        return 1
    quantity: int = int(match.group(1))
    if quantity < 1:
        raise ReceiptParseError("Receipt item quantity must be positive.")
    return quantity


def _first_line(value: str) -> str:
    description: str = re.split(r"\s+\d+\s*(?:lb\s*)?x\s*\$", value, maxsplit=1)[0]
    return _clean_description(description)


def _clean_description(value: str) -> str:
    description: str = re.sub(r"\s+", " ", value).strip()
    if not description:
        raise ReceiptParseError("Receipt item is missing a description.")
    return description
