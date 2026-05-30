#!/usr/bin/env sh
set -eu

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d

