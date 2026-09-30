import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("BUDDY_LOG", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    from .main import PORT, create_app
    uvicorn.run(create_app(), host=os.environ.get("BUDDY_HOST", "0.0.0.0"), port=PORT,
                log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
