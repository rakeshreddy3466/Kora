# One image for every KORA container: the website/API, the workers and the deletion executor
# (docker-compose.yml decides which command each runs). The offline speech models are baked in,
# so the containers never need the internet at run time.
ARG BASE=python:3.12-slim
FROM ${BASE}

ARG WITH_SPEECH=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: video/audio; tesseract: OCR of slides in recordings and scanned pages
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home-dir /data --no-create-home kora \
 && mkdir -p /data /opt/kora/models \
 && chown 10001:10001 /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kora ./kora
RUN pip install ".[postgres,s3,whisper]"

# Whisper (small, int8) + speaker-turn models, about 400 MB, downloaded once at build time
ENV KORA_MODEL_CACHE_DIR=/opt/kora/models
RUN if [ "$WITH_SPEECH" = "1" ]; then \
      KORA_DATA_DIR=/tmp/kora-build kora fetch-models && rm -rf /tmp/kora-build; \
    fi

COPY scripts/docker-entrypoint.sh /usr/local/bin/kora-entrypoint
RUN chmod 0755 /usr/local/bin/kora-entrypoint

USER 10001
# /data is a volume: blobs, KORA's working copies of sources, secrets; TMPDIR on it, not in RAM
ENV KORA_DATA_DIR=/data KORA_ENV=production TMPDIR=/data/tmp
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)" || exit 1
ENTRYPOINT ["kora-entrypoint"]
CMD ["kora", "serve", "--host", "0.0.0.0", "--port", "8080"]
