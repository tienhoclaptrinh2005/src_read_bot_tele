"""Durable append-only storage for successful Telegram orders."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from order_parser import SuccessfulOrder


LOGGER = logging.getLogger(__name__)
_PROCESS_LOCK = threading.RLock()


class DuplicateOrderConflict(RuntimeError):
    """The same order_id was reused with different order data."""


class StorageConsistencyError(RuntimeError):
    """An output file changed in a way that cannot be recovered safely."""


class _InterprocessFileLock:
    """Cross-platform advisory lock used around TXT append sequences."""

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._stream: Any | None = None

    def __enter__(self) -> "_InterprocessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._stream = stream
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise TimeoutError(
                        f"Không lấy được storage lock sau {self.timeout:.0f} giây"
                    ) from exc
                time.sleep(0.1)

    def __exit__(self, *_: object) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None


@dataclass(frozen=True, slots=True)
class RecordResult:
    order_id: str
    saved: bool
    duplicate: bool
    product_file: Path
    history_file: Path


@dataclass(frozen=True, slots=True)
class StorageStats:
    orders: int
    accounts: int
    products: int


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _safe_filename_stem(product: str) -> str:
    """Preserve normal product names and neutralize filesystem metacharacters."""

    name = unicodedata.normalize("NFC", product).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    if not name:
        raise ValueError("Tên sản phẩm không tạo được tên file hợp lệ")
    if name.upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


class PurchaseStorage:
    """Append accounts and history while deduplicating globally by order_id.

    SQLite is metadata only. The requested human-readable TXT files remain the
    primary output. Offsets and pending states let startup recovery distinguish
    a completed append from a crash between writing the TXT and marking the
    order complete.
    """

    def __init__(
        self,
        data_dir: str | Path,
        state_dir: str | Path,
        *,
        timezone_name: str = "Asia/Ho_Chi_Minh",
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.history_file = self.data_dir / "purchase_history.txt"
        self.database_file = self.state_dir / "orders.sqlite3"
        self._storage_lock_file = self.state_dir / "storage.lock"
        # All instances in one process share the same append lock. This matters
        # when an existing bot uses the convenience API from several workers.
        self._lock = _PROCESS_LOCK

        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            # Windows Python often has no system IANA database. Vietnam has
            # observed UTC+07:00 without DST since 1975, so the configured
            # default remains correct without requiring a third-party package.
            if timezone_name in {"Asia/Ho_Chi_Minh", "Asia/Saigon"}:
                self.timezone = timezone(timedelta(hours=7), name=timezone_name)
            else:
                raise ValueError(
                    f"TIMEZONE không hợp lệ hoặc VPS thiếu timezone data: {timezone_name}"
                ) from exc

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_file,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product TEXT PRIMARY KEY,
                    filename TEXT NOT NULL COLLATE NOCASE UNIQUE
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    product TEXT NOT NULL,
                    account TEXT NOT NULL,
                    purchased_at TEXT NOT NULL,
                    source_update_id INTEGER,
                    product_file TEXT NOT NULL,
                    product_offset INTEGER,
                    product_payload TEXT,
                    product_written INTEGER NOT NULL DEFAULT 0,
                    history_offset INTEGER,
                    history_payload TEXT,
                    history_written INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_orders_status
                ON orders(status);
                """
            )

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def _product_filename(
        self,
        connection: sqlite3.Connection,
        product: str,
    ) -> str:
        existing = connection.execute(
            "SELECT filename FROM products WHERE product = ?",
            (product,),
        ).fetchone()
        if existing:
            return str(existing["filename"])

        stem = _safe_filename_stem(product)
        candidate = f"{stem}.txt"
        if candidate.casefold() == self.history_file.name.casefold():
            digest = hashlib.sha256(product.encode("utf-8")).hexdigest()[:8]
            candidate = f"{stem}--{digest}.txt"

        collision = connection.execute(
            "SELECT product FROM products WHERE filename = ? COLLATE NOCASE",
            (candidate,),
        ).fetchone()
        if collision and collision["product"] != product:
            digest = hashlib.sha256(product.encode("utf-8")).hexdigest()[:8]
            candidate = f"{stem}--{digest}.txt"

        connection.execute(
            "INSERT INTO products(product, filename) VALUES (?, ?)",
            (product, candidate),
        )
        return candidate

    def _claim(self, order: SuccessfulOrder) -> tuple[bool, sqlite3.Row]:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            self._begin(connection)
            try:
                existing = connection.execute(
                    "SELECT * FROM orders WHERE order_id = ?",
                    (order.order_id,),
                ).fetchone()
                if existing:
                    self._assert_same_order(existing, order)
                    connection.commit()
                    return False, existing

                filename = self._product_filename(connection, order.product)
                connection.execute(
                    """
                    INSERT INTO orders(
                        order_id, user_id, username, product, account,
                        purchased_at, source_update_id, product_file, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.order_id,
                        order.user_id,
                        order.username,
                        order.product,
                        order.account,
                        order.purchased_at.isoformat(),
                        order.source_update_id,
                        filename,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE order_id = ?",
                    (order.order_id,),
                ).fetchone()
                connection.commit()
                assert row is not None
                return True, row
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _assert_same_order(row: sqlite3.Row, order: SuccessfulOrder) -> None:
        fields = {
            "user_id": order.user_id,
            "username": order.username,
            "product": order.product,
            "account": order.account,
        }
        mismatches = [field for field, value in fields.items() if row[field] != value]
        if mismatches:
            raise DuplicateOrderConflict(
                f"order_id={order.order_id!r} đã tồn tại nhưng khác dữ liệu ở: "
                + ", ".join(mismatches)
            )

    def record_purchase(self, order: SuccessfulOrder) -> RecordResult:
        """Store one successful order, or safely ignore an identical retry."""

        with self._lock, _InterprocessFileLock(self._storage_lock_file):
            created, row = self._claim(order)
            if row["status"] != "complete":
                row = self._finish_order(order.order_id)

            return RecordResult(
                order_id=order.order_id,
                saved=created,
                duplicate=not created,
                product_file=self.data_dir / str(row["product_file"]),
                history_file=self.history_file,
            )

    def _get_order(self, order_id: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Không tìm thấy order_id={order_id!r}")
        return row

    def _finish_order(self, order_id: str) -> sqlite3.Row:
        self._ensure_product_line(order_id)
        self._ensure_history_line(order_id)
        with closing(self._connect()) as connection:
            self._begin(connection)
            try:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = 'complete', completed_at = ?
                    WHERE order_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        assert row is not None
        return row

    @staticmethod
    def _needs_separator(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            return stream.read(1) != b"\n"

    def _prepare_append(
        self,
        *,
        order_id: str,
        target: Path,
        payload_column: str,
        offset_column: str,
        content: str,
    ) -> tuple[int, str]:
        target.parent.mkdir(parents=True, exist_ok=True)
        prefix = "\n" if self._needs_separator(target) else ""
        payload = f"{prefix}{content}\n"
        offset = target.stat().st_size if target.exists() else 0

        with closing(self._connect()) as connection:
            self._begin(connection)
            try:
                row = connection.execute(
                    f"SELECT {offset_column}, {payload_column} FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Không tìm thấy order_id={order_id!r}")
                if row[offset_column] is None:
                    connection.execute(
                        f"""
                        UPDATE orders
                        SET {offset_column} = ?, {payload_column} = ?
                        WHERE order_id = ?
                        """,
                        (offset, payload, order_id),
                    )
                else:
                    offset = int(row[offset_column])
                    payload = str(row[payload_column])
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return offset, payload

    @staticmethod
    def _append_or_confirm(target: Path, offset: int, payload: str) -> None:
        expected = payload.encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)

        current_size = target.stat().st_size if target.exists() else 0
        if current_size < offset:
            raise StorageConsistencyError(
                f"File {target} ngắn hơn offset đã lưu ({current_size} < {offset})"
            )

        existing_tail = b""
        if current_size > offset:
            with target.open("rb") as stream:
                stream.seek(offset)
                existing_tail = stream.read(len(expected))

        if existing_tail == expected:
            return
        if existing_tail and not expected.startswith(existing_tail):
            raise StorageConsistencyError(
                f"Nội dung {target} tại offset {offset} đã bị thay đổi; không append để tránh trùng/sai dữ liệu"
            )

        remaining = expected[len(existing_tail) :]
        if not remaining:
            return

        # Normal writes deliberately use text append mode "a" as requested.
        # A rare partial UTF-8 write is resumed in binary append mode.
        if not existing_tail:
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            with target.open("ab") as stream:
                stream.write(remaining)
                stream.flush()
                os.fsync(stream.fileno())

        with target.open("rb") as stream:
            stream.seek(offset)
            actual = stream.read(len(expected))
        if actual != expected:
            raise StorageConsistencyError(f"Không xác minh được dữ liệu vừa append vào {target}")

    def _mark_written(self, order_id: str, column: str) -> None:
        with closing(self._connect()) as connection:
            self._begin(connection)
            try:
                connection.execute(
                    f"UPDATE orders SET {column} = 1 WHERE order_id = ?",
                    (order_id,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _ensure_product_line(self, order_id: str) -> None:
        row = self._get_order(order_id)
        if row["product_written"]:
            return
        target = self.data_dir / str(row["product_file"])
        offset, payload = self._prepare_append(
            order_id=order_id,
            target=target,
            payload_column="product_payload",
            offset_column="product_offset",
            content=str(row["account"]),
        )
        self._append_or_confirm(target, offset, payload)
        self._mark_written(order_id, "product_written")

    def _history_content(self, row: sqlite3.Row) -> str:
        timestamp = datetime.fromisoformat(str(row["purchased_at"]))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        local_time = timestamp.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
        user_id = str(row["user_id"]) or "-"
        username = str(row["username"]).lstrip("@") or "-"
        accounts = [
            line.strip()
            for line in str(row["account"]).splitlines()
            if line.strip()
        ]
        quantity = len(accounts)
        history_lines: list[str] = []
        for index, account in enumerate(accounts, 1):
            item = f" | item={index}/{quantity}" if quantity > 1 else ""
            history_lines.append(
                f"{local_time} | order={row['order_id']}{item} | "
                f"user_id={user_id} | @{username} | {row['product']} | {account}"
            )
        return "\n".join(history_lines)

    def _ensure_history_line(self, order_id: str) -> None:
        row = self._get_order(order_id)
        if row["history_written"]:
            return
        offset, payload = self._prepare_append(
            order_id=order_id,
            target=self.history_file,
            payload_column="history_payload",
            offset_column="history_offset",
            content=self._history_content(row),
        )
        self._append_or_confirm(self.history_file, offset, payload)
        self._mark_written(order_id, "history_written")

    def recover_pending(self) -> int:
        """Finish interrupted writes before the listener accepts new orders."""

        with self._lock, _InterprocessFileLock(self._storage_lock_file):
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT order_id FROM orders WHERE status != 'complete' ORDER BY created_at"
                ).fetchall()
            for row in rows:
                order_id = str(row["order_id"])
                LOGGER.warning("♻ [RECOVERY] | order=%s", order_id)
                self._finish_order(order_id)
            return len(rows)

    def is_processed(self, order_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return bool(row and row["status"] == "complete")

    def get_stats(self) -> StorageStats:
        """Return compact totals for the management console."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS orders,
                    COUNT(DISTINCT product) AS products,
                    COALESCE(SUM(
                        1 + LENGTH(account) - LENGTH(REPLACE(account, CHAR(10), ''))
                    ), 0) AS accounts
                FROM orders
                WHERE status = 'complete'
                """
            ).fetchone()
        assert row is not None
        return StorageStats(
            orders=int(row["orders"]),
            accounts=int(row["accounts"]),
            products=int(row["products"]),
        )


def record_successful_order(
    *,
    order_id: object,
    user_id: object = "",
    username: object = "",
    product: object,
    account: object,
    purchased_at: datetime | None = None,
    source_update_id: int | None = None,
    data_dir: str | Path = "data",
    state_dir: str | Path = "state",
    timezone_name: str = "Asia/Ho_Chi_Minh",
) -> RecordResult:
    """Small integration API for an existing sales bot."""

    order = SuccessfulOrder(
        order_id=order_id,
        user_id=user_id,
        username=username,
        product=product,
        account=account,
        purchased_at=purchased_at or datetime.now(timezone.utc),
        source_update_id=source_update_id,
    )
    storage = PurchaseStorage(data_dir, state_dir, timezone_name=timezone_name)
    return storage.record_purchase(order)
