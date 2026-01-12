# Use Python 3.11 slim base image
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for temp files and cookies
RUN mkdir -p /tmp/ytdl /tmp/cookies_backup && \
    chmod 777 /tmp/ytdl /tmp/cookies_backup

# Expose port for HTTP server
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV TEMP_DIR=/tmp/ytdl
ENV YOUTUBE_COOKIES_PATH=/tmp/cookies.txt
ENV COOKIES_BACKUP_DIR=/tmp/cookies_backup
ENV PORT=8080

# Run as non-root user
USER 1000

# Run the bot
CMD ["python", "bot.py"]
