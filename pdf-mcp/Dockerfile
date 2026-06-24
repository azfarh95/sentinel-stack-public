FROM python:3.12-slim

# tesseract-ocr + language packs power the DEFAULT OCR tier (PyMuPDF's OCR path)
# for scanned / image-only PDFs. eng is the baseline; rus adds Cyrillic, plus a
# common multilingual set. The RapidOCR (upgrade) tier uses ONNX models bundled
# in its wheel, but its OpenCV needs libgl1 / libglib2.0-0. curl is the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-rus \
    tesseract-ocr-deu \
    tesseract-ocr-fra \
    tesseract-ocr-spa \
    tesseract-ocr-por \
    tesseract-ocr-ara \
    tesseract-ocr-chi-sim \
    tesseract-ocr-jpn \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
# PyMuPDF's OCR looks here for Tesseract language data (bookworm / tesseract 5).
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

EXPOSE 8099

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
