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
from celery import Celery, Task # Keep Celery import here
from config import Config, PLAN_NAME_MAP
import stripe
import logging

# Initialize Flask extensions first (but not Celery yet)
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
cors = CORS()

# Configure Flask-Login
login_manager.login_view = 'main.login'
login_manager.login_message_category = 'info'

# Define make_celery helper function
def make_celery(app: Flask) -> Celery:
    """
    Configures and returns a Celery instance integrated with the Flask app.
    Reads broker/backend URLs from the Flask app's config, which should
    be populated from environment variables.
    """
    # Read URLs from Flask app config - Provide fallbacks just in case
    broker_url = app.config.get('CELERY_BROKER_URL', app.config.get('REDIS_URL', 'redis://localhost:6379/0'))
    backend_url = app.config.get('CELERY_RESULT_BACKEND', app.config.get('REDIS_URL', 'redis://localhost:6379/0'))
    
    if not broker_url or not backend_url:
        app.logger.error("CRITICAL: make_celery could not find Broker/Backend URL in app.config!")
        # Fallback again, though this shouldn't be needed if config is right
        broker_url = broker_url or 'redis://localhost:6379/0'
        backend_url = backend_url or 'redis://localhost:6379/0'

    # Create the Celery instance, configured with broker and backend
    celery_instance = Celery(
        app.import_name,
        broker=broker_url,
        backend=backend_url, # Ensure backend is set for chords
        include=['app.tasks'] # Include your tasks module
    )
    
    # Update Celery config with other settings from Flask config
    celery_instance.conf.update(app.config)
    
    # Define the ContextTask within make_celery's scope
    class ContextTask(celery_instance.Task):
        abstract = True
        def __call__(self, *args, **kwargs):
            # Ensure tasks run within the Flask app context
            with app.app_context():
                return self.run(*args, **kwargs)

    # Set the custom Task class for this Celery instance
    celery_instance.Task = ContextTask
    print(f"--- make_celery: Configured Celery with Broker: {celery_instance.conf.broker_url} ---")
    print(f"--- make_celery: Configured Celery with Backend: {celery_instance.conf.result_backend} ---")
    return celery_instance

# Define celery globally but initialize as None initially
# It will be properly initialized inside create_app using make_celery
celery = None

# --- ContextTask for WORKER PROCESS STARTUP ---
# This task needs access to the *configured* celery instance.
# Since the worker starts by importing this __init__.py, we need a way
# for it to get the configured instance AFTER create_app has run.
# This remains a challenge with the factory pattern if the worker doesn't
# explicitly call create_app itself.
# For now, we keep the previous ContextTask definition which creates its own app instance.
# This might mean the worker log still shows defaults initially, but the tasks *should*
# run in the correct context when executed.
class WorkerContextTask(Task): # Use base Task for the worker startup context
    abstract = True
    _flask_app = None

    @property
    def flask_app(self):
        if WorkerContextTask._flask_app is None or not hasattr(WorkerContextTask._flask_app, 'app_context'):
             print("--- Creating Flask app instance for Celery worker (WorkerContextTask) ---")
             app_config_class = Config
             try:
                 from flask import current_app
                 if current_app:
                     app_config_class = type(current_app.config)
             except RuntimeError:
                 pass
             # Create app, which will configure the global 'celery' variable via make_celery
             WorkerContextTask._flask_app = create_app(config_class=app_config_class)
             print(f"--- Flask app instance created/recreated for Celery worker: {id(WorkerContextTask._flask_app)} ---")
        return WorkerContextTask._flask_app

    def __call__(self, *args, **kwargs):
        with self.flask_app.app_context():
            # Inside the task execution, the global 'celery' should be configured
            return self.run(*args, **kwargs)

# Assign this task base ONLY IF running as a worker (might need adjustment)
# This is tricky; often the worker command itself points to the celery instance
# defined globally, which gets configured by create_app when the worker imports it.
# Let's comment this out for now and rely on the global celery instance being configured.
# celery.Task = WorkerContextTask


def create_app(config_class=Config):
    """Factory function to create and configure an instance of the Flask application."""
    global celery # Declare that we intend to modify the global celery variable
    print(f"--- create_app called (config_class: {config_class.__name__}) ---")

    app = Flask(__name__, instance_path=config_class.INSTANCE_PATH, instance_relative_config=False)
    
    # Load configuration from the specified config_class object.
    # This MUST include reading REDIS_URL from the environment.
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

    # --- Initialize and Configure Celery using the helper ---
    celery = make_celery(app)
    # --- End Celery Initialization ---

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
            # Check if the view function exists before trying to exempt
            view_func = app.view_functions.get(route_name)
            if view_func:
                csrf.exempt(view_func)
                app.logger.info(f"CSRF Exemption applied to {route_name} view.")
            else:
                app.logger.warning(f"--- WARNING: View function '{route_name}' not found for CSRF exemption. ---")

    # Register Context Processor
    @app.context_processor
    def inject_now():
        return {'now': datetime.now(timezone.utc)}

    # Log key config values
    app.logger.info(f"Database URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    # Log the URLs Celery *should* be using now
    app.logger.info(f"Celery Broker URL (from celery.conf): {celery.conf.broker_url}")
    app.logger.info(f"Celery Result Backend (from celery.conf): {celery.conf.result_backend}")
    app.logger.info(f"Stripe Publishable Key Loaded: {'Yes' if app.config.get('STRIPE_PUBLISHABLE_KEY') else 'No'}")
    # ... (log other keys) ...

    # Debug print for URL rules
    print("--- Registered URL Rules (Post Blueprint Registration) ---")
    print(app.url_map)
    print("----------------------------------------------------------")

    return app

