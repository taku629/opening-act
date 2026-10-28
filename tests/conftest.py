import os
import sys
from pathlib import Path

# Force demo mode and no LLM before the app is imported.
os.environ.pop("QLOO_API_KEY", None)
os.environ["QLOO_MOCK"] = "1"
os.environ["LLM_DISABLED"] = "1"
os.environ["CACHE_PATH"] = "/tmp/opening_act_pytest.sqlite"
os.environ["RATE_LIMIT_PER_HOUR"] = "1000"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    os.remove("/tmp/opening_act_pytest.sqlite")
except FileNotFoundError:
    pass
