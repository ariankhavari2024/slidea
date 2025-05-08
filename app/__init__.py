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
from config import Config, PLAN_NAME_MAP
import stripe
import logging

# Initialize extensions first
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect() # Initialize CSRFProtect here
cors = CORS()

# Configure Flask-Login
login_manager.login_view = 'main.login'
login_manager.login_message_category = 'info'

# Celery Initialization
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
    app.config.from_object(config_class)
    app.config['PLAN_NAME_MAP'] = PLAN_NAME_MAP
    print(f"--- App Init: Added PLAN_NAME_MAP to app.config: {app.config.get('PLAN_NAME_MAP')} ---")

    app.logger.setLevel(logging.INFO)

    # Initialize Stripe API key
    stripe.api_key = app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        app.logger.warning("Stripe Secret Key is not configured. Stripe functionality will be disabled.")
    else:
        app.logger.info("Stripe Secret Key loaded for the app.")

    # Initialize Flask extensions with the app instance
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app) # Initialize CSRF protection for the app
    print("--- CSRF Protection Enabled Globally ---")
    cors.init_app(app)

    # Configure Celery
    celery.conf.update(app.config)
    celery.conf.broker_url = app.config.get('CELERY_BROKER_URL')
    celery.conf.result_backend = app.config.get('CELERY_RESULT_BACKEND')

    # Register 'before_request' hook
    @app.before_request
    def log_request_info():
        app.logger.debug(f"--> BEFORE_REQUEST: Path={request.path}, Method={request.method}")

    # Import models within app context
    with app.app_context():
        from . import models

    # --- Blueprint Registration ---
    # Import the blueprint AFTER initializing extensions but BEFORE applying exemptions
    from .routes import main as main_blueprint
    app.register_blueprint(main_blueprint)
    print("--- Registered main blueprint ---")

    # --- Apply CSRF exemptions AFTER blueprint registration ---
    # This ensures the view functions exist when exempt is called
    with app.app_context(): # Ensure exemptions happen within app context
        exempt_routes = ['main.stripe_webhook', 'main.process_plan_change']
        for route_name in exempt_routes:
            if route_name in app.view_functions:
                csrf.exempt(app.view_functions[route_name])
                app.logger.info(f"CSRF Exemption applied to {route_name} view.")
            else:
                # Use app.logger for consistency
                app.logger.warning(f"--- WARNING: View function '{route_name}' not found for CSRF exemption. Check route definition and blueprint registration. ---")

    # Register Context Processor
    @app.context_processor
    def inject_now():
        return {'now': datetime.now(timezone.utc)}

    # Log key config values
    app.logger.info(f"Database URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    app.logger.info(f"Celery Broker: {celery.conf.broker_url}")
    app.logger.info(f"Stripe Publishable Key Loaded: {'Yes' if app.config.get('STRIPE_PUBLISHABLE_KEY') else 'No'}")
    app.logger.info(f"Stripe Secret Key Loaded: {'Yes' if app.config.get('STRIPE_SECRET_KEY') else 'No'}")
    app.logger.info(f"Stripe Endpoint Secret Loaded: {'Yes' if app.config.get('STRIPE_ENDPOINT_SECRET') else 'No'}")
    # ... (log other keys as needed) ...

    # Debug print for URL rules
    print("--- Registered URL Rules (Post Blueprint Registration) ---")
    print(app.url_map)
    print("----------------------------------------------------------")

    return app
