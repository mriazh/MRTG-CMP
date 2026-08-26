"""Application configuration loaded from environment variables."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources import DotEnvSettingsSource


class _PrecedenceDotEnvSource(DotEnvSettingsSource):
    def _read_env_files(self) -> Mapping[str, str | None]:
        if isinstance(self.env_file, (list, tuple)):
            orig = self.env_file
            self.env_file = list(reversed(orig))
            try:
                return super()._read_env_files()
            finally:
                self.env_file = orig
        return super()._read_env_files()


class Settings(BaseSettings):
    """Configuration for the collector and web application.

    Environment variables use the same names as the fields, in upper case.
    Relative database paths are intentionally resolved at use time so tests and
    deployments can choose their own working directory.
    """

    model_config = SettingsConfigDict(
        env_file=("config/.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_title: str = Field(
        default="MRTG Traffic Monitor",
        validation_alias=AliasChoices("app_title", "app_name"),
    )
    site_name: str = Field(
        default="Enterprise Gateway",
        validation_alias=AliasChoices("site_name", "site"),
    )
    location_name: str = Field(
        default="Branch Office",
        validation_alias=AliasChoices("location_name", "location"),
    )
    uplink_name: str = Field(
        default="Main Uplink (150 Mbps)",
        validation_alias=AliasChoices("uplink_name", "uplink"),
    )
    database_path: Path = Field(
        default=Path("data/traffic.db"),
        validation_alias=AliasChoices("database_path", "db_path"),
    )

    # Logging (FR-16.1). An empty LOG_FILE disables file output, leaving the
    # console handler to do the whole job.
    log_file: Path | None = Field(
        default=Path("logs/mrtg-cmp.log"),
        validation_alias=AliasChoices("log_file", "log_path"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("log_level", "loglevel"),
    )

    routeros_host: str = Field(
        default="192.168.88.1",
        validation_alias=AliasChoices("routeros_host", "router_host", "mikrotik_host"),
    )
    routeros_port: int = Field(
        default=8728,
        validation_alias=AliasChoices("routeros_port", "router_port", "mikrotik_port"),
    )
    routeros_username: str = Field(
        default="mrtg",
        validation_alias=AliasChoices("routeros_username", "router_username"),
    )
    routeros_password: str = Field(
        default="",
        validation_alias=AliasChoices("routeros_password", "router_password"),
    )
    routeros_interface: str = Field(
        default="WAN",
        validation_alias=AliasChoices("routeros_interface", "router_interface", "interface"),
    )
    routeros_timeout: int = Field(
        default=15,
        validation_alias=AliasChoices("routeros_timeout", "router_timeout"),
    )

    polling_interval: int = Field(
        default=60,
        validation_alias=AliasChoices("polling_interval", "poll_interval"),
    )
    polling_interval_down: int = Field(
        default=10,
        validation_alias=AliasChoices("polling_interval_down", "poll_interval_down"),
    )
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    secret_key: str | None = None
    session_cookie_secure: bool = False
    session_ttl_seconds: int = 28_800
    remember_me_ttl_seconds: int = 2_592_000

    admin_username: str = "admin"
    admin_password: str | None = None

    # tunnel.web.id watchdog & auto-healing
    tunnel_web_email: str | None = None
    tunnel_web_password: str | None = None
    tunnel_web_service_id: str | None = None
    tunnel_auto_restart: bool = False
    tunnel_restart_cooldown_minutes: int = 30

    # WhatsApp GOWA Alert Notification
    wa_alert_enabled: bool = False
    wa_gateway_url: str = "http://localhost:3000"
    wa_device_id: str | None = None
    wa_target_jid: str | None = None
    wa_fail_threshold: int = 2

    # TelkomCare Netcare scraper
    netcare_enabled: bool = True
    netcare_base_url: str = "https://telkomcare.telkom.co.id"
    netcare_poll_interval_seconds: int = 300
    netcare_cache_dir: Path = Path("data/netcare_cache")
    netcare_catalog_file: Path | None = None
    netcare_profile_dir: Path = Path("~/.mrtg-cmp-scraper-profile")
    netcare_headless: bool = True
    netcare_auto_login: bool = True
    netcare_page_timeout_seconds: int = 30
    netcare_graph_timeout_seconds: int = 25
    netcare_capture_retries: int = 3
    netcare_workers: int = Field(default=3, ge=1, le=8)

    # Telkomsel Orbit Modem Monitoring
    orbit_enabled: bool = True
    orbit_portal_url: str = "https://www.myorbit.id/informasi-modem-input"
    orbit_catalog_file: Path | None = None
    orbit_cache_dir: Path = Path("data/orbit_cache")
    orbit_sync_interval_seconds: int = 1800

    # Gemini Vision multi-key CAPTCHA solving (comma separated in the environment).
    # Keep in step with VALID_GEMINI_VISION_MODELS in netcare/captcha.py: a name
    # Google does not serve costs a request per CAPTCHA before it is retired. The
    # literal is repeated rather than imported so the root config stays free of
    # netcare imports; test_default_gemini_models_are_valid_vision_models is the
    # contract that keeps the two lists from drifting.
    # The two -flash-lite models lead because they hold the 500 RPD / 15 RPM
    # free-tier quota, which keeps routine logins off metered models.
    gemini_api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    gemini_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
    )
    #: Capped by the solver at MAX_TIMEOUT_SECONDS so a dead model fails fast.
    gemini_timeout_seconds: int = 6

    # TelkomCare portal credentials for zero-touch auto-login
    telkom_user: str = ""
    telkom_password: str = ""
    totp_secret: str = ""

    # Unified dashboard auto-refresh
    dashboard_refresh_seconds: int = 180

    @field_validator("gemini_api_keys", "gemini_models", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Accept comma separated environment values for list settings."""

        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_file", mode="before")
    @classmethod
    def _blank_log_file_disables_file_logging(cls, value: object) -> object:
        """Treat ``LOG_FILE=`` as 'console only' rather than as the current directory."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def poll_interval(self) -> int:
        """Backward-compatible short name used by collector code."""

        return self.polling_interval

    @property
    def poll_interval_down(self) -> int:
        """Backward-compatible short name for down polling interval."""

        return self.polling_interval_down

    @property
    def db_path(self) -> Path:
        """Backward-compatible short name for the database path."""

        return self.database_path

    @property
    def app_name(self) -> str:
        """Backward-compatible property returning app_title."""

        return self.app_title

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _PrecedenceDotEnvSource(
                settings_cls,
                env_file=(
                    dotenv_settings.env_file
                    if isinstance(dotenv_settings, DotEnvSettingsSource)
                    else None
                ),
                env_file_encoding=(
                    dotenv_settings.env_file_encoding
                    if isinstance(dotenv_settings, DotEnvSettingsSource)
                    else None
                ),
                case_sensitive=(
                    dotenv_settings.case_sensitive
                    if isinstance(dotenv_settings, DotEnvSettingsSource)
                    else None
                ),
            ),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings instance for the current process."""

    return Settings()


settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
