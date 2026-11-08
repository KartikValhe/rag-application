"""
app/db/models/base.py
─────────────────────
Declarative base shared by all SQLAlchemy models.
All application tables live in the `app` schema (not `public`).
"""

from typing import Any

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Every model that inherits from this base gets:
      - The `app` schema by default (can be overridden per model).
      - SQLAlchemy 2.0 mapped column syntax.
    """
    __table_args__: Any = {"schema": "app"}

