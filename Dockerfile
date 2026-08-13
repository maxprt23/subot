FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUBOT_CONFIG_PATH=/config/config.json \
    SUBOT_SEEN_PATH=/data/seen.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY subot.py ./

RUN mkdir -p /config /data \
    && chown -R 10001:10001 /app /config /data

USER 10001:10001

ENTRYPOINT ["python", "/app/subot.py"]
