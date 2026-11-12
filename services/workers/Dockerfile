FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 gnupg curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 hermes_user
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
RUN chown -R hermes_user:hermes_user /app

USER hermes_user
CMD ["python", "src/main.py"]
