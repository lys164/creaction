import sys
from pathlib import Path

# 讓 `import app.*` 可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
