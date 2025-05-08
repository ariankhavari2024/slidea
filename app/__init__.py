# app/__init__.py
import os
from datetime import datetime, timezone
import redis
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
from celery import Celery, Task
from config import Config, PLAN_NAME_MAP # Import Config and PLAN_NAME_MAP
import stripe
import logging

# Initialize extensions first
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
cors = CORS()

# Configure Flask-Login
login_manager.login_view = 'main.login'
login_manager.login_message_category = 'info'

# Celery Initialization - Define the instance
# Configuration will be explicitly set inside create_app using os.environ
celery = Celery(__name__, include=['app.tasks'])

# ContextTask Definition for Celery
class ContextTask(celery.Task):
    abstract = True
    _flask_app = None

    @property
    def flask_app(self):
        if ContextTask._flask_app is None or not hasattr(ContextTask._flask_app, 'app_context'):
             print("--- Creating Flask app instance for Celery worker (ContextTask) ---")
             app_config_class = Config
             try:
                 from flask import current_app
                 if current_app:
                     app_config_class = type(current_app.config)
             except RuntimeError:
                 pass
             ContextTask._flask_app = create_app(config_class=app_config_class)
             print(f"--- Flask app instance created/recreated for Celery: {id(ContextTask._flask_app)} ---")
        return ContextTask._flask_app

    def __call__(self, *args, **kwargs):
        with self.flask_app.app_context():
            return self.run(*args, **kwargs)

celery.Task = ContextTask


def create_app(config_class=Config):
    """Factory function to create and configure an instance of the Flask application."""
    print(f"--- create_app called (config_class: {config_class.__name__}) ---")

    app = Flask(__name__, instance_path=config_class.INSTANCE_PATH, instance_relative_config=False)
    
    # Load configuration from the specified config_class object.
    app.config.from_object(config_class)
    
    # Add PLAN_NAME_MAP to app.config
    app.config['PLAN_NAME_MAP'] = PLAN_NAME_MAP
    print(f"--- App Init: Added PLAN_NAME_MAP to app.config: {app.config.get('PLAN_NAME_MAP')} ---")

    app.logger.setLevel(logging.INFO)

    # Initialize Stripe API key
    stripe.api_key = app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        app.logger.warning("Stripe Secret Key is not configured.")
    else:
        app.logger.info("Stripe Secret Key loaded.")

    # Initialize Flask extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    print("--- CSRF Protection Enabled Globally ---")
    cors.init_app(app)

    # --- Directly Configure Celery Instance using Environment Variables ---
    # Read Broker/Backend URLs directly from environment variables
    # Render should be injecting REDIS_URL. We use that for both.
    broker_url_from_env = os.environ.get('REDIS_URL')
    backend_url_from_env = os.environ.get('REDIS_URL') # Use Redis for backend too

    # Fallback only if environment variable is missing (shouldn't happen on Render)
    if not broker_url_from_env:
        app.logger.error("CRITICAL: REDIS_URL environment variable not found! Falling back to localhost for Celery Broker.")
        broker_url_from_env = 'redis://localhost:6379/0'
    if not backend_url_from_env:
        app.logger.error("CRITICAL: REDIS_URL environment variable not found! Falling back to localhost for Celery Backend.")
        backend_url_from_env = 'redis://localhost:6379/0'

    # Update the Celery instance configuration directly
    celery.conf.broker_url = broker_url_from_env
    celery.conf.result_backend = backend_url_from_env
    # Optional: Update other Celery settings if needed
    # celery.conf.update(app.config) # You might still want this for other settings

    print(f"--- Celery instance DIRECTLY configured with Broker: {celery.conf.broker_url} ---")
    print(f"--- Celery instance DIRECTLY configured with Backend: {celery.conf.result_backend} ---")
    # --- End Direct Celery Configuration ---

    # Register 'before_request' hook
    @app.before_request
    def log_request_info():
        app.logger.debug(f"--> BEFORE_REQUEST: Path={request.path}, Method={request.method}")

    # Import models within app context
    with app.app_context():
        from . import models

    # --- Blueprint Registration ---
    from .routes import main as main_blueprint
    app.register_blueprint(main_blueprint)
    print("--- Registered main blueprint ---")

    # --- Apply CSRF exemptions AFTER blueprint registration ---
    with app.app_context():
        exempt_routes = ['main.stripe_webhook', 'main.process_plan_change']
        for route_name in exempt_routes:
            if route_name in app.view_functions:
                csrf.exempt(app.view_functions[route_name])
                app.logger.info(f"CSRF Exemption applied to {route_name} view.")
            else:
                app.logger.warning(f"--- WARNING: View function '{route_name}' not found for CSRF exemption. ---")

    # Register Context Processor
    @app.context_processor
    def inject_now():
        return {'now': datetime.now(timezone.utc)}

    # Log key config values
    app.logger.info(f"Database URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    app.logger.info(f"Celery Broker URL (from celery.conf): {celery.conf.broker_url}") # Log from Celery conf
    app.logger.info(f"Celery Result Backend (from celery.conf): {celery.conf.result_backend}") # Log from Celery conf
    app.logger.info(f"Stripe Publishable Key Loaded: {'Yes' if app.config.get('STRIPE_PUBLISHABLE_KEY') else 'No'}")
    # ... (log other keys) ...

    # Debug print for URL rules
    print("--- Registered URL Rules (Post Blueprint Registration) ---")
    print(app.url_map)
    print("----------------------------------------------------------")

    return app
