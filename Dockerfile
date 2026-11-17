# SURYA-KAVACH — single-image deployment (FastAPI serves API + the vendored UI)
FROM python:3.11-slim

WORKDIR /app

# System libs occasionally needed by numpy/scipy wheels are already in slim; none extra required.
COPY pyproject.toml README.md ./
COPY solarflare ./solarflare
COPY frontend/dist ./frontend/dist

RUN pip install --no-cache-dir \
    "numpy>=1.26" "pandas>=2.1" "scipy>=1.11" "scikit-learn>=1.4" "astropy>=6.0" \
    "joblib>=1.3" "pyarrow>=14.0" "fastapi>=0.115" "uvicorn[standard]>=0.30" \
    "pydantic>=2.7" "sqlalchemy>=2.0" "httpx>=0.27" "python-multipart>=0.0.9"

ENV SOLARFLARE_DATA_DIR=/data \
    SOLARFLARE_AUTO_INGEST=1 \
    SOLARFLARE_CHECK_INTERVAL=60 \
    SOLARFLARE_RUN_SCHEDULER=1

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "solarflare.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
