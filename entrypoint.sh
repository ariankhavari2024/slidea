#!/bin/sh

# entrypoint.sh - Script to run migrations and start services

# Exit immediately if a command exits with a non-zero status.
set -e

# Define functions for starting services
start_web() {
  echo "Starting Gunicorn web server..."
  # Use exec to replace the shell process with the Gunicorn process
  exec gunicorn -c gunicorn_config.py wsgi:app
}

start_worker() {
  echo "Starting Celery worker..."
  # Use exec to replace the shell process with the Celery process
  # Make sure 'app.celery' matches how you import/name your Celery instance in __init__.py
  exec celery -A app.celery worker -l info
}

# Run Database Migrations (only on the web service or a dedicated release command)
# Render typically runs the 'startCommand' once during deployment.
# We check an environment variable (IS_WEB_SERVICE) set in render.yaml
# to ensure migrations only run on the web instance, not the worker.
if [ "$IS_WEB_SERVICE" = "true" ] ; then
  echo "Running database migrations..."
  # Wait a few seconds for the database to be ready (optional, adjust as needed)
  # sleep 5
  flask db upgrade
  echo "Database migrations complete."
fi

# Check the SERVICE_TYPE environment variable (set in render.yaml)
# to determine which process to start in this container.
if [ "$SERVICE_TYPE" = "web" ] ; then
  start_web
elif [ "$SERVICE_TYPE" = "worker" ] ; then
  start_worker
else
  echo "Error: SERVICE_TYPE environment variable not set or invalid."
  echo "Expected 'web' or 'worker', got '$SERVICE_TYPE'"
  exit 1
fi

