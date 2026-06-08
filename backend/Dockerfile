FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASR_HOST=0.0.0.0 \
    PORT=8765 \
    ASR_OUTPUT_DIR=/tmp/output

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

EXPOSE 8765

CMD ["python", "server.py"]
