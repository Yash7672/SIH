import logging
import sys

_SENSITIVE_KEYS = ("password", "token", "secret", "authorization", "jwt", "api_key")


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            lowered = msg.lower()
            if any(k in lowered for k in _SENSITIVE_KEYS) and len(msg) > 400:
                record.msg = "[redacted potentially sensitive log]"
                record.args = ()
        except Exception:
            pass
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
