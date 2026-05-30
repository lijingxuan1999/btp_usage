FROM python:3.13-slim

WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=5000
ENV HOST=0.0.0.0
# Platform OTLP collector only supports /v1/traces, not /v1/metrics — disable metrics exporter
ENV OTEL_METRICS_EXPORTER=none

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Copy .env for AI Core credentials (load_dotenv() reads /app/.env at startup)
COPY .env* ./

# Run the agent
CMD ["python", "app/main.py", "--host", "0.0.0.0", "--port", "5000"]
