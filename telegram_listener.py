"""Read-only MTProto observer for successful Telegram bot deliveries.

This module deliberately does not use the HTTP Bot API, ``getUpdates`` or
webhooks. It logs the bot in through MTProto in the same general way as a
Telegram client that supports "Login as Bot Account".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from order_parser import (
    DeliveryEnvelope,
    IncompleteAccountBatch,
    OrderParseError,
    OrderParser,
    PurchaseConfirmation,
    SuccessfulOrder,
)
from storage import DuplicateOrderConflict, PurchaseStorage


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _ConfirmationContext:
    product_key: str
    product: str
    quantity: int
    observed_at: datetime
    message_id: int | None


@dataclass(slots=True)
class _PendingDelivery:
    combined_text: str
    envelope: DeliveryEnvelope
    expected_quantity: int | None
    confirmation: _ConfirmationContext | None
    purchased_at: datetime
    username: str
    header_message_id: int | None
    last_message_id: int | None
    chunk_count: int = 0


def _shorten(value: object, limit: int = 64) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _log_event(
    level: int,
    console_message: str,
    *,
    file_detail: str | None = None,
    exc_info: bool = False,
) -> None:
    LOGGER.log(
        level,
        console_message,
        extra={"file_detail": file_detail or console_message},
        exc_info=exc_info,
    )


class TelegramOrderListener:
    """Observe new bot messages and persist matching successful deliveries.

    The class contains no method for sending, editing, deleting or forwarding a
    Telegram message. Its MTProto client is created internally and is used only
    to authorize, identify the account, receive updates and disconnect.
    """

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_file: str | Path,
        parser: OrderParser,
        storage: PurchaseStorage,
        allowed_chat_ids: Iterable[int] = (),
        only_outgoing: bool = True,
    ) -> None:
        if not isinstance(api_id, int) or api_id <= 0:
            raise ValueError("API_ID phải là số nguyên dương")
        if not api_hash.strip():
            raise ValueError("API_HASH đang trống")
        if not bot_token.strip() or ":" not in bot_token:
            raise ValueError("BOT_TOKEN không đúng định dạng")

        self.api_id = api_id
        self.api_hash = api_hash.strip()
        self.bot_token = bot_token.strip()
        self.session_file = Path(session_file)
        self.parser = parser
        self.storage = storage
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.only_outgoing = only_outgoing

        self.stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any | None = None
        self._confirmations: dict[int, list[_ConfirmationContext]] = {}
        self._pending_deliveries: dict[int, _PendingDelivery] = {}

    def stop(self) -> None:
        """Request a graceful disconnect without sending any Telegram message."""

        self.stop_event.set()
        loop = self._loop
        client = self._client
        if loop is not None and client is not None and loop.is_running():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(client.disconnect())
            )

    def run_forever(self) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError as exc:
            raise RuntimeError(
                "Thiếu Telethon. Hãy chạy: pip install -r requirements.txt"
            ) from exc

        self.session_file.parent.mkdir(parents=True, exist_ok=True)

        async def run_client() -> None:
            self._loop = asyncio.get_running_loop()
            client = TelegramClient(
                str(self.session_file),
                self.api_id,
                self.api_hash,
                sequential_updates=True,
                # We only need live orders. Replaying a large MTProto backlog
                # causes stale-update/session warnings when the same bot also
                # runs through an HTTP webhook.
                catch_up=False,
                auto_reconnect=True,
                connection_retries=-1,
                retry_delay=5,
            )
            self._client = client

            @client.on(events.NewMessage)
            async def on_new_message(event: Any) -> None:
                try:
                    await self._handle_event(event)
                except Exception:
                    # One malformed/unexpected event must not stop the 24/7
                    # observer. The failure remains visible in app.log.
                    _log_event(
                        logging.ERROR,
                        f"❌ [ERROR] msg={event.id} · lỗi xử lý Telegram",
                        file_detail=(
                            f"telegram event processing failed | message_id={event.id}"
                        ),
                        exc_info=True,
                    )

            try:
                await client.start(bot_token=self.bot_token)
                me = await client.get_me()
                if me is None or not bool(getattr(me, "bot", False)):
                    raise RuntimeError(
                        "Session MTProto không đăng nhập bằng tài khoản bot"
                    )

                identity = (
                    f"@{me.username}" if getattr(me, "username", None) else str(me.id)
                )
                chat_scope = (
                    ",".join(str(item) for item in sorted(self.allowed_chat_ids))
                    if self.allowed_chat_ids
                    else "ALL"
                )
                mode = "OUTGOING_ONLY" if self.only_outgoing else "ALL_MESSAGES"
                console_scope = (
                    "ALL CHATS"
                    if not self.allowed_chat_ids
                    else f"{len(self.allowed_chat_ids)} CHATS"
                )
                _log_event(
                    logging.INFO,
                    f"🟢 [READY] {identity} · {console_scope}",
                    file_detail=(
                        f"observer ready | bot={identity} | mode={mode} | "
                        f"chats={chat_scope} | webhook=UNCHANGED"
                    ),
                )
                stats = self.storage.get_stats()
                _log_event(
                    logging.INFO,
                    (
                        f"📊 [STATS] {stats.orders} đơn · {stats.accounts} tài khoản "
                        f"· {stats.products} sản phẩm"
                    ),
                    file_detail=(
                        f"storage stats | orders={stats.orders} | "
                        f"accounts={stats.accounts} | products={stats.products} | "
                        f"data_dir={self.storage.data_dir} | "
                        f"history_file={self.storage.history_file}"
                    ),
                )
                await client.run_until_disconnected()
            finally:
                if client.is_connected():
                    await client.disconnect()
                self._client = None
                self._loop = None

        asyncio.run(run_client())

    async def _handle_event(self, event: Any) -> None:
        message = event.message
        is_outgoing = bool(getattr(message, "out", False))
        if self.only_outgoing and not is_outgoing:
            return

        chat_id = getattr(event, "chat_id", None)
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            return

        text = getattr(message, "raw_text", None) or getattr(message, "message", None)
        if not isinstance(text, str) or not text.strip():
            return

        username = ""
        try:
            chat = await event.get_chat()
            username = str(getattr(chat, "username", None) or "")
        except Exception:
            LOGGER.debug(
                "Không lấy được username cho chat_id=%s", chat_id, exc_info=True
            )

        self._handle_message(
            text=text,
            chat_id=chat_id,
            username=username,
            message_id=getattr(message, "id", None),
            message_date=getattr(message, "date", None),
            is_outgoing=is_outgoing,
        )

    def _handle_message(
        self,
        *,
        text: str,
        chat_id: object = "",
        username: object = "",
        message_id: int | None = None,
        message_date: datetime | None = None,
        is_outgoing: bool = True,
    ) -> None:
        """Pure processing seam used by the MTProto handler and unit tests."""

        if self.only_outgoing and not is_outgoing:
            return
        try:
            numeric_chat_id = int(chat_id) if chat_id is not None else None
        except (TypeError, ValueError):
            numeric_chat_id = None
        if self.allowed_chat_ids and numeric_chat_id not in self.allowed_chat_ids:
            return

        purchased_at = message_date or datetime.now(timezone.utc)
        if purchased_at.tzinfo is None:
            purchased_at = purchased_at.replace(tzinfo=timezone.utc)

        confirmation = self.parser.parse_confirmation(text)
        if confirmation is not None and numeric_chat_id is not None:
            self._remember_confirmation(
                numeric_chat_id,
                confirmation,
                purchased_at,
                message_id,
            )
            return

        envelope = self.parser.parse_delivery_envelope(text)
        if envelope is not None:
            context = self._find_confirmation(
                numeric_chat_id,
                envelope.product,
                purchased_at,
            )
            expected_quantity = context.quantity if context else None
            try:
                order = self.parser.build_delivery_order(
                    envelope,
                    default_user_id=numeric_chat_id or "",
                    default_username=username,
                    purchased_at=purchased_at,
                    source_update_id=message_id,
                    expected_quantity=expected_quantity,
                )
            except IncompleteAccountBatch as exc:
                if numeric_chat_id is None:
                    self._log_parse_error(exc, message_id, numeric_chat_id)
                    return
                self._pending_deliveries[numeric_chat_id] = _PendingDelivery(
                    combined_text=text,
                    envelope=envelope,
                    expected_quantity=expected_quantity,
                    confirmation=context,
                    purchased_at=purchased_at,
                    username=str(username),
                    header_message_id=message_id,
                    last_message_id=message_id,
                )
                self._log_waiting(envelope, exc.found, expected_quantity)
                return
            except OrderParseError as exc:
                # A body with no credential-like data (empty or only a footer
                # such as "Ngày trả đơn") means Telegram split the delivery.
                # Buffer the next outgoing message instead of reporting SKIP.
                if (
                    numeric_chat_id is not None
                    and not self._looks_like_account_chunk(envelope.account_text)
                ):
                    self._pending_deliveries[numeric_chat_id] = _PendingDelivery(
                        combined_text=text,
                        envelope=envelope,
                        expected_quantity=expected_quantity,
                        confirmation=context,
                        purchased_at=purchased_at,
                        username=str(username),
                        header_message_id=message_id,
                        last_message_id=message_id,
                    )
                    self._log_waiting(envelope, 0, expected_quantity)
                    return
                self._log_parse_error(exc, message_id, numeric_chat_id)
                return

            self._record_order(
                order,
                numeric_chat_id=numeric_chat_id,
                username=username,
                message_id=message_id,
            )
            self._consume_confirmation(numeric_chat_id, context)
            return

        pending = (
            self._pending_deliveries.get(numeric_chat_id)
            if numeric_chat_id is not None
            else None
        )
        if pending is not None and self._looks_like_account_chunk(text):
            self._continue_pending_delivery(
                numeric_chat_id,
                pending,
                text=text,
                message_id=message_id,
                message_date=purchased_at,
            )
            return

        try:
            order = self.parser.parse(
                text,
                default_user_id=numeric_chat_id or "",
                default_username=username,
                purchased_at=purchased_at,
                source_update_id=message_id,
            )
        except OrderParseError as exc:
            self._log_parse_error(exc, message_id, numeric_chat_id)
            return

        if order is None:
            return

        self._record_order(
            order,
            numeric_chat_id=numeric_chat_id,
            username=username,
            message_id=message_id,
        )

    def _remember_confirmation(
        self,
        chat_id: int,
        confirmation: PurchaseConfirmation,
        observed_at: datetime,
        message_id: int | None,
    ) -> None:
        contexts = self._confirmations.setdefault(chat_id, [])
        cutoff = observed_at - timedelta(hours=48)
        contexts[:] = [item for item in contexts if item.observed_at >= cutoff]
        contexts.append(
            _ConfirmationContext(
                product_key=self.parser.product_key(confirmation.product),
                product=confirmation.product,
                quantity=confirmation.quantity,
                observed_at=observed_at,
                message_id=message_id,
            )
        )
        del contexts[:-20]
        LOGGER.debug(
            "confirmation tracked | chat_id=%s | product=%s | quantity=%s | message_id=%s",
            chat_id,
            confirmation.product,
            confirmation.quantity,
            message_id,
        )

    def _find_confirmation(
        self,
        chat_id: int | None,
        product: str,
        delivery_at: datetime,
    ) -> _ConfirmationContext | None:
        if chat_id is None:
            return None
        product_key = self.parser.product_key(product)
        candidates = self._confirmations.get(chat_id, [])
        for context in reversed(candidates):
            age = delivery_at - context.observed_at
            if (
                context.product_key == product_key
                and timedelta(minutes=-5) <= age <= timedelta(hours=48)
            ):
                return context
        return None

    def _consume_confirmation(
        self,
        chat_id: int | None,
        context: _ConfirmationContext | None,
    ) -> None:
        if chat_id is None or context is None:
            return
        contexts = self._confirmations.get(chat_id)
        if contexts and context in contexts:
            contexts.remove(context)
        if not contexts:
            self._confirmations.pop(chat_id, None)

    @staticmethod
    def _looks_like_account_chunk(text: str) -> bool:
        lowered = text.casefold()
        return (
            "|" in text
            or "@" in text
            or "http://" in lowered
            or "https://" in lowered
            or len(text.strip()) >= 80
        )

    def _continue_pending_delivery(
        self,
        chat_id: int,
        pending: _PendingDelivery,
        *,
        text: str,
        message_id: int | None,
        message_date: datetime,
    ) -> None:
        if message_date - pending.purchased_at > timedelta(hours=1):
            self._pending_deliveries.pop(chat_id, None)
            _log_event(
                logging.ERROR,
                f"⚠️ [SKIP ] {pending.envelope.order_id} · dữ liệu giao hàng đã quá hạn",
                file_detail=(
                    f"split delivery expired | order={pending.envelope.order_id} | "
                    f"chat_id={chat_id} | header_message_id={pending.header_message_id}"
                ),
            )
            return

        pending.combined_text = f"{pending.combined_text}\n{text}"
        pending.last_message_id = message_id
        pending.chunk_count += 1
        if len(pending.combined_text) > 250_000 or pending.chunk_count > 20:
            self._pending_deliveries.pop(chat_id, None)
            _log_event(
                logging.ERROR,
                f"⚠️ [SKIP ] {pending.envelope.order_id} · dữ liệu giao hàng quá lớn",
                file_detail=(
                    f"split delivery exceeded limits | order={pending.envelope.order_id} | "
                    f"chat_id={chat_id} | chars={len(pending.combined_text)} | "
                    f"chunks={pending.chunk_count}"
                ),
            )
            return

        envelope = self.parser.parse_delivery_envelope(pending.combined_text)
        if envelope is None:
            self._pending_deliveries.pop(chat_id, None)
            self._log_parse_error(
                OrderParseError("Không ghép lại được thông báo giao hàng"),
                message_id,
                chat_id,
            )
            return
        try:
            order = self.parser.build_delivery_order(
                envelope,
                default_user_id=chat_id,
                default_username=pending.username,
                purchased_at=pending.purchased_at,
                source_update_id=pending.header_message_id,
                expected_quantity=pending.expected_quantity,
            )
        except IncompleteAccountBatch as exc:
            self._log_waiting(envelope, exc.found, pending.expected_quantity)
            return
        except OrderParseError as exc:
            self._pending_deliveries.pop(chat_id, None)
            self._log_parse_error(exc, message_id, chat_id)
            return

        self._pending_deliveries.pop(chat_id, None)
        self._record_order(
            order,
            numeric_chat_id=chat_id,
            username=pending.username,
            message_id=message_id,
        )
        self._consume_confirmation(chat_id, pending.confirmation)

    @staticmethod
    def _log_waiting(
        envelope: DeliveryEnvelope,
        found: int,
        expected: int | None,
    ) -> None:
        total = str(expected) if expected is not None else "?"
        _log_event(
            logging.INFO,
            f"⏳ [WAIT ] {envelope.order_id} · {found}/{total} tài khoản",
            file_detail=(
                f"split delivery waiting | order={envelope.order_id} | "
                f"product={envelope.product} | found={found} | expected={total}"
            ),
        )

    @staticmethod
    def _log_parse_error(
        exc: Exception,
        message_id: int | None,
        chat_id: int | None,
    ) -> None:
        _log_event(
            logging.ERROR,
            f"⚠️ [SKIP ] msg={message_id} · {_shorten(exc, 78)}",
            file_detail=(
                f"order-like message skipped | message_id={message_id} | "
                f"chat_id={chat_id} | reason={exc}"
            ),
        )

    def _record_order(
        self,
        order: SuccessfulOrder,
        *,
        numeric_chat_id: int | None,
        username: object,
        message_id: int | None,
    ) -> None:

        try:
            result = self.storage.record_purchase(order)
        except DuplicateOrderConflict as exc:
            _log_event(
                logging.CRITICAL,
                f"⛔ [ERROR] {order.order_id} · order_id xung đột dữ liệu",
                file_detail=(
                    f"order conflict | order={order.order_id} | "
                    f"chat_id={numeric_chat_id} | reason={exc}"
                ),
            )
            return

        if result.duplicate:
            _log_event(
                logging.INFO,
                f"↪️ [DUP  ] {order.order_id} · đã lưu trước đó",
                file_detail=(
                    f"duplicate order skipped | order={order.order_id} | "
                    f"product={order.product} | accounts={order.account_count} | "
                    f"chat_id={numeric_chat_id} | message_id={message_id}"
                ),
            )
        else:
            buyer = str(username).lstrip("@") or "-"
            _log_event(
                logging.INFO,
                (
                    f"✅ [SOLD ] x{order.account_count} · {order.order_id} · "
                    f"{_shorten(order.product)}"
                ),
                file_detail=(
                    f"order saved | order={order.order_id} | "
                    f"user_id={numeric_chat_id or '-'} | username=@{buyer} | "
                    f"product={order.product} | accounts={order.account_count} | "
                    f"message_id={message_id} | product_file={result.product_file} | "
                    f"history_file={result.history_file}"
                ),
            )
