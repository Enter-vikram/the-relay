"""Central settings. Reads .env; never logs or prints the API key."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("GROQ_API_KEY", "")
    base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    results_dir: Path = ROOT / "results"

    def require_key(self) -> str:
        if not self.api_key or self.api_key.endswith("your_key_here"):
            raise SystemExit("GROQ_API_KEY missing. Copy .env.example to .env and set it.")
        return self.api_key


settings = Settings()
