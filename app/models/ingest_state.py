"""Where the last ingest got to, per category set."""

from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IngestState(Base):
    """Incremental watermark, keyed by the category set that produced it (PRD §6.5)."""

    __tablename__ = "ingest_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    categories_key: Mapped[str] = mapped_column(Text, unique=True)  # sorted, "cs.AI|cs.LG"
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
