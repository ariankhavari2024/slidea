# Dockerfile for SlideaAI Flask Application

# Use an official Python runtime as a parent image
# Choose a version compatible with your development environment (e.g., 3.11)
FROM python:3.11-slim-bullseye

# Set environment variables
# Prevents Python from writing pyc files to disc (recommended for containers)
ENV PYTHONDONTWRITEBYTECODE 1
# Ensures Python output is sent straight to terminal without being buffered
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
# - build-essential: Needed for compiling some Python packages
# - libpq-dev: Needed for psycopg2 (PostgreSQL adapter)
# - curl: Useful for health checks or other utilities if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install pipenv (if you use Pipfile) or just copy requirements.txt
# --- Option 1: If using requirements.txt ---
COPY requirements.txt .
# Upgrade pip and install dependencies
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# --- Option 2: If using Pipfile (Comment out Option 1 if using this) ---
# COPY Pipfile Pipfile.lock ./
# RUN pip install --upgrade pip
# RUN pip install pipenv
# RUN pipenv install --system --deploy --ignore-pipfile

# Copy the rest of the application code into the container
COPY . .

# Make the entrypoint script executable
RUN chmod +x /app/entrypoint.sh

# Expose the port Gunicorn will run on (Render sets this via $PORT env var, typically 10000)
# We don't strictly NEED to expose here as Render handles it, but it's good practice.
EXPOSE 10000

# Set the entrypoint script to run when the container starts
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command (can be overridden by entrypoint or render.yaml)
# CMD ["gunicorn", "-c", "gunicorn_config.py", "wsgi:app"]
# Note: The actual command to start gunicorn/celery will be in entrypoint.sh or render.yaml
