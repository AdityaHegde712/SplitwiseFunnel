"""Fail-closed parsers for the supported finalized receipt email formats."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import re
from typing import Final


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


def parse_receipt_email(vendor_id: str, html: str) -> dict[str, object]:
    """Parse one supported finalized receipt email into exact-cent receipt data."""
    if vendor_id not in _SUPPORTED_VENDORS:
        raise ReceiptParseError(f"Unsupported receipt vendor: {vendor_id!r}.")
    if not isinstance(html, str) or not html.strip():
        raise ReceiptParseError("Receipt email must contain a supported HTML body.")

    parser: _ReceiptHtmlParser = _ReceiptHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except ValueError as error:
        raise ReceiptParseError("Receipt email HTML could not be parsed.") from error

    if vendor_id == "walmart_online":
        return _parse_walmart_receipt(parser.root)
    return _parse_costco_receipt(parser.root)


def _parse_walmart_receipt(root: _Node) -> dict[str, object]:
    full_text: str = root.text()
    receipt_id: str = _extract_order_id(full_text)
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

    total_cents: int = _extract_money_after_label(full_text, "Total charged")
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


def _extract_order_id(text: str) -> str:
    match: re.Match[str] | None = re.search(r"Order ID:\s*#\s*([^\s]+)", text)
    if match is None:
        raise ReceiptParseError("Receipt is missing an order ID.")
    return match.group(1)


def _extract_payment_evidence(text: str) -> tuple[str | None, str | None]:
    visa_match: re.Match[str] | None = re.search(
        r"\bvisa\s+(?:ending\s+in\s+)?(\d{4})\b", text, flags=re.IGNORECASE
    )
    if visa_match is not None:
        return "visa", visa_match.group(1)

    paypal_match: re.Match[str] | None = re.search(
        r"\bpaypal(?:\s+card)?\b", text, flags=re.IGNORECASE
    )
    if paypal_match is not None:
        return "paypal", None

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
