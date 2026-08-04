# vid-dupes

Personal scripts for finding duplicate video files using perceptual hashing. Scans a directory tree, stores frame hashes in SQLite, then compares them to find likely duplicates.
I've used it at least once to fix a botched backup/restore. Does it work for other things? Probably?

## How it works

`scan.py` walks your video directories, extracts N evenly-spaced frames per video using ffmpeg, computes a perceptual hash (phash) for each frame, and stores everything in a SQLite database. Files must have their MD5 encoded in the filename as `[<hex32>]`, it won't scan files without it.

`find_dupes.py` reads the database and compares videos that are within a duration window of each other. It computes mean Hamming distance across paired frame hashes. Videos below the threshold are grouped and printed. Can generate a bash script of `rm` commands to review and execute.

`manage_non_matches.py` lets you record pairs of videos that are confirmed NOT duplicates, so they get excluded from future runs.

`merge_db.py` merges multiple scan databases into one. Useful if you scan different directories separately and want to compare across them.

## Dependencies

```
pip install -r requirements.txt
```

ffmpeg and ffprobe must be on your PATH. I would suspect basically any somewhat-new version(s) would work.

## Basic usage

```bash
# Scan a directory
python scan.py --db ~/videos.db /mnt/videos/

# Ignore subdirectories
python scan.py --db ~/videos.db --ignore /mnt/videos/trash /mnt/videos/

# Find duplicates
python find_dupes.py --db ~/videos.db

# Generate a delete script to review
python find_dupes.py --db ~/videos.db --script dupes.sh

# Clean up DB after deleting files
python scan.py --db ~/videos.db --prune-videos
python scan.py --db ~/videos.db --prune-hashes
python scan.py --db ~/videos.db --prune-all

# Mark two videos as confirmed non-duplicates
python manage_non_matches.py --db ~/videos.db add <md5a> <md5b>

# Merge two databases
python merge_db.py a.db b.db --target combined.db
```

## AI disclosure

Used AI to help debug some numpy oddities. Did I need to do that? For my sanity: yes.

## Database

Three tables:

- `videos` - one row per file path, with metadata (duration, resolution, codec, etc.)
- `frame_hashes` - perceptual hashes keyed by md5, shared across duplicate paths
- `non_matches` - confirmed non-duplicate pairs, excluded from comparison

Frame hashes are keyed by md5 rather than by file path, so if you have multiple copies of the same file they share one set of hashes. Moving a file is cheap-ish as the next scan just registers the new path without re-extracting frames.
