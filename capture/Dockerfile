FROM docker.io/library/python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

FROM base AS test
RUN pip install ".[test]"
COPY tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
# picotool flashes RP2040 / RP2350 boards (flash_rp2.py); it reaches them through libusb (/dev/bus/usb).
RUN apt-get update && apt-get install -y --no-install-recommends picotool && rm -rf /var/lib/apt/lists/*
RUN pip install .
EXPOSE 8100
CMD ["python", "-m", "capture.main"]
