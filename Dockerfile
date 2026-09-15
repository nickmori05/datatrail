FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATATRAIL_DB=/data/datatrail.sqlite3
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home datatrail \
    && mkdir /data && chown datatrail:datatrail /data
COPY datatrail ./datatrail
COPY examples ./examples
USER datatrail
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "datatrail.api:app", "--host", "0.0.0.0", "--port", "8000"]
