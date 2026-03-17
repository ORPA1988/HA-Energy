ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.11-alpine3.18

# Stage 1: Build dependencies (scipy/numpy need compilation)
FROM ${BUILD_FROM} AS builder

RUN apk add --no-cache \
    gcc \
    g++ \
    musl-dev \
    libffi-dev \
    openssl-dev \
    gfortran \
    openblas-dev \
    lapack-dev

COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages --prefix=/usr/local -r /app/requirements.txt

# Stage 2: Runtime image (no build tools)
FROM ${BUILD_FROM}

# Only runtime dependencies for scipy/numpy
RUN apk add --no-cache \
    libffi \
    openssl \
    openblas \
    lapack \
    libstdc++ \
    libgfortran

# Copy installed Python packages from builder
COPY --from=builder /usr/local /usr/local

# Copy application
COPY app/ /app/

# Copy s6 service scripts
COPY rootfs/ /

# Expose ingress port
EXPOSE 8080

# Ensure service scripts are executable
RUN chmod +x /etc/services.d/energy-optimizer/run \
    && chmod +x /etc/services.d/energy-optimizer/finish

# Health check for container monitoring
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD wget -q -O /dev/null http://localhost:8080/health || exit 1
