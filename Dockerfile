FROM python:3.12.9-slim

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
    fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

# Set up Supervisor config
RUN mkdir -p /etc/supervisor/conf.d
COPY supervisor/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Upgrade pip and install build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel numpy

COPY production_requirements.txt /app/

RUN pip install --no-cache-dir --prefer-binary -r production_requirements.txt gunicorn

# Install spaCy model deterministically (avoid runtime/build-time downloads)
RUN pip install --no-cache-dir \
  https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl

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

ENV SKIP_NLP_INIT=1
RUN python manage.py collectstatic --noinput
ENV SKIP_NLP_INIT=0


EXPOSE 8020
EXPOSE 80
EXPOSE 5433

ENTRYPOINT ["/app/entrypoint.sh"]

