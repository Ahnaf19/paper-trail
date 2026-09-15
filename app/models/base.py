"""The declarative base — the schema's source of truth. Migrations are generated from it."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic names, so autogenerate is stable and later migrations can reference them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ID_LENGTH = 64  # fits "2401.12345" and "cond-mat/0211034" with room to spare
NAME_LENGTH = 255


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
