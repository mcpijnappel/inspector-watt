# TimescaleDB with this project's schema baked in (no bind mount: keeps compose independent of the checkout path)
FROM docker.io/timescale/timescaledb:2.30.2-pg18
COPY init/ /docker-entrypoint-initdb.d/
