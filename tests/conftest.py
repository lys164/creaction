import sys
from pathlib import Path

# 让 `import app.*` 可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
