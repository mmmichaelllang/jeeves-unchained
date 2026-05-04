"""Archive sessions/ files older than N days into sessions/archive/<YYYY>/<MM>/.

Why: load_prior_sessions() only scans the most recent 7 days, so older session
JSONs and briefing HTMLs are dead weight in the working tree. At 1 file/day
each across session-*.json, briefing-*.html, correspondence-*.{json,html},
run-manifest-*.json, debug-*.html — the repo grows by ~5+ files per day.

Behavior:
  - Default retention: 90 days (everything older is archived).
  - Files matched: sessions/session-*, sessions/briefing-*, sessions/correspondence-*,
    sessions/run-manifest-*, sessions/debug-*.
  - Archive layout: sessions/archive/<YYYY>/<MM>/<original-filename>.
  - Skips files that don't have a parseable YYYY-MM-DD in the name.
  - --dry-run: print what would be moved, take no action.

Usage:
  python scripts/archive_old_sessions.py            # archive >90 days old
  python scripts/archive_old_sessions.py --days 30  # archive >30 days old
  python scripts/archive_old_sessions.py --dry-run  # preview only

Recommended cadence: weekly via .github/workflows/archive_sessions.yml.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Files in sessions/ that follow the YYYY-MM-DD naming convention we archive.
# Patterns are (glob, regex-extracts-date).
ARCHIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("session-*.json", re.compile(r"session-(\d{4}-\d{2}-\d{2})")),
    ("briefing-*.html", re.compile(r"briefing-(\d{4}-\d{2}-\d{2})")),
    ("correspondence-*.json", re.compile(r"correspondence-(\d{4}-\d{2}-\d{2})")),
    ("correspondence-*.html", re.compile(r"correspondence-(\d{4}-\d{2}-\d{2})")),
    ("run-manifest-*.json", re.compile(r"run-manifest-(\d{4}-\d{2}-\d{2})")),
    ("debug-*.html", re.compile(r"debug-(\d{4}-\d{2}-\d{2})")),
)


def _file_date(path: Path, regex: re.Pattern[str]) -> date | None:
    m = regex.search(path.name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def archive_old_sessions(
    sessions_dir: Path,
    *,
    days: int = 90,
    today: date | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Archive files older than ``days`` from ``sessions_dir`` into archive/YYYY/MM.

    Returns (archived_count, skipped_count). Skipped includes files we couldn't
    date-parse (e.g. .local.json files don't follow the pattern).
    """
    if today is None:
        today = date.today()
    cutoff = today - timedelta(days=days)
    archive_root = sessions_dir / "archive"

    archived = 0
    skipped = 0
    for glob, regex in ARCHIVE_PATTERNS:
        for path in sorted(sessions_dir.glob(glob)):
            if not path.is_file():
                continue
            if "/archive/" in str(path):  # safety: skip already-archived
                continue
            file_date = _file_date(path, regex)
            if file_date is None:
                skipped += 1
                continue
            if file_date >= cutoff:
                continue
            target_dir = archive_root / f"{file_date.year:04d}" / f"{file_date.month:02d}"
            target = target_dir / path.name
            if dry_run:
                log.info("[dry-run] would archive %s -> %s", path.name, target)
                archived += 1
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            log.info("archived %s -> %s", path.name, target)
            archived += 1
    return archived, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90,
                        help="Archive files older than this many days (default 90)")
    parser.add_argument("--sessions-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent / "sessions",
                        help="Path to sessions/ directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen, take no action")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.sessions_dir.exists():
        log.error("sessions directory not found: %s", args.sessions_dir)
        return 1

    archived, skipped = archive_old_sessions(
        args.sessions_dir, days=args.days, dry_run=args.dry_run,
    )
    log.info(
        "archive complete: %d files %s, %d skipped (no parseable date)",
        archived, "to-be-moved" if args.dry_run else "moved", skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
