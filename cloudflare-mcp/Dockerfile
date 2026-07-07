FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
# Scoped, read-only Cloudflare API token (from .env.local via compose).
ENV CLOUDFLARE_API_TOKEN=""

EXPOSE 8106

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8106"]
