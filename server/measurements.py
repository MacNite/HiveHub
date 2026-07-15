"""Measurement ingest and read paths: insert/select column maps, per-hive
readings, temperature compensation and serialization."""

import json
import re
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException

from auth import (
    require_api_key,
    require_device_role,
    require_hivepal_service_key,
    require_user_id,
)
from config import MIN_PLAUSIBLE_YEAR, logger
from db import get_conn
from devices import ensure_device_config
from hiveheart_fft import decode_fft
from mqtt_publisher import publisher as mqtt_publisher
from schemas import MAX_HIVES, HiveReadingIn, MeasurementImportIn, MeasurementIn
from sd_import import split_new_and_duplicate
from tempcomp import TEMP_SOURCE_FIELD, compensate_weight, ema_temperatures

router = APIRouter()


# Column/value mapping for a single measurement row. Shared by the device-facing
# ingest endpoint (POST /api/v1/measurements) and the app-facing bulk SD import
# (POST /api/v1/app/devices/{device_id}/measurements/import) so the two paths can
# never drift apart. The statement deliberately omits a trailing clause: callers
# append " RETURNING id;" (single insert) or ";" (executemany bulk insert).
MEASUREMENT_INSERT_SQL = """
                INSERT INTO measurements (
                    device_id, measured_at, scale_1_weight_kg, scale_2_weight_kg,
                    hive_1_temp_c, hive_2_temp_c,
                    hive_1_humidity_percent, hive_2_humidity_percent, ambient_temp_c,
                    ambient_humidity_percent, battery_voltage, battery_soc_percent,
                    battery_alert, battery_monitor_ok, solar_monitor_ok,
                    solar_bus_voltage_v, solar_shunt_voltage_mv, solar_load_voltage_v,
                    solar_current_ma, solar_power_mw, network_transport,
                    cellular_ok, cellular_csq, calibration_mode, boot_count,
                    time_source, rssi_dbm, firmware_version, config_version, sd_ok,
                    rtc_ok, sht_ok, scale_1_raw, scale_2_raw,
                    mic_ok, mic_sample_rate_hz, mic_sample_frames,
                    mic_left_ok, mic_left_rms_dbfs, mic_left_peak_dbfs, mic_left_rms_normalized,
                    mic_right_ok, mic_right_rms_dbfs, mic_right_peak_dbfs, mic_right_rms_normalized,
                    mic_left_band_sub_bass_dbfs, mic_left_band_hum_dbfs, mic_left_band_piping_dbfs,
                    mic_left_band_stress_dbfs, mic_left_band_high_dbfs,
                    mic_right_band_sub_bass_dbfs, mic_right_band_hum_dbfs, mic_right_band_piping_dbfs,
                    mic_right_band_stress_dbfs, mic_right_band_high_dbfs,
                    bee_counter_1_ok, bee_counter_1_protocol_version, bee_counter_1_status_flags,
                    bee_counter_1_uptime_s, bee_counter_1_num_gates, bee_counter_1_gates_healthy,
                    bee_counter_1_total_in, bee_counter_1_total_out,
                    bee_counter_1_interval_in, bee_counter_1_interval_out,
                    bee_counter_1_glitch_count, bee_counter_1_busy_retries,
                    bee_counter_1_read_attempts, bee_counter_1_latch_succeeded,
                    bee_counter_2_ok, bee_counter_2_protocol_version, bee_counter_2_status_flags,
                    bee_counter_2_uptime_s, bee_counter_2_num_gates, bee_counter_2_gates_healthy,
                    bee_counter_2_total_in, bee_counter_2_total_out,
                    bee_counter_2_interval_in, bee_counter_2_interval_out,
                    bee_counter_2_glitch_count, bee_counter_2_busy_retries,
                    bee_counter_2_read_attempts, bee_counter_2_latch_succeeded,
                    accel_1_ok, accel_1_sample_rate_hz, accel_1_sample_count,
                    accel_1_range_g, accel_1_rms_mg, accel_1_peak_mg,
                    accel_1_band_swarm_mg, accel_1_band_fanning_mg, accel_1_band_activity_mg,
                    accel_2_ok, accel_2_sample_rate_hz, accel_2_sample_count,
                    accel_2_range_g, accel_2_rms_mg, accel_2_peak_mg,
                    accel_2_band_swarm_mg, accel_2_band_fanning_mg, accel_2_band_activity_mg,
                    ble_1_humidity_percent, ble_1_pressure_hpa,
                    ble_2_humidity_percent, ble_2_pressure_hpa,
                    hiveheart_1_frequency_hz, hiveheart_1_energy, hiveheart_1_peak, hiveheart_1_battery_v,
                    hiveheart_2_frequency_hz, hiveheart_2_energy, hiveheart_2_peak, hiveheart_2_battery_v,
                    hivescale_1_weight_kg, hivescale_1_raw_weight, hivescale_1_temp_c,
                    hivescale_1_humidity_percent, hivescale_1_pressure_hpa, hivescale_1_battery_v,
                    hivescale_2_weight_kg, hivescale_2_raw_weight, hivescale_2_temp_c,
                    hivescale_2_humidity_percent, hivescale_2_pressure_hpa, hivescale_2_battery_v,
                    raw_json
                )
                VALUES (
                    %(device_id)s, %(measured_at)s, %(scale_1_weight_kg)s,
                    %(scale_2_weight_kg)s, %(hive_1_temp_c)s, %(hive_2_temp_c)s,
                    %(hive_1_humidity_percent)s, %(hive_2_humidity_percent)s,
                    %(ambient_temp_c)s, %(ambient_humidity_percent)s,
                    %(battery_voltage)s, %(battery_soc_percent)s,
                    %(battery_alert)s, %(battery_monitor_ok)s, %(solar_monitor_ok)s,
                    %(solar_bus_voltage_v)s, %(solar_shunt_voltage_mv)s,
                    %(solar_load_voltage_v)s, %(solar_current_ma)s,
                    %(solar_power_mw)s, %(network_transport)s, %(cellular_ok)s,
                    %(cellular_csq)s, %(calibration_mode)s, %(boot_count)s,
                    %(time_source)s, %(rssi_dbm)s, %(firmware_version)s,
                    %(config_version)s, %(sd_ok)s, %(rtc_ok)s, %(sht_ok)s,
                    %(scale_1_raw)s, %(scale_2_raw)s,
                    %(mic_ok)s, %(mic_sample_rate_hz)s, %(mic_sample_frames)s,
                    %(mic_left_ok)s, %(mic_left_rms_dbfs)s, %(mic_left_peak_dbfs)s, %(mic_left_rms_normalized)s,
                    %(mic_right_ok)s, %(mic_right_rms_dbfs)s, %(mic_right_peak_dbfs)s, %(mic_right_rms_normalized)s,
                    %(mic_left_band_sub_bass_dbfs)s, %(mic_left_band_hum_dbfs)s, %(mic_left_band_piping_dbfs)s,
                    %(mic_left_band_stress_dbfs)s, %(mic_left_band_high_dbfs)s,
                    %(mic_right_band_sub_bass_dbfs)s, %(mic_right_band_hum_dbfs)s, %(mic_right_band_piping_dbfs)s,
                    %(mic_right_band_stress_dbfs)s, %(mic_right_band_high_dbfs)s,
                    %(bee_counter_1_ok)s, %(bee_counter_1_protocol_version)s, %(bee_counter_1_status_flags)s,
                    %(bee_counter_1_uptime_s)s, %(bee_counter_1_num_gates)s, %(bee_counter_1_gates_healthy)s,
                    %(bee_counter_1_total_in)s, %(bee_counter_1_total_out)s,
                    %(bee_counter_1_interval_in)s, %(bee_counter_1_interval_out)s,
                    %(bee_counter_1_glitch_count)s, %(bee_counter_1_busy_retries)s,
                    %(bee_counter_1_read_attempts)s, %(bee_counter_1_latch_succeeded)s,
                    %(bee_counter_2_ok)s, %(bee_counter_2_protocol_version)s, %(bee_counter_2_status_flags)s,
                    %(bee_counter_2_uptime_s)s, %(bee_counter_2_num_gates)s, %(bee_counter_2_gates_healthy)s,
                    %(bee_counter_2_total_in)s, %(bee_counter_2_total_out)s,
                    %(bee_counter_2_interval_in)s, %(bee_counter_2_interval_out)s,
                    %(bee_counter_2_glitch_count)s, %(bee_counter_2_busy_retries)s,
                    %(bee_counter_2_read_attempts)s, %(bee_counter_2_latch_succeeded)s,
                    %(accel_1_ok)s, %(accel_1_sample_rate_hz)s, %(accel_1_sample_count)s,
                    %(accel_1_range_g)s, %(accel_1_rms_mg)s, %(accel_1_peak_mg)s,
                    %(accel_1_band_swarm_mg)s, %(accel_1_band_fanning_mg)s, %(accel_1_band_activity_mg)s,
                    %(accel_2_ok)s, %(accel_2_sample_rate_hz)s, %(accel_2_sample_count)s,
                    %(accel_2_range_g)s, %(accel_2_rms_mg)s, %(accel_2_peak_mg)s,
                    %(accel_2_band_swarm_mg)s, %(accel_2_band_fanning_mg)s, %(accel_2_band_activity_mg)s,
                    %(ble_1_humidity_percent)s, %(ble_1_pressure_hpa)s,
                    %(ble_2_humidity_percent)s, %(ble_2_pressure_hpa)s,
                    %(hiveheart_1_frequency_hz)s, %(hiveheart_1_energy)s, %(hiveheart_1_peak)s, %(hiveheart_1_battery_v)s,
                    %(hiveheart_2_frequency_hz)s, %(hiveheart_2_energy)s, %(hiveheart_2_peak)s, %(hiveheart_2_battery_v)s,
                    %(hivescale_1_weight_kg)s, %(hivescale_1_raw_weight)s, %(hivescale_1_temp_c)s,
                    %(hivescale_1_humidity_percent)s, %(hivescale_1_pressure_hpa)s, %(hivescale_1_battery_v)s,
                    %(hivescale_2_weight_kg)s, %(hivescale_2_raw_weight)s, %(hivescale_2_temp_c)s,
                    %(hivescale_2_humidity_percent)s, %(hivescale_2_pressure_hpa)s, %(hivescale_2_battery_v)s,
                    %(raw_json)s
                )"""


def measurement_insert_params(payload: "MeasurementIn", measured_at: datetime) -> dict:
    """Build the named-parameter dict for ``MEASUREMENT_INSERT_SQL`` from a payload."""
    return {
        "device_id": payload.device_id,
        "measured_at": measured_at,
        "scale_1_weight_kg": payload.scale_1_weight_kg,
        "scale_2_weight_kg": payload.scale_2_weight_kg,
        "hive_1_temp_c": payload.hive_1_temp_c,
        "hive_2_temp_c": payload.hive_2_temp_c,
        "hive_1_humidity_percent": payload.hive_1_humidity_percent,
        "hive_2_humidity_percent": payload.hive_2_humidity_percent,
        "ambient_temp_c": payload.ambient_temp_c,
        "ambient_humidity_percent": payload.ambient_humidity_percent,
        "battery_voltage": payload.battery_voltage_v if payload.battery_voltage_v is not None else payload.battery_voltage,
        "battery_soc_percent": payload.battery_soc_percent,
        "battery_alert": payload.battery_alert,
        "battery_monitor_ok": payload.battery_monitor_ok,
        "solar_monitor_ok": payload.solar_monitor_ok,
        "solar_bus_voltage_v": payload.solar_bus_voltage_v,
        "solar_shunt_voltage_mv": payload.solar_shunt_voltage_mv,
        "solar_load_voltage_v": payload.solar_load_voltage_v,
        "solar_current_ma": payload.solar_current_ma,
        "solar_power_mw": payload.solar_power_mw,
        "network_transport": payload.network_transport,
        "cellular_ok": payload.cellular_ok,
        "cellular_csq": payload.cellular_csq,
        "calibration_mode": payload.calibration_mode,
        "boot_count": payload.boot_count,
        "time_source": payload.time_source,
        "rssi_dbm": payload.rssi_dbm,
        "firmware_version": payload.firmware_version,
        "config_version": payload.config_version,
        "sd_ok": payload.sd_ok,
        "rtc_ok": payload.rtc_ok,
        "sht_ok": payload.sht_ok,
        "scale_1_raw": payload.scale_1_raw,
        "scale_2_raw": payload.scale_2_raw,
        "mic_ok": payload.mic_ok,
        "mic_sample_rate_hz": payload.mic_sample_rate_hz,
        "mic_sample_frames": payload.mic_sample_frames,
        "mic_left_ok": payload.mic_left_ok,
        "mic_left_rms_dbfs": payload.mic_left_rms_dbfs,
        "mic_left_peak_dbfs": payload.mic_left_peak_dbfs,
        "mic_left_rms_normalized": payload.mic_left_rms_normalized,
        "mic_right_ok": payload.mic_right_ok,
        "mic_right_rms_dbfs": payload.mic_right_rms_dbfs,
        "mic_right_peak_dbfs": payload.mic_right_peak_dbfs,
        "mic_right_rms_normalized": payload.mic_right_rms_normalized,
        "mic_left_band_sub_bass_dbfs":  payload.mic_left_band_sub_bass_dbfs,
        "mic_left_band_hum_dbfs":       payload.mic_left_band_hum_dbfs,
        "mic_left_band_piping_dbfs":    payload.mic_left_band_piping_dbfs,
        "mic_left_band_stress_dbfs":    payload.mic_left_band_stress_dbfs,
        "mic_left_band_high_dbfs":      payload.mic_left_band_high_dbfs,
        "mic_right_band_sub_bass_dbfs": payload.mic_right_band_sub_bass_dbfs,
        "mic_right_band_hum_dbfs":      payload.mic_right_band_hum_dbfs,
        "mic_right_band_piping_dbfs":   payload.mic_right_band_piping_dbfs,
        "mic_right_band_stress_dbfs":   payload.mic_right_band_stress_dbfs,
        "mic_right_band_high_dbfs":     payload.mic_right_band_high_dbfs,
        "bee_counter_1_ok":               payload.bee_counter_1_ok,
        "bee_counter_1_protocol_version": payload.bee_counter_1_protocol_version,
        "bee_counter_1_status_flags":     payload.bee_counter_1_status_flags,
        "bee_counter_1_uptime_s":         payload.bee_counter_1_uptime_s,
        "bee_counter_1_num_gates":        payload.bee_counter_1_num_gates,
        "bee_counter_1_gates_healthy":    payload.bee_counter_1_gates_healthy,
        "bee_counter_1_total_in":         payload.bee_counter_1_total_in,
        "bee_counter_1_total_out":        payload.bee_counter_1_total_out,
        "bee_counter_1_interval_in":      payload.bee_counter_1_interval_in,
        "bee_counter_1_interval_out":     payload.bee_counter_1_interval_out,
        "bee_counter_1_glitch_count":     payload.bee_counter_1_glitch_count,
        "bee_counter_1_busy_retries":     payload.bee_counter_1_busy_retries,
        "bee_counter_1_read_attempts":    payload.bee_counter_1_read_attempts,
        "bee_counter_1_latch_succeeded":  payload.bee_counter_1_latch_succeeded,
        "bee_counter_2_ok":               payload.bee_counter_2_ok,
        "bee_counter_2_protocol_version": payload.bee_counter_2_protocol_version,
        "bee_counter_2_status_flags":     payload.bee_counter_2_status_flags,
        "bee_counter_2_uptime_s":         payload.bee_counter_2_uptime_s,
        "bee_counter_2_num_gates":        payload.bee_counter_2_num_gates,
        "bee_counter_2_gates_healthy":    payload.bee_counter_2_gates_healthy,
        "bee_counter_2_total_in":         payload.bee_counter_2_total_in,
        "bee_counter_2_total_out":        payload.bee_counter_2_total_out,
        "bee_counter_2_interval_in":      payload.bee_counter_2_interval_in,
        "bee_counter_2_interval_out":     payload.bee_counter_2_interval_out,
        "bee_counter_2_glitch_count":     payload.bee_counter_2_glitch_count,
        "bee_counter_2_busy_retries":     payload.bee_counter_2_busy_retries,
        "bee_counter_2_read_attempts":    payload.bee_counter_2_read_attempts,
        "bee_counter_2_latch_succeeded":  payload.bee_counter_2_latch_succeeded,
        "accel_1_ok":               payload.accel_1_ok,
        "accel_1_sample_rate_hz":   payload.accel_1_sample_rate_hz,
        "accel_1_sample_count":     payload.accel_1_sample_count,
        "accel_1_range_g":          payload.accel_1_range_g,
        "accel_1_rms_mg":           payload.accel_1_rms_mg,
        "accel_1_peak_mg":          payload.accel_1_peak_mg,
        "accel_1_band_swarm_mg":    payload.accel_1_band_swarm_mg,
        "accel_1_band_fanning_mg":  payload.accel_1_band_fanning_mg,
        "accel_1_band_activity_mg": payload.accel_1_band_activity_mg,
        "accel_2_ok":               payload.accel_2_ok,
        "accel_2_sample_rate_hz":   payload.accel_2_sample_rate_hz,
        "accel_2_sample_count":     payload.accel_2_sample_count,
        "accel_2_range_g":          payload.accel_2_range_g,
        "accel_2_rms_mg":           payload.accel_2_rms_mg,
        "accel_2_peak_mg":          payload.accel_2_peak_mg,
        "accel_2_band_swarm_mg":    payload.accel_2_band_swarm_mg,
        "accel_2_band_fanning_mg":  payload.accel_2_band_fanning_mg,
        "accel_2_band_activity_mg": payload.accel_2_band_activity_mg,
        "ble_1_humidity_percent":   payload.ble_1_humidity_percent,
        "ble_1_pressure_hpa":       payload.ble_1_pressure_hpa,
        "ble_2_humidity_percent":   payload.ble_2_humidity_percent,
        "ble_2_pressure_hpa":       payload.ble_2_pressure_hpa,
        "hiveheart_1_frequency_hz":     payload.hiveheart_1_frequency_hz,
        "hiveheart_1_energy":           payload.hiveheart_1_energy,
        "hiveheart_1_peak":             payload.hiveheart_1_peak,
        "hiveheart_1_battery_v":        payload.hiveheart_1_battery_v,
        "hiveheart_2_frequency_hz":     payload.hiveheart_2_frequency_hz,
        "hiveheart_2_energy":           payload.hiveheart_2_energy,
        "hiveheart_2_peak":             payload.hiveheart_2_peak,
        "hiveheart_2_battery_v":        payload.hiveheart_2_battery_v,
        "hivescale_1_weight_kg":        payload.hivescale_1_weight_kg,
        "hivescale_1_raw_weight":       payload.hivescale_1_raw_weight,
        "hivescale_1_temp_c":           payload.hivescale_1_temp_c,
        "hivescale_1_humidity_percent": payload.hivescale_1_humidity_percent,
        "hivescale_1_pressure_hpa":     payload.hivescale_1_pressure_hpa,
        "hivescale_1_battery_v":        payload.hivescale_1_battery_v,
        "hivescale_2_weight_kg":        payload.hivescale_2_weight_kg,
        "hivescale_2_raw_weight":       payload.hivescale_2_raw_weight,
        "hivescale_2_temp_c":           payload.hivescale_2_temp_c,
        "hivescale_2_humidity_percent": payload.hivescale_2_humidity_percent,
        "hivescale_2_pressure_hpa":     payload.hivescale_2_pressure_hpa,
        "hivescale_2_battery_v":        payload.hivescale_2_battery_v,
        "raw_json": psycopg.types.json.Jsonb(payload.model_dump(mode="json", exclude={"claim_code"})),
    }


# ── Multi-hive ingest helpers ────────────────────────────────────────────────

HIVE_READINGS_INSERT_SQL = """
    INSERT INTO hive_readings (
        measurement_id, device_id, measured_at, hive_index, name,
        weight_kg, raw_weight, scale_source, temp_c, temp_source, humidity_percent,
        accel_ok, accel_sample_count, accel_range_g, accel_rms_mg, accel_peak_mg,
        accel_band_swarm_mg, accel_band_fanning_mg, accel_band_activity_mg,
        ble_present, ble_sensor_type, ble_humidity_percent, ble_pressure_hpa,
        ble_battery_percent, ble_rssi_dbm,
        bee_counter_ok, bee_counter_total_in, bee_counter_total_out,
        bee_counter_interval_in, bee_counter_interval_out, raw_json
    ) VALUES (
        %(measurement_id)s, %(device_id)s, %(measured_at)s, %(hive_index)s, %(name)s,
        %(weight_kg)s, %(raw_weight)s, %(scale_source)s, %(temp_c)s, %(temp_source)s, %(humidity_percent)s,
        %(accel_ok)s, %(accel_sample_count)s, %(accel_range_g)s, %(accel_rms_mg)s, %(accel_peak_mg)s,
        %(accel_band_swarm_mg)s, %(accel_band_fanning_mg)s, %(accel_band_activity_mg)s,
        %(ble_present)s, %(ble_sensor_type)s, %(ble_humidity_percent)s, %(ble_pressure_hpa)s,
        %(ble_battery_percent)s, %(ble_rssi_dbm)s,
        %(bee_counter_ok)s, %(bee_counter_total_in)s, %(bee_counter_total_out)s,
        %(bee_counter_interval_in)s, %(bee_counter_interval_out)s, %(raw_json)s
    )
    ON CONFLICT (measurement_id, hive_index) DO NOTHING
"""


def _hive_reading_row_params(device_id: str, h: "HiveReadingIn",
                             measurement_id: int, measured_at: datetime) -> dict:
    a, b, c = h.accel, h.ble, h.bee_counter
    return {
        "measurement_id": measurement_id,
        "device_id": device_id,
        "measured_at": measured_at,
        "hive_index": h.index,
        "name": h.name,
        "weight_kg": h.weight_kg,
        "raw_weight": h.raw_weight,
        "scale_source": h.scale_source,
        "temp_c": h.temp_c,
        "temp_source": h.temp_source,
        "humidity_percent": h.humidity_percent,
        "accel_ok": a.ok if a else None,
        "accel_sample_count": a.sample_count if a else None,
        "accel_range_g": a.range_g if a else None,
        "accel_rms_mg": a.rms_mg if a else None,
        "accel_peak_mg": a.peak_mg if a else None,
        "accel_band_swarm_mg": a.band_swarm_mg if a else None,
        "accel_band_fanning_mg": a.band_fanning_mg if a else None,
        "accel_band_activity_mg": a.band_activity_mg if a else None,
        "ble_present": b.present if b else None,
        "ble_sensor_type": b.sensor_type if b else None,
        "ble_humidity_percent": b.humidity_percent if b else None,
        "ble_pressure_hpa": b.pressure_hpa if b else None,
        "ble_battery_percent": b.battery_percent if b else None,
        "ble_rssi_dbm": b.rssi_dbm if b else None,
        "bee_counter_ok": c.ok if c else None,
        "bee_counter_total_in": c.total_in if c else None,
        "bee_counter_total_out": c.total_out if c else None,
        "bee_counter_interval_in": c.interval_in if c else None,
        "bee_counter_interval_out": c.interval_out if c else None,
        "raw_json": psycopg.types.json.Jsonb(h.model_dump(mode="json", exclude_none=True)),
    }


def overlay_legacy_hive_columns(params: dict, payload: "MeasurementIn") -> None:
    """When a payload carries the hives[] array (new firmware), copy hives 1–2 onto
    the legacy measurements columns the column-based read / insights / temp-comp
    still use. Only fills a column the flat payload left None, so an explicit flat
    field always wins."""
    if not payload.hives:
        return
    by_index = {h.index: h for h in payload.hives}
    for n in (1, 2):
        h = by_index.get(n)
        if h is None:
            continue

        def put(col, val):
            if val is not None and params.get(col) is None:
                params[col] = val

        put(f"scale_{n}_weight_kg", h.weight_kg)
        put(f"scale_{n}_raw", h.raw_weight)
        put(f"hive_{n}_temp_c", h.temp_c)
        put(f"hive_{n}_humidity_percent", h.humidity_percent)
        if h.accel:
            put(f"accel_{n}_ok", h.accel.ok)
            put(f"accel_{n}_sample_count", h.accel.sample_count)
            put(f"accel_{n}_range_g", h.accel.range_g)
            put(f"accel_{n}_rms_mg", h.accel.rms_mg)
            put(f"accel_{n}_peak_mg", h.accel.peak_mg)
            put(f"accel_{n}_band_swarm_mg", h.accel.band_swarm_mg)
            put(f"accel_{n}_band_fanning_mg", h.accel.band_fanning_mg)
            put(f"accel_{n}_band_activity_mg", h.accel.band_activity_mg)
        if h.ble:
            put(f"ble_{n}_humidity_percent", h.ble.humidity_percent)
            put(f"ble_{n}_pressure_hpa", h.ble.pressure_hpa)
        if h.bee_counter:
            put(f"bee_counter_{n}_ok", h.bee_counter.ok)
            put(f"bee_counter_{n}_total_in", h.bee_counter.total_in)
            put(f"bee_counter_{n}_total_out", h.bee_counter.total_out)
            put(f"bee_counter_{n}_interval_in", h.bee_counter.interval_in)
            put(f"bee_counter_{n}_interval_out", h.bee_counter.interval_out)


def insert_hive_readings(cur, payload: "MeasurementIn",
                         measurement_id: int, measured_at: datetime) -> None:
    """Fan a payload's hives[] array into hive_readings rows. No-op for legacy
    (flat-field) payloads — the read path synthesizes hives 1–2 from the legacy
    columns for those."""
    if not payload.hives:
        return
    seen: set[int] = set()
    rows = []
    for h in payload.hives:
        if h.index in seen:
            continue
        seen.add(h.index)
        rows.append(_hive_reading_row_params(payload.device_id, h, measurement_id, measured_at))
    if rows:
        cur.executemany(HIVE_READINGS_INSERT_SQL, rows)


def resolve_measured_at(
    client_ts: "datetime | None",
    device_id: str,
    now: "datetime | None" = None,
) -> datetime:
    """Return a trustworthy ``measured_at``, falling back to the server clock.

    A device whose RTC and NTP have both failed sends no timestamp, or the 1970
    epoch fallback, or (more generally) a clock far from reality. Storing that
    verbatim freezes "last data" in the dashboard even though uploads keep
    arriving, and on the SD-import path it collapses every untimed row onto a
    single ``(device_id, measured_at)`` natural key. For any missing or
    implausible timestamp we substitute the server clock at ingest.

    Both ingest paths — live POST and bulk SD import — must resolve timestamps
    through this one helper so they can never drift apart. Because there is no
    RTC/NTP anchor for such a reading, the resulting ``measured_at`` is the
    server's receive time; it is the best estimate available, not the true
    moment of measurement.
    """
    now = now or datetime.now(timezone.utc)
    if client_ts is not None and (
        client_ts.year >= MIN_PLAUSIBLE_YEAR and client_ts <= now + timedelta(days=1)
    ):
        return client_ts
    if client_ts is not None:
        logger.warning(
            "Ignoring implausible client timestamp %s from device %s; using server time",
            client_ts.isoformat(), device_id,
        )
    return now


@router.post("/api/v1/measurements")
def create_measurement(payload: MeasurementIn, x_api_key: str = Header(default="")):
    if len(x_api_key) < 16:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = datetime.now(timezone.utc)
    measured_at = resolve_measured_at(payload.timestamp, payload.device_id, now)
    claimed = ensure_device_config(
        payload.device_id, payload.claim_code, payload.firmware_version, x_api_key,
        touch_last_seen=True,
    )
    params = measurement_insert_params(payload, measured_at)
    overlay_legacy_hive_columns(params, payload)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(MEASUREMENT_INSERT_SQL + " RETURNING id;", params)
            new_id = cur.fetchone()[0]
            # Fan the hives[] array (if any) into the normalized child table.
            insert_hive_readings(cur, payload, new_id, measured_at)
            conn.commit()
    # Mirror the reading to MQTT (Home Assistant etc.) when the bridge is enabled.
    # This is purely additive and fail-soft — it never affects the stored result.
    # Pass the device's temp-compensation config so the live payload carries
    # scale_{1,2}_weight_kg_compensated alongside the raw weights; the lookup is
    # skipped entirely when the bridge is disabled.
    if mqtt_publisher.is_active():
        mqtt_tempco = load_tempco_configs([payload.device_id]).get(payload.device_id)
        mqtt_publisher.publish_measurement(
            payload.device_id,
            payload.model_dump(mode="json", exclude_none=True, exclude={"claim_code"}),
            measured_at,
            tempco=mqtt_tempco,
        )
    # `claimed` lets the firmware defer latching its "claim registered" flag until
    # the server has actually recorded the claim. Until then the device keeps
    # sending its claim code, so a rebuilt/restored backend re-learns it and the
    # device stays claimable (see server/devices.ensure_device_config).
    return {
        "status": "ok",
        "id": new_id,
        "measured_at": measured_at.isoformat(),
        "claimed": claimed,
    }


@router.post(
    "/api/v1/app/devices/{device_id}/measurements/import",
    dependencies=[Depends(require_hivepal_service_key)],
)
def import_measurements(
    device_id: str,
    payload: MeasurementImportIn,
    user_id: str = Depends(require_user_id),
):
    """Bulk-import measurements parsed from a device's SD card backup.

    Called by the HivePal web backend after a beekeeper uploads the NDJSON/TAR
    they pulled from the scale in AP mode. The device must already be claimed by
    a user with owner/admin access — we never auto-create devices from uploaded
    data, since the file's ``device_id`` is attacker-controllable and ownership
    is established through the claim-code flow.

    Re-importing the same file is a no-op: ``(device_id, measured_at)`` is treated
    as the natural key and existing rows are skipped, so duplicates inside the
    file and rows already stored are both counted and ignored.
    """
    require_device_role(user_id, device_id, ["owner", "admin"])
    return bulk_import_measurements(device_id, payload.measurements)


def bulk_import_measurements(
    device_id: str, measurements: list[MeasurementIn]
) -> dict:
    """Insert a batch of measurements for ``device_id``, skipping duplicates.

    Shared by the HivePal-facing bulk import endpoint
    (``POST /api/v1/app/devices/{device_id}/measurements/import``) and the local
    dashboard's SD-upload endpoint so both paths de-duplicate and fan out
    multi-hive rows identically. The caller is responsible for authorization; this
    function assumes the device is already owned by the requester.

    Returns ``{status, device_id, received, inserted, duplicates}``.
    """
    # Force the path device_id onto every row so a file cannot smuggle readings
    # in under a different device the caller may not own.
    prepared: list[tuple[datetime, MeasurementIn]] = []
    for measurement in measurements:
        # Clamp missing *and* implausible timestamps (e.g. the 1970 epoch a
        # no-RTC device writes into its SD cache) to the server clock, matching
        # the live-ingest path. Storing 1970 verbatim would otherwise both
        # mis-date the reading and collapse every untimed row onto one key.
        measured_at = resolve_measured_at(measurement.timestamp, device_id)
        prepared.append(
            (measured_at, measurement.model_copy(update={"device_id": device_id}))
        )

    # Keep the first record seen for each timestamp (file duplicates are identical).
    record_by_key: dict[datetime, MeasurementIn] = {}
    for measured_at, measurement in prepared:
        record_by_key.setdefault(measured_at, measurement)

    received = len(measurements)
    inserted = 0
    duplicates = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            keys = [measured_at for measured_at, _ in prepared]
            existing: set = set()
            unique_keys = list(record_by_key.keys())
            if unique_keys:
                cur.execute(
                    "SELECT measured_at FROM measurements "
                    "WHERE device_id = %s AND measured_at = ANY(%s);",
                    (device_id, unique_keys),
                )
                existing = {row[0] for row in cur.fetchall()}

            new_keys, duplicates = split_new_and_duplicate(keys, existing)
            if new_keys:
                def _params(key):
                    p = measurement_insert_params(record_by_key[key], key)
                    overlay_legacy_hive_columns(p, record_by_key[key])
                    return p

                cur.executemany(
                    MEASUREMENT_INSERT_SQL + ";",
                    [_params(key) for key in new_keys],
                )
                inserted = len(new_keys)

                # Fan any multi-hive payloads into hive_readings. executemany gives
                # no ids back, so look up the rows we just inserted by their key.
                hive_keys = [k for k in new_keys if record_by_key[k].hives]
                if hive_keys:
                    cur.execute(
                        "SELECT measured_at, id FROM measurements "
                        "WHERE device_id = %s AND measured_at = ANY(%s);",
                        (device_id, hive_keys),
                    )
                    id_by_key = {row[0]: row[1] for row in cur.fetchall()}
                    for key in hive_keys:
                        mid = id_by_key.get(key)
                        if mid is not None:
                            insert_hive_readings(cur, record_by_key[key], mid, key)
            conn.commit()

    return {
        "status": "ok",
        "device_id": device_id,
        "received": received,
        "inserted": inserted,
        "duplicates": duplicates,
    }


# ---------------------------------------------------------------------------
# Indices for measurement_row_to_dict (keep in sync with SELECT below):
#
#  0  id                        17  scale_1_raw
#  1  device_id                 18  scale_2_raw
#  2  measured_at               19  battery_soc_percent
#  3  received_at               20  battery_alert
#  4  scale_1_weight_kg         21  battery_monitor_ok
#  5  scale_2_weight_kg         22  solar_monitor_ok
#  6  hive_1_temp_c             23  solar_bus_voltage_v
#  7  hive_2_temp_c             24  solar_shunt_voltage_mv
#  8  ambient_temp_c            25  solar_load_voltage_v
#  9  ambient_humidity_percent  26  solar_current_ma
# 10  battery_voltage           27  solar_power_mw
# 11  rssi_dbm                  28  network_transport
# 12  firmware_version          29  cellular_ok
# 13  config_version            30  cellular_csq
# 14  sd_ok                     31  calibration_mode
# 15  rtc_ok                    32  boot_count
# 16  sht_ok                    33  time_source
#                               34  mic_ok
#                               35  mic_sample_rate_hz
#                               36  mic_sample_frames
#                               37  mic_left_ok
#                               38  mic_left_rms_dbfs
#                               39  mic_left_peak_dbfs
#                               40  mic_left_rms_normalized
#                               41  mic_right_ok
#                               42  mic_right_rms_dbfs
#                               43  mic_right_peak_dbfs
#                               44  mic_right_rms_normalized
#                               45  mic_left_band_sub_bass_dbfs
#                               46  mic_left_band_hum_dbfs
#                               47  mic_left_band_piping_dbfs
#                               48  mic_left_band_stress_dbfs
#                               49  mic_left_band_high_dbfs
#                               50  mic_right_band_sub_bass_dbfs
#                               51  mic_right_band_hum_dbfs
#                               52  mic_right_band_piping_dbfs
#                               53  mic_right_band_stress_dbfs
#                               54  mic_right_band_high_dbfs
#                               55  bee_counter_1_ok
#                               56  bee_counter_1_protocol_version
#                               57  bee_counter_1_status_flags
#                               58  bee_counter_1_uptime_s
#                               59  bee_counter_1_num_gates
#                               60  bee_counter_1_gates_healthy
#                               61  bee_counter_1_total_in
#                               62  bee_counter_1_total_out
#                               63  bee_counter_1_interval_in
#                               64  bee_counter_1_interval_out
#                               65  bee_counter_1_glitch_count
#                               66  bee_counter_1_busy_retries
#                               67  bee_counter_1_read_attempts
#                               68  bee_counter_1_latch_succeeded
#                               69  bee_counter_2_ok
#                               70  bee_counter_2_protocol_version
#                               71  bee_counter_2_status_flags
#                               72  bee_counter_2_uptime_s
#                               73  bee_counter_2_num_gates
#                               74  bee_counter_2_gates_healthy
#                               75  bee_counter_2_total_in
#                               76  bee_counter_2_total_out
#                               77  bee_counter_2_interval_in
#                               78  bee_counter_2_interval_out
#                               79  bee_counter_2_glitch_count
#                               80  bee_counter_2_busy_retries
#                               81  bee_counter_2_read_attempts
#                               82  bee_counter_2_latch_succeeded
#                               83  accel_1_ok
#                               84  accel_1_sample_rate_hz
#                               85  accel_1_sample_count
#                               86  accel_1_range_g
#                               87  accel_1_rms_mg
#                               88  accel_1_peak_mg
#                               89  accel_1_band_swarm_mg
#                               90  accel_1_band_fanning_mg
#                               91  accel_1_band_activity_mg
#                               92  accel_2_ok
#                               93  accel_2_sample_rate_hz
#                               94  accel_2_sample_count
#                               95  accel_2_range_g
#                               96  accel_2_rms_mg
#                               97  accel_2_peak_mg
#                               98  accel_2_band_swarm_mg
#                               99  accel_2_band_fanning_mg
#                              100  accel_2_band_activity_mg
#                              101  ble_1_humidity_percent
#                              102  ble_1_pressure_hpa
#                              103  ble_2_humidity_percent
#                              104  ble_2_pressure_hpa
#                              105  hive_1_humidity_percent
#                              106  hive_2_humidity_percent
#  (107..130 hiveheart/hivescale fields — see SELECT order below)
#                              131  ble_1_battery_percent
#                              132  ble_1_rssi_dbm
#                              133  ble_2_battery_percent
#                              134  ble_2_rssi_dbm
#                              135  ble_1_firmware_version
#                              136  ble_2_firmware_version
# ---------------------------------------------------------------------------

MEASUREMENT_SELECT_COLUMNS = """
    id, device_id, measured_at, received_at, scale_1_weight_kg,
    scale_2_weight_kg, hive_1_temp_c, hive_2_temp_c,
    ambient_temp_c, ambient_humidity_percent,
    COALESCE(battery_voltage, NULLIF(raw_json->>'battery_voltage_v', '')::double precision, NULLIF(raw_json->>'battery_voltage', '')::double precision) AS battery_voltage,
    rssi_dbm, firmware_version, config_version, sd_ok, rtc_ok, sht_ok,
    scale_1_raw, scale_2_raw,
    COALESCE(battery_soc_percent, NULLIF(raw_json->>'battery_soc_percent', '')::double precision) AS battery_soc_percent,
    COALESCE(battery_alert, NULLIF(raw_json->>'battery_alert', '')::boolean) AS battery_alert,
    COALESCE(battery_monitor_ok, NULLIF(raw_json->>'battery_monitor_ok', '')::boolean) AS battery_monitor_ok,
    COALESCE(solar_monitor_ok, NULLIF(raw_json->>'solar_monitor_ok', '')::boolean) AS solar_monitor_ok,
    COALESCE(solar_bus_voltage_v, NULLIF(raw_json->>'solar_bus_voltage_v', '')::double precision) AS solar_bus_voltage_v,
    COALESCE(solar_shunt_voltage_mv, NULLIF(raw_json->>'solar_shunt_voltage_mv', '')::double precision) AS solar_shunt_voltage_mv,
    COALESCE(solar_load_voltage_v, NULLIF(raw_json->>'solar_load_voltage_v', '')::double precision) AS solar_load_voltage_v,
    COALESCE(solar_current_ma, NULLIF(raw_json->>'solar_current_ma', '')::double precision) AS solar_current_ma,
    COALESCE(solar_power_mw, NULLIF(raw_json->>'solar_power_mw', '')::double precision) AS solar_power_mw,
    COALESCE(network_transport, raw_json->>'network_transport') AS network_transport,
    COALESCE(cellular_ok, NULLIF(raw_json->>'cellular_ok', '')::boolean) AS cellular_ok,
    COALESCE(cellular_csq, NULLIF(raw_json->>'cellular_csq', '')::integer) AS cellular_csq,
    COALESCE(calibration_mode, NULLIF(raw_json->>'calibration_mode', '')::boolean) AS calibration_mode,
    COALESCE(boot_count, NULLIF(raw_json->>'boot_count', '')::bigint) AS boot_count,
    COALESCE(time_source, raw_json->>'time_source') AS time_source,
    COALESCE(mic_ok,                   NULLIF(raw_json->>'mic_ok',                   '')::boolean)          AS mic_ok,
    COALESCE(mic_sample_rate_hz,       NULLIF(raw_json->>'mic_sample_rate_hz',       '')::integer)          AS mic_sample_rate_hz,
    COALESCE(mic_sample_frames,        NULLIF(raw_json->>'mic_sample_frames',        '')::integer)          AS mic_sample_frames,
    COALESCE(mic_left_ok,              NULLIF(raw_json->>'mic_left_ok',              '')::boolean)          AS mic_left_ok,
    COALESCE(mic_left_rms_dbfs,        NULLIF(raw_json->>'mic_left_rms_dbfs',        '')::double precision) AS mic_left_rms_dbfs,
    COALESCE(mic_left_peak_dbfs,       NULLIF(raw_json->>'mic_left_peak_dbfs',       '')::double precision) AS mic_left_peak_dbfs,
    COALESCE(mic_left_rms_normalized,  NULLIF(raw_json->>'mic_left_rms_normalized',  '')::double precision) AS mic_left_rms_normalized,
    COALESCE(mic_right_ok,             NULLIF(raw_json->>'mic_right_ok',             '')::boolean)          AS mic_right_ok,
    COALESCE(mic_right_rms_dbfs,       NULLIF(raw_json->>'mic_right_rms_dbfs',       '')::double precision) AS mic_right_rms_dbfs,
    COALESCE(mic_right_peak_dbfs,      NULLIF(raw_json->>'mic_right_peak_dbfs',      '')::double precision) AS mic_right_peak_dbfs,
    COALESCE(mic_right_rms_normalized, NULLIF(raw_json->>'mic_right_rms_normalized', '')::double precision) AS mic_right_rms_normalized,
    COALESCE(mic_left_band_sub_bass_dbfs,  NULLIF(raw_json->>'mic_left_band_sub_bass_dbfs',  '')::double precision) AS mic_left_band_sub_bass_dbfs,
    COALESCE(mic_left_band_hum_dbfs,       NULLIF(raw_json->>'mic_left_band_hum_dbfs',       '')::double precision) AS mic_left_band_hum_dbfs,
    COALESCE(mic_left_band_piping_dbfs,    NULLIF(raw_json->>'mic_left_band_piping_dbfs',    '')::double precision) AS mic_left_band_piping_dbfs,
    COALESCE(mic_left_band_stress_dbfs,    NULLIF(raw_json->>'mic_left_band_stress_dbfs',    '')::double precision) AS mic_left_band_stress_dbfs,
    COALESCE(mic_left_band_high_dbfs,      NULLIF(raw_json->>'mic_left_band_high_dbfs',      '')::double precision) AS mic_left_band_high_dbfs,
    COALESCE(mic_right_band_sub_bass_dbfs, NULLIF(raw_json->>'mic_right_band_sub_bass_dbfs', '')::double precision) AS mic_right_band_sub_bass_dbfs,
    COALESCE(mic_right_band_hum_dbfs,      NULLIF(raw_json->>'mic_right_band_hum_dbfs',      '')::double precision) AS mic_right_band_hum_dbfs,
    COALESCE(mic_right_band_piping_dbfs,   NULLIF(raw_json->>'mic_right_band_piping_dbfs',   '')::double precision) AS mic_right_band_piping_dbfs,
    COALESCE(mic_right_band_stress_dbfs,   NULLIF(raw_json->>'mic_right_band_stress_dbfs',   '')::double precision) AS mic_right_band_stress_dbfs,
    COALESCE(mic_right_band_high_dbfs,     NULLIF(raw_json->>'mic_right_band_high_dbfs',     '')::double precision) AS mic_right_band_high_dbfs,
    COALESCE(bee_counter_1_ok,                NULLIF(raw_json->>'bee_counter_1_ok',                '')::boolean) AS bee_counter_1_ok,
    COALESCE(bee_counter_1_protocol_version,  NULLIF(raw_json->>'bee_counter_1_protocol_version',  '')::integer) AS bee_counter_1_protocol_version,
    COALESCE(bee_counter_1_status_flags,      NULLIF(raw_json->>'bee_counter_1_status_flags',      '')::integer) AS bee_counter_1_status_flags,
    COALESCE(bee_counter_1_uptime_s,          NULLIF(raw_json->>'bee_counter_1_uptime_s',          '')::integer) AS bee_counter_1_uptime_s,
    COALESCE(bee_counter_1_num_gates,         NULLIF(raw_json->>'bee_counter_1_num_gates',         '')::integer) AS bee_counter_1_num_gates,
    COALESCE(bee_counter_1_gates_healthy,     NULLIF(raw_json->>'bee_counter_1_gates_healthy',     '')::integer) AS bee_counter_1_gates_healthy,
    COALESCE(bee_counter_1_total_in,          NULLIF(raw_json->>'bee_counter_1_total_in',          '')::bigint)  AS bee_counter_1_total_in,
    COALESCE(bee_counter_1_total_out,         NULLIF(raw_json->>'bee_counter_1_total_out',         '')::bigint)  AS bee_counter_1_total_out,
    COALESCE(bee_counter_1_interval_in,       NULLIF(raw_json->>'bee_counter_1_interval_in',       '')::bigint)  AS bee_counter_1_interval_in,
    COALESCE(bee_counter_1_interval_out,      NULLIF(raw_json->>'bee_counter_1_interval_out',      '')::bigint)  AS bee_counter_1_interval_out,
    COALESCE(bee_counter_1_glitch_count,      NULLIF(raw_json->>'bee_counter_1_glitch_count',      '')::integer) AS bee_counter_1_glitch_count,
    COALESCE(bee_counter_1_busy_retries,      NULLIF(raw_json->>'bee_counter_1_busy_retries',      '')::integer) AS bee_counter_1_busy_retries,
    COALESCE(bee_counter_1_read_attempts,     NULLIF(raw_json->>'bee_counter_1_read_attempts',     '')::integer) AS bee_counter_1_read_attempts,
    COALESCE(bee_counter_1_latch_succeeded,   NULLIF(raw_json->>'bee_counter_1_latch_succeeded',   '')::boolean) AS bee_counter_1_latch_succeeded,
    COALESCE(bee_counter_2_ok,                NULLIF(raw_json->>'bee_counter_2_ok',                '')::boolean) AS bee_counter_2_ok,
    COALESCE(bee_counter_2_protocol_version,  NULLIF(raw_json->>'bee_counter_2_protocol_version',  '')::integer) AS bee_counter_2_protocol_version,
    COALESCE(bee_counter_2_status_flags,      NULLIF(raw_json->>'bee_counter_2_status_flags',      '')::integer) AS bee_counter_2_status_flags,
    COALESCE(bee_counter_2_uptime_s,          NULLIF(raw_json->>'bee_counter_2_uptime_s',          '')::integer) AS bee_counter_2_uptime_s,
    COALESCE(bee_counter_2_num_gates,         NULLIF(raw_json->>'bee_counter_2_num_gates',         '')::integer) AS bee_counter_2_num_gates,
    COALESCE(bee_counter_2_gates_healthy,     NULLIF(raw_json->>'bee_counter_2_gates_healthy',     '')::integer) AS bee_counter_2_gates_healthy,
    COALESCE(bee_counter_2_total_in,          NULLIF(raw_json->>'bee_counter_2_total_in',          '')::bigint)  AS bee_counter_2_total_in,
    COALESCE(bee_counter_2_total_out,         NULLIF(raw_json->>'bee_counter_2_total_out',         '')::bigint)  AS bee_counter_2_total_out,
    COALESCE(bee_counter_2_interval_in,       NULLIF(raw_json->>'bee_counter_2_interval_in',       '')::bigint)  AS bee_counter_2_interval_in,
    COALESCE(bee_counter_2_interval_out,      NULLIF(raw_json->>'bee_counter_2_interval_out',      '')::bigint)  AS bee_counter_2_interval_out,
    COALESCE(bee_counter_2_glitch_count,      NULLIF(raw_json->>'bee_counter_2_glitch_count',      '')::integer) AS bee_counter_2_glitch_count,
    COALESCE(bee_counter_2_busy_retries,      NULLIF(raw_json->>'bee_counter_2_busy_retries',      '')::integer) AS bee_counter_2_busy_retries,
    COALESCE(bee_counter_2_read_attempts,     NULLIF(raw_json->>'bee_counter_2_read_attempts',     '')::integer) AS bee_counter_2_read_attempts,
    COALESCE(bee_counter_2_latch_succeeded,   NULLIF(raw_json->>'bee_counter_2_latch_succeeded',   '')::boolean) AS bee_counter_2_latch_succeeded,
    COALESCE(accel_1_ok,               NULLIF(raw_json->>'accel_1_ok',               '')::boolean)          AS accel_1_ok,
    COALESCE(accel_1_sample_rate_hz,   NULLIF(raw_json->>'accel_1_sample_rate_hz',   '')::integer)          AS accel_1_sample_rate_hz,
    COALESCE(accel_1_sample_count,     NULLIF(raw_json->>'accel_1_sample_count',     '')::integer)          AS accel_1_sample_count,
    COALESCE(accel_1_range_g,          NULLIF(raw_json->>'accel_1_range_g',          '')::integer)          AS accel_1_range_g,
    COALESCE(accel_1_rms_mg,           NULLIF(raw_json->>'accel_1_rms_mg',           '')::double precision) AS accel_1_rms_mg,
    COALESCE(accel_1_peak_mg,          NULLIF(raw_json->>'accel_1_peak_mg',          '')::double precision) AS accel_1_peak_mg,
    COALESCE(accel_1_band_swarm_mg,    NULLIF(raw_json->>'accel_1_band_swarm_mg',    '')::double precision) AS accel_1_band_swarm_mg,
    COALESCE(accel_1_band_fanning_mg,  NULLIF(raw_json->>'accel_1_band_fanning_mg',  '')::double precision) AS accel_1_band_fanning_mg,
    COALESCE(accel_1_band_activity_mg, NULLIF(raw_json->>'accel_1_band_activity_mg', '')::double precision) AS accel_1_band_activity_mg,
    COALESCE(accel_2_ok,               NULLIF(raw_json->>'accel_2_ok',               '')::boolean)          AS accel_2_ok,
    COALESCE(accel_2_sample_rate_hz,   NULLIF(raw_json->>'accel_2_sample_rate_hz',   '')::integer)          AS accel_2_sample_rate_hz,
    COALESCE(accel_2_sample_count,     NULLIF(raw_json->>'accel_2_sample_count',     '')::integer)          AS accel_2_sample_count,
    COALESCE(accel_2_range_g,          NULLIF(raw_json->>'accel_2_range_g',          '')::integer)          AS accel_2_range_g,
    COALESCE(accel_2_rms_mg,           NULLIF(raw_json->>'accel_2_rms_mg',           '')::double precision) AS accel_2_rms_mg,
    COALESCE(accel_2_peak_mg,          NULLIF(raw_json->>'accel_2_peak_mg',          '')::double precision) AS accel_2_peak_mg,
    COALESCE(accel_2_band_swarm_mg,    NULLIF(raw_json->>'accel_2_band_swarm_mg',    '')::double precision) AS accel_2_band_swarm_mg,
    COALESCE(accel_2_band_fanning_mg,  NULLIF(raw_json->>'accel_2_band_fanning_mg',  '')::double precision) AS accel_2_band_fanning_mg,
    COALESCE(accel_2_band_activity_mg, NULLIF(raw_json->>'accel_2_band_activity_mg', '')::double precision) AS accel_2_band_activity_mg,
    COALESCE(ble_1_humidity_percent, NULLIF(raw_json->>'ble_1_humidity_percent', '')::double precision) AS ble_1_humidity_percent,
    COALESCE(ble_1_pressure_hpa,     NULLIF(raw_json->>'ble_1_pressure_hpa',     '')::double precision) AS ble_1_pressure_hpa,
    COALESCE(ble_2_humidity_percent, NULLIF(raw_json->>'ble_2_humidity_percent', '')::double precision) AS ble_2_humidity_percent,
    COALESCE(ble_2_pressure_hpa,     NULLIF(raw_json->>'ble_2_pressure_hpa',     '')::double precision) AS ble_2_pressure_hpa,
    COALESCE(hive_1_humidity_percent, NULLIF(raw_json->>'hive_1_humidity_percent', '')::double precision) AS hive_1_humidity_percent,
    COALESCE(hive_2_humidity_percent, NULLIF(raw_json->>'hive_2_humidity_percent', '')::double precision) AS hive_2_humidity_percent,
    COALESCE(hiveheart_1_frequency_hz,     NULLIF(raw_json->>'hiveheart_1_frequency_hz',     '')::double precision) AS hiveheart_1_frequency_hz,
    COALESCE(hiveheart_1_energy,           NULLIF(raw_json->>'hiveheart_1_energy',           '')::integer)          AS hiveheart_1_energy,
    COALESCE(hiveheart_1_peak,             NULLIF(raw_json->>'hiveheart_1_peak',             '')::integer)          AS hiveheart_1_peak,
    COALESCE(hiveheart_1_battery_v,        NULLIF(raw_json->>'hiveheart_1_battery_v',        '')::double precision) AS hiveheart_1_battery_v,
    NULLIF(raw_json->>'hiveheart_1_temp_c',           '')::double precision AS hiveheart_1_temp_c,
    NULLIF(raw_json->>'hiveheart_1_humidity_percent', '')::double precision AS hiveheart_1_humidity_percent,
    COALESCE(hiveheart_2_frequency_hz,     NULLIF(raw_json->>'hiveheart_2_frequency_hz',     '')::double precision) AS hiveheart_2_frequency_hz,
    COALESCE(hiveheart_2_energy,           NULLIF(raw_json->>'hiveheart_2_energy',           '')::integer)          AS hiveheart_2_energy,
    COALESCE(hiveheart_2_peak,             NULLIF(raw_json->>'hiveheart_2_peak',             '')::integer)          AS hiveheart_2_peak,
    COALESCE(hiveheart_2_battery_v,        NULLIF(raw_json->>'hiveheart_2_battery_v',        '')::double precision) AS hiveheart_2_battery_v,
    NULLIF(raw_json->>'hiveheart_2_temp_c',           '')::double precision AS hiveheart_2_temp_c,
    NULLIF(raw_json->>'hiveheart_2_humidity_percent', '')::double precision AS hiveheart_2_humidity_percent,
    COALESCE(hivescale_1_weight_kg,        NULLIF(raw_json->>'hivescale_1_weight_kg',        '')::double precision) AS hivescale_1_weight_kg,
    COALESCE(hivescale_1_raw_weight,       NULLIF(raw_json->>'hivescale_1_raw_weight',       '')::bigint)           AS hivescale_1_raw_weight,
    COALESCE(hivescale_1_temp_c,           NULLIF(raw_json->>'hivescale_1_temp_c',           '')::double precision) AS hivescale_1_temp_c,
    COALESCE(hivescale_1_humidity_percent, NULLIF(raw_json->>'hivescale_1_humidity_percent', '')::double precision) AS hivescale_1_humidity_percent,
    COALESCE(hivescale_1_pressure_hpa,     NULLIF(raw_json->>'hivescale_1_pressure_hpa',     '')::double precision) AS hivescale_1_pressure_hpa,
    COALESCE(hivescale_1_battery_v,        NULLIF(raw_json->>'hivescale_1_battery_v',        '')::double precision) AS hivescale_1_battery_v,
    COALESCE(hivescale_2_weight_kg,        NULLIF(raw_json->>'hivescale_2_weight_kg',        '')::double precision) AS hivescale_2_weight_kg,
    COALESCE(hivescale_2_raw_weight,       NULLIF(raw_json->>'hivescale_2_raw_weight',       '')::bigint)           AS hivescale_2_raw_weight,
    COALESCE(hivescale_2_temp_c,           NULLIF(raw_json->>'hivescale_2_temp_c',           '')::double precision) AS hivescale_2_temp_c,
    COALESCE(hivescale_2_humidity_percent, NULLIF(raw_json->>'hivescale_2_humidity_percent', '')::double precision) AS hivescale_2_humidity_percent,
    COALESCE(hivescale_2_pressure_hpa,     NULLIF(raw_json->>'hivescale_2_pressure_hpa',     '')::double precision) AS hivescale_2_pressure_hpa,
    COALESCE(hivescale_2_battery_v,        NULLIF(raw_json->>'hivescale_2_battery_v',        '')::double precision) AS hivescale_2_battery_v,
    NULLIF(raw_json->>'ble_1_battery_percent', '')::integer AS ble_1_battery_percent,
    NULLIF(raw_json->>'ble_1_rssi_dbm',        '')::integer AS ble_1_rssi_dbm,
    NULLIF(raw_json->>'ble_2_battery_percent', '')::integer AS ble_2_battery_percent,
    NULLIF(raw_json->>'ble_2_rssi_dbm',        '')::integer AS ble_2_rssi_dbm,
    raw_json->>'ble_1_firmware_version' AS ble_1_firmware_version,
    raw_json->>'ble_2_firmware_version' AS ble_2_firmware_version,
    -- Raw HiveHeart FFT (8-byte packed-nibble array). Legacy flat rows keep it in
    -- the measurement raw_json; the `->` operator returns the JSON array so the
    -- read path can decode it into fft_bins. Appended last so the positional
    -- row_to_dict indices below stay stable.
    raw_json->'hiveheart_1_fft' AS hiveheart_1_fft,
    raw_json->'hiveheart_2_fft' AS hiveheart_2_fft
"""


# Slim column set for the dashboard's time-series (chart) endpoint.
#
# The dashboard charts read only the ~30 fields below from the measurements
# series (the metric cards read the separate `latest` row, which keeps the full
# column set). Every column here is a real, typed table column — crucially, none
# fall back to `raw_json`. The full MEASUREMENT_SELECT_COLUMNS wraps almost every
# field in COALESCE(col, raw_json->>'…'), which forces Postgres to read and
# de-TOAST the large raw_json blob for every row even though the typed columns
# are populated for current firmware. Selecting only typed columns here avoids
# that de-TOAST entirely, which is the dominant per-row cost on the chart query.
#
# Trade-off: the down-sample decimation (see execute_measurement_query) does its
# window pass over the cheap (id, measured_at) index and only materialises this
# column list for the ~max_points rows that survive, so the raw_json de-TOAST is
# bounded to a few hundred rows per chart request rather than the whole range.
# That makes it affordable to mirror the fallbacks the `latest`/app paths use for
# the charted fields, so a chart matches its metric card on legacy rows that
# stored a value only in raw_json (in-hive humidity, pressure, mic level, mic FFT
# bands, battery, solar, bee-counter). Weight / hive & ambient temperature / ambient humidity /
# RSSI have no raw_json fallback in MEASUREMENT_SELECT_COLUMNS either, so they stay
# bare typed columns here — there is nothing extra to coalesce.
CHART_MEASUREMENT_COLUMNS = """
    id, device_id, measured_at,
    scale_1_weight_kg, scale_2_weight_kg,
    hive_1_temp_c, hive_2_temp_c, ambient_temp_c,
    ambient_humidity_percent,
    COALESCE(hive_1_humidity_percent, NULLIF(raw_json->>'hive_1_humidity_percent', '')::double precision) AS hive_1_humidity_percent,
    COALESCE(hive_2_humidity_percent, NULLIF(raw_json->>'hive_2_humidity_percent', '')::double precision) AS hive_2_humidity_percent,
    COALESCE(ble_1_pressure_hpa, NULLIF(raw_json->>'ble_1_pressure_hpa', '')::double precision) AS ble_1_pressure_hpa,
    COALESCE(ble_2_pressure_hpa, NULLIF(raw_json->>'ble_2_pressure_hpa', '')::double precision) AS ble_2_pressure_hpa,
    COALESCE(hivescale_1_pressure_hpa, NULLIF(raw_json->>'hivescale_1_pressure_hpa', '')::double precision) AS hivescale_1_pressure_hpa,
    COALESCE(hivescale_2_pressure_hpa, NULLIF(raw_json->>'hivescale_2_pressure_hpa', '')::double precision) AS hivescale_2_pressure_hpa,
    COALESCE(mic_left_rms_dbfs, NULLIF(raw_json->>'mic_left_rms_dbfs', '')::double precision) AS mic_left_rms_dbfs,
    COALESCE(mic_right_rms_dbfs, NULLIF(raw_json->>'mic_right_rms_dbfs', '')::double precision) AS mic_right_rms_dbfs,
    COALESCE(mic_left_peak_dbfs, NULLIF(raw_json->>'mic_left_peak_dbfs', '')::double precision) AS mic_left_peak_dbfs,
    COALESCE(mic_right_peak_dbfs, NULLIF(raw_json->>'mic_right_peak_dbfs', '')::double precision) AS mic_right_peak_dbfs,
    COALESCE(mic_left_band_sub_bass_dbfs, NULLIF(raw_json->>'mic_left_band_sub_bass_dbfs', '')::double precision) AS mic_left_band_sub_bass_dbfs,
    COALESCE(mic_left_band_hum_dbfs, NULLIF(raw_json->>'mic_left_band_hum_dbfs', '')::double precision) AS mic_left_band_hum_dbfs,
    COALESCE(mic_left_band_piping_dbfs, NULLIF(raw_json->>'mic_left_band_piping_dbfs', '')::double precision) AS mic_left_band_piping_dbfs,
    COALESCE(mic_left_band_stress_dbfs, NULLIF(raw_json->>'mic_left_band_stress_dbfs', '')::double precision) AS mic_left_band_stress_dbfs,
    COALESCE(mic_left_band_high_dbfs, NULLIF(raw_json->>'mic_left_band_high_dbfs', '')::double precision) AS mic_left_band_high_dbfs,
    COALESCE(mic_right_band_sub_bass_dbfs, NULLIF(raw_json->>'mic_right_band_sub_bass_dbfs', '')::double precision) AS mic_right_band_sub_bass_dbfs,
    COALESCE(mic_right_band_hum_dbfs, NULLIF(raw_json->>'mic_right_band_hum_dbfs', '')::double precision) AS mic_right_band_hum_dbfs,
    COALESCE(mic_right_band_piping_dbfs, NULLIF(raw_json->>'mic_right_band_piping_dbfs', '')::double precision) AS mic_right_band_piping_dbfs,
    COALESCE(mic_right_band_stress_dbfs, NULLIF(raw_json->>'mic_right_band_stress_dbfs', '')::double precision) AS mic_right_band_stress_dbfs,
    COALESCE(mic_right_band_high_dbfs, NULLIF(raw_json->>'mic_right_band_high_dbfs', '')::double precision) AS mic_right_band_high_dbfs,
    COALESCE(battery_soc_percent, NULLIF(raw_json->>'battery_soc_percent', '')::double precision) AS battery_soc_percent,
    COALESCE(battery_voltage, NULLIF(raw_json->>'battery_voltage_v', '')::double precision, NULLIF(raw_json->>'battery_voltage', '')::double precision) AS battery_voltage,
    COALESCE(solar_power_mw, NULLIF(raw_json->>'solar_power_mw', '')::double precision) AS solar_power_mw,
    COALESCE(solar_current_ma, NULLIF(raw_json->>'solar_current_ma', '')::double precision) AS solar_current_ma,
    rssi_dbm,
    COALESCE(bee_counter_1_ok, NULLIF(raw_json->>'bee_counter_1_ok', '')::boolean) AS bee_counter_1_ok,
    COALESCE(bee_counter_1_total_in, NULLIF(raw_json->>'bee_counter_1_total_in', '')::bigint) AS bee_counter_1_total_in,
    COALESCE(bee_counter_1_total_out, NULLIF(raw_json->>'bee_counter_1_total_out', '')::bigint) AS bee_counter_1_total_out,
    COALESCE(bee_counter_1_interval_in, NULLIF(raw_json->>'bee_counter_1_interval_in', '')::bigint) AS bee_counter_1_interval_in,
    COALESCE(bee_counter_1_interval_out, NULLIF(raw_json->>'bee_counter_1_interval_out', '')::bigint) AS bee_counter_1_interval_out,
    COALESCE(bee_counter_2_ok, NULLIF(raw_json->>'bee_counter_2_ok', '')::boolean) AS bee_counter_2_ok,
    COALESCE(bee_counter_2_total_in, NULLIF(raw_json->>'bee_counter_2_total_in', '')::bigint) AS bee_counter_2_total_in,
    COALESCE(bee_counter_2_total_out, NULLIF(raw_json->>'bee_counter_2_total_out', '')::bigint) AS bee_counter_2_total_out,
    COALESCE(bee_counter_2_interval_in, NULLIF(raw_json->>'bee_counter_2_interval_in', '')::bigint) AS bee_counter_2_interval_in,
    COALESCE(bee_counter_2_interval_out, NULLIF(raw_json->>'bee_counter_2_interval_out', '')::bigint) AS bee_counter_2_interval_out,
    -- Raw HiveHeart FFT so the dashboard Frequency Bands view can render the
    -- HiveHeart spectrum (decoded to fft_bins on read). Legacy flat rows store it
    -- in raw_json; nested rows carry it in hive_readings and are merged in by
    -- attach_hive_readings, so this only needs the flat/legacy fallback.
    raw_json->'hiveheart_1_fft' AS hiveheart_1_fft,
    raw_json->'hiveheart_2_fft' AS hiveheart_2_fft
"""


def measurement_row_to_dict(r):
    return {
        "id": r[0],
        "device_id": r[1],
        "measured_at": r[2],
        "received_at": r[3],
        "scale_1_weight_kg": r[4],
        "scale_2_weight_kg": r[5],
        "hive_1_temp_c": r[6],
        "hive_2_temp_c": r[7],
        "ambient_temp_c": r[8],
        "ambient_humidity_percent": r[9],
        "battery_voltage": r[10],
        "battery_voltage_v": r[10],
        "rssi_dbm": r[11],
        "firmware_version": r[12],
        "config_version": r[13],
        "sd_ok": r[14],
        "rtc_ok": r[15],
        "sht_ok": r[16],
        "scale_1_raw": r[17],
        "scale_2_raw": r[18],
        # Temperature-compensated weights. Default to the raw weight and
        # tempco_applied=False; attach_temperature_compensation() overrides
        # these per device when a coefficient is configured and enabled.
        "scale_1_weight_kg_compensated": r[4],
        "scale_2_weight_kg_compensated": r[5],
        "tempco_applied": False,
        "battery_soc_percent": r[19],
        "battery_alert": r[20],
        "battery_monitor_ok": r[21],
        "solar_monitor_ok": r[22],
        "solar_bus_voltage_v": r[23],
        "solar_shunt_voltage_mv": r[24],
        "solar_load_voltage_v": r[25],
        "solar_current_ma": r[26],
        "solar_power_mw": r[27],
        "network_transport": r[28],
        "cellular_ok": r[29],
        "cellular_csq": r[30],
        "calibration_mode": r[31],
        "boot_count": r[32],
        "time_source": r[33],
        # mic telemetry
        "mic_ok": r[34],
        "mic_sample_rate_hz": r[35],
        "mic_sample_frames": r[36],
        "mic_left_ok": r[37],
        "mic_left_rms_dbfs": r[38],
        "mic_left_peak_dbfs": r[39],
        "mic_left_rms_normalized": r[40],
        "mic_right_ok": r[41],
        "mic_right_rms_dbfs": r[42],
        "mic_right_peak_dbfs": r[43],
        "mic_right_rms_normalized": r[44],
        # fft frequency band energy
        "mic_left_band_sub_bass_dbfs":  r[45],
        "mic_left_band_hum_dbfs":       r[46],
        "mic_left_band_piping_dbfs":    r[47],
        "mic_left_band_stress_dbfs":    r[48],
        "mic_left_band_high_dbfs":      r[49],
        "mic_right_band_sub_bass_dbfs": r[50],
        "mic_right_band_hum_dbfs":      r[51],
        "mic_right_band_piping_dbfs":   r[52],
        "mic_right_band_stress_dbfs":   r[53],
        "mic_right_band_high_dbfs":     r[54],
        # bee counter (per-hive entrance counters)
        "bee_counter_1_ok":                r[55],
        "bee_counter_1_protocol_version":  r[56],
        "bee_counter_1_status_flags":      r[57],
        "bee_counter_1_uptime_s":          r[58],
        "bee_counter_1_num_gates":         r[59],
        "bee_counter_1_gates_healthy":     r[60],
        "bee_counter_1_total_in":          r[61],
        "bee_counter_1_total_out":         r[62],
        "bee_counter_1_interval_in":       r[63],
        "bee_counter_1_interval_out":      r[64],
        "bee_counter_1_glitch_count":      r[65],
        "bee_counter_1_busy_retries":      r[66],
        "bee_counter_1_read_attempts":     r[67],
        "bee_counter_1_latch_succeeded":   r[68],
        "bee_counter_2_ok":                r[69],
        "bee_counter_2_protocol_version":  r[70],
        "bee_counter_2_status_flags":      r[71],
        "bee_counter_2_uptime_s":          r[72],
        "bee_counter_2_num_gates":         r[73],
        "bee_counter_2_gates_healthy":     r[74],
        "bee_counter_2_total_in":          r[75],
        "bee_counter_2_total_out":         r[76],
        "bee_counter_2_interval_in":       r[77],
        "bee_counter_2_interval_out":      r[78],
        "bee_counter_2_glitch_count":      r[79],
        "bee_counter_2_busy_retries":      r[80],
        "bee_counter_2_read_attempts":     r[81],
        "bee_counter_2_latch_succeeded":   r[82],
        # accelerometer (per-hive vibration, mg)
        "accel_1_ok":                r[83],
        "accel_1_sample_rate_hz":    r[84],
        "accel_1_sample_count":      r[85],
        "accel_1_range_g":           r[86],
        "accel_1_rms_mg":            r[87],
        "accel_1_peak_mg":           r[88],
        "accel_1_band_swarm_mg":     r[89],
        "accel_1_band_fanning_mg":   r[90],
        "accel_1_band_activity_mg":  r[91],
        "accel_2_ok":                r[92],
        "accel_2_sample_rate_hz":    r[93],
        "accel_2_sample_count":      r[94],
        "accel_2_range_g":           r[95],
        "accel_2_rms_mg":            r[96],
        "accel_2_peak_mg":           r[97],
        "accel_2_band_swarm_mg":     r[98],
        "accel_2_band_fanning_mg":   r[99],
        "accel_2_band_activity_mg":  r[100],
        # HolyIot 25015 in-hive BLE sensor (per hive)
        "ble_1_humidity_percent":    r[101],
        "ble_1_pressure_hpa":        r[102],
        "ble_2_humidity_percent":    r[103],
        "ble_2_pressure_hpa":        r[104],
        # In-hive relative humidity (mirrors hive_N_temp_c; appended last).
        "hive_1_humidity_percent":   r[105],
        "hive_2_humidity_percent":   r[106],
        # beehivemonitoring.com GATT sensors (HiveHeart / HiveScale).
        # NOTE: hiveheart_N_temp_c / hiveheart_N_humidity_percent are SELECTed
        # (they are kept "independently visible" per the MeasurementIn comment)
        # and so MUST be mapped here — omitting them shifts every positional
        # index below them and silently mis-reads hiveheart_2_* / hivescale_*.
        "hiveheart_1_frequency_hz":     r[107],
        "hiveheart_1_energy":           r[108],
        "hiveheart_1_peak":             r[109],
        "hiveheart_1_battery_v":        r[110],
        "hiveheart_1_temp_c":           r[111],
        "hiveheart_1_humidity_percent": r[112],
        "hiveheart_2_frequency_hz":     r[113],
        "hiveheart_2_energy":           r[114],
        "hiveheart_2_peak":             r[115],
        "hiveheart_2_battery_v":        r[116],
        "hiveheart_2_temp_c":           r[117],
        "hiveheart_2_humidity_percent": r[118],
        "hivescale_1_weight_kg":        r[119],
        "hivescale_1_raw_weight":       r[120],
        "hivescale_1_temp_c":           r[121],
        "hivescale_1_humidity_percent": r[122],
        "hivescale_1_pressure_hpa":     r[123],
        "hivescale_1_battery_v":        r[124],
        "hivescale_2_weight_kg":        r[125],
        "hivescale_2_raw_weight":       r[126],
        "hivescale_2_temp_c":           r[127],
        "hivescale_2_humidity_percent": r[128],
        "hivescale_2_pressure_hpa":     r[129],
        "hivescale_2_battery_v":        r[130],
        # HolyIot 25015 / HiveInside in-hive BLE sensor battery + link RSSI.
        # Stored in raw_json on ingest; surfaced here so the app can show a
        # per-sensor charge level (HolyIot reports %, HiveInside reports % too —
        # 0% while USB-powered). Appended last in the SELECT.
        "ble_1_battery_percent":        r[131],
        "ble_1_rssi_dbm":               r[132],
        "ble_2_battery_percent":        r[133],
        "ble_2_rssi_dbm":               r[134],
        "ble_1_firmware_version":       r[135],
        "ble_2_firmware_version":       r[136],
        # Raw HiveHeart FFT arrays from raw_json (legacy flat rows). Decoded into
        # fft_bins by attach_hive_readings → _attach_hiveheart_fft_bins.
        "hiveheart_1_fft":              r[137],
        "hiveheart_2_fft":              r[138],
    }


def load_tempco_configs(device_ids) -> dict:
    """Fetch the temperature-compensation config for a set of devices.

    Returns ``{device_id: (source, ref_temp_c, scale1_coeff, scale2_coeff)}``
    for devices that have compensation *enabled* with at least one non-zero
    coefficient. Devices absent from the map are left uncompensated.
    """
    ids = [d for d in {d for d in device_ids} if d]
    if not ids:
        return {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT device_id, tempco_source, tempco_ref_temp_c,
                           scale1_tempco_kg_per_c, scale2_tempco_kg_per_c
                    FROM device_configs
                    WHERE device_id = ANY(%s)
                      AND tempco_enabled
                      AND (scale1_tempco_kg_per_c <> 0 OR scale2_tempco_kg_per_c <> 0);
                    """,
                    (ids,),
                )
                rows = cur.fetchall()
    except psycopg.errors.UndefinedColumn:
        # The temp-compensation columns are missing — migration 006 has not been
        # applied (e.g. the process was hot-reloaded without re-running init_db).
        # Degrade to serving raw, uncompensated weights rather than 500-ing the
        # whole measurement-read endpoint, which would blank "last data" in the
        # dashboard. Reads keep working; compensation resumes once the migration
        # runs.
        logger.warning(
            "device_configs temp-compensation columns missing; "
            "serving uncompensated weights. Apply migration "
            "006_loadcell_temp_compensation.sql (or restart to run init_db)."
        )
        return {}
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def attach_temperature_compensation(measurements: list[dict]) -> list[dict]:
    """Fill in the compensated-weight fields on serialized measurement dicts.

    Looks up each device's coefficient once (a single batched query), applies an
    EMA to the temperature series (per device, in time order) to damp transient
    lag errors, then applies the first-order correction from server/tempcomp.py.
    Rows whose device has no enabled coefficient keep the defaults set in
    measurement_row_to_dict (raw weight, tempco_applied=False).
    """
    if not measurements:
        return measurements
    cfgs = load_tempco_configs(m["device_id"] for m in measurements)
    if not cfgs:
        return measurements

    # Group by device so EMA runs over each device's time-ordered sequence.
    from collections import defaultdict
    by_device: dict = defaultdict(list)
    for m in measurements:
        by_device[m["device_id"]].append(m)

    for device_id, rows in by_device.items():
        cfg = cfgs.get(device_id)
        if not cfg:
            continue
        source, ref_temp, c1, c2 = cfg
        field = TEMP_SOURCE_FIELD.get(source, "ambient_temp_c")

        rows.sort(key=lambda m: m["measured_at"])
        smoothed_temps = ema_temperatures([m.get(field) for m in rows])

        for m, temp in zip(rows, smoothed_temps):
            m["scale_1_weight_kg_compensated"] = compensate_weight(
                m["scale_1_weight_kg"], temp, ref_temp, c1
            )
            m["scale_2_weight_kg_compensated"] = compensate_weight(
                m["scale_2_weight_kg"], temp, ref_temp, c2
            )
            m["tempco_applied"] = True

    return measurements


def difference_bee_counter_intervals(measurements: list[dict]) -> list[dict]:
    """Backfill NULL per-interval bee-counter counts from the lifetime totals.

    The wireless/BLE (HiveTraffic) bee-counter path is *totals-only*: the device
    reports the monotonic lifetime ``total_in``/``total_out`` but no per-poll
    ``interval_in``/``interval_out`` — it performs no I2C ``CMD_LATCH`` handshake,
    so those interval columns are stored NULL (see
    2026-easy-bee-counter/docs/ble-mode.md and bee_counter_client's totals_only
    path). Display clients (HivePal) chart the interval fields directly, so a
    BLE-sourced counter would otherwise read as zero traffic for every row.

    Here we derive each missing interval as ``total_now - total_prev`` between
    consecutive readings of the same channel — the same differencing the insight
    engine already does in insights._extract_counter_series, applied to the read
    APIs so the chart panel matches the insight card. Rules:

    * Only NULL intervals are filled; the wired I2C path, which already reports a
      device interval, is left untouched.
    * Rows where the channel was unreachable (``bee_counter_{ch}_ok`` falsy) are
      skipped and never advance the baseline, so a dead counter can't inject a
      bogus delta.
    * A counter reboot or uint32 wrap (``total_now < total_prev``) is attributed
      as ``total_now`` so the restart from zero is not seen as a huge negative.

    The dicts are mutated in place; the caller's ordering is preserved (we sort a
    shallow copy by ``measured_at`` only to compute the deltas). Run this *after*
    attach_hive_readings so the flat ``bee_counter_{n}_*`` aliases exist for hives
    3–18 (which have no fixed table columns); otherwise only hives 1–2 are filled.
    """
    ordered = sorted(
        (m for m in measurements if m.get("measured_at") is not None),
        key=lambda m: m["measured_at"],
    )
    for ch in range(1, MAX_HIVES + 1):
        ok_key = f"bee_counter_{ch}_ok"
        for direction in ("in", "out"):
            interval_key = f"bee_counter_{ch}_interval_{direction}"
            total_key = f"bee_counter_{ch}_total_{direction}"
            prev_total = None
            for m in ordered:
                if not m.get(ok_key):
                    continue
                cur_total = m.get(total_key)
                if m.get(interval_key) is None and cur_total is not None:
                    if prev_total is None:
                        pass  # no baseline yet — can't attribute the first reading
                    elif cur_total >= prev_total:
                        m[interval_key] = cur_total - prev_total
                    else:
                        m[interval_key] = cur_total  # reboot / wrap
                if cur_total is not None:
                    prev_total = cur_total
    return measurements


HIVE_READINGS_SELECT = """
    SELECT measurement_id, hive_index, name, weight_kg, raw_weight, scale_source,
           temp_c, temp_source, humidity_percent,
           accel_ok, accel_sample_count, accel_range_g, accel_rms_mg, accel_peak_mg,
           accel_band_swarm_mg, accel_band_fanning_mg, accel_band_activity_mg,
           ble_present, ble_sensor_type, ble_humidity_percent, ble_pressure_hpa,
           ble_battery_percent, ble_rssi_dbm,
           bee_counter_ok, bee_counter_total_in, bee_counter_total_out,
           bee_counter_interval_in, bee_counter_interval_out, raw_json
    FROM hive_readings WHERE measurement_id = ANY(%s)
    ORDER BY measurement_id, hive_index
"""


def _json_obj(v) -> dict:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _merge_nested_sensor(out: dict, key: str, values: dict) -> None:
    existing = out.get(key) if isinstance(out.get(key), dict) else {}
    merged = dict(existing)
    for k, v in values.items():
        if v is not None or k not in merged:
            merged[k] = v
    if any(v is not None for v in merged.values()):
        out[key] = merged


def _hive_reading_row_to_dict(r) -> dict:
    # Preserve the original per-hive raw_json payload so nested telemetry that
    # does not have dedicated hive_readings columns, such as HiveHeart acoustic
    # fields and HiveScale/HiveHeart RSSI, survives the read API. Column values
    # still override the canonical fields below because they are the indexed /
    # migration-friendly representation.
    out = _json_obj(r[28])
    out.update({
        "index": r[1], "name": r[2], "weight_kg": r[3], "raw_weight": r[4],
        "scale_source": r[5], "temp_c": r[6], "temp_source": r[7],
        "humidity_percent": r[8],
    })
    _merge_nested_sensor(out, "accel", {
        "ok": r[9], "sample_count": r[10], "range_g": r[11],
        "rms_mg": r[12], "peak_mg": r[13], "band_swarm_mg": r[14],
        "band_fanning_mg": r[15], "band_activity_mg": r[16],
    })
    _merge_nested_sensor(out, "ble", {
        "present": r[17], "sensor_type": r[18], "humidity_percent": r[19],
        "pressure_hpa": r[20], "battery_percent": r[21], "rssi_dbm": r[22],
    })
    _merge_nested_sensor(out, "bee_counter", {
        "ok": r[23], "total_in": r[24], "total_out": r[25],
        "interval_in": r[26], "interval_out": r[27],
    })
    return out


def _synthesize_hives_from_flat(m: dict) -> list[dict]:
    """For historical / old-firmware rows with no hive_readings, build the hives[]
    array (hives 1–2) from the legacy measurements columns so the API shape is
    uniform regardless of firmware version."""
    hives = []
    for n in (1, 2):
        accel = {
            "ok": m.get(f"accel_{n}_ok"), "rms_mg": m.get(f"accel_{n}_rms_mg"),
            "peak_mg": m.get(f"accel_{n}_peak_mg"),
            "band_swarm_mg": m.get(f"accel_{n}_band_swarm_mg"),
            "band_fanning_mg": m.get(f"accel_{n}_band_fanning_mg"),
            "band_activity_mg": m.get(f"accel_{n}_band_activity_mg"),
        }
        ble = {"humidity_percent": m.get(f"ble_{n}_humidity_percent"),
               "pressure_hpa": m.get(f"ble_{n}_pressure_hpa")}
        bc = {"ok": m.get(f"bee_counter_{n}_ok"),
              "total_in": m.get(f"bee_counter_{n}_total_in"),
              "total_out": m.get(f"bee_counter_{n}_total_out"),
              "interval_in": m.get(f"bee_counter_{n}_interval_in"),
              "interval_out": m.get(f"bee_counter_{n}_interval_out")}
        hiveheart = {"temp_c": m.get(f"hiveheart_{n}_temp_c"),
                     "humidity_percent": m.get(f"hiveheart_{n}_humidity_percent"),
                     "frequency_hz": m.get(f"hiveheart_{n}_frequency_hz"),
                     "energy": m.get(f"hiveheart_{n}_energy"),
                     "peak": m.get(f"hiveheart_{n}_peak"),
                     "battery_v": m.get(f"hiveheart_{n}_battery_v"),
                     "rssi_dbm": m.get(f"hiveheart_{n}_rssi_dbm"),
                     # Raw FFT extracted from the measurement raw_json by the
                     # SELECT; decoded into fft_bins by _attach_hiveheart_fft_bins.
                     "fft": m.get(f"hiveheart_{n}_fft")}
        hivescale = {"weight_kg": m.get(f"hivescale_{n}_weight_kg"),
                     "raw_weight": m.get(f"hivescale_{n}_raw_weight"),
                     "temp_c": m.get(f"hivescale_{n}_temp_c"),
                     "humidity_percent": m.get(f"hivescale_{n}_humidity_percent"),
                     "pressure_hpa": m.get(f"hivescale_{n}_pressure_hpa"),
                     "battery_v": m.get(f"hivescale_{n}_battery_v"),
                     "rssi_dbm": m.get(f"hivescale_{n}_rssi_dbm")}
        fields = [m.get(f"scale_{n}_weight_kg"), m.get(f"hive_{n}_temp_c"),
                  m.get(f"hive_{n}_humidity_percent"), m.get(f"scale_{n}_raw")]
        if not any(v is not None for v in fields + list(accel.values())
                   + list(ble.values()) + list(bc.values())
                   + list(hiveheart.values()) + list(hivescale.values())):
            continue
        hive = {
            "index": n, "weight_kg": m.get(f"scale_{n}_weight_kg"),
            "raw_weight": m.get(f"scale_{n}_raw"), "temp_c": m.get(f"hive_{n}_temp_c"),
            "humidity_percent": m.get(f"hive_{n}_humidity_percent"),
            "accel": accel, "ble": ble, "bee_counter": bc,
        }
        if any(v is not None for v in hiveheart.values()):
            hive["hiveheart"] = hiveheart
        if any(v is not None for v in hivescale.values()):
            hive["hivescale"] = hivescale
        hives.append(hive)
    return hives


def _flatten_hive_to_measurement(m: dict, h: dict) -> None:
    """Synthesize flat per-hive keys from hives[].

    The nested ``hives[]`` object is the canonical multi-hive representation, but
    several app/dashboard paths still consume column-shaped keys. Generate those
    aliases from each hive row so every configured hive (up to MAX_HIVES on the
    firmware side) exposes the same read-response shape. Existing legacy columns
    for hives 1–2 are left intact; these synthesized keys are read-time aliases
    and do not require adding unbounded database columns.
    """
    n = h.get("index")
    if not n:
        return

    def put(key: str, value) -> None:
        if value is not None:
            m[key] = value

    hh, hs = h.get("hiveheart") or {}, h.get("hivescale") or {}
    if hh:
        put(f"hiveheart_{n}_temp_c", hh.get("temp_c"))
        put(f"hiveheart_{n}_humidity_percent", hh.get("humidity_percent"))
        put(f"hiveheart_{n}_frequency_hz", hh.get("frequency_hz"))
        put(f"hiveheart_{n}_energy", hh.get("energy"))
        put(f"hiveheart_{n}_peak", hh.get("peak"))
        put(f"hiveheart_{n}_battery_v", hh.get("battery_v"))
        put(f"hiveheart_{n}_rssi_dbm", hh.get("rssi_dbm"))
        if hh.get("fft") is not None:
            m[f"hiveheart_{n}_fft"] = hh.get("fft")
    if hs:
        put(f"hivescale_{n}_weight_kg", hs.get("weight_kg"))
        put(f"hivescale_{n}_raw_weight", hs.get("raw_weight"))
        put(f"hivescale_{n}_temp_c", hs.get("temp_c"))
        put(f"hivescale_{n}_humidity_percent", hs.get("humidity_percent"))
        put(f"hivescale_{n}_pressure_hpa", hs.get("pressure_hpa"))
        put(f"hivescale_{n}_battery_v", hs.get("battery_v"))
        put(f"hivescale_{n}_rssi_dbm", hs.get("rssi_dbm"))

    a = h.get("accel") or {}
    b = h.get("ble") or {}
    c = h.get("bee_counter") or {}
    mic = h.get("mic") or {}

    # Canonical per-hive aliases. These are especially important for hives 3–18,
    # which have no fixed measurements-table columns. For hives 1–2 they are
    # harmless aliases next to the historical scale_1/2 and hive_1/2 columns.
    put(f"scale_{n}_weight_kg", h.get("weight_kg"))
    put(f"scale_{n}_raw", h.get("raw_weight"))
    put(f"hive_{n}_temp_c", h.get("temp_c"))
    put(f"hive_{n}_humidity_percent", h.get("humidity_percent"))

    # Vibration aliases from hives[n].accel. Include the full HiveInside band set
    # and metadata, not only the old two-hive subset.
    if a:
        put(f"accel_{n}_ok", a.get("ok"))
        put(f"accel_{n}_sample_rate_hz", a.get("sample_rate_hz"))
        put(f"accel_{n}_sample_count", a.get("sample_count"))
        put(f"accel_{n}_range_g", a.get("range_g"))
        put(f"accel_{n}_rms_mg", a.get("rms_mg"))
        put(f"accel_{n}_peak_mg", a.get("peak_mg"))
        put(f"accel_{n}_band_swarm_mg", a.get("band_swarm_mg"))
        put(f"accel_{n}_band_fanning_mg", a.get("band_fanning_mg"))
        put(f"accel_{n}_band_activity_mg", a.get("band_activity_mg"))

    # BLE aliases from hives[n].ble. This covers HolyIot/Ruuvi/HiveInside fields
    # for all hive numbers, including RSSI, battery and HiveInside firmware.
    if b:
        put(f"ble_{n}_present", b.get("present"))
        put(f"ble_{n}_sensor_type", b.get("sensor_type"))
        put(f"ble_{n}_humidity_percent", b.get("humidity_percent"))
        put(f"ble_{n}_pressure_hpa", b.get("pressure_hpa"))
        put(f"ble_{n}_accel_x_mg", b.get("accel_x_mg"))
        put(f"ble_{n}_accel_y_mg", b.get("accel_y_mg"))
        put(f"ble_{n}_accel_z_mg", b.get("accel_z_mg"))
        put(f"ble_{n}_battery_percent", b.get("battery_percent"))
        put(f"ble_{n}_rssi_dbm", b.get("rssi_dbm"))
        put(f"ble_{n}_firmware_version", b.get("firmware_version"))

    # HiveInside acoustic aliases from hives[n].mic. The historical flat mic
    # schema is stereo-only (mic_left/mic_right), so expose per-hive mic_N_*
    # aliases for hive 3 and beyond while also making hives 1–2 addressable by
    # number. Nested hives[n].mic remains the canonical multi-hive field.
    if mic:
        put(f"mic_{n}_ok", mic.get("ok"))
        put(f"mic_{n}_rms_dbfs", mic.get("rms_dbfs"))
        put(f"mic_{n}_peak_dbfs", mic.get("peak_dbfs"))
        put(f"mic_{n}_rms_normalized", mic.get("rms_normalized"))
        put(f"mic_{n}_band_sub_bass_dbfs", mic.get("band_sub_bass_dbfs"))
        put(f"mic_{n}_band_hum_dbfs", mic.get("band_hum_dbfs"))
        put(f"mic_{n}_band_piping_dbfs", mic.get("band_piping_dbfs"))
        put(f"mic_{n}_band_stress_dbfs", mic.get("band_stress_dbfs"))
        put(f"mic_{n}_band_high_dbfs", mic.get("band_high_dbfs"))

    if c:
        put(f"bee_counter_{n}_ok", c.get("ok"))
        put(f"bee_counter_{n}_total_in", c.get("total_in"))
        put(f"bee_counter_{n}_total_out", c.get("total_out"))
        put(f"bee_counter_{n}_interval_in", c.get("interval_in"))
        put(f"bee_counter_{n}_interval_out", c.get("interval_out"))


_HIVEHEART_FLAT_FFT_RE = re.compile(r"^hiveheart_(\d+)_fft$")


def _attach_hiveheart_fft_bins(m: dict) -> None:
    """Decode the raw HiveHeart FFT into 16 relative levels and expose it.

    The raw 8-byte array stays canonical (``hives[].hiveheart.fft`` /
    ``hiveheart_N_fft``). This adds the decoded ``fft_bins`` in both the nested and
    the flat shapes so old flat records and new nested records return the same
    public API shape. Malformed / missing FFT data yields no ``fft_bins`` (the raw
    value is left untouched) rather than raising — see hiveheart_fft.decode_fft.

    Two carriers are handled so the bins appear regardless of how the firmware
    delivered the FFT:

    1. Nested ``hives[].hiveheart.fft`` — the canonical multi-hive path.
    2. Flat ``hiveheart_N_fft`` — the flat compatibility field (surfaced from
       ``raw_json`` by the SELECT, or synthesized). The device sends both, but
       when a measurement has hive_readings rows whose nested hive carries no
       ``hiveheart`` (older/edge firmware, or a HiveHeart that only populated the
       flat field), the nested pass alone would drop the FFT. The flat pass
       recovers it and mirrors the bins back onto the matching hive so the nested
       shape stays consistent too.
    """
    hives_by_index: dict[int, dict] = {}
    for h in m.get("hives") or []:
        idx = h.get("index")
        if idx is not None:
            hives_by_index[idx] = h
        hh = h.get("hiveheart")
        if not isinstance(hh, dict):
            continue
        bins = decode_fft(hh.get("fft"))
        if bins is None:
            continue
        hh["fft_bins"] = bins
        if idx:
            m[f"hiveheart_{idx}_fft"] = hh.get("fft")
            m[f"hiveheart_{idx}_fft_bins"] = bins

    # Flat-field fallback: decode any hiveheart_N_fft that the nested pass didn't
    # already resolve, and mirror it onto the matching hive when one exists.
    for key in [k for k in m if isinstance(k, str) and _HIVEHEART_FLAT_FFT_RE.match(k)]:
        n = int(_HIVEHEART_FLAT_FFT_RE.match(key).group(1))
        if m.get(f"hiveheart_{n}_fft_bins") is not None:
            continue
        bins = decode_fft(m.get(key))
        if bins is None:
            continue
        m[f"hiveheart_{n}_fft_bins"] = bins
        hive = hives_by_index.get(n)
        if hive is not None:
            hh = hive.get("hiveheart")
            if not isinstance(hh, dict):
                hh = {}
                hive["hiveheart"] = hh
            hh.setdefault("fft", m.get(key))
            hh["fft_bins"] = bins


def attach_hive_readings(measurements: list[dict]) -> list[dict]:
    """Attach a per-hive ``hives`` array (from hive_readings, else synthesized from
    the legacy columns) to each measurement, and synthesize flat per-hive keys for
    hives beyond 2 so the existing column-based read consumers see all hives."""
    if not measurements:
        return measurements
    ids = [m["id"] for m in measurements if m.get("id") is not None]
    by_mid: dict[int, list[dict]] = {}
    if ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(HIVE_READINGS_SELECT, (ids,))
                for r in cur.fetchall():
                    by_mid.setdefault(r[0], []).append(_hive_reading_row_to_dict(r))
    for m in measurements:
        hive_rows = by_mid.get(m.get("id"))
        m["hives"] = hive_rows if hive_rows else _synthesize_hives_from_flat(m)
        for h in m["hives"]:
            _flatten_hive_to_measurement(m, h)
        _attach_hiveheart_fft_bins(m)
    return measurements


def serialize_measurements(rows) -> list[dict]:
    """Map raw DB rows to API dicts, attach temperature compensation and the
    per-hive readings (hives[] array + flat keys for hives 3–18)."""
    measurements = attach_temperature_compensation(
        [measurement_row_to_dict(r) for r in rows]
    )
    # Flatten per-hive readings first so hives 3–18 expose bee_counter_{n}_* keys,
    # then difference so those hives get derived interval counts too.
    measurements = attach_hive_readings(measurements)
    return difference_bee_counter_intervals(measurements)


def serialize_chart_measurements(cur) -> list[dict]:
    """Map the slim CHART_MEASUREMENT_COLUMNS result set to API dicts.

    Builds dicts by column name (rather than the positional mapping used for the
    full row), seeds the compensated-weight defaults that measurement_row_to_dict
    would normally set, then runs the same compensation / bee-counter / per-hive
    transforms so the chart payload stays consistent with the full read APIs.
    """
    cols = [c.name for c in cur.description]
    measurements: list[dict] = []
    for r in cur.fetchall():
        m = dict(zip(cols, r))
        # Mirror measurement_row_to_dict: default compensated weights to the raw
        # weight so the keys always exist; attach_temperature_compensation()
        # overrides them when a coefficient is configured.
        m["scale_1_weight_kg_compensated"] = m.get("scale_1_weight_kg")
        m["scale_2_weight_kg_compensated"] = m.get("scale_2_weight_kg")
        m["tempco_applied"] = False
        measurements.append(m)
    measurements = attach_temperature_compensation(measurements)
    # Flatten per-hive readings first (adds bee_counter_{n}_* for hives 3–18),
    # then difference so those hives get derived interval counts too.
    measurements = attach_hive_readings(measurements)
    return difference_bee_counter_intervals(measurements)


def execute_measurement_query(cur, columns, where_parts, params, limit, max_points):
    """Run a measurements query (newest first), optionally time-decimated.

    When ``max_points`` is set and the matched row count exceeds it, the rows are
    thinned to ~max_points evenly-spaced samples using a stride computed in SQL.
    The stride is derived from a cheap, index-only window pass over (id,
    measured_at); the heavy column list is then materialised only for the rows
    that survive the stride, so a wide range no longer de-TOASTs raw_json (or
    ships JSON) for thousands of rows just to draw a few hundred chart pixels.
    The newest row (rn = 1) is always kept. ``limit`` still applies as a hard cap.
    """
    where_sql = " AND ".join(where_parts)
    if max_points and max_points > 0:
        cur.execute(
            f"""
            WITH filtered AS (
                SELECT id,
                       row_number() OVER (ORDER BY measured_at DESC) AS rn,
                       count(*) OVER () AS total
                FROM measurements
                WHERE {where_sql}
            ),
            picked AS (
                SELECT id AS pid FROM filtered
                WHERE (rn - 1) %% GREATEST(1, (total + %s - 1) / %s) = 0
                ORDER BY rn
                LIMIT %s
            )
            SELECT {columns}
            FROM measurements m
            JOIN picked p ON p.pid = m.id
            ORDER BY m.measured_at DESC;
            """,
            [*params, max_points, max_points, limit],
        )
    else:
        cur.execute(
            f"""
            SELECT {columns}
            FROM measurements
            WHERE {where_sql}
            ORDER BY measured_at DESC
            LIMIT %s;
            """,
            [*params, limit],
        )


def measurements_for_insights(rows) -> list[dict]:
    """Build measurement dicts for the insight engine on temperature-compensated
    weights.

    compute_insights() reads weight from ``scale_1_weight_kg`` /
    ``scale_2_weight_kg``. Those keys hold the raw load-cell weight; the
    compensated values live under the separate ``*_compensated`` keys. We run the
    same compensation as the read APIs (serialize_measurements) and then fold the
    compensated weight into the primary keys so every weight-based detector
    (swarm, robbing, foraging, absconding, winter risk, harvest window) operates
    on corrected weight without any change to the engine.

    The compensated fields default to the raw weight when compensation is
    disabled or no coefficient is set, so this is a no-op in that case. These
    dicts are throwaway inputs to the engine — the stored DB rows are untouched.
    """
    measurements = serialize_measurements(rows)
    for m in measurements:
        comp1 = m.get("scale_1_weight_kg_compensated")
        comp2 = m.get("scale_2_weight_kg_compensated")
        if comp1 is not None:
            m["scale_1_weight_kg"] = comp1
        if comp2 is not None:
            m["scale_2_weight_kg"] = comp2
    return measurements


@router.get("/api/v1/measurements/latest", dependencies=[Depends(require_api_key)])
def latest_measurements(limit: int = 50):
    limit = min(max(limit, 1), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                ORDER BY measured_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return serialize_measurements(rows)
