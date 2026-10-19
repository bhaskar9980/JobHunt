"""Helpers shared by the CLI sub-apps (cli.py, cli_mail.py, cli_refer.py)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import typer

from jobhunt.config import Settings, load_settings
from jobhunt.db import connect, init_db
from jobhunt.logging_setup import configure_logging

EXIT_INVALID = 2


@dataclass(slots=True)
class Ctx:
    settings: Settings
    conn: sqlite3.Connection


def build_ctx(verbose: bool = False) -> Ctx:
    settings = load_settings()
    configure_logging(settings, verbose=verbose)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    return Ctx(settings, conn)


def echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def fail(message: str, code: int = EXIT_INVALID) -> typer.Exit:
    """Print to stderr and return an Exit for the caller to raise."""
    typer.echo(message, err=True)
    return typer.Exit(code)
