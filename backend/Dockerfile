# Backend Dockerfile for Fuel Intelligence & Resilience Platform
FROM python:3.11-slim

# Prevent writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend:/app

WORKDIR /app

# Install curl for healthcheck verification
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first to leverage Docker layer caching
COPY requirements.txt ./

# Install python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy backend application source
COPY backend/ ./backend/

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose FastAPI backend port
EXPOSE 8001

# Healthcheck monitoring the platform health endpoint
HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=3 \
  CMD curl -f http://localhost:8001/health || exit 1

# Start Uvicorn ASGI server
WORKDIR /app/backend
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
