#!/usr/bin/with-contenv bashio

bashio::log.info "Starting EnergieHA energy management..."
cd /app
exec python3 -m energieha.main
