# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
ARG BUILD_FROM
FROM ${BUILD_FROM}

# The base-python images ship a venv at /usr/lib/venv on PATH already.
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application code.
COPY app/ /app/

# S6-overlay service definitions (entrypoint is the base image's /init).
COPY rootfs/ /

LABEL \
    io.hass.name="TideSync DJ" \
    io.hass.description="AI-powered Tidal DJ using Music Assistant and Claude" \
    io.hass.type="addon" \
    io.hass.version="0.1.0"
