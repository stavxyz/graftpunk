"""Pytest configuration for BSC tests."""

import sys
from pathlib import Path

# Add src directory to sys.path for test imports
src_dir = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_dir))
