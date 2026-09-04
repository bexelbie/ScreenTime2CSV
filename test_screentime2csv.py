import contextlib
import io
import os
import sqlite3
import struct
import tempfile
import unittest

import screentime2csv


def encode_event(cfa_ts, bundle):
    bundle = bundle.encode("utf-8")
    return b"\x21" + struct.pack("<d", cfa_ts) + b"\x32" + bytes([len(bundle)]) + bundle


def encode_structured_page(events, *, prefix=b"", suffix=b"", truncated=False):
    payload = bytearray(b"SEGB")
    payload.extend(prefix)
    for cfa_ts, bundle in events:
        payload.extend(b"\x10\x00\x00\x00\x00")
        payload.extend(encode_event(cfa_ts, bundle))
        payload.extend(b"\x99\x88\x77\x66")
    if truncated:
        payload.extend(b"\x21" + struct.pack("<d", 648_000_500.0) + b"\x32" + b"\x08" + b"bad")
    payload.extend(suffix)
    return bytes(payload)


class ScreenTime2CSVTests(unittest.TestCase):
    def test_parse_segb_page_reads_bundle_events(self):
        ts1 = 648_000_000.0
        ts2 = 648_000_100.0
        payload = b"SEGB" + encode_event(ts1, "com.example.app") + encode_event(ts2, "com.other.app")

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(payload)
            tmp = handle.name

        try:
            rows = list(screentime2csv.parse_segb_page(tmp))
        finally:
            os.unlink(tmp)

        self.assertEqual(
            rows,
            [
                (ts1 + screentime2csv.CFA_EPOCH, "com.example.app"),
                (ts2 + screentime2csv.CFA_EPOCH, "com.other.app"),
            ],
        )

    def test_parse_segb_page_ignores_unrelated_trailing_bytes(self):
        ts1 = 648_000_000.0
        ts2 = 648_000_100.0
        payload = encode_structured_page(
            [(ts1, "com.example.app"), (ts2, "com.other.app")],
            prefix=b"\x00\x00\x00\x00\xFF\x00\x01\x02",
            suffix=b"\x7F\xFE",
        )

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(payload)
            tmp = handle.name

        try:
            rows = list(screentime2csv.parse_segb_page(tmp))
        finally:
            os.unlink(tmp)

        self.assertEqual(
            rows,
            [
                (ts1 + screentime2csv.CFA_EPOCH, "com.example.app"),
                (ts2 + screentime2csv.CFA_EPOCH, "com.other.app"),
            ],
        )

    def test_parse_segb_page_ignores_random_false_candidate_with_bundle_noise(self):
        ts1 = 648_000_000.0
        ts2 = 648_000_100.0
        payload = b"SEGB" + encode_event(648_000_050.0, "SBSpotlightAlerth") + encode_event(ts1, "com.example.app") + encode_event(ts2, "com.other.app")

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(payload)
            tmp = handle.name

        try:
            rows = list(screentime2csv.parse_segb_page(tmp))
        finally:
            os.unlink(tmp)

        self.assertEqual(
            rows,
            [
                (ts1 + screentime2csv.CFA_EPOCH, "com.example.app"),
                (ts2 + screentime2csv.CFA_EPOCH, "com.other.app"),
            ],
        )

    def test_parse_segb_page_ignores_random_false_candidate_with_implausible_timestamp(self):
        payload = b"SEGB" + encode_event(1.0, "SBSpotlightAlerth") + encode_event(648_000_000.0, "com.example.app")

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(payload)
            tmp = handle.name

        try:
            rows = list(screentime2csv.parse_segb_page(tmp))
        finally:
            os.unlink(tmp)

        self.assertEqual(rows, [(648_000_000.0 + screentime2csv.CFA_EPOCH, "com.example.app")])

    def test_parse_segb_page_raises_on_truncated_event_candidate(self):
        payload = encode_structured_page(
            [(648_000_000.0, "com.example.app")],
            truncated=True,
        )

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(payload)
            tmp = handle.name

        try:
            with self.assertRaises(RuntimeError) as caught:
                screentime2csv.parse_segb_page(tmp)
        finally:
            os.unlink(tmp)

        self.assertIn("Biome", str(caught.exception))
        self.assertIn("page", str(caught.exception).lower())

    def test_sessionize_biome_events_closes_transition_interval_and_respects_max_gap(self):
        events = [
            (1_700_000_000.0, "com.example.app"),
            (1_700_000_150.0, "com.example.app"),
            (1_700_000_300.0, "com.other.app"),
        ]
        sessions = screentime2csv.sessionize_biome_events(events, max_gap=300)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["end"], 1_700_000_300.0)
        self.assertEqual(sessions[0]["usage"], 300.0)
        self.assertEqual(sessions[0]["usage_inferred"], True)
        self.assertEqual(sessions[1]["start"], 1_700_000_300.0)
        self.assertEqual(sessions[1]["usage"], 0.0)

        gap_events = [
            (1_700_000_000.0, "com.example.app"),
            (1_700_000_150.0, "com.example.app"),
            (1_700_000_600.0, "com.other.app"),
        ]
        capped = screentime2csv.sessionize_biome_events(gap_events, max_gap=300)
        self.assertEqual(capped[0]["end"], 1_700_000_450.0)
        self.assertEqual(capped[0]["usage"], 450.0)

    def test_sessionize_biome_events_rejects_negative_max_gap_and_accepts_zero_boundary(self):
        events = [
            (1_700_000_000.0, "com.example.app"),
            (1_700_000_000.0, "com.example.app"),
        ]
        zero_boundary = screentime2csv.sessionize_biome_events(events, max_gap=0)
        self.assertEqual(len(zero_boundary), 1)
        self.assertEqual(zero_boundary[0]["usage"], 0.0)

        with self.assertRaises(ValueError):
            screentime2csv.sessionize_biome_events(events, max_gap=-1)

    def test_main_rejects_negative_biome_max_gap_without_replacing_output(self):
        output_dir = tempfile.mkdtemp()
        output_path = os.path.join(output_dir, "output.csv")
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("existing\n")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                screentime2csv.main(["--biome-max-gap", "-1", "--output", output_path, "--no-knowledge", "--no-biome"])

        self.assertEqual(caught.exception.code, 2)
        with open(output_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "existing\n")
        self.assertIn("--biome-max-gap", stderr.getvalue())
        os.unlink(output_path)
        os.rmdir(output_dir)

    def test_timestamp_formatting_uses_microseconds_for_biome_utc_and_fractional_exactness(self):
        self.assertEqual(screentime2csv.iso_core_data(0, 0), "2001-01-01T00:00:00+00:00")
        self.assertEqual(screentime2csv.iso_utc(1_700_000_000), "2023-11-14T22:13:20.000000Z")
        self.assertEqual(screentime2csv.iso_utc(1_700_000_000.125), "2023-11-14T22:13:20.125000Z")

        row = screentime2csv.make_biome_row(
            "com.example.app",
            1_700_000_000.125,
            1_700_000_100.5,
        )
        self.assertEqual(row["start_time"], 1_700_000_000.125)
        self.assertEqual(row["end_time"], 1_700_000_100.5)
        self.assertEqual(row["usage"], 100.375)
        self.assertEqual(row["start_time_iso"], "2023-11-14T22:13:20.125000Z")
        self.assertEqual(row["end_time_iso"], "2023-11-14T22:15:00.500000Z")

    def test_missing_biome_metadata_stays_truthful(self):
        row = screentime2csv.make_biome_row(
            "com.example.app",
            1_700_000_000,
            1_700_000_100,
            peer_uuid="",
            peer_name="",
            peer_platform="",
            peer_model="",
        )

        self.assertEqual(row["created_at"], "")
        self.assertEqual(row["tz"], "")
        self.assertEqual(row["peer_uuid"], "")
        self.assertEqual(row["peer_name"], "")
        self.assertEqual(row["peer_platform"], "")
        self.assertEqual(row["peer_model"], "")
        self.assertEqual(row["usage_inferred"], True)

    def test_query_knowledgec_uses_single_local_model_for_local_rows(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            db_path = handle.name

        try:
            with sqlite3.connect(f"file:{db_path}?mode=rwc", uri=True) as con:
                con.execute(
                    "CREATE TABLE ZOBJECT (ZVALUESTRING TEXT, ZENDDATE REAL, ZSTARTDATE REAL, ZCREATIONDATE REAL, ZSECONDSFROMGMT INTEGER, ZSOURCE INTEGER, Z_PK INTEGER, ZUUID TEXT, ZSTREAMNAME TEXT)"
                )
                con.execute("CREATE TABLE ZSOURCE (Z_PK INTEGER, ZDEVICEID TEXT)")
                con.execute("CREATE TABLE ZSYNCPEER (ZDEVICEID TEXT, ZMODEL TEXT)")
                con.execute(
                    "INSERT INTO ZOBJECT (ZVALUESTRING, ZENDDATE, ZSTARTDATE, ZCREATIONDATE, ZSECONDSFROMGMT, ZSOURCE, Z_PK, ZUUID, ZSTREAMNAME) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("com.example.app", 10.0, 0.0, 0.0, 0, None, 1, "uuid-1", "/app/usage"),
                )
                con.execute(
                    "INSERT INTO ZOBJECT (ZVALUESTRING, ZENDDATE, ZSTARTDATE, ZCREATIONDATE, ZSECONDSFROMGMT, ZSOURCE, Z_PK, ZUUID, ZSTREAMNAME) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("com.other.app", 20.0, 10.0, 0.0, 0, None, 2, "uuid-2", "/app/usage"),
                )
                con.commit()

            old_db = screentime2csv.KNOWLEDGE_DB
            screentime2csv.KNOWLEDGE_DB = db_path
            try:
                rows = screentime2csv.query_knowledgec(0)
            finally:
                screentime2csv.KNOWLEDGE_DB = old_db

            self.assertEqual(len(rows), 2)
            expected_model = screentime2csv.local_hardware_model()
            self.assertTrue(all(row["origin_status"].startswith(f"local/current Mac ({expected_model})") for row in rows))
        finally:
            os.unlink(db_path)

    def test_write_csv_uses_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.csv")
            row = screentime2csv.make_biome_row("com.example.app", 1_700_000_000, 1_700_000_100)
            screentime2csv.write_csv([row], ",", output=output)

            with open(output, "r", encoding="utf-8") as handle:
                content = handle.read()

            self.assertIn("com.example.app", content)
            self.assertEqual(sum(name.startswith(".screentime2csv-") for name in os.listdir(tmpdir)), 0)

    def test_missing_knowledge_source_aborts_before_output_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.csv")
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("existing\n")

            old_knowledge = screentime2csv.KNOWLEDGE_DB
            old_biome_base = screentime2csv.BIOME_BASE
            old_biome_sync = screentime2csv.BIOME_SYNC_DB
            screentime2csv.KNOWLEDGE_DB = os.path.join(tmpdir, "missing.db")
            screentime2csv.BIOME_BASE = tmpdir
            screentime2csv.BIOME_SYNC_DB = os.path.join(tmpdir, "sync.db")
            try:
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as caught:
                        screentime2csv.main(["-o", output])
                self.assertEqual(caught.exception.code, 1)
                self.assertIn("missing.db", stderr.getvalue())
                with open(output, "r", encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "existing\n")
            finally:
                screentime2csv.KNOWLEDGE_DB = old_knowledge
                screentime2csv.BIOME_BASE = old_biome_base
                screentime2csv.BIOME_SYNC_DB = old_biome_sync

    def test_bad_biome_page_aborts_before_output_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.csv")
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("existing\n")

            stream_dir = os.path.join(tmpdir, "App.InFocus", "remote", "peer")
            os.makedirs(stream_dir)
            with open(os.path.join(stream_dir, "bad.page"), "wb") as handle:
                handle.write(b"bad")

            old_biome_base = screentime2csv.BIOME_BASE
            old_biome_sync = screentime2csv.BIOME_SYNC_DB
            screentime2csv.BIOME_BASE = tmpdir
            screentime2csv.BIOME_SYNC_DB = os.path.join(tmpdir, "sync.db")
            try:
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as caught:
                        screentime2csv.main(["-o", output, "--biome-stream", "App.InFocus", "--no-knowledge"])
                self.assertEqual(caught.exception.code, 1)
                self.assertIn("Biome", stderr.getvalue())
                self.assertIn("page", stderr.getvalue().lower())
                with open(output, "r", encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "existing\n")
            finally:
                screentime2csv.BIOME_BASE = old_biome_base
                screentime2csv.BIOME_SYNC_DB = old_biome_sync

    def test_missing_remote_biome_aborts_before_output_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "out.csv")
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("existing\n")

            os.makedirs(os.path.join(tmpdir, "App.InFocus"))

            old_biome_base = screentime2csv.BIOME_BASE
            old_biome_sync = screentime2csv.BIOME_SYNC_DB
            screentime2csv.BIOME_BASE = tmpdir
            screentime2csv.BIOME_SYNC_DB = os.path.join(tmpdir, "sync.db")
            try:
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as caught:
                        screentime2csv.main(["-o", output, "--biome-stream", "App.InFocus", "--no-knowledge"])
                self.assertEqual(caught.exception.code, 1)
                self.assertIn("Biome", stderr.getvalue())
                self.assertIn("remote", stderr.getvalue().lower())
                with open(output, "r", encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "existing\n")
            finally:
                screentime2csv.BIOME_BASE = old_biome_base
                screentime2csv.BIOME_SYNC_DB = old_biome_sync


if __name__ == "__main__":
    unittest.main()
