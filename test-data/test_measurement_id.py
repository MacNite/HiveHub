"""Pure checks for the firmware/backend measurement-delivery id contract.

Run: cd server && python3 ../test-data/test_measurement_id.py
"""

from schemas import MeasurementIn
from measurements import measurement_insert_params


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
