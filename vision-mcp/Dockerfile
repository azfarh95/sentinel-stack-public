FROM python:3.12-slim

# libgl1 / libglib2.0-0 for RapidOCR's OpenCV; curl for the healthcheck.
# tesseract-ocr + packs are the P1b fallback OcrEngine — eng baseline, rus for
# Cyrillic, plus a common multilingual set (RapidOCR's default models are weak
# on non-Latin, so we auto-route those to Tesseract).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
# Bound ONNX/OpenMP threading so a single OCR can't grab every core, and don't
# burn CPU spin-waiting on idle threads (ONNX threading guidance). The app also
# sets ONNX intra_op_num_threads + a concurrency semaphore on top of this.
ENV OMP_NUM_THREADS=2
ENV OMP_WAIT_POLICY=PASSIVE

EXPOSE 8097

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097"]
