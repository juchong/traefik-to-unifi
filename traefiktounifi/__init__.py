import logging
import logging.handlers
import os

# ANSI color codes
RESET = "\033[0m"
GRAY = "\033[90m"
YELLOW = "\033[33m"
RED = "\033[31m"


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: GRAY,
        logging.INFO: "",
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: RED,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{RESET}"


# Create logger
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

handler = logging.StreamHandler()
formatter = ColorFormatter("%(asctime)s [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# Optional plain-text file log so the web UI (and the operator) can read history
# without docker-socket/root access. Best-effort: never fatal if the path is
# unwritable (e.g. /data not mounted).
_log_file = os.environ.get("LOG_FILE", "/data/traefik-to-unifi.log")
if _log_file:
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            _log_file,
            maxBytes=int(os.environ.get("LOG_FILE_MAX_BYTES", "1000000")),
            backupCount=int(os.environ.get("LOG_FILE_BACKUPS", "3")),
        )
        # No ANSI colors in the file.
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning(f"File logging disabled ({_log_file}): {e}")
