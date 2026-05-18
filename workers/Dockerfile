# Standalone worker supervisor — for remote hosts without the full Dorian stack.
# Inside the Dorian Docker Compose stack, workers reuse the backend image instead.
#
# Build context: repo root
#   docker build -f workers/Dockerfile -t dorian-workers .
#
# Run:
#   docker run --rm dorian-workers --scheduler tcp://scheduler:8786 --standalone

FROM docker.io/library/python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only the workers module + minimal deps.
COPY dorian/__init__.py dorian/__init__.py
COPY dorian/workers/ dorian/workers/

RUN pip install --no-cache-dir psutil distributed click

ENTRYPOINT ["python", "-m", "dorian.workers", "--standalone"]
