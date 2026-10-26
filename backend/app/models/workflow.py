"""Durable workflow configuration, execution snapshots and delivery ledger."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint

from app.database import Base


class ExportTemplate(Base):
    __tablename__ = "export_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="", nullable=False)
    draft_config = Column(JSON, nullable=False)
    published_config = Column(JSON, nullable=True)
    version = Column(Integer, default=0, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    kind = Column(String(30), nullable=False)
    config = Column(JSON, default=dict, nullable=False)
    encrypted_secret = Column(Text, nullable=True)
    enabled = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class WorkflowDefinition(Base):
    __tablename__ = "workflow_definitions"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    config = Column(JSON, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id = Column(Integer, primary_key=True)
    workflow_id = Column(Integer, ForeignKey("workflow_definitions.id"), nullable=False)
    name = Column(String(150), nullable=False)
    owner = Column(String(100), nullable=False, index=True)
    state = Column(String(30), default="draft", nullable=False, index=True)
    config_snapshot = Column(JSON, nullable=False)
    upload_manifest = Column(JSON, default=dict, nullable=False)
    uploaded_attachments = Column(JSON, default=dict, nullable=False)
    upload_errors = Column(JSON, default=dict, nullable=False)
    error = Column(Text, nullable=True)
    lease_owner = Column(String(100), nullable=True)
    lease_until = Column(DateTime, nullable=True, index=True)
    attempts = Column(JSON, default=dict, nullable=False)
    next_attempt_at = Column(DateTime, nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class WorkflowRunItem(Base):
    __tablename__ = "workflow_run_items"
    __table_args__ = (UniqueConstraint("run_id", "invoice_id"), UniqueConstraint("run_id", "client_file_id"))
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False)
    client_file_id = Column(String(100), nullable=True)
    state = Column(String(30), default="pending", nullable=False)
    data_snapshot = Column(JSON, nullable=True)
    source_hash = Column(String(64), nullable=True)
    verification = Column(JSON, nullable=True)
    corrections = Column(JSON, default=list, nullable=False)
    approval = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(JSON, default=dict, nullable=False)


class ExportArtifact(Base):
    __tablename__ = "workflow_artifacts"
    __table_args__ = (UniqueConstraint("run_id", "kind"),)
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    file_name = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)
    file_data = Column(LargeBinary, nullable=False)
    sha256 = Column(String(64), nullable=False)
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Delivery(Base):
    __tablename__ = "workflow_deliveries"
    __table_args__ = (UniqueConstraint("run_id", "channel_id", "target", "part"),)
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("workflow_runs.id"), nullable=False, index=True)
    channel_id = Column(Integer, ForeignKey("notification_channels.id"), nullable=False)
    target = Column(String(255), nullable=False)
    part = Column(String(100), nullable=False)
    state = Column(String(30), default="pending", nullable=False)
    attempts = Column(JSON, default=list, nullable=False)
    receipt = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
