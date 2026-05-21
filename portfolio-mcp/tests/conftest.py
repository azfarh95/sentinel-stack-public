"""Shared test fixtures + sys.path setup so tests can import app.*"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
