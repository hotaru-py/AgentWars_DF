"""Runtime configuration from environment variables (spec §15). Read once; pass Settings around."""

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///dispatch_dev.db"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL"))
    osrm_url: str = field(default_factory=lambda: _env("OSRM_URL", "http://127.0.0.1:5000"))
    osrm_time_factor: float = field(default_factory=lambda: float(_env("OSRM_TIME_FACTOR", "1.0")))
    plan_time_limit_s: float = field(default_factory=lambda: float(_env("PLAN_TIME_LIMIT_S", "1.0")))
    google_api_key: str = field(default_factory=lambda: _env("GOOGLE_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.6-flash"))
    typesafe_api_key: str = field(default_factory=lambda: _env("TYPESAFE_API_KEY"))   # optional
    jev_model: str = field(default_factory=lambda: _env("JEV_MODEL", "jev-latest"))
    deciders: str = field(default_factory=lambda: _env("DECIDERS"))                   # chain overrides
    deciders_shadow: str = field(default_factory=lambda: _env("DECIDERS_SHADOW"))
    llm_prob_method: str = field(default_factory=lambda: _env("LLM_PROB_METHOD", "auto"))
    messenger: str = field(default_factory=lambda: _env("MESSENGER", "sim"))
    signals: str = field(default_factory=lambda: _env("SIGNALS", "sim"))
    depot_tz: str = field(default_factory=lambda: _env("DEPOT_TZ", "Asia/Kolkata"))
    risk_confirm_threshold: float = field(default_factory=lambda: float(_env("RISK_CONFIRM_THRESHOLD", "0.1")))
    risk_holdout: float = field(default_factory=lambda: float(_env("RISK_HOLDOUT", "0")))   # exploration share
    max_attempts: int = field(default_factory=lambda: int(_env("MAX_ATTEMPTS", "3")))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.depot_tz)

    @property
    def jev_enabled(self) -> bool:
        return bool(self.typesafe_api_key)

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.google_api_key)

    def chain_overrides(self) -> dict[str, list[str]]:
        """DECIDERS="incident_triage=jev,llm,human;reply_intent=rules,llm,human" -> {type: [providers]}."""
        out: dict[str, list[str]] = {}
        for part in filter(None, (p.strip() for p in self.deciders.split(";"))):
            kind, _, chain = part.partition("=")
            if not chain:
                raise ValueError(f"DECIDERS entry {part!r} must look like type=provider,provider")
            out[kind.strip()] = [x.strip() for x in chain.split(",") if x.strip()]
        return out

    def shadow_providers(self) -> list[str]:
        return [x.strip() for x in self.deciders_shadow.split(",") if x.strip()]


ROOT_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")


def load(env_file: str | None = ROOT_ENV) -> Settings:
    """Settings from the environment; values in the repo-root .env fill in anything not already set
    (real environment variables always win). Pass env_file=None to skip the file."""
    if env_file and os.path.exists(env_file):
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)
    return Settings()
