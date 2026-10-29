FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data REFERENCE_DIR=/app/reference
WORKDIR /app
COPY pyproject.toml ./
COPY threads_factory ./threads_factory
COPY reference/data ./reference/data
COPY AGENT.md ./
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home factory && mkdir /data && chown factory:factory /data
USER factory
CMD ["threads-factory", "serve", "--host", "0.0.0.0"]
