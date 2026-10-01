FROM python:3.11-slim

# ffmpeg обязателен: без него бот не сможет рендерить видео
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Баннеры монтируются томом, поэтому в образе только пустая структура папок
RUN mkdir -p videos && \
    mkdir -p banners/mostbet banners/playerok banners/funpay banners/mycsgo banners/1win

CMD ["python", "bot.py"]
