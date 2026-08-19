FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        git \
    && rm -rf /var/lib/apt/lists/*
