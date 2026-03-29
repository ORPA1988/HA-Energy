## EnergieHA with integrated EMHASS optimization engine
ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install system dependencies for numpy/scipy/cvxpy + Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    # numpy/scipy/cvxpy build deps
    gcc g++ gfortran \
    libopenblas-dev libatlas-base-dev \
    # General
    curl ca-certificates tzdata git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python packages in stages (heaviest first for better caching)
# Stage 1: Core scientific packages
RUN pip3 install --no-cache-dir --break-system-packages \
    numpy scipy pandas

# Stage 2: Optimization engine (cvxpy is the heaviest)
RUN pip3 install --no-cache-dir --break-system-packages \
    cvxpy

# Stage 3: EMHASS from our fork (only the package, no web server)
RUN pip3 install --no-cache-dir --break-system-packages \
    "emhass @ git+https://github.com/ORPA1988/emhass.git@master"

# Stage 4: Flask for EnergieHA GUI
RUN pip3 install --no-cache-dir --break-system-packages \
    "flask>=3.0,<4.0" requests

# Remove build-only packages to reduce image size
RUN apt-get remove --purge -y --auto-remove gcc g++ gfortran python3-dev git \
    && rm -rf /var/lib/apt/lists/*

# Copy EnergieHA source
COPY src/ /app/energieha/
COPY run.sh /

RUN chmod a+x /run.sh \
    && mkdir -p /data

CMD [ "/run.sh" ]
