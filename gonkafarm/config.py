"""Env / .env loader + file path config (env-based)."""
import os

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env(path=None):
    p = path or os.path.join(_DIR, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

RESI_POOL_FILE = os.environ.get("RESI_POOL_FILE", "residential_alive.txt")
RESULT_FILE = os.environ.get("RESULT_FILE", "gonka_email_accounts.txt")
PROGRESS_DIR = os.environ.get(
    "PROGRESS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "progress"),
)
