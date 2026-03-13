FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install system dependencies including cron
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc-dev \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure entrypoint is executable
RUN chmod +x entrypoint.sh

# Environment variables for overriding
ENV SYNC_CMD="python cli.py --verbose --sources coolify,pulse,npm,proxmox --export infisical,peekaping,proxmox_notes"
ENV SYNC_CRON="*/2 * * * *"

# Run the sync loop
ENTRYPOINT ["/app/entrypoint.sh"]
