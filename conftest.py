import pathlib
import sys

_repo_root = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_repo_root))          # so `import src.engine` resolves
sys.path.insert(0, str(_repo_root / "src"))  # so `import product_copilot` resolves
