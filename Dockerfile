FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Данные вынесены в volume, чтобы не терялись при перезапуске
VOLUME ["/app/data"]
ENV DATA_FILE=/app/data/users.json

CMD ["python", "-u", "bot.py"]
