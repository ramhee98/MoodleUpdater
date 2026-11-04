import os
import sys

# Make the top-level `modules` package and `moodle_updater` importable from tests.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
