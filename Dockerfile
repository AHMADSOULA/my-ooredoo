FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# ✅ تثبيت Xvfb + أدوات X11 (للـ headful mode)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11-utils \
    x11-xserver-utils \
    fonts-dejavu \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    XVFB_WHD=1280x1024x24

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY . .

# ✅ نشغلو Xvfb ثم البوت
CMD ["sh", "-c", "Xvfb :99 -screen 0 ${XVFB_WHD} -ac +extension GLX +render -noreset & sleep 3 && python bot.py"]
