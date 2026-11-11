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


# ---------------------------------------------------------------------------
# Pipeline / gateway settings (added in the full build; names above unchanged).
# Every limit below is a CONFIGURED value for client-side pacing, not a claim
# about the account. Provenance is in docs/DECISIONS.md and results/limits.json.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GatewaySettings:
    # Observed in baseline_v2.csv headers (RPD, TPM). RPM/TPD are documented only.
    rpm: int = int(os.getenv("RELAY_RPM", "30"))            # documented (unobserved)
    tpm: int = int(os.getenv("RELAY_TPM", "8000"))          # observed header 8000
    rpd: int = int(os.getenv("RELAY_RPD", "1000"))          # observed header 1000
    tpd: int = int(os.getenv("RELAY_TPD", "200000"))        # documented (unobserved)
    safety_fraction: float = float(os.getenv("RELAY_SAFETY", "0.9"))  # pace at 90% of limits
    max_concurrency: int = int(os.getenv("RELAY_CONCURRENCY", "4"))
    max_queue: int = int(os.getenv("RELAY_MAX_QUEUE", "50"))
    deadline_s: float = float(os.getenv("RELAY_DEADLINE_S", "30"))
    request_timeout_s: float = float(os.getenv("RELAY_TIMEOUT_S", "20"))
    gen_max_tokens: int = int(os.getenv("RELAY_GEN_MAX_TOKENS", "900"))
    reasoning_effort: str = os.getenv("GROQ_REASONING_EFFORT", "")  # "" = not sent
    max_model_calls: int = 4          # generate, verify, regenerate, re-verify


gateway_settings = GatewaySettings()
