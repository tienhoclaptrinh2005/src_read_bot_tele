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
    r"duoi\s+day\s+la\s+tai\s+khoan\s+cua\s+ban\s*:\s*"
    r"(?P<account>.+)\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_cosmetic_product_prefix(product: str) -> str:
    """Remove a leading delivery emoji while preserving the product name."""

    cleaned = product.strip()
    cleaned = re.sub(r"^[^\w]+", "", cleaned, flags=re.UNICODE)
    return cleaned.strip()


def _accounts_from_tail(value: str) -> tuple[str, ...]:
    """Extract all delivered accounts, one non-empty Telegram line per item."""

    accounts: list[str] = []
    for raw_line in value.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```") or line.casefold() == "copy":
            continue
        # Also accept bots that number or bullet each delivered credential.
        line = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", line).strip()
        if line:
            accounts.append(line)

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
    ) -> SuccessfulOrder | None:
        bot_delivery = self._from_bot_delivery(
            text,
            default_user_id=default_user_id,
            default_username=default_username,
            purchased_at=purchased_at,
            source_update_id=source_update_id,
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
            values["account"] = _accounts_from_tail(str(values["account"]))
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
    ) -> SuccessfulOrder | None:
        normalized_text = _normalized(text)
        if "duoi day la tai khoan cua ban" not in normalized_text:
            return None

        # Run the regex on a diacritic-free copy so spelling/case variations do
        # not matter. Capture spans still line up with the original because NFKD
        # removes combining marks only after decomposition; therefore use a
        # separately normalized-by-character representation that preserves one
        # output character per original character.
        comparable = "".join(
            "d" if char.casefold() == "đ" else (
                unicodedata.normalize("NFKD", char)[0].casefold()
                if unicodedata.normalize("NFKD", char)
                else char.casefold()
            )
            for char in text
        )
        account_match = BOT_ACCOUNT_MARKER_PATTERN.search(comparable)
        if not account_match:
            raise OrderParseError(
                "Nhận thấy thông báo giao tài khoản nhưng không đọc được dữ liệu"
            )

        header = comparable[: account_match.start()]
        normalized_header = _normalized(header)
        if any(phrase in normalized_header for phrase in FAILURE_PHRASES):
            return None

        order_match = BOT_ORDER_PRODUCT_PATTERN.search(header)
        if not order_match:
            # A labelled wallet-success format may put order/product on their
            # own lines instead of ``ORDER_ID (Product)``. Let the generic
            # labelled parser handle that variant.
            return None

        order_id = text[
            order_match.start("order_id") : order_match.end("order_id")
        ]
        product = text[order_match.start("product") : order_match.end("product")]
        account_tail = text[
            account_match.start("account") : account_match.end("account")
        ]
        product = _strip_cosmetic_product_prefix(product)
        accounts = _accounts_from_tail(account_tail)

        return SuccessfulOrder(
            order_id=order_id,
            user_id=default_user_id,
            username=default_username,
            product=product,
            account=accounts,
            purchased_at=purchased_at,
            source_update_id=source_update_id,
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
