FROM python:3.12-slim

# BlueZ utilities needed by bleak on Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py index.html favicon.svg ./

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
