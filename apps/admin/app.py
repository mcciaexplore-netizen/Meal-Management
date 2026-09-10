import os
import sys
from pathlib import Path


APPLICATION = 'admin'
if os.environ.get("MEAL_APPLICATION", APPLICATION) != APPLICATION:
    raise RuntimeError("VERCEL_APPLICATION_MISMATCH")
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from meal_management.vercel_runtime import create_vercel_app


app = create_vercel_app(APPLICATION)
