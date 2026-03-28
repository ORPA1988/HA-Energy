#!/usr/bin/with-contenv bashio

bashio::log.info "Starting EnergieHA energy management..."

cd /app

# Verify Python can import the package
python3 -c "import energieha; print(f'EnergieHA v{energieha.__version__} loaded')" 2>&1 || {
    bashio::log.error "Failed to import energieha package"
    exit 1
}

# Verify Flask is available
python3 -c "import flask; print(f'Flask v{flask.__version__} available')" 2>&1 || {
    bashio::log.error "Flask not available - web GUI will not work"
}

exec python3 -u -m energieha 2>&1
