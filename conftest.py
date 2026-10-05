import sys
from pathlib import Path

# Repo root must win over the stdlib `profile` module (profile.py here
# shadows it intentionally) regardless of how pytest's rootdir insertion
# behaves, so pin it at the front explicitly.
sys.path.insert(0, str(Path(__file__).parent))
