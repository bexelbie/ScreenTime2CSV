#!/usr/bin/env python3

import argparse
import csv
import glob
import os
import sqlite3
import string
import struct
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import StringIO

KNOWLEDGE_DB = os.path.expanduser("~/Library/Application Support/Knowledge/knowledgeC.db")
BIOME_BASE = os.path.expanduser("~/Library/Biome/streams/restricted")
BIOME_SYNC_DB = os.path.expanduser("~/Library/Biome/sync/sync.db")
CFA_EPOCH = 978307200
DEFAULT_BIOME_STREAM = "App.InFocus"
DEFAULT_BIOME_MAX_GAP = 300

CSV_FIELDS = [
    "app",
    "usage",
    "start_time",
    "end_time",
    "created_at",
    "tz",
    "device_id",
    "device_model",
    "object_id",
    "event_uuid",
    "start_date_raw",
    "end_date_raw",
    "creation_date_raw",
    "start_time_iso",
    "end_time_iso",
    "created_at_iso",
    "origin_status",
    "source_id",
    "peer_uuid",
    "peer_name",
    "peer_platform",
    "peer_model",
    "usage_inferred",
]

def parse_since(value):
    if value is None:
        return 0.0
    value = value.strip()
    if value.endswith("d"):
        return time.time() - int(value[:-1]) * 86400
    if value.endswith("h"):
        return time.time() - int(value[:-1]) * 3600
    if value.endswith("m"):
        return time.time() - int(value[:-1]) * 60
    return float(value)


def non_negative_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def iso_core_data(core_data_value, seconds_from_gmt):
    if core_data_value in (None, ""):
        return ""
    tz = timezone(timedelta(seconds=int(seconds_from_gmt or 0)))
    return datetime.fromtimestamp(float(core_data_value) + CFA_EPOCH, tz).isoformat()


def iso_utc(unix_timestamp):
    if unix_timestamp in (None, ""):
        return ""
    return datetime.fromtimestamp(float(unix_timestamp), tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@lru_cache(maxsize=1)
def local_hardware_model():
    try:
        return subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip()
    except Exception:
        return "unknown"


def normalize_numeric(value):
    if value in (None, ""):
        return ""
    value = float(value)
    if value.is_integer():
        return int(value)
    return value


def looks_like_biome_bundle_id(value):
    if value in (None, ""):
        return False
    if not isinstance(value, str):
        value = value.decode("utf-8")
    if not value or "." not in value:
        return False
    if value.startswith(".") or value.endswith("."):
        return False
    if any(part == "" for part in value.split(".")):
        return False
    allowed = set(string.ascii_letters + string.digits + "-_.")
    return all(ch in allowed for ch in value)


# knowledgeC.db

def query_knowledgec(since_ts, *, required=True):
    if not os.path.exists(KNOWLEDGE_DB):
        if not required:
            return []
        raise FileNotFoundError(f"{KNOWLEDGE_DB} not found")
    if not os.access(KNOWLEDGE_DB, os.R_OK):
        if not required:
            return []
        raise PermissionError(
            f"{KNOWLEDGE_DB} is not readable — grant Full Disk Access to your terminal/Python and retry"
        )

    query = """
        SELECT
            ZOBJECT.ZVALUESTRING AS app,
            (ZOBJECT.ZENDDATE - ZOBJECT.ZSTARTDATE) AS usage,
            (ZOBJECT.ZSTARTDATE + 978307200) AS start_time,
            (ZOBJECT.ZENDDATE + 978307200) AS end_time,
            (ZOBJECT.ZCREATIONDATE + 978307200) AS created_at,
            ZOBJECT.ZSECONDSFROMGMT AS tz,
            ZSOURCE.ZDEVICEID AS device_id,
            ZSYNCPEER.ZMODEL AS device_model,
            ZOBJECT.Z_PK AS object_id,
            ZOBJECT.ZUUID AS event_uuid,
            ZOBJECT.ZSTARTDATE AS start_date_raw,
            ZOBJECT.ZENDDATE AS end_date_raw,
            ZOBJECT.ZCREATIONDATE AS creation_date_raw,
            ZOBJECT.ZSOURCE AS source_id
        FROM ZOBJECT
        LEFT JOIN ZSOURCE ON ZOBJECT.ZSOURCE = ZSOURCE.Z_PK
        LEFT JOIN ZSYNCPEER ON ZSOURCE.ZDEVICEID = ZSYNCPEER.ZDEVICEID
        WHERE ZSTREAMNAME = '/app/usage'
          AND (ZOBJECT.ZSTARTDATE + 978307200) >= ?
        ORDER BY ZOBJECT.ZSTARTDATE ASC
    """

    local_model = local_hardware_model()
    rows = []
    try:
        with sqlite3.connect(f"file:{KNOWLEDGE_DB}?mode=ro", uri=True) as con:
            for row in con.execute(query, (since_ts,)):
                app, usage, start_time, end_time, created_at, tz, device_id, device_model, object_id, event_uuid, start_date_raw, end_date_raw, creation_date_raw, source_id = row
                if source_id is None:
                    origin_status = f"local/current Mac ({local_model}); no per-event ID"
                elif device_id is None:
                    origin_status = "source present; device ID unavailable"
                else:
                    origin_status = "synced/remote device"

                rows.append(
                    {
                        "app": app or "",
                        "usage": int(round(float(usage or 0))),
                        "start_time": int(round(float(start_time or 0))),
                        "end_time": int(round(float(end_time or 0))),
                        "created_at": float(created_at or 0.0),
                        "tz": int(tz) if tz is not None else "",
                        "device_id": device_id or "",
                        "device_model": device_model or "",
                        "object_id": object_id or "",
                        "event_uuid": event_uuid or "",
                        "start_date_raw": float(start_date_raw) if start_date_raw is not None else "",
                        "end_date_raw": float(end_date_raw) if end_date_raw is not None else "",
                        "creation_date_raw": float(creation_date_raw) if creation_date_raw is not None else "",
                        "start_time_iso": iso_core_data(start_date_raw, tz),
                        "end_time_iso": iso_core_data(end_date_raw, tz),
                        "created_at_iso": iso_core_data(creation_date_raw, tz),
                        "origin_status": origin_status,
                        "source_id": source_id or "",
                        "peer_uuid": "",
                        "peer_name": "",
                        "peer_platform": "",
                        "peer_model": "",
                        "usage_inferred": "",
                    }
                )
    except sqlite3.Error as exc:
        raise RuntimeError(f"knowledgeC query failed for {KNOWLEDGE_DB}: {exc}") from exc
    return rows


def normalize_device_model(value):
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.casefold()


def knowledgec_hardware_bridge_supported(path=KNOWLEDGE_DB):
    if not os.path.exists(path):
        return False
    if not os.access(path, os.R_OK):
        return False

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            columns = [row[1] for row in con.execute("PRAGMA table_info(ZSYNCPEER)")]
    except sqlite3.Error:
        return False

    return "ZRAPPORTID" in columns and "ZMODEL" in columns and "ZLASTSEENDATE" in columns


def biome_hardware_bridge_supported(path=BIOME_SYNC_DB):
    if not os.path.exists(path):
        return False
    if not os.access(path, os.R_OK):
        return False

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            columns = [row[1] for row in con.execute("PRAGMA table_info(DevicePeer)")]
    except sqlite3.Error:
        return False

    return "device_identifier" in columns and "ids_device_identifier" in columns


def load_knowledgec_hardware_models(required=True):
    if not os.path.exists(KNOWLEDGE_DB):
        if not required:
            return {}
        raise FileNotFoundError(f"{KNOWLEDGE_DB} not found")
    if not os.access(KNOWLEDGE_DB, os.R_OK):
        if not required:
            return {}
        raise PermissionError(
            f"{KNOWLEDGE_DB} is not readable — grant Full Disk Access to your terminal/Python and retry"
        )

    if not knowledgec_hardware_bridge_supported(KNOWLEDGE_DB):
        return {}

    hardware_by_rapport = defaultdict(list)
    try:
        with sqlite3.connect(f"file:{KNOWLEDGE_DB}?mode=ro", uri=True) as con:
            for rapport_id, model, last_seen, _ in con.execute(
                "SELECT ZRAPPORTID, ZMODEL, ZLASTSEENDATE, Z_PK FROM ZSYNCPEER"
            ):
                if rapport_id is None:
                    continue

                rapport_key = str(rapport_id).strip()
                if not rapport_key:
                    continue

                model_value = str(model).strip() if model is not None else ""
                normalized_model = normalize_device_model(model_value)
                try:
                    last_seen_value = float(last_seen) if last_seen not in (None, "") else float("-inf")
                except (TypeError, ValueError):
                    last_seen_value = float("-inf")

                hardware_by_rapport[rapport_key].append(
                    {
                        "model": model_value,
                        "normalized_model": normalized_model,
                        "last_seen": last_seen_value,
                    }
                )
    except sqlite3.Error as exc:
        if "no such column" in str(exc).lower() or "no such table" in str(exc).lower():
            return {}
        raise RuntimeError(f"knowledgeC hardware mapping query failed for {KNOWLEDGE_DB}: {exc}") from exc

    hardware_models = {}
    for rapport_key, records in hardware_by_rapport.items():
        if not records:
            continue

        newest_seen = max(record["last_seen"] for record in records)
        newest_records = [record for record in records if record["last_seen"] == newest_seen]
        nonblank_normalized = {record["normalized_model"] for record in newest_records if record["normalized_model"] is not None}
        if len(nonblank_normalized) != 1:
            continue

        selected = next(iter(nonblank_normalized))
        for record in newest_records:
            if record["normalized_model"] == selected:
                hardware_models[rapport_key] = record["model"]
                break

    return hardware_models


def load_biome_peers():
    if not os.path.exists(BIOME_SYNC_DB):
        return {}
    if not os.access(BIOME_SYNC_DB, os.R_OK):
        raise PermissionError(f"{BIOME_SYNC_DB} is not readable")

    peers = {}
    try:
        with sqlite3.connect(f"file:{BIOME_SYNC_DB}?mode=ro", uri=True) as con:
            columns = [row[1] for row in con.execute("PRAGMA table_info(DevicePeer)")]
            if not columns or "device_identifier" not in columns:
                return {}

            select_columns = [
                column_name for column_name in ("device_identifier", "ids_device_identifier", "name", "platform", "model") if column_name in columns
            ]
            if not select_columns:
                return {}

            for row in con.execute(f"SELECT {', '.join(select_columns)} FROM DevicePeer"):
                values = dict(zip(select_columns, row))
                device_identifier = values.get("device_identifier")
                if device_identifier is None:
                    continue

                key = str(device_identifier).strip()
                if not key:
                    continue

                peers[key] = {
                    "peer_uuid": key,
                    "peer_name": values.get("name") or "",
                    "peer_platform": values.get("platform") if values.get("platform") is not None else "",
                    "peer_model": values.get("model") or "",
                    "ids_device_identifier": values.get("ids_device_identifier") or "",
                }
    except sqlite3.Error as exc:
        if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
            return {}
        raise RuntimeError(f"could not read {BIOME_SYNC_DB}: {exc}") from exc
    return peers


def parse_segb_page(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise RuntimeError(f"unreadable Biome page {path}: {exc}") from exc

    if len(data) < 4 or data[:4] != b"SEGB":
        raise RuntimeError(f"invalid Biome page format {path}")

    events = []
    cursor = 4
    while cursor < len(data):
        if data[cursor] != 0x21:
            cursor += 1
            continue

        if cursor + 9 > len(data):
            raise RuntimeError(f"malformed Biome page {path}: truncated field-4/fixed64 candidate at offset {cursor}")

        try:
            ts_cfa = struct.unpack_from("<d", data, cursor + 1)[0]
        except struct.error as exc:
            raise RuntimeError(f"malformed Biome page {path}: invalid field-4/fixed64 candidate at offset {cursor}") from exc

        if not (6.0e8 < ts_cfa < 9.5e8):
            cursor += 1
            continue

        field_pos = cursor + 9
        if field_pos >= len(data):
            raise RuntimeError(f"malformed Biome page {path}: truncated field-6/length candidate at offset {cursor}")
        if data[field_pos] != 0x32:
            cursor += 1
            continue

        if field_pos + 1 >= len(data):
            raise RuntimeError(f"malformed Biome page {path}: truncated field-6 length at offset {field_pos}")

        name_len = data[field_pos + 1]
        name_start = field_pos + 2
        name_end = name_start + name_len
        if name_end > len(data):
            raise RuntimeError(f"malformed Biome page {path}: truncated bundle payload at offset {field_pos}")

        bundle_bytes = data[name_start:name_end]
        if not bundle_bytes:
            cursor += 1
            continue

        try:
            bundle = bundle_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"malformed Biome page {path}: invalid UTF-8 bundle payload at offset {field_pos}") from exc

        if not looks_like_biome_bundle_id(bundle):
            cursor = name_end
            continue

        events.append((ts_cfa + CFA_EPOCH, bundle))
        cursor = name_end

    return events


def sessionize_biome_events(events, max_gap=DEFAULT_BIOME_MAX_GAP):
    if max_gap < 0:
        raise ValueError("max_gap must be >= 0")
    if not events:
        return []

    ordered = sorted(events, key=lambda item: item[0])
    sessions = []
    current_bundle = None
    current_start = None
    current_end = None
    last_event_ts = None

    for ts, bundle in ordered:
        if current_bundle is None:
            current_bundle = bundle
            current_start = ts
            current_end = ts
            last_event_ts = ts
            continue

        gap = ts - last_event_ts
        if bundle == current_bundle and gap <= max_gap:
            current_end = ts
            last_event_ts = ts
            continue

        close_ts = last_event_ts + min(max_gap, gap)
        sessions.append(
            {
                "bundle": current_bundle,
                "start": current_start,
                "end": close_ts,
                "usage": max(close_ts - current_start, 0.0),
                "usage_inferred": True,
            }
        )

        current_bundle = bundle
        current_start = ts
        current_end = ts
        last_event_ts = ts

    if current_bundle is not None:
        sessions.append(
            {
                "bundle": current_bundle,
                "start": current_start,
                "end": current_end,
                "usage": max(current_end - current_start, 0.0),
                "usage_inferred": True,
            }
        )

    return sessions


def make_biome_row(app, start_time, end_time, peer_uuid="", peer_name="", peer_platform="", peer_model="", device_model=""):
    start_time = float(start_time)
    end_time = float(end_time)
    usage = max(end_time - start_time, 0.0)
    raw_start = start_time - CFA_EPOCH
    raw_end = end_time - CFA_EPOCH

    return {
        "app": app or "",
        "usage": normalize_numeric(usage),
        "start_time": normalize_numeric(start_time),
        "end_time": normalize_numeric(end_time),
        "created_at": "",
        "tz": "",
        "device_id": "",
        "device_model": device_model or "",
        "object_id": "",
        "event_uuid": "",
        "start_date_raw": raw_start,
        "end_date_raw": raw_end,
        "creation_date_raw": "",
        "start_time_iso": iso_utc(start_time),
        "end_time_iso": iso_utc(end_time),
        "created_at_iso": "",
        "origin_status": "inferred Biome point telemetry session; not authoritative Screen Time duration",
        "source_id": "",
        "peer_uuid": peer_uuid or "",
        "peer_name": peer_name or "",
        "peer_platform": peer_platform if peer_platform is not None else "",
        "peer_model": peer_model or "",
        "usage_inferred": True,
    }


def collect_biome_rows(stream_name, since_ts, max_gap=DEFAULT_BIOME_MAX_GAP, include_local=False, *, required=True, hardware_models=None):
    root = os.path.join(BIOME_BASE, stream_name)
    if not os.path.isdir(root):
        if not required:
            return []
        raise FileNotFoundError(f"Biome stream {root} not found")
    if not os.access(root, os.R_OK | os.X_OK):
        if not required:
            return []
        raise PermissionError(f"Biome stream {root} is not readable")

    remote_dir = os.path.join(root, "remote")
    if not os.path.isdir(remote_dir):
        if not required:
            return []
        raise FileNotFoundError(f"Biome stream remote {remote_dir} not found")
    if not os.access(remote_dir, os.R_OK | os.X_OK):
        if not required:
            return []
        raise PermissionError(f"Biome stream remote {remote_dir} is not readable")

    by_peer = defaultdict(list)

    if include_local:
        local_dir = os.path.join(root, "local")
        if os.path.isdir(local_dir):
            for path in sorted(glob.glob(os.path.join(local_dir, "*"))):
                if not os.path.isfile(path):
                    continue
                events = parse_segb_page(path)
                for ts, bundle in events:
                    if ts >= since_ts:
                        by_peer[None].append((ts, bundle))

    for peer in sorted(os.listdir(remote_dir)):
        peer_path = os.path.join(remote_dir, peer)
        if not os.path.isdir(peer_path):
            continue
        for path in sorted(glob.glob(os.path.join(peer_path, "*"))):
            if not os.path.isfile(path):
                continue
            events = parse_segb_page(path)
            for ts, bundle in events:
                if ts >= since_ts:
                    by_peer[peer].append((ts, bundle))

    peers = load_biome_peers()
    hardware_models = {} if hardware_models is None else hardware_models
    rows = []
    for peer, events in by_peer.items():
        if not events:
            continue
        peer_info = peers.get(peer, {})
        peer_ids = (peer_info.get("ids_device_identifier") or "").strip()
        device_model = hardware_models.get(peer_ids, "") if peer_ids else ""
        sessions = sessionize_biome_events(events, max_gap=max_gap)
        for session in sessions:
            rows.append(
                make_biome_row(
                    session["bundle"],
                    session["start"],
                    session["end"],
                    peer_uuid=peer or "",
                    peer_name=peer_info.get("peer_name", ""),
                    peer_platform=peer_info.get("peer_platform", ""),
                    peer_model=peer_info.get("peer_model", ""),
                    device_model=device_model,
                )
            )

    rows.sort(key=lambda row: row["start_time"])
    return rows


def flatten_row(row):
    return [row.get(field, "") for field in CSV_FIELDS]


def write_csv(rows, delimiter, output=None):
    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_FIELDS)
    for row in rows:
        writer.writerow(flatten_row(row))

    if output is None:
        sys.stdout.write(csv_buffer.getvalue())
        return

    directory = os.path.dirname(output) or "."
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(prefix=".screentime2csv-", dir=directory)
    try:
        with os.fdopen(fd, "w", newline="") as handle:
            handle.write(csv_buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export Mac + iPhone Screen Time app usage to CSV",
    )
    parser.add_argument("-o", "--output", dest="output", help="Output file path (default: stdout)")
    parser.add_argument("-d", "--delimiter", default=",", help="Delimiter for output file (default: comma)")
    parser.add_argument("--since", default=None, help="Only include events newer than 7d/24h/30m or a unix epoch")
    parser.add_argument("--biome-stream", default=DEFAULT_BIOME_STREAM, help=f"Biome stream name (default: {DEFAULT_BIOME_STREAM})")
    parser.add_argument(
        "--biome-max-gap",
        type=non_negative_int,
        default=DEFAULT_BIOME_MAX_GAP,
        help=f"Seconds between Biome point events to still count as one inferred session (default: {DEFAULT_BIOME_MAX_GAP})",
    )
    parser.add_argument("--include-biome-local", action="store_true", help="Also include this Mac's local Biome stream when present")
    parser.add_argument("--no-knowledge", action="store_true", help="Skip knowledgeC input")
    parser.add_argument("--no-biome", action="store_true", help="Skip Biome input")
    args = parser.parse_args(argv)

    delimiter = args.delimiter.replace("\\t", "\t")
    if len(delimiter) != 1:
        parser.error("delimiter must be one character")

    since_ts = parse_since(args.since)
    try:
        rows = []
        hardware_models = {}
        if not args.no_knowledge:
            rows.extend(query_knowledgec(since_ts, required=True))

        should_load_bridge_models = (
            not args.no_knowledge
            and not args.no_biome
            and knowledgec_hardware_bridge_supported(KNOWLEDGE_DB)
            and biome_hardware_bridge_supported(BIOME_SYNC_DB)
        )
        if should_load_bridge_models:
            hardware_models = load_knowledgec_hardware_models(required=True)

        if not args.no_biome:
            rows.extend(
                collect_biome_rows(
                    args.biome_stream,
                    since_ts,
                    max_gap=args.biome_max_gap,
                    include_local=args.include_biome_local,
                    required=True,
                    hardware_models=hardware_models,
                )
            )
    except (FileNotFoundError, PermissionError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    rows.sort(key=lambda row: row["start_time"] if row.get("start_time") not in (None, "") else 0)
    write_csv(rows, delimiter, output=args.output)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
