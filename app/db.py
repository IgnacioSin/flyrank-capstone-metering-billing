"""Engine and session factory."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL

# pool_pre_ping issues a cheap SELECT 1 before handing out a pooled
# connection. Without it, a connection the database dropped while idle comes
# back as an error on the next real query.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# expire_on_commit=False keeps attributes readable after commit. With the
# default, reading any field post-commit fires a refresh query — and raises
# DetachedInstanceError once the session is closed.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. One session per request, always closed."""
    with SessionLocal() as session:
        yield session