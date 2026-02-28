FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    tar \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY src/ src/

# Create all required directories
RUN mkdir -p \
    /app/context/pending \
    /app/context/processing \
    /app/context/completed \
    /app/backups \
    /app/logs \
    /var/simpleclaw/sessions \
    /tmp/simpleclaw_files \
    /tmp/simpleclaw_workers

# Create non-root user
RUN useradd -m -s /bin/bash simpleclaw && \
    chown -R simpleclaw:simpleclaw /app /var/simpleclaw /tmp/simpleclaw_files /tmp/simpleclaw_workers

USER simpleclaw

CMD ["python", "-m", "src.main"]
