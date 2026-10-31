FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# база живёт в volume, чтобы переживать пересборку контейнера
ENV DB_PATH=/data/bot.sqlite3
VOLUME /data
EXPOSE 8080

CMD ["python", "-m", "bot"]
