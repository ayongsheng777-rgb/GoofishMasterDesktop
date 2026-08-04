FROM python:3.11-slim

WORKDIR /app

# Use China mirror for Playwright browser downloads
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libxss1 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (uses npmmirror)
RUN playwright install chromium

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p /app/data /app/state /app/jsonl /app/logs

EXPOSE 8904

# stdout 同时落盘 /app/logs（宿主机映射 ./data/spider-logs）：spider 崩溃后
# docker logs 随容器替换消失，落盘日志是排查崩溃根因的唯一现场
CMD ["/bin/sh", "-c", "python -u main.py 2>&1 | tee -a /app/logs/spider-stdout.log"]
