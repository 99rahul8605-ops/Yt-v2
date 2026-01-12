# Use Python 3.11 slim base image
FROM python:3.11-slim

# Install system dependencies including libnss3 for browser compatibility
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    libnss3 \
    libgconf-2-4 \
    libxi6 \
    libxrandr2 \
    libxfixes3 \
    libxcursor1 \
    libxcomposite1 \
    libasound2 \
    libcups2 \
    libxdamage1 \
    libxext6 \
    libxrender1 \
    libxtst6 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directory for temp files
RUN mkdir -p /tmp/ytdl && chmod 777 /tmp/ytdl

# Create cookies directory
RUN mkdir -p /tmp && chmod 777 /tmp

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV TEMP_DIR=/tmp/ytdl
ENV YOUTUBE_COOKIES_PATH=/tmp/cookies.txt

# Run as non-root user
USER 1000

# Run the bot
CMD ["python", "bot.py"]
