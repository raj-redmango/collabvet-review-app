"""Database models for review state, revisions, and audit history."""

from __future__ import annotations

from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(160), nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    role = db.Column(db.String(32), nullable=False, default="reviewer")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    @property
    def is_active(self) -> bool:
        return self.active

    def set_password(self, password: str) -> None:
        if len(password) < 12:
            raise ValueError("Password must be at least 12 characters")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class CaseRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(160), unique=True, nullable=False, index=True)
    patient_folder = db.Column(db.String(255), nullable=False)
    source_path = db.Column(db.Text, nullable=False)
    source_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="pending", index=True)
    split = db.Column(db.String(32))
    is_holdout = db.Column(db.Boolean, nullable=False, default=False, index=True)
    visit_count = db.Column(db.Integer, nullable=False, default=0)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    current_revision = db.Column(db.Integer, nullable=False, default=0)
    indexed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    assigned_user = db.relationship("User", foreign_keys=[assigned_user_id])


class Revision(db.Model):
    __table_args__ = (UniqueConstraint("case_id", "source_hash", "revision_number"),)
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("case_record.id"), nullable=False, index=True)
    source_hash = db.Column(db.String(64), nullable=False, index=True)
    revision_number = db.Column(db.Integer, nullable=False)
    patch_json = db.Column(db.Text, nullable=False)
    reason = db.Column(db.Text, nullable=False, default="")
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    case = db.relationship("CaseRecord")
    author = db.relationship("User")


class SectionReview(db.Model):
    __table_args__ = (UniqueConstraint("case_id", "section_key"),)
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("case_record.id"), nullable=False, index=True)
    section_key = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="pending")
    comment = db.Column(db.Text, nullable=False, default="")
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    revision_number = db.Column(db.Integer, nullable=False, default=0)
    artifact_hash = db.Column(db.String(64))

    case = db.relationship("CaseRecord")
    reviewed_by = db.relationship("User")


class ReviewComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("case_record.id"), nullable=False, index=True)
    section_key = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    blocking = db.Column(db.Boolean, nullable=False, default=False)
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True))

    case = db.relationship("CaseRecord")
    author = db.relationship("User", foreign_keys=[author_id])


class ApprovedExport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("case_record.id"), nullable=False, index=True)
    source_hash = db.Column(db.String(64), nullable=False)
    approved_path = db.Column(db.Text, nullable=False)
    review_path = db.Column(db.Text, nullable=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    case = db.relationship("CaseRecord")
    approved_by = db.relationship("User")


class AuditEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(64), nullable=False, index=True)
    case_id = db.Column(db.Integer, db.ForeignKey("case_record.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    cf_identity = db.Column(db.String(320))
    remote_addr = db.Column(db.String(64))
    detail_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    case = db.relationship("CaseRecord")
    user = db.relationship("User")

