"""Recallatron's own storage package: DDL and thin row mapping, nothing else.

Every statement here runs on a ``Connection`` the core handed over (a
``UnitOfWork.connection``). Nothing in this package opens a connection, builds an
engine, or names another module's schema — ``tests/postgres/
test_module_storage_ownership.py``'s two AST scans measure both properties over this
source tree.
"""
