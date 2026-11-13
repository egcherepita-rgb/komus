FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home komus
COPY planner.py importer.py server.py ./
COPY public ./public
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENV HOST=0.0.0.0 PORT=8080 DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
