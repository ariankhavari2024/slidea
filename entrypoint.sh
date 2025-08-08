#!/bin/sh
set -e

start_web() {
  echo "Starting Gunicorn web server..."
  # ensure bind to Render port (override if config doesn’t)
  exec gunicorn wsgi:app -c gunicorn_config.py -b 0.0.0.0:${PORT:-10000}
}

start_worker() {
  echo "Starting Celery worker..."
  # If your Celery instance is app.celery_app, change -A accordingly
  exec celery -A app.celery worker -l info
}

# run DB migrations only on web
if [ "$IS_WEB_SERVICE" = "true" ]; then
  echo "Running database migrations..."
  flask db upgrade
  echo "Database migrations complete."
fi

# dispatch
if [ "$SERVICE_TYPE" = "web" ]; then
  start_web
elif [ "$SERVICE_TYPE" = "worker" ]; then
  start_worker
else
  echo "Error: SERVICE_TYPE must be 'web' or 'worker' (got '$SERVICE_TYPE')"
  exit 1
fi
