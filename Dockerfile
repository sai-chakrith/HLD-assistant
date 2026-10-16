# One image for both services (api and ui); compose picks the command.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/hf-cache

WORKDIR /app

# CPU-only torch first (keeps the image small), then the rest.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

# Code only. data/raw (AUTOSAR PDFs), data/processed, chroma and *.db are mounted at runtime, never copied.
COPY app ./app
COPY scripts ./scripts
COPY ui ./ui
COPY eval ./eval
COPY docs ./docs
COPY data/synthetic ./data/synthetic

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
