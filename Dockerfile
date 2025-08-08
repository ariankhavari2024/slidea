# ---- Base ----
FROM python:3.11-slim-bullseye

# System settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

WORKDIR /app

# ---- OS deps (only what we actually need) ----
# - build-essential: for packages that need compilation
# - libpq-dev: for psycopg2 (skip if you use psycopg2-binary)
# - curl: optional, handy for debugging/health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
  && rm -rf /var/lib/apt/lists/*

# ---- Python deps (cache-friendly) ----
# If you use private indexes, COPY a .pip/pip.conf here before install.
COPY requirements.txt .
RUN python -m pip install --upgrade pip \
 && pip install -r requirements.txt

# ---- App code ----
COPY . .

# ---- Entrypoint ----
# Ensure the script is executable even if repo perms are off
RUN chmod +x /app/entrypoint.sh

# Render listens on $PORT; we bind to it in entrypoint
EXPOSE 10000

# Default command (entrypoint dispatches web vs worker via SERVICE_TYPE)
CMD ["/app/entrypoint.sh"]
