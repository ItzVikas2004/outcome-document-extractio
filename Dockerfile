FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /service
COPY requirements.txt requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt \
    && useradd --create-home --uid 10001 appuser
COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations
RUN mkdir -p /service/.pytest_cache /service/uploads && chown -R appuser:appuser /service
USER appuser
EXPOSE 8080
CMD ["python", "-m", "app"]
