# ---- build stage (optional for slim images) ----
FROM python:3.13-slim AS runtime

# Prevents Python from writing .pyc files & enables unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system deps (optional: curl for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

# Create app dir
WORKDIR /app

# Copy only what we need
COPY app.py /app/app.py

# Install dependencies
# (fastapi, uvicorn, requests)
RUN pip install --no-cache-dir fastapi uvicorn[standard] requests

# Expose port
EXPOSE 8000

# Healthcheck (optional)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8000/health || exit 0

# Run
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

