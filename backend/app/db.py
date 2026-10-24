from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def add_missing_columns(engine: Engine) -> None:
    """The only migration: add columns a newer model has to tables an older version created.

    Additive and nullable only, so existing rows stay valid. Anything else is a manual SQL step. Columns added
    this way carry no foreign-key constraint (SQLite can't add one later); fresh databases get it.
    """
    existing = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not existing.has_table(table.name):
                continue
            have = {c["name"] for c in existing.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable:
                    raise RuntimeError(f"Can't add NOT NULL column {table.name}.{col.name} to an existing table.")
                ddl = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


class Database:
    """Engine + session factory. One per app; tests build their own in-memory instance."""

    def __init__(self, url: str):
        self.engine = make_engine(url)
        self.sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from app import models  # noqa: F401  (register tables)

        Base.metadata.create_all(self.engine)
        add_missing_columns(self.engine)

    def session(self) -> Iterator[Session]:
        db = self.sessionmaker()
        try:
            yield db
        finally:
            db.close()
