FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir redis

COPY backup_daemon.py .

CMD ["python", "backup_daemon.py"]
