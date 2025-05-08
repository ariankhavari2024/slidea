# gunicorn_config.py
import os
import multiprocessing

# Bind to 0.0.0.0 to accept connections from Render's proxy
# Render provides the port to use via the PORT environment variable
port = os.environ.get("PORT", "10000") # Default to 10000 if PORT not set
bind = f"0.0.0.0:{port}"

# Number of worker processes
# Render recommends setting this based on your instance type's resources.
# A common starting point is (2 * number_of_cores) + 1
# Defaulting to a reasonable number, adjust based on Render plan/monitoring.
# workers = multiprocessing.cpu_count() * 2 + 1
# Let's start with a simpler default, Render might override this based on plan.
workers = int(os.environ.get("WEB_CONCURRENCY", 3)) # Use WEB_CONCURRENCY or default to 3

# Worker class (sync is default, but gevent or eventlet can be used for async)
# sync workers are generally fine for most Flask apps unless highly I/O bound.
worker_class = 'sync'

# Logging
# Log to stdout/stderr so Render can capture logs
accesslog = '-'
errorlog = '-'
loglevel = 'info' # Adjust to 'debug' for more verbose logs if needed

# Timeout settings (adjust if you have long-running requests)
timeout = 120 # Seconds before a worker is killed and restarted
keepalive = 5 # Seconds to wait for requests on a Keep-Alive connection

# Optional: Preload app for potential memory savings (can sometimes cause issues)
# preload_app = True

print(f"Gunicorn config: Binding to {bind}, Workers: {workers}")
