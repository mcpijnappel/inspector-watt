import logging

import uvicorn

from .api import create_app
from .config import Config


def main() -> None:
    cfg = Config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(cfg), host="0.0.0.0", port=cfg.http_port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
