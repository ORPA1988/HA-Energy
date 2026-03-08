ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.11-alpine3.18
FROM ${BUILD_FROM}

# Install build dependencies for scipy/numpy
RUN apk add --no-cache \
    gcc \
    g++ \
    musl-dev \
    libffi-dev \
    openssl-dev \
    gfortran \
    openblas-dev \
    lapack-dev

# Copy requirements first for layer caching
COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Copy application
COPY app/ /app/

# Copy s6 service scripts
COPY rootfs/ /

# Expose ingress port
EXPOSE 8080

# Ensure service scripts are executable
RUN chmod +x /etc/services.d/energy-optimizer/run \
    && chmod +x /etc/services.d/energy-optimizer/finish
