from __future__ import annotations

import logging
import unittest

from main import CompactConsoleFormatter, ConsoleNoiseFilter, FileDetailFormatter


class LoggingFormatterTests(unittest.TestCase):
    @staticmethod
    def record() -> logging.LogRecord:
        record = logging.LogRecord(
            name="telegram_listener",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="✅ [SOLD ] x3 · ORDER123 · Canva Pro",
            args=(),
            exc_info=None,
        )
        record.file_detail = (
            "order saved | order=ORDER123 | accounts=3 | "
            "product_file=/home/container/data/Canva Pro.txt"
        )
        return record

    def test_console_is_compact_and_file_is_detailed(self) -> None:
        record = self.record()
        console = CompactConsoleFormatter("%(message)s").format(record)
        file_text = FileDetailFormatter(
            "%(levelname)s | %(name)s | %(message)s",
            timezone_name="Asia/Ho_Chi_Minh",
        ).format(record)

        self.assertEqual(console, "✅ [SOLD ] x3 · ORDER123 · Canva Pro")
        self.assertIn("order saved | order=ORDER123", file_text)
        self.assertIn("product_file=/home/container/data/Canva Pro.txt", file_text)
        self.assertNotIn("product_file=", console)

    def test_telethon_transport_noise_is_hidden_only_from_console(self) -> None:
        noise_filter = ConsoleNoiseFilter()
        telethon_record = logging.LogRecord(
            name="telethon.network.mtprotosender",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Server sent a very old message",
            args=(),
            exc_info=None,
        )

        self.assertFalse(noise_filter.filter(telethon_record))
        self.assertTrue(noise_filter.filter(self.record()))


if __name__ == "__main__":
    unittest.main()
