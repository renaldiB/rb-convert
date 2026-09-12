FROM python:3.12-slim

# Install system dependencies (FFmpeg + Node.js for yt-dlp JS runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set up a new user named "user" with UID 1000 for Hugging Face Spaces security
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Install Python requirements
COPY --chown=user backend/requirements.txt $HOME/app/backend/requirements.txt
RUN pip install --no-cache-dir --user -r $HOME/app/backend/requirements.txt

# Copy source code
COPY --chown=user backend/ $HOME/app/backend/
COPY --chown=user frontend/ $HOME/app/frontend/

# Create temporary working directory with write permission
RUN mkdir -p $HOME/app/temp

# Hugging Face default port is 7860
EXPOSE 7860

ENV PORT=7860

CMD ["sh", "-c", "uvicorn app:app --app-dir backend --host 0.0.0.0 --port ${PORT:-7860}"]
