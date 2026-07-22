"""Local administration commands for the review application."""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import inspect, text

from collabvet_review_app.models import User, db
from collabvet_review_app.services import (
    index_cases,
    rebuild_approved_index,
    verify_clinical_data_checkout,
)


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Create or upgrade the local review database."""

    inspector = inspect(db.engine)
    if "revision" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("revision")}
        if "source_hash" not in columns:
            db.session.execute(text("ALTER TABLE revision ADD COLUMN source_hash VARCHAR(64)"))
            db.session.execute(
                text(
                    "UPDATE revision SET source_hash = "
                    "(SELECT source_hash FROM case_record WHERE case_record.id = revision.case_id)"
                )
            )
            db.session.commit()
    if "section_review" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("section_review")}
        if "artifact_hash" not in columns:
            db.session.execute(
                text("ALTER TABLE section_review ADD COLUMN artifact_hash VARCHAR(64)")
            )
            db.session.commit()
    db.create_all()
    if current_app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        db.session.execute(text("PRAGMA user_version = 3"))
    db.session.commit()
    click.echo("Review database schema is at version 3.")


@click.command("create-user")
@click.option("--email", prompt=True)
@click.option("--name", prompt="Display name")
@click.option("--role", type=click.Choice(["reviewer", "admin"]), default="reviewer")
@click.password_option(confirmation_prompt=True)
@with_appcontext
def create_user_command(email: str, name: str, role: str, password: str) -> None:
    """Create an attributable application user."""

    normalized = email.strip().lower()
    if User.query.filter_by(email=normalized).one_or_none():
        raise click.ClickException("A user with that email already exists")
    user = User(email=normalized, display_name=name.strip(), role=role)
    try:
        user.set_password(password)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    db.session.add(user)
    db.session.commit()
    click.echo(f"Created {role} user {normalized}.")


@click.command("set-password")
@click.option("--email", prompt=True)
@click.password_option(confirmation_prompt=True)
@with_appcontext
def set_password_command(email: str, password: str) -> None:
    """Set or rotate an existing application user's password."""

    normalized = email.strip().lower()
    user = User.query.filter_by(email=normalized).one_or_none()
    if user is None:
        raise click.ClickException("No user exists with that email")
    try:
        user.set_password(password)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    db.session.commit()
    click.echo(f"Updated password for {normalized}.")


@click.command("index-cases")
@with_appcontext
def index_cases_command() -> None:
    """Index immutable longitudinal source records."""

    result = index_cases()
    click.echo(json.dumps(result, indent=2))


@click.command("verify-case-source")
@with_appcontext
def verify_case_source_command() -> None:
    """Verify the external clinical-data checkout and print its revision."""

    revision = verify_clinical_data_checkout()
    click.echo(
        json.dumps(
            {
                "repository": current_app.config["CLINICAL_DATA_REPOSITORY"],
                "cases_root": str(current_app.config["INPUT_ROOT"]),
                "revision": revision,
            },
            indent=2,
        )
    )


@click.command("export-index")
@with_appcontext
def export_index_command() -> None:
    """Rebuild the approved case index."""

    click.echo(str(rebuild_approved_index()))


@click.command("backup")
@click.option("--destination", type=click.Path(path_type=Path), required=True)
@with_appcontext
def backup_command(destination: Path) -> None:
    """Back up SQLite and portable review outputs."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination.mkdir(parents=True, exist_ok=True)
    database_uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not database_uri.startswith("sqlite:///"):
        raise click.ClickException("The built-in backup command supports SQLite only")
    database = Path(database_uri.removeprefix("sqlite:///"))
    if database.exists():
        shutil.copy2(database, destination / f"review-{stamp}.sqlite3")
    output = Path(current_app.config["OUTPUT_ROOT"])
    if output.exists():
        shutil.make_archive(str(destination / f"review-artifacts-{stamp}"), "zip", output)
    click.echo(f"Backup written to {destination}.")


@click.command("generate-secret")
def generate_secret_command() -> None:
    """Generate a suitable REVIEW_SECRET_KEY."""

    click.echo(secrets.token_urlsafe(48))


def register_cli(app) -> None:
    app.cli.add_command(init_db_command)
    app.cli.add_command(create_user_command)
    app.cli.add_command(set_password_command)
    app.cli.add_command(index_cases_command)
    app.cli.add_command(verify_case_source_command)
    app.cli.add_command(export_index_command)
    app.cli.add_command(backup_command)
    app.cli.add_command(generate_secret_command)

