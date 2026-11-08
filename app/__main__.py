import uvicorn

from app.config import Settings
from app.logging import configure_logging


if __name__ == "__main__":
    configure_logging(Settings().log_level)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, log_config=None)
