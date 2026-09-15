# Lightweight mock of the HiveScale API for reviewing the HivePal <-> HiveScale
# interface. No database, no hardware - just FastAPI + in-memory dummy data.
FROM python:3.12-slim

WORKDIR /app

# Install only the runtime deps first so the layer is cached across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Keep the production insights engine and its FFT helper together so the mock's
# insights endpoints behave exactly like production.
COPY hive_data.py hiveheart_fft.py insights.py app.py ./

EXPOSE 8000

# INTERVAL_MINUTES controls dummy-data density (default 30 min ~= 24.8k points).
ENV INTERVAL_MINUTES=30

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
