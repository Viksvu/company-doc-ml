import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINES_DIR = PROJECT_ROOT / "pipelines"

sys.path.insert(0, str(PIPELINES_DIR))
