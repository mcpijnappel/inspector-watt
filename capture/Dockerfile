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
RUN pip install .
EXPOSE 8100
CMD ["python", "-m", "capture.main"]
