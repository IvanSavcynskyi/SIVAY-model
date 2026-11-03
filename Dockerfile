FROM python:3.12-slim
WORKDIR /srv/sivay
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
RUN python -c "import rasterio, shapely, pyproj, psycopg"
COPY app ./app
RUN pip install --no-cache-dir --no-deps .
COPY rules ./rules
COPY scripts ./scripts
COPY examples ./examples
RUN useradd --create-home sivay && mkdir -p /srv/sivay/data && chown -R sivay:sivay /srv/sivay
USER sivay
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
