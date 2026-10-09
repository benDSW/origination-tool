"""Domain layer: pure models, business rules and ports.

Nothing in this package performs I/O or imports from any other ``app`` package.
Every other layer depends on this one; it depends on nothing but stdlib, pydantic
and email-validator. Enforced by ``.importlinter``.
"""
