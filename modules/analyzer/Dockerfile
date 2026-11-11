FROM docker.io/library/python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# sigrok-cli drives the logic analyzer and decodes I2C / SPI; fx2lafw is the open firmware it loads into
# FX2-based analyzers (nanoDLA, the 8-channel 24 MHz "Saleae clones") at every connect.
RUN apt-get update && apt-get install -y --no-install-recommends sigrok-cli sigrok-firmware-fx2lafw \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

FROM base AS test
RUN pip install ".[test]"
COPY tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
RUN pip install .
EXPOSE 8400
CMD ["uvicorn", "pst_analyzer.main:app", "--host", "0.0.0.0", "--port", "8400"]
