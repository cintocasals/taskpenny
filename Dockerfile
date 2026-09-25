# SIAC: the live page and the OpenAI-compatible API in one container.
#   docker build -t siac .
#   docker run -p 8765:8765 -e AI_GATEWAY_API_KEY -e SIAC_API_KEY=choose-one siac
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 SIAC_RUNS_DIR=/data/runs
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 1000 siac && mkdir -p /data/runs && chown -R siac /data
USER siac
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/info')"
# Inside a container the server must listen on every interface; set SIAC_API_KEY if others can reach the port.
CMD ["siac", "serve", "--host", "0.0.0.0", "--port", "8765"]
