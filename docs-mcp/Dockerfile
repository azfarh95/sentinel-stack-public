FROM python:3.12-slim

# curl is only for the compose healthcheck. The docs themselves are read-only
# bind mounts under /docs — no other system deps needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1

EXPOSE 8107

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8107"]
