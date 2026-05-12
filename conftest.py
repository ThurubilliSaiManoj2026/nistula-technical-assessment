# conftest.py
# ─────────────────────────────────────────────────────────────────────────────
# Pytest configuration file at the project root.
# This file tells pytest to add the project root to sys.path, which allows
# 'from src.main import app' style imports to resolve correctly during testing.
# Without this, pytest cannot find the src package.
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os

# Add the project root directory to Python's module search path.
sys.path.insert(0, os.path.dirname(__file__))