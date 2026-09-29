"""Application entry point for the 24/7 MTProto order observer."""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from order_parser import OrderParser
from storage import PurchaseStorage
from telegram_listener import TelegramOrderListener


BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Load a small, dependency-free subset of .env syntax."""

    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path.name}:{line_number}: dòng .env phải có KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"{path.name}:{line_number}: tên biến không hợp lệ")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_setting_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def parse_id_list(value: str, setting_name: str) -> set[int]:
    ids: set[int] = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ValueError(f"{setting_name} chỉ nhận ID số, giá trị lỗi: {item!r}") from exc
    return ids


def parse_bool(value: str, setting_name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{setting_name} phải là true hoặc false")


class LocalTimeFormatter(logging.Formatter):
    def __init__(self, *args: object, timezone_name: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            if timezone_name in {"Asia/Ho_Chi_Minh", "Asia/Saigon"}:
                self.timezone = timezone(timedelta(hours=7), name=timezone_name)
            else:
                raise ValueError(
                    f"TIMEZONE không hợp lệ hoặc VPS thiếu timezone data: {timezone_name}"
                ) from exc

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=self.timezone)
        return moment.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


class FileDetailFormatter(LocalTimeFormatter):
    """Use a detailed event message in app.log while console stays compact."""

    def format(self, record: logging.LogRecord) -> str:
        original_message = record.msg
        original_args = record.args
        detail = getattr(record, "file_detail", None)
        if detail:
            record.msg = detail
            record.args = ()
        try:
            return super().format(record)
        finally:
            record.msg = original_message
            record.args = original_args


class CompactConsoleFormatter(logging.Formatter):
    """Keep tracebacks in app.log without flooding the Wispbyte console."""

    def format(self, record: logging.LogRecord) -> str:
        original_exc_info = record.exc_info
        original_exc_text = record.exc_text
        original_stack_info = record.stack_info
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info = original_exc_info
            record.exc_text = original_exc_text
            record.stack_info = original_stack_info


def configure_logging(log_dir: Path, level_name: str, timezone_name: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"LOG_LEVEL không hợp lệ: {level_name}")

    # Keep Unicode product names and status icons readable in VPS/Windows
    # consoles instead of letting a legacy code page crash the logger.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass

    console_formatter = CompactConsoleFormatter("%(message)s")
    file_formatter = FileDetailFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        timezone_name=timezone_name,
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(console_formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)
    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)
    # Keep the console focused on order backup events. Connection retries and
    # actual MTProto problems are still shown at WARNING/ERROR level.
    logging.getLogger("telethon").setLevel(logging.WARNING)


class SingleInstanceLock:
    """Prevent two observer processes from racing on append offsets."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"0")
            self._stream.flush()
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            raise RuntimeError("Đã có một tiến trình telegram_order_tool khác đang chạy") from exc

    def close(self) -> None:
        if self._stream.closed:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()

    def __enter__(self) -> "SingleInstanceLock":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def run() -> None:
    load_dotenv(BASE_DIR / ".env")

    data_dir = resolve_setting_path(os.getenv("DATA_DIR", "data"))
    state_dir = resolve_setting_path(os.getenv("STATE_DIR", "state"))
    log_dir = resolve_setting_path(os.getenv("LOG_DIR", "logs"))
    timezone_name = os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh")
    configure_logging(log_dir, os.getenv("LOG_LEVEL", "INFO"), timezone_name)
    logger = logging.getLogger(__name__)

    api_id_text = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not api_id_text:
        raise ValueError("Hãy điền API_ID lấy từ my.telegram.org/apps")
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise ValueError("API_ID phải là một dãy số") from exc
    if api_id <= 0:
        raise ValueError("API_ID phải là số nguyên dương")
    if not api_hash:
        raise ValueError("Hãy điền API_HASH lấy từ my.telegram.org/apps")
    if not token:
        raise ValueError("Hãy điền BOT_TOKEN của bot bán hàng")
    if ":" not in token or not token.split(":", 1)[0].isdigit():
        raise ValueError("BOT_TOKEN không đúng định dạng <bot_id>:<secret>")

    source_chat_ids = os.getenv("SOURCE_CHAT_IDS", "")
    allowed_chat_ids = parse_id_list(source_chat_ids, "SOURCE_CHAT_IDS")
    only_outgoing = parse_bool(os.getenv("ONLY_OUTGOING", "true"), "ONLY_OUTGOING")

    # Use the public numeric bot id in the session filename. Changing to a
    # different bot automatically creates a separate session; secrets are never
    # embedded in paths or logs.
    bot_id = token.split(":", 1)[0]
    session_setting = os.getenv("SESSION_FILE", "").strip()
    session_file = (
        resolve_setting_path(session_setting)
        if session_setting
        else state_dir / f"mtproto_bot_{bot_id}"
    )

    with SingleInstanceLock(state_dir / "listener.lock"):
        storage = PurchaseStorage(
            data_dir=data_dir,
            state_dir=state_dir,
            timezone_name=timezone_name,
        )
        recovered = storage.recover_pending()
        if recovered:
            logger.warning("♻ [RECOVERY DONE] | orders=%s", recovered)

        listener = TelegramOrderListener(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=token,
            session_file=session_file,
            parser=OrderParser(),
            storage=storage,
            allowed_chat_ids=allowed_chat_ids,
            only_outgoing=only_outgoing,
        )

        def request_stop(signum: int, _frame: object) -> None:
            logger.info("⏹ [STOPPING] | signal=%s", signum)
            listener.stop()

        for signal_name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, signal_name):
                signal.signal(getattr(signal, signal_name), request_stop)

        listener.run_forever()


def main() -> int:
    try:
        run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger(__name__).exception("❌ [FATAL] | observer stopped")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
