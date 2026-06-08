# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
ARG BUILD_FROM
FROM ${BUILD_FROM}

# The base-python images ship a venv at /usr/lib/venv on PATH already.
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
# build-base/libffi-dev are added temporarily in case any dependency needs to
# compile a C extension on musl (most ship musllinux wheels), then removed.
COPY app/requirements.txt /app/requirements.txt
RUN apk add --no-cache --virtual .build-deps build-base libffi-dev \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && apk del .build-deps

# Application code.
COPY app/ /app/

# S6-overlay service definitions (entrypoint is the base image's /init).
COPY rootfs/ /

LABEL \
    io.hass.name="TideSync DJ" \
    io.hass.description="AI-powered Tidal DJ using Music Assistant and Claude" \
    io.hass.type="addon"
