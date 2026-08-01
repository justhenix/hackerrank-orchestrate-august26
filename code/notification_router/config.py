"""Environment-only configuration for bounded model integration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class IntegrationConfigError(ValueError):
    """Raised when integration configuration is missing or unsafe."""


GEMINI_BACKEND_VALUES = frozenset({"vertex", "ai-studio"})
GEMINI_VERTEX_AUTH_VALUES = frozenset({"adc", "service-account"})


def _env_value(environ: Mapping[str, str], name: str, default: str) -> str:
    value = environ.get(name, default)
    if value == "":
        raise IntegrationConfigError(f"{name} must not be empty")
    return value


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise IntegrationConfigError(f"{name} must be a boolean")


def _parse_int(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise IntegrationConfigError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise IntegrationConfigError(f"{name} is outside its allowed range")
    return parsed


def _parse_float(value: str, *, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise IntegrationConfigError(f"{name} must be a number") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise IntegrationConfigError(f"{name} must be finite")
    if not minimum <= parsed <= maximum:
        raise IntegrationConfigError(f"{name} is outside its allowed range")
    return parsed


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Provider-neutral operational limits with API calls off by default."""

    provider_name: str = "fake"
    extraction_model: str = "fake-extraction-v0"
    routing_model: str = "fake-routing-v0"
    api_enabled: bool = False
    credential_env_var: str = "NOTIFICATION_ROUTER_API_KEY"
    extraction_url: str | None = None
    routing_url: str | None = None
    gemini_backend: str | None = None
    gemini_api_key_env_var: str = "NOTIFICATION_ROUTER_GEMINI_API_KEY"
    gemini_vertex_project: str | None = None
    gemini_vertex_location: str | None = None
    gemini_vertex_auth: str = "adc"
    gemini_vertex_credentials_file: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 1
    retry_backoff_seconds: float = 0.0
    concurrency: int = 1
    cost_limit_usd: float = 0.60
    per_call_cost_limit_usd: float = 0.05
    input_cost_per_1k_tokens: float = 0.0
    output_cost_per_1k_tokens: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "provider_name",
            "extraction_model",
            "routing_model",
            "credential_env_var",
            "gemini_api_key_env_var",
            "gemini_vertex_auth",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise IntegrationConfigError(f"{name} must be nonempty")
        if self.gemini_backend is not None and self.gemini_backend not in GEMINI_BACKEND_VALUES:
            raise IntegrationConfigError(
                "gemini_backend must be one of: " + ", ".join(sorted(GEMINI_BACKEND_VALUES))
            )
        if self.gemini_vertex_auth not in GEMINI_VERTEX_AUTH_VALUES:
            raise IntegrationConfigError(
                "gemini_vertex_auth must be one of: "
                + ", ".join(sorted(GEMINI_VERTEX_AUTH_VALUES))
            )
        for name in (
            "gemini_vertex_project",
            "gemini_vertex_location",
            "gemini_vertex_credentials_file",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise IntegrationConfigError(f"{name} must be nonempty or null")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise IntegrationConfigError("timeout_seconds must be in (0, 300]")
        if not 0 <= self.max_retries <= 5:
            raise IntegrationConfigError("max_retries must be between 0 and 5")
        if not 0 <= self.retry_backoff_seconds <= 30:
            raise IntegrationConfigError("retry_backoff_seconds must be between 0 and 30")
        if not 1 <= self.concurrency <= 16:
            raise IntegrationConfigError("concurrency must be between 1 and 16")
        for name in (
            "cost_limit_usd",
            "per_call_cost_limit_usd",
            "input_cost_per_1k_tokens",
            "output_cost_per_1k_tokens",
        ):
            value = getattr(self, name)
            if value < 0 or value != value or value in {float("inf"), float("-inf")}:
                raise IntegrationConfigError(f"{name} must be a finite non-negative number")
        if self.per_call_cost_limit_usd > self.cost_limit_usd:
            raise IntegrationConfigError("per_call_cost_limit_usd cannot exceed cost_limit_usd")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "IntegrationConfig":
        env = os.environ if environ is None else environ
        api_enabled = _parse_bool(
            env.get("NOTIFICATION_ROUTER_API_ENABLED", "0"),
            name="NOTIFICATION_ROUTER_API_ENABLED",
        )
        return cls(
            provider_name=_env_value(env, "NOTIFICATION_ROUTER_PROVIDER", "fake"),
            extraction_model=_env_value(
                env, "NOTIFICATION_ROUTER_EXTRACTION_MODEL", "fake-extraction-v0"
            ),
            routing_model=_env_value(
                env, "NOTIFICATION_ROUTER_ROUTING_MODEL", "fake-routing-v0"
            ),
            api_enabled=api_enabled,
            credential_env_var=_env_value(
                env, "NOTIFICATION_ROUTER_CREDENTIAL_ENV", "NOTIFICATION_ROUTER_API_KEY"
            ),
            extraction_url=env.get("NOTIFICATION_ROUTER_EXTRACTION_URL") or None,
            routing_url=env.get("NOTIFICATION_ROUTER_ROUTING_URL") or None,
            gemini_backend=env.get("NOTIFICATION_ROUTER_GEMINI_BACKEND") or None,
            gemini_api_key_env_var=_env_value(
                env,
                "NOTIFICATION_ROUTER_GEMINI_API_KEY_ENV",
                "NOTIFICATION_ROUTER_GEMINI_API_KEY",
            ),
            gemini_vertex_project=(
                env.get("NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT")
                or env.get("GOOGLE_CLOUD_PROJECT")
                or None
            ),
            gemini_vertex_location=(
                env.get("NOTIFICATION_ROUTER_GEMINI_VERTEX_LOCATION")
                or env.get("GOOGLE_CLOUD_LOCATION")
                or None
            ),
            gemini_vertex_auth=_env_value(
                env, "NOTIFICATION_ROUTER_GEMINI_VERTEX_AUTH", "adc"
            ),
            gemini_vertex_credentials_file=(
                env.get("NOTIFICATION_ROUTER_GEMINI_VERTEX_CREDENTIALS_FILE")
                or env.get("GOOGLE_APPLICATION_CREDENTIALS")
                or None
            ),
            timeout_seconds=_parse_float(
                env.get("NOTIFICATION_ROUTER_TIMEOUT_SECONDS", "30"),
                name="NOTIFICATION_ROUTER_TIMEOUT_SECONDS",
                minimum=0.001,
                maximum=300,
            ),
            max_retries=_parse_int(
                env.get("NOTIFICATION_ROUTER_MAX_RETRIES", "1"),
                name="NOTIFICATION_ROUTER_MAX_RETRIES",
                minimum=0,
                maximum=5,
            ),
            retry_backoff_seconds=_parse_float(
                env.get("NOTIFICATION_ROUTER_RETRY_BACKOFF_SECONDS", "0"),
                name="NOTIFICATION_ROUTER_RETRY_BACKOFF_SECONDS",
                minimum=0,
                maximum=30,
            ),
            concurrency=_parse_int(
                env.get("NOTIFICATION_ROUTER_CONCURRENCY", "1"),
                name="NOTIFICATION_ROUTER_CONCURRENCY",
                minimum=1,
                maximum=16,
            ),
            cost_limit_usd=_parse_float(
                env.get("NOTIFICATION_ROUTER_COST_LIMIT_USD", "0.60"),
                name="NOTIFICATION_ROUTER_COST_LIMIT_USD",
                minimum=0,
                maximum=10000,
            ),
            per_call_cost_limit_usd=_parse_float(
                env.get("NOTIFICATION_ROUTER_PER_CALL_COST_LIMIT_USD", "0.05"),
                name="NOTIFICATION_ROUTER_PER_CALL_COST_LIMIT_USD",
                minimum=0,
                maximum=10000,
            ),
            input_cost_per_1k_tokens=_parse_float(
                env.get("NOTIFICATION_ROUTER_INPUT_COST_PER_1K", "0"),
                name="NOTIFICATION_ROUTER_INPUT_COST_PER_1K",
                minimum=0,
                maximum=10000,
            ),
            output_cost_per_1k_tokens=_parse_float(
                env.get("NOTIFICATION_ROUTER_OUTPUT_COST_PER_1K", "0"),
                name="NOTIFICATION_ROUTER_OUTPUT_COST_PER_1K",
                minimum=0,
                maximum=10000,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return manifest-safe configuration without secret values."""

        return {
            "provider_name": self.provider_name,
            "extraction_model": self.extraction_model,
            "routing_model": self.routing_model,
            "api_enabled": self.api_enabled,
            "credential_env_var": self.credential_env_var,
            "extraction_endpoint_configured": self.extraction_url is not None,
            "routing_endpoint_configured": self.routing_url is not None,
            "gemini_backend": self.gemini_backend,
            "gemini_api_key_env_var": self.gemini_api_key_env_var,
            "gemini_vertex_project": self.gemini_vertex_project,
            "gemini_vertex_location": self.gemini_vertex_location,
            "gemini_vertex_auth": self.gemini_vertex_auth,
            "gemini_vertex_credentials_file_configured": (
                self.gemini_vertex_credentials_file is not None
            ),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "concurrency": self.concurrency,
            "cost_limit_usd": self.cost_limit_usd,
            "per_call_cost_limit_usd": self.per_call_cost_limit_usd,
            "input_cost_per_1k_tokens": self.input_cost_per_1k_tokens,
            "output_cost_per_1k_tokens": self.output_cost_per_1k_tokens,
        }

    @property
    def maximum_attempts(self) -> int:
        return self.max_retries + 1

    def maximum_smoke_cost(self, *, logical_calls: int = 6) -> float:
        """Upper bound for three modality samples, including configured retries."""

        configured_maximum = logical_calls * self.maximum_attempts * self.per_call_cost_limit_usd
        return min(self.cost_limit_usd, configured_maximum)

    def credential(self, environ: Mapping[str, str] | None = None) -> str:
        if not self.api_enabled:
            raise IntegrationConfigError("API calls are disabled")
        env = os.environ if environ is None else environ
        value = env.get(self.credential_env_var)
        if not value:
            raise IntegrationConfigError(
                f"credential environment variable {self.credential_env_var} is missing"
            )
        return value
