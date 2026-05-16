# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY clore_search.py api.py ./
COPY static/ ./static/

EXPOSE 8000

# CLORE_API_KEY must be supplied at runtime: docker run -e CLORE_API_KEY=xxx -p 8000:8000 clore-search
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
