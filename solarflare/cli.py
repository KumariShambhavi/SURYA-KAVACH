"""
cli.py — command-line entry point.

  solarflare convert [--source DIR] [--include SUBSTR ...]   # PRADAN zips -> CSVs
  solarflare ingest  [--force]                               # run pipeline -> DB
  solarflare status                                          # operations status
  solarflare serve   [--host H] [--port P]                   # run the API + UI
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config


def _cmd_convert(args) -> None:
    from .io import convert_all
    config.ensure_dirs()
    src = Path(args.source) if args.source else config.default_incoming_dir()
    convert_all(source_dir=src, include=args.include)


def _cmd_ingest(args) -> None:
    from .ingest import ingest
    res = ingest(force=args.force)
    print(json.dumps({k: v for k, v in res.items() if k != "summary"}, indent=2, default=str))
    if res.get("summary"):
        print("\nSummary:")
        print(json.dumps(res["summary"], indent=2, default=str))


def _cmd_status(_args) -> None:
    from .ingest import monitor_status
    print(json.dumps(monitor_status(), indent=2, default=str))


def _cmd_validate(_args) -> None:
    import pandas as pd
    from . import store
    from .ingest import run_validation_sync
    store.init_db()
    master = pd.DataFrame(store.get_flares(limit=100000))
    if master.empty:
        print("No catalogue yet — run 'ingest' first.")
        return
    print("Fetching GOES reference and validating (first run may take ~1-2 min)...")
    print(json.dumps(run_validation_sync(master), indent=2, default=str))


def _cmd_serve(args) -> None:
    import os
    import uvicorn
    os.environ.setdefault("SOLARFLARE_RUN_SCHEDULER", "1")   # enable the live daemon
    uvicorn.run("solarflare.api.app:app", host=args.host, port=args.port, reload=args.reload)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="solarflare",
                                description="SURYA-KAVACH solar-flare nowcasting & forecasting")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("convert", help="convert raw PRADAN zips to CSVs")
    c.add_argument("--source", default=None, help="folder with AL1_SLX_*.zip / HLS_*.zip")
    c.add_argument("--include", action="append", default=None, metavar="SUBSTR",
                   help="only archives whose name contains this (repeatable), e.g. 202403")
    c.set_defaults(func=_cmd_convert)

    i = sub.add_parser("ingest", help="run the pipeline and persist results")
    i.add_argument("--force", action="store_true", help="ingest even if no new raw data")
    i.set_defaults(func=_cmd_ingest)

    s = sub.add_parser("status", help="show operations status")
    s.set_defaults(func=_cmd_status)

    v = sub.add_parser("validate", help="cross-validate the catalogue against the GOES list")
    v.set_defaults(func=_cmd_validate)

    sv = sub.add_parser("serve", help="run the API + dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=_cmd_serve)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
