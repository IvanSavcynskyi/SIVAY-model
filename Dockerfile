FROM python:3.12-slim
WORKDIR /srv/sivay
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
RUN pip install --no-cache-dir --no-deps .
COPY rules ./rules
RUN useradd --create-home sivay && mkdir -p /srv/sivay/data && chown -R sivay:sivay /srv/sivay
USER sivay
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
