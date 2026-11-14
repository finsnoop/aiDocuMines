ARG PYTHON_BASE_IMAGE=python:3.12.16-slim-bookworm
FROM ${PYTHON_BASE_IMAGE} AS dependencies

ENV PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    USE_BLIS=0

WORKDIR /app

# Consolidate all apt installs and clean up in one layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    procps \
    coreutils \
    make \
    libffi-dev \
    libjpeg-dev \
    libtiff-dev \
    zlib1g-dev \
    libx11-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libwebp-dev \
    libharfbuzz-dev \
    ocrmypdf \
    poppler-utils \
    libfribidi-dev \
    libxcb1-dev \
    curl \
    netcat-openbsd \
    postgresql-client \
    build-essential \
    tesseract-ocr \
    libtesseract-dev \
    libopenjp2-7-dev \
    libfontconfig1-dev \
    libxext-dev \
    libxrender-dev \
    libpng-dev \
    file \
    ghostscript \
    imagemagick \
    libmagickwand-dev \
    libmariadb-dev-compat \
    libmariadb-dev \
    libgl1-mesa-glx \
    supervisor \
    openjdk-17-jre-headless \
    libreoffice-writer \
    pandoc \
    fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

# Set up Supervisor config
RUN mkdir -p /etc/supervisor/conf.d
COPY supervisor/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Upgrade pip and install build tools
RUN pip install --no-cache-dir pip==26.3 setuptools==81.0.0 wheel==0.46.3

COPY production_requirements.lock /app/

RUN pip install --no-cache-dir --prefer-binary --require-hashes -r production_requirements.lock

# Install spaCy model deterministically (avoid runtime/build-time downloads)
RUN python -c "import en_core_web_lg; assert en_core_web_lg.__version__ == '3.8.0'" 2>/dev/null || pip install --no-cache-dir \
  https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl

FROM dependencies AS runtime

RUN mkdir -p /app/media /app/logs && chown -R www-data:www-data /app/media

# Entrypoint + service utilities
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

COPY wait-for-it.sh /app/wait-for-it.sh
RUN chmod +x /app/wait-for-it.sh

# Supervisor for file_monitor
COPY supervisor/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Secrets are NOT baked into the image: compose/CapRover inject .env at runtime
# (env_file). .env is excluded by .dockerignore.
COPY . /app/

# Ensure logs directory exists
RUN mkdir -p /app/logs /app/media && chown -R www-data:www-data /app/media

# RUN python manage.py collectstatic --noinput

# Build-time credentials are temporary and never become runtime environment defaults.
RUN openssl genrsa -out /tmp/build-oidc.pem 2048 2>/dev/null && \
    SECRET_KEY=build-static-only SKIP_NLP_INIT=1 \
    OIDC_RSA_PRIVATE_KEY_PATH=/tmp/build-oidc.pem \
    FIELD_ENCRYPTION_KEY=ZHVtbXkta2V5LWZvci10ZXN0cy1vbmx5LTAwMDAwMDA= \
    ENCRYPTION_SECRET_KEY=ZHVtbXkta2V5LWZvci10ZXN0cy1vbmx5LTAwMDAwMDA= \
    python manage.py collectstatic --noinput && rm /tmp/build-oidc.pem


EXPOSE 8020
EXPOSE 80
EXPOSE 5433

ENTRYPOINT ["/app/entrypoint.sh"]

