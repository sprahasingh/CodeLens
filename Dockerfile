FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# setuptools is supplied by the Python base image; keep its fixed security
# release without changing the application's dependency set.
RUN python -m pip install --no-cache-dir --upgrade "setuptools>=83.0.0"

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
