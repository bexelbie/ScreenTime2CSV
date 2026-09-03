import os
import sqlite3
import argparse
import csv
import subprocess
from datetime import datetime, timedelta, timezone
from io import StringIO

knowledge_db = os.path.expanduser("~/Library/Application Support/Knowledge/knowledgeC.db")
core_data_epoch = 978307200
headers = [
    "app", "usage", "start_time", "end_time", "created_at", "tz",
    "device_id", "device_model", "object_id", "event_uuid",
    "start_date_raw", "end_date_raw", "creation_date_raw",
    "start_time_iso", "end_time_iso", "created_at_iso",
    "origin_status", "source_id",
]

def query_database(last_created_at):
    # Check if knowledgeC.db exists
    if not os.path.exists(knowledge_db):
        print("Could not find knowledgeC.db at %s." % (knowledge_db))
        exit(1)

    # Check if knowledgeC.db is readable
    if not os.access(knowledge_db, os.R_OK):
        print("The knowledgeC.db at %s is not readable.\nPlease grant full disk access to the application running the script (e.g. Terminal, iTerm, VSCode etc.)." % (knowledge_db))
        exit(1)

    # Connect to the SQLite database
    with sqlite3.connect("file:%s?mode=ro" % knowledge_db, uri=True) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # Execute the SQL query to fetch data
        # Modified from https://rud.is/b/2019/10/28/spelunking-macos-screentime-app-usage-with-r/
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
        FROM
            ZOBJECT
            LEFT JOIN
            ZSOURCE
            ON ZOBJECT.ZSOURCE = ZSOURCE.Z_PK
            LEFT JOIN
            ZSYNCPEER
            ON ZSOURCE.ZDEVICEID = ZSYNCPEER.ZDEVICEID
        WHERE
            ZSTREAMNAME = "/app/usage" AND
            (ZOBJECT.ZCREATIONDATE + 978307200) > ?
        ORDER BY
            ZCREATIONDATE DESC
        """
        cur.execute(query, (last_created_at,))

        # Fetch all rows from the result set
        return cur.fetchall()

def iso_date(value, offset):
    """Convert a Core Data timestamp using the offset stored with its event.

    >>> iso_date(0, 0)
    '2001-01-01T00:00:00+00:00'
    """
    event_timezone = timezone(timedelta(seconds=offset or 0))
    return datetime.fromtimestamp(value + core_data_epoch, event_timezone).isoformat()

def output_rows(rows):
    local_model = subprocess.check_output(
        ["sysctl", "-n", "hw.model"], text=True
    ).strip()

    for row in rows:
        if row["source_id"] is None:
            origin_status = "local/current Mac (%s); no per-event ID" % local_model
        elif row["device_id"] is None:
            origin_status = "source present; device ID unavailable"
        else:
            origin_status = "synced/remote device"

        yield [
            row["app"], row["usage"], row["start_time"], row["end_time"],
            row["created_at"], row["tz"], row["device_id"],
            row["device_model"], row["object_id"], row["event_uuid"],
            row["start_date_raw"], row["end_date_raw"],
            row["creation_date_raw"],
            iso_date(row["start_date_raw"], row["tz"]),
            iso_date(row["end_date_raw"], row["tz"]),
            iso_date(row["creation_date_raw"], row["tz"]),
            origin_status, row["source_id"],
        ]

def write_to_csv(rows, output, delimiter):
    writer = csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)

def main():
    parser = argparse.ArgumentParser(description="Query knowledge database")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("-d", "--delimiter", default=',', help="Delimiter for output file (default: comma)")
    args = parser.parse_args()

    # Prepare output format
    delimiter = args.delimiter.replace("\\t", "\t")
    if len(delimiter) != 1:
        parser.error("delimiter must be one character")

    file_has_data = args.output and os.path.isfile(args.output) and os.path.getsize(args.output)
    if file_has_data:
        with open(args.output, newline="") as f:
            if next(csv.reader(f, delimiter=delimiter), []) != headers:
                parser.error("%s has an incompatible header; use a new output file" % args.output)

    last_created_at_file = args.output + ".last" if args.output else None
    if last_created_at_file and os.path.isfile(last_created_at_file):
        with open(last_created_at_file, "r") as f:
            last_created_at = float(f.read().strip())
    else:
        last_created_at = 0.0

    # Query the database and fetch the rows
    rows = query_database(last_created_at)
    formatted_rows = list(output_rows(rows))

    # Write the output to a file or print to stdout
    if args.output:
        with open(args.output, "a", newline='') as f:
            writer = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
            if not file_has_data:
                writer.writerow(headers)
            writer.writerows(formatted_rows)
        if rows:
            with open(last_created_at_file, "w") as f:
                f.write(str(rows[0]["created_at"]))
    else:
        output = StringIO()
        write_to_csv(formatted_rows, output, delimiter)
        print(output.getvalue())

if __name__ == "__main__":
    main()