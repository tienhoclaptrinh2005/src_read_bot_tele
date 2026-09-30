from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from order_parser import (
    IncompleteAccountBatch,
    OrderParseError,
    OrderParser,
    split_account_records,
)


class OrderParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = OrderParser()
        self.timestamp = datetime(2026, 9, 29, 11, 1, 22, tzinfo=timezone.utc)

    def test_parses_vietnamese_labelled_message(self) -> None:
        order = self.parser.parse(
            """✅ Mua hàng thành công
Mã đơn: ORD001
User ID: 123456
Username: @userA
Sản phẩm: Canva Pro 1 Tháng
Tài khoản: account01@gmail.com|pass01""",
            purchased_at=self.timestamp,
            source_update_id=99,
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.order_id, "ORD001")
        self.assertEqual(order.user_id, "123456")
        self.assertEqual(order.username, "userA")
        self.assertEqual(order.product, "Canva Pro 1 Tháng")
        self.assertEqual(order.account, "account01@gmail.com|pass01")
        self.assertEqual(order.source_update_id, 99)

    def test_parses_json_message(self) -> None:
        message = json.dumps(
            {
                "status": "success",
                "order_id": "ORD002",
                "user_id": 456789,
                "username": "userB",
                "product": "Netflix Premium",
                "account": "netflix01@gmail.com|pass01",
            },
            ensure_ascii=False,
        )
        order = self.parser.parse(message, purchased_at=self.timestamp)

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.product, "Netflix Premium")

    def test_parses_actual_bot_delivery_format(self) -> None:
        order = self.parser.parse(
            """Đã nhận thanh toán cho đơn hàng ORDERRRZVTMPUIU (🚚 Express VPN 3 Ngày BHF). Dưới đây là tài khoản của bạn:
expressos72709x7@catshopvip.site|Admin123@""",
            default_user_id=987654,
            default_username="buyer_name",
            purchased_at=self.timestamp,
            source_update_id=888,
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.order_id, "ORDERRRZVTMPUIU")
        self.assertEqual(order.product, "Express VPN 3 Ngày BHF")
        self.assertEqual(
            order.account,
            "expressos72709x7@catshopvip.site|Admin123@",
        )
        self.assertEqual(order.user_id, "987654")
        self.assertEqual(order.username, "buyer_name")

    def test_parses_actual_delivery_with_code_fence(self) -> None:
        order = self.parser.parse(
            """Đã nhận thanh toán cho đơn hàng ORD100 (📦 Canva Pro 1 Tháng). Dưới đây là tài khoản của bạn:
```copy
account@example.com|pass123
```""",
            purchased_at=self.timestamp,
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.product, "Canva Pro 1 Tháng")
        self.assertEqual(order.account, "account@example.com|pass123")

    def test_actual_delivery_accepts_multiple_account_lines(self) -> None:
        order = self.parser.parse(
            """Đã nhận thanh toán cho đơn hàng ORD101 (Netflix Premium). Dưới đây là tài khoản của bạn:
first@example.com|one
second@example.com|two""",
            purchased_at=self.timestamp,
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(
            order.accounts,
            ("first@example.com|one", "second@example.com|two"),
        )
        self.assertEqual(order.account_count, 2)

    def test_wallet_payment_delivery_is_parsed_like_direct_payment(self) -> None:
        order = self.parser.parse(
            """✅ Thanh toán qua ví thành công
Đã trừ số dư ví cho đơn hàng ORDER-WALLET-1 (💰 Canva Pro 1 Tháng). Dưới đây là tài khoản của bạn:
wallet01@example.com|pass01
wallet02@example.com|pass02
wallet03@example.com|pass03""",
            default_user_id=123456,
            default_username="wallet_buyer",
            purchased_at=self.timestamp,
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.order_id, "ORDER-WALLET-1")
        self.assertEqual(order.product, "Canva Pro 1 Tháng")
        self.assertEqual(order.account_count, 3)
        self.assertEqual(order.accounts[2], "wallet03@example.com|pass03")

    def test_confirmation_exposes_product_and_requested_quantity(self) -> None:
        confirmation = self.parser.parse_confirmation(
            """🧾 Xác nhận đơn hàng
Sản phẩm: Meitu VIP 7 Ngày BHF
Số lượng: 4
Thành tiền: 40k"""
        )

        self.assertIsNotNone(confirmation)
        assert confirmation is not None
        self.assertEqual(confirmation.product, "Meitu VIP 7 Ngày BHF")
        self.assertEqual(confirmation.quantity, 4)

    def test_concatenated_meitu_stock_is_split_by_four_uuid_boundaries(self) -> None:
        records = tuple(
            (
                f"buyer{index}@hotmail.com|pw{index}|"
                f"Admin123@(Email|Pass Hotmail|Pass Meitu)"
                f"buyer{index}@hotmail.com|pw{index}|M.C{index}-very-long-token|$|"
                f"9e5f94bc-e8a4-4e73-b8be-63364c29d75{index}"
            )
            for index in range(4)
        )

        self.assertEqual(
            split_account_records("".join(records), expected_quantity=4),
            records,
        )

    def test_incomplete_batch_waits_instead_of_saving_x2_as_x4(self) -> None:
        partial = "\n".join(
            (
                "first@example.com|one",
                "second@example.com|two",
            )
        )

        with self.assertRaises(IncompleteAccountBatch) as context:
            split_account_records(partial, expected_quantity=4)

        self.assertEqual(context.exception.expected, 4)
        self.assertEqual(context.exception.found, 2)

    def test_labelled_wallet_payment_with_accounts_on_following_lines(self) -> None:
        order = self.parser.parse(
            """✅ Thanh toán bằng ví thành công
Trạng thái: Thanh toán bằng ví thành công
Mã đơn: ORDER-WALLET-2
Sản phẩm: Netflix Premium
Dưới đây là tài khoản của bạn:
netflix01@example.com|pass01
netflix02@example.com|pass02""",
            purchased_at=self.timestamp,
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.order_id, "ORDER-WALLET-2")
        self.assertEqual(order.product, "Netflix Premium")
        self.assertEqual(order.account_count, 2)

    def test_failed_wallet_payment_is_not_saved(self) -> None:
        order = self.parser.parse(
            """Thanh toán qua ví thất bại cho đơn hàng ORDER-WALLET-FAIL (Canva Pro). Dưới đây là tài khoản của bạn:
should-not-save@example.com|pass""",
            purchased_at=self.timestamp,
        )
        self.assertIsNone(order)

    def test_ignores_pending_or_failed_message(self) -> None:
        pending = """Trạng thái: pending
Mã đơn: ORD003
Sản phẩm: Canva Pro 1 Tháng
Tài khoản: account03@gmail.com|pass03"""
        failed = pending.replace("pending", "thất bại")
        self.assertIsNone(self.parser.parse(pending, purchased_at=self.timestamp))
        self.assertIsNone(self.parser.parse(failed, purchased_at=self.timestamp))

    def test_success_message_missing_required_field_raises(self) -> None:
        with self.assertRaises(OrderParseError):
            self.parser.parse(
                """Trạng thái: thành công
Mã đơn: ORD004
Sản phẩm: ChatGPT Plus""",
                purchased_at=self.timestamp,
            )

    def test_json_account_field_can_contain_multiple_accounts(self) -> None:
        payload = json.dumps(
            {
                "status": "success",
                "order_id": "ORD005",
                "product": "ChatGPT Plus",
                "account": "first@example.com|one\nsecond@example.com|two",
            }
        )
        order = self.parser.parse(payload, purchased_at=self.timestamp)
        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.account_count, 2)


if __name__ == "__main__":
    unittest.main()
