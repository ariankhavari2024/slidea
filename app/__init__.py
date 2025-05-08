# app/__init__.py
import os
from datetime import datetime, timezone # Ensure timezone is imported
import redis # Ensure redis is imported if used by Celery or elsewhere
from flask import Flask, request # Import request for before_request hook
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
from celery import Celery, Task # Ensure Celery and Task are imported
from config import Config, PLAN_NAME_MAP # Import Config and PLAN_NAME_MAP
import stripe
import logging

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
cors = CORS()

# Configure Flask-Login
login_manager.login_view = 'main.login' # The route to redirect to if @login_required fails
login_manager.login_message_category = 'info' # Flash message category for login_required

# Celery Initialization
# The __name__ argument is the name of the current module.
# This is needed so that Celery can automatically find tasks in the tasks.py module.
celery = Celery(__name__, include=['app.tasks'])


# ContextTask Definition for Celery
# This custom Celery Task class ensures that Flask's application context
# is available when Celery tasks are executed. This is crucial for tasks
# that need to access Flask's configuration (app.config), database (db),
# or other Flask extensions.
class ContextTask(celery.Task):
    abstract = True  # Marks this class as abstract, so it's not registered as a task itself.
    _flask_app = None # Class variable to hold the Flask app instance for the Celery worker.

    @property
    def flask_app(self):
        """
        Provides a Flask app instance. If one doesn't exist or is not usable,
        it creates a new one. This is important for Celery workers that run
        in separate processes from the main Flask app.
        """
        if ContextTask._flask_app is None or not hasattr(ContextTask._flask_app, 'app_context'):
             print("--- Creating Flask app instance for Celery worker (ContextTask) ---")
             # Determine the config class to use. Default to Config.
             # If a Flask app is currently active (e.g., during web request), use its config type.
             app_config_class = Config
             try:
                 from flask import current_app # Attempt to import current_app
                 if current_app: # Check if current_app exists (i.e., we are in a Flask app context)
                     app_config_class = type(current_app.config)
             except RuntimeError:
                 # This occurs if current_app is accessed outside of an active Flask app context,
                 # which is expected in a standalone Celery worker.
                 pass # No app context available, use default Config.
             
             # Create a new Flask app instance using the determined config class.
             ContextTask._flask_app = create_app(config_class=app_config_class)
             print(f"--- Flask app instance created/recreated for Celery: {id(ContextTask._flask_app)} ---")
        return ContextTask._flask_app

    def __call__(self, *args, **kwargs):
        """
        Overrides the default call method of a Celery task.
        It ensures that the task's `run` method is executed within
        the Flask application context.
        """
        with self.flask_app.app_context():
            return self.run(*args, **kwargs)

# Set the custom ContextTask as the base for all Celery tasks.
celery.Task = ContextTask


def create_app(config_class=Config):
    """
    Factory function to create and configure an instance of the Flask application.
    This pattern is useful for creating multiple app instances (e.g., for testing)
    or for managing configurations more cleanly.

    Args:
        config_class: The configuration class to use for the app (e.g., Config, TestConfig).
                      Defaults to the main Config class.

    Returns:
        A configured Flask application instance.
    """
    print(f"--- create_app called (config_class: {config_class.__name__}) ---")

    # Create the Flask app instance.
    # instance_path specifies the path to the instance folder.
    # instance_relative_config=False means config files are loaded relative to the app root.
    app = Flask(__name__, instance_path=config_class.INSTANCE_PATH, instance_relative_config=False)
    
    # Load configuration from the specified config_class object.
    app.config.from_object(config_class)

    # Add PLAN_NAME_MAP to app.config for easy access in routes/templates
    # This ensures that the map (populated from environment variables in config.py)
    # is available throughout the application.
    app.config['PLAN_NAME_MAP'] = PLAN_NAME_MAP
    print(f"--- App Init: Added PLAN_NAME_MAP to app.config: {app.config.get('PLAN_NAME_MAP')} ---")

    # Set the logging level for the app's logger.
    app.logger.setLevel(logging.INFO) # Or logging.DEBUG for more verbose output

    # Initialize Stripe API key from app configuration.
    # This key is used for all server-side interactions with the Stripe API.
    stripe.api_key = app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        app.logger.warning("Stripe Secret Key is not configured. Stripe functionality will be disabled.")
    else:
        app.logger.info("Stripe Secret Key loaded for the app.")

    # Initialize Flask extensions with the app instance.
    db.init_app(app)        # SQLAlchemy for database interactions
    migrate.init_app(app, db) # Flask-Migrate for database migrations
    login_manager.init_app(app) # Flask-Login for user session management
    csrf.init_app(app)      # Flask-WTF CSRF protection (globally enabled)
    print("--- CSRF Protection Enabled Globally ---")
    cors.init_app(app)      # Flask-CORS for handling Cross-Origin Resource Sharing

    # Configure Celery with settings from the Flask app's config.
    celery.conf.update(app.config)
    # Explicitly set broker_url and result_backend for Celery from app config.
    celery.conf.broker_url = app.config.get('CELERY_BROKER_URL')
    celery.conf.result_backend = app.config.get('CELERY_RESULT_BACKEND')

    # Register a 'before_request' hook to log information about each incoming request.
    # This is useful for debugging and monitoring.
    @app.before_request
    def log_request_info():
        app.logger.debug(f"--> BEFORE_REQUEST: Path={request.path}, Method={request.method}")
        # To avoid overly verbose logs, you might want to log headers conditionally or not at all in production.
        # app.logger.debug(f"--> Headers: {dict(request.headers)}")

    # Import models within the app context to ensure they are registered with SQLAlchemy.
    # This is crucial for SQLAlchemy to know about your database tables.
    with app.app_context():
        from . import models # This registers the models with SQLAlchemy

    # Blueprint Registration
    # Import the main blueprint (containing routes) from the local routes.py file.
    from .routes import main as main_blueprint
    app.register_blueprint(main_blueprint) # Register the blueprint with the app.

    # Apply CSRF exemption AFTER blueprint registration.
    # The Stripe webhook endpoint needs to be exempt from CSRF protection because
    # Stripe sends POST requests without a CSRF token.
    # It's important to do this after the blueprint is registered so that the view function
    # is known to the CSRF extension.
    if 'main.stripe_webhook' in main_blueprint.view_functions:
        csrf.exempt(main_blueprint.view_functions['main.stripe_webhook'])
        app.logger.info("CSRF Exemption applied to main.stripe_webhook view.")
    else:
        # This warning helps catch issues if the route name changes or is misspelled.
        app.logger.warning("--- WARNING: main.stripe_webhook view not found for CSRF exemption. Webhook may fail. ---")

    # CSRF Exemption for process_plan_change, as it's a POST from a redirect potentially
    # and might not carry the CSRF token in the same way a direct form submission would.
    if 'main.process_plan_change' in main_blueprint.view_functions:
        csrf.exempt(main_blueprint.view_functions['main.process_plan_change'])
        app.logger.info("CSRF Exemption applied to main.process_plan_change view.")
    else:
        app.logger.warning("--- WARNING: main.process_plan_change view not found for CSRF exemption. ---")


    # Register a context processor to make 'now' (current UTC time) available in all templates.
    # This is useful for displaying dynamic year in footers, etc.
    @app.context_processor
    def inject_now():
        return {'now': datetime.now(timezone.utc)}

    # Log some key configuration values for verification during startup.
    app.logger.info(f"Database URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")
    app.logger.info(f"Celery Broker: {celery.conf.broker_url}")
    app.logger.info(f"Stripe Publishable Key Loaded: {'Yes' if app.config.get('STRIPE_PUBLISHABLE_KEY') else 'No'}")
    app.logger.info(f"Stripe Secret Key Loaded: {'Yes' if app.config.get('STRIPE_SECRET_KEY') else 'No'}")
    app.logger.info(f"Stripe Endpoint Secret Loaded: {'Yes' if app.config.get('STRIPE_ENDPOINT_SECRET') else 'No'}")
    app.logger.info(f"Stripe Pro Price ID: {app.config.get('STRIPE_PRICE_ID_PRO')}")
    app.logger.info(f"Stripe Creator Price ID: {app.config.get('STRIPE_PRICE_ID_CREATOR')}")
    app.logger.info(f"PLAN_NAME_MAP in app.config: {app.config.get('PLAN_NAME_MAP')}")

    # Debug print for URL rules - useful for checking if routes are registered correctly.
    # This will list all known routes to the application.
    print("--- Registered URL Rules ---")
    print(app.url_map)
    print("--------------------------")

    return app
