"""Every model is imported here, so `Base.metadata` is complete for Alembic autogenerate.

Importing a model module only where it is used would leave tables invisible to migrations —
a silent, and very confusing, way to lose a table.
"""

from app.models.base import Base
from app.models.embedding import EMBEDDING_DIMENSIONS, Embedding
from app.models.ingest_state import IngestState
from app.models.paper import Author, Category, Paper, PaperAuthor, PaperCategory

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "Author",
    "Base",
    "Category",
    "Embedding",
    "IngestState",
    "Paper",
    "PaperAuthor",
    "PaperCategory",
]
