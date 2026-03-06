FROM python:3.10-slim

WORKDIR /app

# Install curl for healthcheck & dependencies
RUN apt-get update && apt-get install -y curl libzbar0 libgl1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Environment variables can be overridden by docker-compose
ENV PYTHONPATH=/app
