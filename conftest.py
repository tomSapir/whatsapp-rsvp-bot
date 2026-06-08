"""Pytest configuration.

A ``conftest.py`` at the repository root puts the root on ``sys.path`` (pytest's default
"prepend" import mode), so tests can ``import app...`` no matter which directory pytest is
invoked from. No fixtures live here yet — it exists purely to anchor the import root.
"""
