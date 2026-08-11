FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install shared package first (editable install — no rebuild needed for model changes)
COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared

# Install Platform API dependencies
COPY platform_api/requirements.txt /app/platform_api/
RUN pip install --no-cache-dir -r /app/platform_api/requirements.txt

COPY platform_api/ /app/platform_api/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "platform_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
