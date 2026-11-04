"""Install logging before Qt and application imports, including windowed builds."""
import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from app_paths import DATA_ROOT, ensure_data_dirs

_stream = None

def configure_logging():
    global _stream
    ensure_data_dirs()
    handler = RotatingFileHandler(DATA_ROOT / "logs/startup.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    _stream = (DATA_ROOT / "logs/console.log").open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _stream
    if sys.stderr is None:
        sys.stderr = _stream
    faulthandler.enable(_stream)
    def exception_hook(kind, value, tb):
        logging.critical("Unhandled exception", exc_info=(kind, value, tb))
        handler.flush()
    sys.excepthook = exception_hook
    threading.excepthook = lambda args: exception_hook(args.exc_type, args.exc_value, args.exc_traceback)
