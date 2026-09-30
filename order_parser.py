"""Parse successful-order notifications into a strict, storage-ready model."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


class OrderParseError(ValueError):
    """The message looks like a successful order but misses required data."""


class IncompleteAccountBatch(OrderParseError):
    """A delivery started, but not all expected account records arrived yet."""

    def __init__(self, expected: int, found: int) -> None:
        super().__init__(f"Đang chờ đủ tài khoản: đã nhận {found}/{expected}")
        self.expected = expected
        self.found = found


@dataclass(frozen=True, slots=True)
class PurchaseConfirmation:
    product: str
    quantity: int


@dataclass(frozen=True, slots=True)
class DeliveryEnvelope:
    order_id: str
    product: str
    account_text: str


def _one_line(value: object, field: str, *, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)

    if "\x00" in text or "\r" in text or "\n" in text:
        raise OrderParseError(f"{field} phải nằm trên đúng một dòng")
    text = text.strip()
    if required and not text:
        raise OrderParseError(f"Thiếu trường bắt buộc: {field}")
    return text


def _account_value(value: object) -> str:
    """Normalize one or more delivered accounts to newline-delimited storage."""

    if value is None:
        raw_values: list[object] = []
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        raw_values = [value]

    accounts: list[str] = []
    for raw_value in raw_values:
        text = str(raw_value)
        if "\x00" in text:
            raise OrderParseError("Dữ liệu tài khoản chứa ký tự không hợp lệ")
        accounts.extend(line.strip() for line in text.splitlines() if line.strip())

    if not accounts:
        raise OrderParseError("Thiếu trường bắt buộc: account")
    if len(accounts) > 1000:
        raise OrderParseError("Một đơn không được vượt quá 1.000 tài khoản")
    if any(len(account) > 4000 for account in accounts):
        raise OrderParseError("Mỗi tài khoản không được dài quá 4.000 ký tự")
    return "\n".join(accounts)


@dataclass(frozen=True, slots=True)
class SuccessfulOrder:
    """A successfully delivered order.

    Product is single-line. ``account`` is a canonical newline-delimited value:
    each non-empty line is one delivered account. This allows one Telegram order
    to append multiple product/history lines while remaining one deduplicated
    transaction.
    """

    order_id: str
    user_id: str
    username: str
    product: str
    account: str | tuple[str, ...] | list[str]
    purchased_at: datetime
    source_update_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _one_line(self.order_id, "order_id", required=True))
        object.__setattr__(self, "user_id", _one_line(self.user_id, "user_id"))
        object.__setattr__(self, "username", _one_line(self.username, "username").lstrip("@"))
        object.__setattr__(self, "product", _one_line(self.product, "product", required=True))
        object.__setattr__(self, "account", _account_value(self.account))

        if len(self.order_id) > 200:
            raise OrderParseError("order_id dài quá 200 ký tự")
        if len(self.product) > 180:
            raise OrderParseError("Tên sản phẩm dài quá 180 ký tự")
        timestamp = self.purchased_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "purchased_at", timestamp)

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(str(self.account).splitlines())

    @property
    def account_count(self) -> int:
        return len(self.accounts)


def _normalized(value: object) -> str:
    # Vietnamese đ/Đ is not decomposed by NFKD, so map it explicitly before
    # removing combining marks.
    text = str(value).casefold().replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _comparable_text(text: str) -> str:
    """Remove accents while preserving one output character per input char."""

    comparable: list[str] = []
    for char in text:
        if char.casefold() == "đ":
            comparable.append("d")
            continue
        decomposed = unicodedata.normalize("NFKD", char)
        comparable.append((decomposed[0] if decomposed else char).casefold())
    return "".join(comparable)


FIELD_ALIASES: dict[str, set[str]] = {
    "status": {
        "status",
        "trang thai",
        "ket qua",
        "result",
    },
    "order_id": {
        "order id",
        "order",
        "ma don",
        "ma don hang",
        "don hang",
    },
    "user_id": {
        "user id",
        "telegram id",
        "id khach hang",
        "id nguoi dung",
        "buyer id",
    },
    "username": {
        "username",
        "telegram username",
        "user",
        "nguoi mua",
        "khach hang",
    },
    "product": {
        "product",
        "product name",
        "san pham",
        "ten san pham",
        "goi",
    },
    "account": {
        "account",
        "credential",
        "credentials",
        "delivery",
        "delivered account",
        "tai khoan",
        "tai khoan giao",
        "tai khoan da giao",
        "duoi day la tai khoan cua ban",
        "du lieu",
        "du lieu giao",
    },
    "quantity": {
        "quantity",
        "qty",
        "so luong",
        "sl",
    },
}

ALIAS_LOOKUP = {
    alias: canonical
    for canonical, aliases in FIELD_ALIASES.items()
    for alias in aliases
}

SUCCESS_VALUES = {
    "success",
    "successful",
    "completed",
    "complete",
    "paid",
    "delivered",
    "ok",
    "thanh cong",
    "da thanh cong",
    "da thanh toan",
    "da giao",
    "giao thanh cong",
    "paid wallet",
    "wallet paid",
    "thanh toan bang vi thanh cong",
    "thanh toan qua vi thanh cong",
    "da thanh toan bang vi",
}

FAILURE_PHRASES = (
    "khong thanh cong",
    "chua thanh cong",
    "that bai",
    "da huy",
    "cancelled",
    "canceled",
    "failed",
    "pending",
)

SUCCESS_PHRASES = (
    "da nhan thanh toan cho don hang",
    "thanh toan bang vi thanh cong",
    "thanh toan qua vi thanh cong",
    "da thanh toan bang vi",
    "da thanh toan qua vi",
    "mua hang bang vi thanh cong",
    "mua hang qua vi thanh cong",
    "tru tien vi thanh cong",
    "thanh toan tu so du vi thanh cong",
    "mua hang thanh cong",
    "don hang thanh cong",
    "giao hang thanh cong",
    "giao tai khoan thanh cong",
    "thanh toan thanh cong",
    "purchase successful",
    "order successful",
    "payment successful",
    "delivery completed",
)


# Delivery format used by the sales bot, for example:
#
#   Đã nhận thanh toán cho đơn hàng ORD001 (🚚 Express VPN 3 Ngày BHF).
#   Dưới đây là tài khoản của bạn:
#   account@example.com|password
#
# The account may be wrapped in a Telegram pre/code entity. Telegram keeps the
# code fence in raw text only in some client/export formats, so accept both.
BOT_ORDER_PRODUCT_PATTERN = re.compile(
    r"\bdon\s+hang\s*:?\s*(?P<order_id>[^\s(]+)\s*"
    r"\(\s*(?P<product>[^)\r\n]+?)\s*\)",
    re.IGNORECASE,
)

BOT_ACCOUNT_MARKER_PATTERN = re.compile(
    r"(?:duoi\s+day\s+(?:chinh\s+)?la\s+)?"
    r"(?:thong\s+tin\s+)?tai\s+khoan"
    r"(?:\s+da\s+giao)?(?:\s+cua\s+ban)?(?:\s+nhu\s+sau)?"
    r"\s*(?::|：|-)?\s*(?P<account>.*)\Z",
    re.IGNORECASE | re.DOTALL,
)

EMAIL_PATTERN = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.IGNORECASE,
)
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _strip_cosmetic_product_prefix(product: str) -> str:
    """Remove a leading delivery emoji while preserving the product name."""

    cleaned = product.strip()
    cleaned = re.sub(r"^[^\w]+", "", cleaned, flags=re.UNICODE)
    return cleaned.strip()


def _clean_account_lines(value: str) -> list[str]:
    """Remove Telegram formatting wrappers and known non-credential footers."""

    lines: list[str] = []
    for raw_line in value.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```") or line.casefold() == "copy":
            continue
        normalized_line = _normalized(line)
        if normalized_line.startswith(("ngay tra don", "ma don", "cam on ban")):
            continue
        # Also accept bots that number or bullet each delivered credential.
        line = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", line).strip()
        if line:
            lines.append(line)
    return lines


def _split_after_uuid(text: str) -> tuple[str, ...]:
    """Split long records whose final field is a UUID (Meitu-style stock)."""

    matches = list(UUID_PATTERN.finditer(text))
    if len(matches) < 2:
        return ()
    records: list[str] = []
    start = 0
    for match in matches:
        record = text[start : match.end()].strip()
        if record:
            records.append(record)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        records.append(tail)
    return tuple(records)


def _split_by_distinct_email(text: str, expected_quantity: int) -> tuple[str, ...]:
    """Split concatenated records at the first occurrence of each login email."""

    starts: list[int] = []
    seen: set[str] = set()
    for match in EMAIL_PATTERN.finditer(text):
        email = match.group(0).casefold()
        if email in seen:
            continue
        seen.add(email)
        starts.append(match.start())

    # Only use this heuristic when it produces exactly the confirmed quantity;
    # otherwise a recovery/secondary email could be mistaken for a new account.
    if len(starts) != expected_quantity:
        return ()
    records: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        record = text[start:end].strip()
        if record:
            records.append(record)
    return tuple(records)


def split_account_records(
    value: str,
    *,
    expected_quantity: int | None = None,
) -> tuple[str, ...]:
    """Split delivered stock into one exact, single-line record per account.

    Telegram may put each account on its own line, concatenate several large
    records in one code block, or split one delivery across multiple messages.
    The confirmed quantity is used as an invariant: incomplete batches wait for
    the next message instead of being incorrectly saved as one account.
    """

    lines = _clean_account_lines(value)
    if not lines:
        if expected_quantity:
            raise IncompleteAccountBatch(expected_quantity, 0)
        raise OrderParseError("Thông báo giao hàng không có dữ liệu tài khoản")

    if expected_quantity is not None:
        if not 1 <= expected_quantity <= 1000:
            raise OrderParseError("Số lượng tài khoản phải nằm trong khoảng 1..1000")
        if len(lines) == expected_quantity:
            return tuple(lines)

    # Join physical lines before applying structural splitters. Long Telegram
    # code blocks may contain inserted/wrapped newlines inside one credential.
    compact = "".join(lines)
    uuid_records = _split_after_uuid(compact)
    if uuid_records:
        if expected_quantity is None or len(uuid_records) == expected_quantity:
            return uuid_records

    if expected_quantity and expected_quantity > 1:
        email_records = _split_by_distinct_email(compact, expected_quantity)
        if email_records:
            return email_records

        # Fewer physical records than confirmed means Telegram has not delivered
        # every chunk yet. The listener buffers the next outgoing message(s).
        found = max(len(lines), len(uuid_records))
        if found < expected_quantity:
            raise IncompleteAccountBatch(expected_quantity, found)
        raise OrderParseError(
            f"Không thể chia chính xác {expected_quantity} tài khoản từ dữ liệu nhận được"
        )

    # Without a prior quantity confirmation, prefer explicit Telegram lines or
    # high-confidence UUID boundaries. This retains compatibility with one-item
    # orders and older notification formats.
    accounts = list(uuid_records or tuple(lines))

    # This sales bot normally delivers credentials as ``login|password``. If
    # such lines exist, discard non-credential footer text after the code block.
    pipe_accounts = [line for line in accounts if "|" in line]
    if pipe_accounts:
        accounts = pipe_accounts

    if not accounts:
        raise OrderParseError("Thông báo giao hàng không có dữ liệu tài khoản")
    return tuple(accounts)


def _is_success_status(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _normalized(value)
    if any(phrase in normalized for phrase in FAILURE_PHRASES):
        return False
    return normalized in SUCCESS_VALUES


def _pick(mapping: Mapping[str, Any], field: str) -> Any | None:
    for key, value in mapping.items():
        if ALIAS_LOOKUP.get(_normalized(key)) == field:
            return value
    return None


class OrderParser:
    """Parse JSON or human-readable labelled Telegram notifications."""

    _label_pattern = re.compile(r"^\s*(?:[-*\u2022]\s*)?([^:=]{1,60})\s*[:=]\s*(.*?)\s*$")

    def parse(
        self,
        text: str,
        *,
        default_user_id: object = "",
        default_username: object = "",
        purchased_at: datetime | None = None,
        source_update_id: int | None = None,
        expected_quantity: int | None = None,
    ) -> SuccessfulOrder | None:
        """Return an order, or ``None`` when the message is not successful.

        ``OrderParseError`` is raised only when the message explicitly claims
        success but is malformed. This distinction lets the listener quietly
        ignore ordinary Telegram messages while loudly logging broken orders.
        """

        if not isinstance(text, str) or not text.strip():
            return None

        timestamp = purchased_at or datetime.now(timezone.utc)
        payload = self._try_json(text)
        if payload is not None:
            return self._from_json(
                payload,
                default_user_id=default_user_id,
                default_username=default_username,
                purchased_at=timestamp,
                source_update_id=source_update_id,
            )

        return self._from_text(
            text,
            default_user_id=default_user_id,
            default_username=default_username,
            purchased_at=timestamp,
            source_update_id=source_update_id,
            expected_quantity=expected_quantity,
        )

    @staticmethod
    def product_key(product: object) -> str:
        return _normalized(_strip_cosmetic_product_prefix(str(product)))

    def parse_confirmation(self, text: str) -> PurchaseConfirmation | None:
        """Read the product and quantity from an order-confirmation message."""

        if not isinstance(text, str) or not text.strip():
            return None
        normalized_text = _normalized(text)
        if "xac nhan don hang" not in normalized_text:
            return None

        values: dict[str, str] = {}
        for line in text.splitlines():
            match = self._label_pattern.match(line)
            if not match:
                continue
            label, value = match.groups()
            field = ALIAS_LOOKUP.get(_normalized(label))
            if field in {"product", "quantity"} and field not in values:
                values[field] = value.strip()

        product = values.get("product", "").strip()
        quantity_text = values.get("quantity", "").strip()
        quantity_match = re.search(r"\d+", quantity_text)
        if not product or quantity_match is None:
            return None
        quantity = int(quantity_match.group(0))
        if not 1 <= quantity <= 1000:
            return None
        return PurchaseConfirmation(
            product=_strip_cosmetic_product_prefix(product),
            quantity=quantity,
        )

    @staticmethod
    def parse_delivery_envelope(text: str) -> DeliveryEnvelope | None:
        """Extract order/product/body, accepting an empty or split body."""

        if not isinstance(text, str) or not text.strip():
            return None
        comparable = _comparable_text(text)
        order_match = BOT_ORDER_PRODUCT_PATTERN.search(comparable)
        if not order_match:
            return None

        marker_match = BOT_ACCOUNT_MARKER_PATTERN.search(
            comparable,
            order_match.end(),
        )
        if not marker_match:
            return None
        header = comparable[: marker_match.start()]
        if any(phrase in _normalized(header) for phrase in FAILURE_PHRASES):
            return None

        order_id = text[
            order_match.start("order_id") : order_match.end("order_id")
        ]
        product = text[
            order_match.start("product") : order_match.end("product")
        ]
        account_text = text[
            marker_match.start("account") : marker_match.end("account")
        ]
        return DeliveryEnvelope(
            order_id=order_id.strip(),
            product=_strip_cosmetic_product_prefix(product),
            account_text=account_text.strip(),
        )

    @staticmethod
    def build_delivery_order(
        envelope: DeliveryEnvelope,
        *,
        default_user_id: object = "",
        default_username: object = "",
        purchased_at: datetime | None = None,
        source_update_id: int | None = None,
        expected_quantity: int | None = None,
    ) -> SuccessfulOrder:
        accounts = split_account_records(
            envelope.account_text,
            expected_quantity=expected_quantity,
        )
        return SuccessfulOrder(
            order_id=envelope.order_id,
            user_id=default_user_id,
            username=default_username,
            product=envelope.product,
            account=accounts,
            purchased_at=purchased_at or datetime.now(timezone.utc),
            source_update_id=source_update_id,
        )

    @staticmethod
    def _try_json(text: str) -> Mapping[str, Any] | None:
        candidate = text.strip()
        if candidate.startswith("```json") and candidate.endswith("```"):
            candidate = candidate[7:-3].strip()
        elif candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate[3:-3].strip()

        if not candidate.startswith("{"):
            return None
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise OrderParseError(f"JSON đơn hàng không hợp lệ: {exc.msg}") from exc
        if not isinstance(decoded, dict):
            raise OrderParseError("JSON đơn hàng phải là một object")
        return decoded

    def _from_json(
        self,
        payload: Mapping[str, Any],
        *,
        default_user_id: object,
        default_username: object,
        purchased_at: datetime,
        source_update_id: int | None,
    ) -> SuccessfulOrder | None:
        status = _pick(payload, "status")
        if status is None:
            for key in ("success", "successful"):
                if key in payload:
                    status = payload[key]
                    break
        if status is None or not _is_success_status(status):
            return None

        return self._build_order(
            values={
                "order_id": _pick(payload, "order_id"),
                "user_id": _pick(payload, "user_id") or default_user_id,
                "username": _pick(payload, "username") or default_username,
                "product": _pick(payload, "product"),
                "account": _pick(payload, "account"),
            },
            purchased_at=purchased_at,
            source_update_id=source_update_id,
        )

    def _from_text(
        self,
        text: str,
        *,
        default_user_id: object,
        default_username: object,
        purchased_at: datetime,
        source_update_id: int | None,
        expected_quantity: int | None,
    ) -> SuccessfulOrder | None:
        bot_delivery = self._from_bot_delivery(
            text,
            default_user_id=default_user_id,
            default_username=default_username,
            purchased_at=purchased_at,
            source_update_id=source_update_id,
            expected_quantity=expected_quantity,
        )
        if bot_delivery is not None:
            return bot_delivery

        values: dict[str, Any] = {}
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = self._label_pattern.match(line)
            if not match:
                continue
            label, value = match.groups()
            field = ALIAS_LOOKUP.get(_normalized(label))
            if field and field not in values:
                if field == "account":
                    values[field] = "\n".join([value, *lines[index + 1 :]])
                else:
                    values[field] = value.strip()

        status = values.get("status")
        if status is not None:
            is_success = _is_success_status(status)
        else:
            normalized_text = _normalized(text)
            is_success = not any(phrase in normalized_text for phrase in FAILURE_PHRASES) and any(
                phrase in normalized_text for phrase in SUCCESS_PHRASES
            )

        if not is_success:
            return None

        if values.get("account") is not None:
            values["account"] = split_account_records(
                str(values["account"]),
                expected_quantity=expected_quantity,
            )
        values.setdefault("user_id", default_user_id)
        values.setdefault("username", default_username)
        return self._build_order(
            values=values,
            purchased_at=purchased_at,
            source_update_id=source_update_id,
        )

    @staticmethod
    def _from_bot_delivery(
        text: str,
        *,
        default_user_id: object,
        default_username: object,
        purchased_at: datetime,
        source_update_id: int | None,
        expected_quantity: int | None,
    ) -> SuccessfulOrder | None:
        envelope = OrderParser.parse_delivery_envelope(text)
        if envelope is None:
            return None
        return OrderParser.build_delivery_order(
            envelope,
            default_user_id=default_user_id,
            default_username=default_username,
            purchased_at=purchased_at,
            source_update_id=source_update_id,
            expected_quantity=expected_quantity,
        )

    @staticmethod
    def _build_order(
        *,
        values: Mapping[str, Any],
        purchased_at: datetime,
        source_update_id: int | None,
    ) -> SuccessfulOrder:
        missing = [
            field
            for field in ("order_id", "product", "account")
            if values.get(field) is None or not str(values[field]).strip()
        ]
        if missing:
            raise OrderParseError(
                "Thông báo thành công nhưng thiếu: " + ", ".join(missing)
            )

        return SuccessfulOrder(
            order_id=values["order_id"],
            user_id=values.get("user_id", ""),
            username=values.get("username", ""),
            product=values["product"],
            account=values["account"],
            purchased_at=purchased_at,
            source_update_id=source_update_id,
        )
