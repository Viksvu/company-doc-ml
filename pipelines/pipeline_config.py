import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = "postgresql://postgres:2219@localhost:5432/company_app"


def load_env_file() -> None:
    env_path = APP_DIR / ".env"

    if not env_path.is_file():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")

        os.environ.setdefault(key, value)


def get_database_url() -> str:
    load_env_file()

    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(APP_DIR))
    except ValueError:
        return str(path)


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None

    path = Path(value)

    if path.is_absolute():
        return path

    return APP_DIR / path
