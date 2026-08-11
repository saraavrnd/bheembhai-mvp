"""Declarative Base — all models inherit from this."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
