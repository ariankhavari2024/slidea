# config.py
import os
from dotenv import load_dotenv

# ----- .env loading (Render envs still win because override=True) -----
basedir = os.path.abspath(os.path.dirname(__file__))
dotenv_path = os.path.join(basedir, "..", ".env")
load_dotenv(dotenv_path=dotenv_path, verbose=True, override=True)
print(f"--- Config: Attempted to load .env from: {dotenv_path} ---")

class Config:
    """Base configuration."""
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY") or "you-will-never-guess"
    PREFERRED_URL_SCHEME = "https"  # so url_for(..., _external=True) uses https
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB uploads safety limit

    # Paths
    BASE_DIR = os.path.abspath(os.path.join(basedir, ".."))
    INSTANCE_PATH = os.path.join(BASE_DIR, "instance")
    if not os.path.exists(INSTANCE_PATH):
        try:
            os.makedirs(INSTANCE_PATH, exist_ok=True)
            print(f"Created instance folder at: {INSTANCE_PATH}")
        except OSError as e:
            print(f"Error creating instance folder at {INSTANCE_PATH}: {e}")

    # Database (Render injects DATABASE_URL; fallback for local dev)
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or \
        "sqlite:///" + os.path.join(INSTANCE_PATH, "app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Redis (Render injects REDIS_URL)
    REDIS_URL = os.environ.get("REDIS_URL")

    # OpenAI
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

    # Logging (files land in /app/instance in container)
    LOG_FILE = os.path.join(INSTANCE_PATH, "app.log")
    PROMPT_LOG_FILE = os.path.join(INSTANCE_PATH, "prompts.log")

    # Stripe
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
    STRIPE_ENDPOINT_SECRET = os.environ.get("STRIPE_ENDPOINT_SECRET")
    STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO")
    STRIPE_PRICE_ID_CREATOR = os.environ.get("STRIPE_PRICE_ID_CREATOR")

    # Credits config
    CREDITS_PER_PLAN = {
        "free": 400,
        "pro": 1250,
        "creator": 2500,
        "unknown": 0,
    }
    CREDITS_PER_SLIDE = 25
    CREDITS_PER_REGENERATE = 25

# --- Map Stripe price IDs to plan names after envs are loaded ---
PLAN_NAME_MAP = {
    k: v for k, v in {
        os.environ.get("STRIPE_PRICE_ID_PRO"): "pro",
        os.environ.get("STRIPE_PRICE_ID_CREATOR"): "creator",
    }.items() if k is not None
}
print(f"--- Config: Loaded PLAN_NAME_MAP: {PLAN_NAME_MAP} ---")

# --- Warnings for missing Stripe config (non-fatal) ---
if not os.environ.get("STRIPE_PRICE_ID_PRO"):
    print("--- Config WARNING: STRIPE_PRICE_ID_PRO is not set ---")
if not os.environ.get("STRIPE_PRICE_ID_CREATOR"):
    print("--- Config WARNING: STRIPE_PRICE_ID_CREATOR is not set ---")
if not PLAN_NAME_MAP:
    print("--- Config WARNING: PLAN_NAME_MAP is empty! Check Stripe Price ID env vars. ---")

# --- MinIO/S3 sanity checks (non-fatal, just helpful logs) ---
if not os.environ.get("S3_ENDPOINT"):
    print("--- Config WARNING: S3_ENDPOINT not set (MinIO) ---")
if not os.environ.get("S3_BUCKET"):
    print("--- Config WARNING: S3_BUCKET not set (MinIO) ---")

# --- Celery env fallbacks (so worker boots even if not explicitly set elsewhere) ---
os.environ.setdefault("CELERY_BROKER_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
os.environ.setdefault("CELERY_RESULT_BACKEND", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
if not os.environ.get("REDIS_URL"):
    print("--- Config WARNING: REDIS_URL not set; Celery will try localhost for dev ---")
