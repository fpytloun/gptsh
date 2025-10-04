import logging


def setup_logging(level: str = "WARNING", fmt: str = "text"):  # text|json
    level = level.upper()
    format_str = (
        '[%(asctime)s] %(levelname)s %(name)s %(message)s'
        if fmt == 'text' else '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "msg": %(message)s}'
    )
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=format_str)

    # Optionally add redaction later
    return logging.getLogger("gptsh")
