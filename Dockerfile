FROM python:3.12-slim

# Install system dependencies (FFmpeg + Node.js for yt-dlp JS runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    && (which node || ln -s $(which nodejs) /usr/local/bin/node) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy source code
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Create temporary working directory
RUN mkdir -p /app/temp

EXPOSE 8000 10000

CMD ["sh", "-c", "uvicorn app:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
