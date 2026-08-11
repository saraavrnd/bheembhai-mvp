FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install shared package first (editable install — no rebuild needed for model changes)
COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared

# Install Engine Service dependencies
COPY engine_service/requirements.txt /app/engine_service/
RUN pip install --no-cache-dir -r /app/engine_service/requirements.txt

COPY engine_service/ /app/engine_service/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8001

CMD ["uvicorn", "engine_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
