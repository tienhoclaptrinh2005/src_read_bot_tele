from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from order_parser import SuccessfulOrder
from storage import DuplicateOrderConflict, PurchaseStorage


class PurchaseStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.state_dir = root / "state"
        self.storage = PurchaseStorage(self.data_dir, self.state_dir)
        self.timestamp = datetime(2026, 9, 29, 11, 1, 22, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def order(
        self,
        order_id: str,
        *,
        product: str = "Canva Pro 1 Tháng",
        account: str = "account01@gmail.com|pass01",
    ) -> SuccessfulOrder:
        return SuccessfulOrder(
            order_id=order_id,
            user_id="123456",
            username="userA",
            product=product,
            account=account,
            purchased_at=self.timestamp,
        )

    def test_appends_one_purchase_per_line_and_preserves_existing_data(self) -> None:
        product_file = self.data_dir / "Canva Pro 1 Tháng.txt"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        product_file.write_text("old01@gmail.com|oldpass", encoding="utf-8")

        self.storage.record_purchase(self.order("ORD001"))
        self.storage.record_purchase(
            self.order("ORD002", account="account02@gmail.com|pass02")
        )

        self.assertEqual(
            product_file.read_text(encoding="utf-8").splitlines(),
            [
                "old01@gmail.com|oldpass",
                "account01@gmail.com|pass01",
                "account02@gmail.com|pass02",
            ],
        )

    def test_duplicate_order_id_does_not_append_twice(self) -> None:
        first = self.storage.record_purchase(self.order("ORD001"))
        retry = self.storage.record_purchase(self.order("ORD001"))

        self.assertTrue(first.saved)
        self.assertFalse(first.duplicate)
        self.assertFalse(retry.saved)
        self.assertTrue(retry.duplicate)
        lines = first.product_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["account01@gmail.com|pass01"])
        history = first.history_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(history), 1)

    def test_same_order_id_with_different_payload_is_rejected(self) -> None:
        self.storage.record_purchase(self.order("ORD001"))
        with self.assertRaises(DuplicateOrderConflict):
            self.storage.record_purchase(
                self.order("ORD001", account="attacker@example.com|changed")
            )

    def test_each_product_gets_its_own_file(self) -> None:
        canva = self.storage.record_purchase(self.order("ORD001"))
        netflix = self.storage.record_purchase(
            self.order(
                "ORD002",
                product="Netflix Premium",
                account="netflix01@gmail.com|pass01",
            )
        )

        self.assertEqual(canva.product_file.name, "Canva Pro 1 Tháng.txt")
        self.assertEqual(netflix.product_file.name, "Netflix Premium.txt")
        self.assertEqual(
            netflix.product_file.read_text(encoding="utf-8").splitlines(),
            ["netflix01@gmail.com|pass01"],
        )
        self.assertEqual(
            len(self.storage.history_file.read_text(encoding="utf-8").splitlines()),
            2,
        )

    def test_multi_account_order_appends_one_line_per_account(self) -> None:
        result = self.storage.record_purchase(
            self.order(
                "ORD-MULTI",
                account=(
                    "first@example.com|one\n"
                    "second@example.com|two\n"
                    "third@example.com|three"
                ),
            )
        )

        self.assertEqual(
            result.product_file.read_text(encoding="utf-8").splitlines(),
            [
                "first@example.com|one",
                "second@example.com|two",
                "third@example.com|three",
            ],
        )
        history = result.history_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(history), 3)
        self.assertIn("order=ORD-MULTI | item=1/3", history[0])
        self.assertIn("order=ORD-MULTI | item=3/3", history[2])

        retry = self.storage.record_purchase(
            self.order(
                "ORD-MULTI",
                account=(
                    "first@example.com|one\n"
                    "second@example.com|two\n"
                    "third@example.com|three"
                ),
            )
        )
        self.assertTrue(retry.duplicate)
        self.assertEqual(len(result.product_file.read_text(encoding="utf-8").splitlines()), 3)

    def test_storage_stats_count_orders_accounts_and_products(self) -> None:
        self.storage.record_purchase(
            self.order(
                "ORD-STATS-1",
                account="one@example.com|1\ntwo@example.com|2",
            )
        )
        self.storage.record_purchase(
            self.order(
                "ORD-STATS-2",
                product="Netflix Premium",
                account="netflix@example.com|3",
            )
        )

        stats = self.storage.get_stats()
        self.assertEqual(stats.orders, 2)
        self.assertEqual(stats.accounts, 3)
        self.assertEqual(stats.products, 2)

    def test_pending_recovery_confirms_existing_bytes_without_duplication(self) -> None:
        result = self.storage.record_purchase(self.order("ORD001"))
        original_product = result.product_file.read_bytes()
        original_history = result.history_file.read_bytes()

        with closing(sqlite3.connect(self.storage.database_file)) as connection:
            connection.execute(
                """
                UPDATE orders
                SET status = 'pending', product_written = 0,
                    history_written = 0, completed_at = NULL
                WHERE order_id = 'ORD001'
                """
            )
            connection.commit()

        recovered = self.storage.recover_pending()
        self.assertEqual(recovered, 1)
        self.assertEqual(result.product_file.read_bytes(), original_product)
        self.assertEqual(result.history_file.read_bytes(), original_history)
        self.assertTrue(self.storage.is_processed("ORD001"))

    def test_multi_account_recovery_does_not_duplicate_any_line(self) -> None:
        order = self.order(
            "ORD-RECOVER-MULTI",
            account="one@example.com|1\ntwo@example.com|2\nthree@example.com|3",
        )
        result = self.storage.record_purchase(order)
        original_product = result.product_file.read_bytes()
        original_history = result.history_file.read_bytes()

        with closing(sqlite3.connect(self.storage.database_file)) as connection:
            connection.execute(
                """
                UPDATE orders
                SET status = 'pending', product_written = 0,
                    history_written = 0, completed_at = NULL
                WHERE order_id = 'ORD-RECOVER-MULTI'
                """
            )
            connection.commit()

        self.assertEqual(self.storage.recover_pending(), 1)
        self.assertEqual(result.product_file.read_bytes(), original_product)
        self.assertEqual(result.history_file.read_bytes(), original_history)
        self.assertEqual(len(result.product_file.read_text(encoding="utf-8").splitlines()), 3)

    def test_history_uses_configured_vietnam_time(self) -> None:
        result = self.storage.record_purchase(self.order("ORD001"))
        history = result.history_file.read_text(encoding="utf-8")
        self.assertEqual(
            history,
            "2026-09-29 18:01:22 | order=ORD001 | user_id=123456 | @userA | "
            "Canva Pro 1 Tháng | account01@gmail.com|pass01\n",
        )


if __name__ == "__main__":
    unittest.main()
