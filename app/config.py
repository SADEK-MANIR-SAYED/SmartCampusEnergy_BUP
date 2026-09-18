"""Application configuration from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()

# Gemini API configuration
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT: int = int(os.environ.get("GEMINI_TIMEOUT", "25"))

# Server configuration
PORT: int = int(os.environ.get("PORT", "8000"))

# Optimization tolerance
TOLERANCE: float = 0.01  # kWh / BDT

# Logging
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
