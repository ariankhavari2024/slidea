# config.py
import os
from dotenv import load_dotenv

# Determine the absolute path to the directory containing this config.py file
basedir = os.path.abspath(os.path.dirname(__file__))
# Construct the path to the .env file located one level above the app directory
dotenv_path = os.path.join(basedir, '..', '.env')

# Load environment variables from .env file FIRST
# override=True ensures environment variables set directly (like by Render) take precedence
load_dotenv(dotenv_path=dotenv_path, verbose=True, override=True)
print(f"--- Config: Attempted to load .env from: {dotenv_path} ---")

class Config:
    """Base configuration class."""
    # Flask specific config
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess'
    BASE_DIR = os.path.abspath(os.path.join(basedir, '..'))
    INSTANCE_PATH = os.path.join(BASE_DIR, 'instance')
    if not os.path.exists(INSTANCE_PATH):
        try:
            os.makedirs(INSTANCE_PATH)
            print(f"Created instance folder at: {INSTANCE_PATH}")
        except OSError as e:
            print(f"Error creating instance folder at {INSTANCE_PATH}: {e}")

    # SQLAlchemy config
    # Prioritize DATABASE_URL from environment (set by Render)
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(INSTANCE_PATH, 'app.db') # Fallback to SQLite
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # OpenAI Config
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

    # --- Celery Config - PRIORITIZE RENDER'S REDIS_URL ---
    # Render automatically injects REDIS_URL for linked Redis services
    REDIS_URL = os.environ.get('REDIS_URL') # Read the Render-injected URL
    CELERY_BROKER_URL = REDIS_URL or 'redis://localhost:6379/0' # Use REDIS_URL if available, else fallback
    CELERY_RESULT_BACKEND = REDIS_URL or 'redis://localhost:6379/0' # Use REDIS_URL if available, else fallback
    # --- End Celery Config ---

    # Logging Config
    LOG_FILE = os.path.join(INSTANCE_PATH, 'app.log')
    PROMPT_LOG_FILE = os.path.join(INSTANCE_PATH, 'prompts.log')

    # --- Stripe Configuration ---
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
    STRIPE_ENDPOINT_SECRET = os.environ.get('STRIPE_ENDPOINT_SECRET')
    STRIPE_PRICE_ID_PRO = os.environ.get('STRIPE_PRICE_ID_PRO')
    STRIPE_PRICE_ID_CREATOR = os.environ.get('STRIPE_PRICE_ID_CREATOR')
    # --- End Stripe Configuration ---

    # Define credit amounts per plan (centralized)
    CREDITS_PER_PLAN = {
        'free': 400,
        'pro': 1250,
        'creator': 2500,
        'unknown': 0
    }
    # Define credits per action
    CREDITS_PER_SLIDE = 25
    CREDITS_PER_REGENERATE = 25

# --- DEFINE PLAN_NAME_MAP *AFTER* the Config class and load_dotenv ---
# This ensures os.environ.get has been called
PLAN_NAME_MAP = {
    k: v for k, v in {
        os.environ.get('STRIPE_PRICE_ID_PRO'): 'pro',
        os.environ.get('STRIPE_PRICE_ID_CREATOR'): 'creator'
    }.items() if k is not None # Only include if the env var is set
}
print(f"--- Config: Loaded PLAN_NAME_MAP: {PLAN_NAME_MAP} ---")

# Add warnings if essential Stripe Price IDs are missing
if not os.environ.get('STRIPE_PRICE_ID_PRO'):
    print("--- Config WARNING: STRIPE_PRICE_ID_PRO is not set in environment! ---")
if not os.environ.get('STRIPE_PRICE_ID_CREATOR'):
    print("--- Config WARNING: STRIPE_PRICE_ID_CREATOR is not set in environment! ---")
if not PLAN_NAME_MAP:
     print("--- Config WARNING: PLAN_NAME_MAP is empty! Check Stripe Price ID env vars. ---")

