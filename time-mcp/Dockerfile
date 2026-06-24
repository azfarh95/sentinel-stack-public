FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
# Default timezone for bare now()/today() calls (overridable per-call).
ENV LOCAL_TIMEZONE=Asia/Kuala_Lumpur

EXPOSE 8103

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8103"]
