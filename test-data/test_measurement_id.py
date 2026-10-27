"""Pure checks for the firmware/backend measurement-delivery id contract.

Run: cd server && python3 ../test-data/test_measurement_id.py
"""

import os

# measurements imports the server config, which requires these at import time.
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "postgresql://ci-not-used/ci")

from schemas import MeasurementIn  # noqa: E402
from measurements import measurement_insert_params  # noqa: E402


payload = MeasurementIn.model_validate(
    {
        "device_id": "hive-a",
        "measurement_id": "a1b2c3d4-7-123",
        "timestamp": "2026-07-23T12:00:00Z",
    }
)
params = measurement_insert_params(payload, payload.timestamp)

assert payload.measurement_id == "a1b2c3d4-7-123"
assert params["measurement_id"] == payload.measurement_id
assert MeasurementIn.model_validate({"device_id": "legacy"}).measurement_id is None

print("Measurement id schema and insert mapping checks passed.")
