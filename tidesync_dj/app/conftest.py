"""Make the app modules importable as top-level (ma_client, scheduler, …).

The application code imports its siblings by bare name (e.g. `from ma_client
import …`), matching how it runs in the container with /app on the path. This
conftest lives in /app so pytest adds /app to sys.path, but we insert it
explicitly too so `pytest tests/` works from any working directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
