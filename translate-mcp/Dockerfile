FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
ENV LT_BASE_URL="http://libretranslate:5000"
ENV LT_FALLBACK_URL="https://translate.argosopentech.com"
ENV LT_API_KEY=""

EXPOSE 8094

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8094"]
