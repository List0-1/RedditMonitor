FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py monitor.py promo.py proxies.py reddit_fetch.py scan.py vouchers.py geo.py ./

ENV PYTHONUNBUFFERED=1

# Continuous monitor: Reddit every 3h, status every 5m
CMD ["python", "-u", "monitor.py"]
