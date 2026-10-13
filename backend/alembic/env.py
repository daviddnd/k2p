"""Alembic environment.

Migration tooling is in place before the first schema lands (Phase 1 plan).
The database URL comes from application settings, never from alembic.ini, so
the same AICS_DATABASE_URL drives both the app and migrations.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.adapters.persistence import (
    attribution,  # noqa: F401  (the case persistence seam; keep it wired)
    audit,  # noqa: F401  (register tables)
    calendar_push,  # noqa: F401  (register tables)
    calendar_sync,  # noqa: F401  (register tables)
    change_events,  # noqa: F401  (register tables)
    commands,  # noqa: F401  (register tables)
    communications,  # noqa: F401  (register tables)
    company_master,  # noqa: F401  (register tables)
    confirmation,  # noqa: F401  (register tables)
    contacts,  # noqa: F401  (register tables)
    documents,  # noqa: F401  (register tables)
    drafts,  # noqa: F401  (register tables)
    entitlement,  # noqa: F401  (register tables)
    filing_packages,  # noqa: F401  (register tables)
    mail_ingestion,  # noqa: F401  (register tables)
    mail_outbox,  # noqa: F401  (register tables)
    models,  # noqa: F401  (register tables on the metadata)
    officers,  # noqa: F401  (register tables)
    packs,  # noqa: F401  (register tables)
    prior_filing,  # noqa: F401  (register tables)
    quarantine,  # noqa: F401  (register tables)
    reply_parsing,  # noqa: F401  (register tables)
    service_packages,  # noqa: F401  (register tables)
    service_renewals,  # noqa: F401  (register tables)
    tenant_config,  # noqa: F401  (register tables)
)
from app.adapters.persistence.base import Base
from app.core.config import settings

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
