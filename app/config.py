"""Runtime configuration, read from environment variables (see .env.example)."""
import os
from pathlib import Path
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    llm_provider: str = "none"  # "anthropic" or "none" (deterministic question parser + code templates)
    llm_model: str = "claude-opus-5-5"
    sandbox: str = "subprocess"  # "subprocess" or "docker"
    docker_image: str = "pcda-sandbox:latest"
    timeout_s: float = 30.0
    memory_mb: int = 1024
    max_repairs: int = 2
    debug: bool = False

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> "Config":
        if env_file.exists():  # KEY=VALUE lines; real environment variables take precedence
            for line in env_file.read_text(encoding="utf-8").splitlines():
                k, sep, v = line.partition("=")
                if sep and not k.strip().startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip())
        provider = _env("PCDA_LLM_PROVIDER", "auto")
        if provider == "auto":
            provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "none"
        return cls(
            llm_provider=provider,
            llm_model=_env("PCDA_LLM_MODEL", cls.llm_model),
            sandbox=_env("PCDA_SANDBOX", cls.sandbox),
            docker_image=_env("PCDA_DOCKER_IMAGE", cls.docker_image),
            timeout_s=float(_env("PCDA_TIMEOUT_S", str(cls.timeout_s))),
            memory_mb=int(_env("PCDA_MEMORY_MB", str(cls.memory_mb))),
            max_repairs=int(_env("PCDA_MAX_REPAIRS", str(cls.max_repairs))),
            debug=_env("PCDA_DEBUG", "0") in ("1", "true", "yes"),
        )
