"""Runtime configuration, read from environment variables (see .env.example)."""
import os
from pathlib import Path
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    llm_provider: str = "none"  # "anthropic", "gemini", "local" (Ollama, reads questions only) or "none" (parser only)
    llm_model: str = "claude-opus-5-5"
    gemini_model: str = "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.5-flash"  # tried in order; lite answers in ~2 s
    local_model: str = "pcda-interpreter"
    ollama_url: str = "http://127.0.0.1:11434"
    sandbox: str = "subprocess"  # "subprocess" or "docker"
    docker_image: str = "pcda-sandbox:latest"
    timeout_s: float = 30.0
    memory_mb: int = 1024
    max_repairs: int = 2
    llm_timeout_s: float = 3.0  # an AI answer slower than this is abandoned for the parser / template
    debug: bool = False

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> "Config":
        if env_file.exists():  # KEY=VALUE lines; real environment variables take precedence
            for line in env_file.read_text(encoding="utf-8").splitlines():
                k, sep, v = line.partition("=")
                if sep and not k.strip().startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip())
        provider = _env("PCDA_LLM_PROVIDER", "auto")
        local_model = _env("PCDA_LOCAL_MODEL", cls.local_model)
        ollama_url = _env("PCDA_OLLAMA_URL", cls.ollama_url)
        if provider == "auto":  # Claude key -> Claude; Gemini key -> Gemini; local model -> local; else the parser
            if os.environ.get("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif os.environ.get("GEMINI_API_KEY"):
                provider = "gemini"
            else:
                from .local_model import ollama_status
                provider = "local" if ollama_status(local_model, ollama_url)["model_installed"] else "none"
        return cls(
            llm_provider=provider,
            llm_model=_env("PCDA_LLM_MODEL", cls.llm_model),
            gemini_model=_env("PCDA_GEMINI_MODEL", cls.gemini_model),
            local_model=local_model,
            ollama_url=ollama_url,
            sandbox=_env("PCDA_SANDBOX", cls.sandbox),
            docker_image=_env("PCDA_DOCKER_IMAGE", cls.docker_image),
            timeout_s=float(_env("PCDA_TIMEOUT_S", str(cls.timeout_s))),
            memory_mb=int(_env("PCDA_MEMORY_MB", str(cls.memory_mb))),
            max_repairs=int(_env("PCDA_MAX_REPAIRS", str(cls.max_repairs))),
            llm_timeout_s=float(_env("PCDA_LLM_TIMEOUT_S", str(cls.llm_timeout_s))),
            debug=_env("PCDA_DEBUG", "0") in ("1", "true", "yes"),
        )
