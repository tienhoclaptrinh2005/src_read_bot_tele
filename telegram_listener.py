"""Read-only MTProto observer for successful Telegram bot deliveries.

This module deliberately does not use the HTTP Bot API, ``getUpdates`` or
webhooks. It logs the bot in through MTProto in the same general way as a
Telegram client that supports "Login as Bot Account".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from order_parser import OrderParseError, OrderParser
from storage import DuplicateOrderConflict, PurchaseStorage


LOGGER = logging.getLogger(__name__)


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
                catch_up=True,
                auto_reconnect=True,
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

        try:
            order = self.parser.parse(
                text,
                default_user_id=numeric_chat_id or "",
                default_username=username,
                purchased_at=purchased_at,
                source_update_id=message_id,
            )
        except OrderParseError as exc:
            _log_event(
                logging.ERROR,
                f"⚠️ [SKIP ] msg={message_id} · {_shorten(exc, 78)}",
                file_detail=(
                    f"order-like message skipped | message_id={message_id} | "
                    f"chat_id={numeric_chat_id} | reason={exc}"
                ),
            )
            return

        if order is None:
            return

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
