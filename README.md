# YouTube Downloader Telegram Bot

A Telegram bot that downloads YouTube videos and sends them to users.

## Features

- Download YouTube videos up to 720p
- Handle age-restricted videos (with cookies)
- Automatic format selection
- Progress updates
- Cleanup after processing
- Rate limiting
- User access control

## Deployment on Render

### Method 1: Docker (Recommended)

1. **Fork/Clone** this repository

2. **Create a new Web Service** on Render:
   - Connect your GitHub repository
   - Select "Docker"
   - Render will automatically detect the `Dockerfile`

3. **Set Environment Variables**:
   - `TELEGRAM_API_ID` - From [my.telegram.org](https://my.telegram.org)
   - `TELEGRAM_API_HASH` - From [my.telegram.org](https://my.telegram.org)
   - `TELEGRAM_BOT_TOKEN` - From [@BotFather](https://t.me/botfather)
   - Optional: `YOUTUBE_COOKIES_PATH` - Path to cookies.txt
   - Optional: `ALLOWED_USERS` - Comma-separated user IDs

4. **Deploy**:
   - Click "Create Web Service"
   - The bot will start automatically

### Method 2: Without Docker

1. **Create a new Worker Service** on Render:
   - Select "Python"
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`

2. **Add build script** for ffmpeg:
   Create a `build.sh` file:
   ```bash
   #!/bin/bash
   apt-get update
   apt-get install -y ffmpeg
   pip install -r requirements.txt
