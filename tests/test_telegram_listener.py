from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from order_parser import OrderParser
from storage import PurchaseStorage
from telegram_listener import TelegramOrderListener


SUCCESS_MESSAGE = """Đã nhận thanh toán cho đơn hàng ORDERRRZVTMPUIU (🚚 Express VPN 3 Ngày BHF). Dưới đây là tài khoản của bạn:
expressos72709x7@catshopvip.site|Admin123@"""


class TelegramOrderListenerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.storage = PurchaseStorage(root / "data", root / "state")
        self.listener = TelegramOrderListener(
            api_id=12345,
            api_hash="0123456789abcdef0123456789abcdef",
            bot_token="123456:fake-token-for-tests-only",
            session_file=root / "state" / "test_bot",
            parser=OrderParser(),
            storage=self.storage,
            only_outgoing=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def handle(
        self,
        *,
        text: str = SUCCESS_MESSAGE,
        chat_id: int = 123456,
        outgoing: bool = True,
    ) -> None:
        self.listener._handle_message(
            text=text,
            chat_id=chat_id,
            username="buyer",
            message_id=501,
            message_date=datetime(2026, 9, 28, 12, 16, tzinfo=timezone.utc),
            is_outgoing=outgoing,
        )

    def test_outgoing_delivery_is_recorded_and_retry_is_deduplicated(self) -> None:
        self.handle()
        self.handle()

        product_file = self.storage.data_dir / "Express VPN 3 Ngày BHF.txt"
        self.assertEqual(
            product_file.read_text(encoding="utf-8").splitlines(),
            ["expressos72709x7@catshopvip.site|Admin123@"],
        )
        self.assertEqual(
            len(self.storage.history_file.read_text(encoding="utf-8").splitlines()),
            1,
        )

    def test_incoming_customer_message_is_ignored(self) -> None:
        self.handle(outgoing=False)
        self.assertFalse(
            (self.storage.data_dir / "Express VPN 3 Ngày BHF.txt").exists()
        )

    def test_one_order_with_three_accounts_writes_three_lines(self) -> None:
        self.handle(
            text="""Đã nhận thanh toán cho đơn hàng ORDER-MULTI-3 (🚚 Adobe All Apps 7 Ngày KBH). Dưới đây là tài khoản của bạn:
adobe01@example.com|pass01
adobe02@example.com|pass02
adobe03@example.com|pass03"""
        )

        product_file = self.storage.data_dir / "Adobe All Apps 7 Ngày KBH.txt"
        self.assertEqual(
            product_file.read_text(encoding="utf-8").splitlines(),
            [
                "adobe01@example.com|pass01",
                "adobe02@example.com|pass02",
                "adobe03@example.com|pass03",
            ],
        )
        self.assertEqual(
            len(self.storage.history_file.read_text(encoding="utf-8").splitlines()),
            3,
        )

    def test_wallet_payment_order_is_persisted(self) -> None:
        self.handle(
            text="""✅ Thanh toán qua ví thành công
Đã trừ tiền ví cho đơn hàng ORDER-WALLET-SAVED (💰 ChatGPT Plus). Dưới đây là tài khoản của bạn:
walletbuyer@example.com|secret"""
        )

        product_file = self.storage.data_dir / "ChatGPT Plus.txt"
        self.assertEqual(
            product_file.read_text(encoding="utf-8").splitlines(),
            ["walletbuyer@example.com|secret"],
        )

    def test_optional_chat_allowlist_is_enforced(self) -> None:
        self.listener.allowed_chat_ids = {999999}
        self.handle(chat_id=123456)
        self.assertFalse(self.storage.history_file.exists())

    def test_listener_exposes_no_message_write_operation(self) -> None:
        for method in (
            "send_message",
            "edit_message",
            "delete_messages",
            "forward_messages",
        ):
            with self.subTest(method=method):
                self.assertFalse(hasattr(self.listener, method))


if __name__ == "__main__":
    unittest.main()
