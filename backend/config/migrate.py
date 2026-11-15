"""
Database migrations (Alembic), run at startup and by `manage.py`.

    upgrade()   bring the database to the latest revision
    drift()     differences between the models and the database, as text

Before this, startup called `create_all()` and then added any missing nullable
column by hand. That cannot rename a column, add a NOT NULL one, drop anything,
or record what was done, and it hid the fact that the schema had changed at all.

Three cases:

  * A NEW database                      -> every revision is applied in order.
  * A database ALREADY under Alembic    -> the revisions it is missing are applied.
  * A database from BEFORE migrations   -> ADOPTED once: it is brought to the
    current models the old way (create missing tables, add missing nullable
    columns), then stamped as being at the latest revision. From then on it is
    an ordinary Alembic database. Nothing is dropped and no data is rewritten.

After changing a model, write the migration:

    cd backend && alembic revision --autogenerate -m "what changed"

If you forget, startup says so loudly (`drift()`) instead of the app failing
later with "no such column".
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from .database import Base, engine

BACKEND = Path(__file__).resolve().parent.parent


def _config() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    # Run inside the app: keep uvicorn's logging, and use the app's own engine.
    cfg.attributes["configure_logger"] = False
    return cfg


def _load_models() -> None:
    import auth.models  # noqa: F401
    import auth.ratelimit  # noqa: F401
    import models  # noqa: F401


def _add_missing_columns() -> List[str]:
    """Adoption only: columns added to a model after a pre-migration database
    was created (all nullable, by the old rule)."""
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have and col.nullable:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN '
                                      f'{col.name} {col.type.compile(engine.dialect)}'))
                    added.append(f"{table.name}.{col.name}")
    return added


def upgrade() -> None:
    _load_models()
    cfg = _config()
    tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in tables and tables & set(Base.metadata.tables):
        Base.metadata.create_all(bind=engine)
        added = _add_missing_columns()
        for table in Base.metadata.sorted_tables:      # indexes on the added columns
            for index in table.indexes:
                index.create(bind=engine, checkfirst=True)
        command.stamp(cfg, "head")
        print(f"[DB] adopted a pre-migration database: {len(added)} column(s) added"
              + (f" ({', '.join(added)})" if added else "") + "; now tracked by Alembic.", flush=True)
        return
    command.upgrade(cfg, "head")


def drift() -> List[str]:
    """Tables and columns the models have and the database does not: the
    differences that break the app at runtime ("no such column"). Empty when
    every model change has a migration. Index and nullability differences are
    left out: an adopted database legitimately has some, and they break nothing.
    """
    _load_models()
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        out = []
        for d in compare_metadata(ctx, Base.metadata):
            d = d[0] if isinstance(d, list) else d
            if d[0] == "add_table":
                out.append(f"table {d[1].name}")
            elif d[0] == "add_column":
                out.append(f"column {d[2]}.{d[3].name}")
        return out
