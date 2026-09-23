FROM python:3.12-slim
RUN pip install --no-cache-dir fastembed==0.7.4 aiohttp==3.12.15
WORKDIR /app
COPY server.py /app/server.py
ENV HF_HUB_DISABLE_TELEMETRY=1
CMD ["python", "/app/server.py"]
