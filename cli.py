"""CLI entry point for homelab-netbox sync.

Usage:
    python cli.py [--dry-run] [--verbose] [--sources proxmox,coolify,pulse,npm] [--export infisical]
"""

from __future__ import annotations

import argparse
import sys
import urllib3

from config import load_config
from sync import run_sync

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Sync homelab infrastructure state to NetBox.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without applying anything.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show skipped (unchanged) objects too.",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default=None,
        help="Comma-separated list of sources to use (default: all configured). "
             "Options: proxmox,coolify,pulse,npm",
    )
    parser.add_argument(
        "--export",
        type=str,
        default=None,
        help="Comma-separated list of exporters to run (default: all configured). "
             "Options: infisical",
    )
    args = parser.parse_args(argv)

    # Load config
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    # Parse source/export flags
    sources = set(args.sources.split(",")) if args.sources else None
    exporters = set(args.export.split(",")) if args.export else None

    run_sync(
        cfg=cfg,
        dry_run=args.dry_run,
        verbose=args.verbose,
        sources=sources,
        exporters=exporters,
    )


if __name__ == "__main__":
    main()
