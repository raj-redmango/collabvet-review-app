"""Local-first clinician longitudinal review application."""

from __future__ import annotations

from flask import Flask, redirect, render_template, url_for
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from collabvet_review_app.cli import register_cli
from collabvet_review_app.config import ReviewConfig, validate_config
from collabvet_review_app.models import User, db
from collabvet_review_app.security import (
    apply_security_headers,
    validate_cloudflare_request,
)

login_manager = LoginManager()
csrf = CSRFProtect()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(ReviewConfig)
    if test_config:
        app.config.update(test_config)
    validate_config(app.config)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.jinja_env.cache.clear()

    app.config["INSTANCE_ROOT"].mkdir(parents=True, exist_ok=True)
    app.config["OUTPUT_ROOT"].mkdir(parents=True, exist_ok=True)
    if app.config.get("CF_ACCESS_REQUIRED"):
        app.config["SESSION_COOKIE_SECURE"] = True

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "review.login"
    login_manager.session_protection = "strong"

    from collabvet_review_app.routes import bp

    app.register_blueprint(bp)
    register_cli(app)
    app.before_request(validate_cloudflare_request)
    app.after_request(apply_security_headers)

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("error.html", status=403, message=str(error)), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", status=404, message="Not found"), 404

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/start")
    def start():
        return redirect(url_for("review.queue"))

    return app

