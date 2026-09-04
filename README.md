# ScreenTime2CSV

ScreenTime2CSV exports macOS + synced Screen Time data to CSV. The Biome support was adapted from Nate Will's upstream commit [cfbec49de97a044b1147b194baced98eb49a6341](https://github.com/natewill/ScreenTime2CSV/commit/cfbec49de97a044b1147b194baced98eb49a6341).

It reads:

1. `~/Library/Application Support/Knowledge/knowledgeC.db` for the legacy `/app/usage` stream.
2. `~/Library/Biome/streams/restricted/App.InFocus/remote/<peer>/` for sync-peer Screen Time and peer metadata from `~/Library/Biome/sync/sync.db`.

Use `python3 screentime2csv.py` to print CSV to stdout, or `python3 screentime2csv.py -o /path/to/output.csv` to write a deterministic snapshot.

## Usage

```
usage: screentime2csv.py [-h] [-o OUTPUT] [-d DELIMITER] [--since SINCE]
                        [--biome-stream BIOME_STREAM]
                        [--biome-max-gap BIOME_MAX_GAP]
                        [--include-biome-local] [--no-knowledge]
                        [--no-biome]

Export Screen Time rows from knowledgeC.db and the Biome remote stream.

options:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Output file path (default: stdout)
  -d DELIMITER, --delimiter DELIMITER
                        CSV delimiter (default: comma)
  --since SINCE         Only include events newer than 7d/24h/30m or a unix epoch
  --biome-stream BIOME_STREAM
                        Biome stream to scan (default: App.InFocus)
  --biome-max-gap BIOME_MAX_GAP
                        Gap between point-telemetry events that still counts as one inferred session
  --include-biome-local
                        Also include this Mac's local Biome stream when present
```

## Behavior

- Reads SQLite databases in read-only mode.
- Preserves the original numeric timestamps and raw Core Data/CFAbsoluteTime values alongside readable ISO 8601 timestamps.
- Keeps knowledgeC ISO formatting tied to each row's `ZSECONDSFROMGMT` offset.
- Uses UTC for known Biome instants because Biome does not provide a timezone or created_at value.
- Leaves unavailable Biome metadata blank instead of fabricating it.
- Marks Biome durations as inferred sessions from point telemetry, not authoritative Screen Time usage intervals.
- Writes a full snapshot when `-o` is supplied; it does not append incremental `.last` data.

## Example

```bash
python3 screentime2csv.py --since 7d -o /tmp/screentime.csv
python3 screentime2csv.py -d '\t' > screentime.tsv
```
