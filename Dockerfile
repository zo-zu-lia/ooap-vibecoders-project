FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/fileflow
COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --uid 10001 --create-home fileflow
COPY app ./app
COPY docs/FileFlow_OpenAPI.yaml ./docs/FileFlow_OpenAPI.yaml
USER fileflow
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "80"]

