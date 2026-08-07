FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
ENV DATA_DIR="/data"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY nodeseek_watcher.py ./

RUN mkdir -p /data \
    && chown -R 10001:10001 /data \
    && chmod 755 /app /app/nodeseek_watcher.py

USER 10001:10001

CMD ["python", "/app/nodeseek_watcher.py"]
