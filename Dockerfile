# Pinned by digest for reproducible builds. To refresh: pull the tag locally,
# run `docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'`,
# and paste the new digest here.
FROM python:3.12-slim@sha256:520153e2deb359602c9cffd84e491e3431d76e7bf95a3255c9ce9433b76ab99a

# BlueZ utilities needed by bleak on Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py index.html favicon.svg manifest.json service-worker.js \
     icon-192.png icon-512.png icon-maskable-512.png apple-touch-icon.png ./
COPY vendor/ ./vendor/

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
