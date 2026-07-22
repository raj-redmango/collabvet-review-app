"""Run the clinician review app locally or on an explicitly enabled LAN bind."""

from __future__ import annotations

from waitress import serve

from collabvet_review_app import create_app


def main() -> None:
    app = create_app()
    host = app.config["BIND_HOST"]
    if host not in {"127.0.0.1", "::1", "localhost"} and not app.config[
        "ALLOW_LAN_BIND"
    ]:
        raise SystemExit(
            "Non-loopback REVIEW_BIND_HOST requires REVIEW_ALLOW_LAN_BIND=true"
        )
    serve(app, host=host, port=app.config["PORT"], threads=4, url_scheme="http")


if __name__ == "__main__":
    main()

