import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()
logger = logging.getLogger(__name__)

BUNDLED_CA = Path(__file__).resolve().parents[2] / "certs" / "russian_trusted_root_ca.pem"
PLACEHOLDER_SECRETS = {"", "change-me", "replace-with-a-random-secret", "local-bot-token", "same-value-as-backend"}


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() == "production"


def _list(name: str) -> tuple[str, ...]:
    return tuple(item.strip().rstrip("/") for item in os.getenv(name, "").split(",") if item.strip())


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:admin@127.0.0.1:5432/focus_day").strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development").strip().lower()
    database_url: str = _database_url()
    secret_key: str = os.getenv("SECRET_KEY", "change-me")
    storage_path: Path = Path(os.getenv("STORAGE_PATH", "./storage"))
    max_file_size_mb: int = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
    google_client_id: str | None = os.getenv("GOOGLE_CLIENT_ID")
    google_client_secret: str | None = os.getenv("GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = os.getenv(
        "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
    )
    yandex_client_id: str | None = os.getenv("YANDEX_CLIENT_ID")
    yandex_client_secret: str | None = os.getenv("YANDEX_CLIENT_SECRET")
    yandex_redirect_uri: str | None = os.getenv("YANDEX_REDIRECT_URI")

    apple_client_id: str | None = os.getenv("APPLE_CLIENT_ID")
    apple_client_secret: str | None = os.getenv("APPLE_CLIENT_SECRET")
    apple_redirect_uri: str | None = os.getenv("APPLE_REDIRECT_URI")

    jira_client_id: str | None = os.getenv("JIRA_CLIENT_ID")
    jira_client_secret: str | None = os.getenv("JIRA_CLIENT_SECRET")
    jira_redirect_uri: str | None = os.getenv("JIRA_REDIRECT_URI")

    notion_client_id: str | None = os.getenv("NOTION_CLIENT_ID")
    notion_client_secret: str | None = os.getenv("NOTION_CLIENT_SECRET")
    notion_redirect_uri: str | None = os.getenv("NOTION_REDIRECT_URI")

    obsidian_client_id: str | None = os.getenv("OBSIDIAN_CLIENT_ID")
    obsidian_client_secret: str | None = os.getenv("OBSIDIAN_CLIENT_SECRET")
    obsidian_redirect_uri: str | None = os.getenv("OBSIDIAN_REDIRECT_URI")

    gigachat_credentials: str = os.getenv("SBER_AUTHORIZATION_KEY", "")
    gigachat_scope: str = os.getenv("SBER_SCOPE", "GIGACHAT_API_PERS")
    gigachat_model: str = os.getenv("GIGACHAT_MODEL", "GigaChat")
    # The chat assistant is an agent that calls the app's functions (app/services/agent.py);
    # off: the older rule-based routing in app/services/chat.py answers alone
    assistant_agent: bool = _bool("ASSISTANT_AGENT", True)
    gigachat_agent_model: str = os.getenv("GIGACHAT_AGENT_MODEL") or os.getenv("GIGACHAT_MODEL", "GigaChat")
    # Which service answers every model request: "gigachat" or "openai" (any OpenAI-compatible API at BASE_URL)
    llm_provider: str = os.getenv("LLM_PROVIDER", "gigachat").strip().lower()
    # Model tokens one user may spend; over it the assistant pauses for them until older requests leave the window
    # (quick commands like «Сегодня» keep working, they need no model). 0 turns a limit off.
    llm_user_tokens_per_hour: int = int(os.getenv("LLM_USER_TOKENS_PER_HOUR", "60000"))
    llm_user_tokens_per_day: int = int(os.getenv("LLM_USER_TOKENS_PER_DAY", "250000"))
    openai_api_key: str = os.getenv("API_KEY", "").strip()
    openai_base_url: str = os.getenv("BASE_URL", "").strip().rstrip("/")
    openai_model: str = os.getenv("OPENAI_MODEL", "").strip()
    openai_agent_model: str = (os.getenv("OPENAI_AGENT_MODEL") or os.getenv("OPENAI_MODEL", "")).strip()
    # Reasoning models (DeepSeek V4): "off" sends thinking disabled, "low"/"high" a reasoning effort with room for it;
    # empty sends neither, for APIs that do not know these parameters
    openai_thinking: str = os.getenv("OPENAI_THINKING", "").strip().lower()
    # Reads photos sent to the bot (a ticket, a screenshot of a chat); empty: OPENAI_MODEL
    openai_vision_model: str = (os.getenv("OPENAI_VISION_MODEL") or os.getenv("OPENAI_MODEL", "")).strip()
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "15"))
    jwt_refresh_expire_days: int = int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "30"))
    cookie_secure: bool = _bool("COOKIE_SECURE")
    integrations_encryption_key: str | None = os.getenv("INTEGRATIONS_ENCRYPTION_KEY")
    # AES-256 key for personal data in the database (app/core/dataenc.py)
    data_encryption_key: str | None = os.getenv("DATA_ENCRYPTION_KEY") or None
    data_encryption_old_keys: tuple[str, ...] = _list("DATA_ENCRYPTION_OLD_KEYS")
    bot_api_token: str | None = os.getenv("BOT_API_TOKEN")
    telegram_bot_username: str | None = os.getenv("TELEGRAM_BOT_USERNAME")
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")
    # Accounts made admins (dayla.tech/dashboard) on every start; removing one from the list does not revoke it
    admin_emails: tuple[str, ...] = _list("ADMIN_EMAILS")
    enable_docs: bool = _bool("ENABLE_DOCS", not _is_production())
    allow_private_integration_urls: bool = _bool("ALLOW_PRIVATE_INTEGRATION_URLS", not _is_production())
    # GigaChat uses the Russian Trusted Root CA (Минцифры); the certificate ships in backend/certs
    gigachat_ca_bundle: str = os.getenv("GIGACHAT_CA_BUNDLE") or (str(BUNDLED_CA) if BUNDLED_CA.is_file() else "")
    cors_origins: tuple[str, ...] = _list("CORS_ORIGINS")
    public_app_url: str = os.getenv("PUBLIC_APP_URL", "").strip().rstrip("/")
    # Mail for sign-up codes (Yandex: smtp.yandex.ru:465 over SSL, the password is an app password from id.yandex.ru).
    # With SMTP_USER and SMTP_PASSWORD set, a password sign-up needs the code sent to the email
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.yandex.ru").strip()
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "").strip()
    smtp_password: str = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from: str = (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "")).strip()

    @property
    def email_verification(self) -> bool:
        return bool(self.smtp_user and self.smtp_password)

    @property
    def uses_openai(self) -> bool:
        return self.llm_provider == "openai"

    @property
    def llm_enabled(self) -> bool:
        """Whether the selected model service is configured; without it the assistant answers by rules alone."""
        if self.uses_openai:
            return bool(self.openai_api_key and self.openai_base_url and self.openai_model)
        return bool(self.gigachat_credentials)

    @property
    def llm_model(self) -> str:
        return self.openai_model if self.uses_openai else self.gigachat_model

    @property
    def llm_agent_model(self) -> str:
        return self.openai_agent_model if self.uses_openai else self.gigachat_agent_model

    def validate(self) -> None:
        if self.llm_provider not in {"gigachat", "openai"}:
            raise RuntimeError(f"LLM_PROVIDER must be gigachat or openai, not {self.llm_provider!r}")
        if self.uses_openai and not self.llm_enabled:
            missing = [name for name, value in (("API_KEY", self.openai_api_key), ("BASE_URL", self.openai_base_url), ("OPENAI_MODEL", self.openai_model)) if not value]
            logger.warning("LLM_PROVIDER=openai but %s is not set; the assistant answers by rules only", ", ".join(missing))
        problems = []
        if self.secret_key in PLACEHOLDER_SECRETS or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be a random value of at least 32 characters")
        if self.bot_api_token and (self.bot_api_token in PLACEHOLDER_SECRETS or len(self.bot_api_token) < 24):
            problems.append("BOT_API_TOKEN must be a random value of at least 24 characters (or empty to disable the bot API)")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true behind HTTPS")
        if "*" in self.cors_origins:
            raise RuntimeError("CORS_ORIGINS must list explicit origins; '*' is not allowed")
        insecure = [origin for origin in self.cors_origins if not origin.startswith("https://")]
        if insecure:
            problems.append(f"CORS_ORIGINS must use https:// ({', '.join(insecure)})")
        if not problems:
            if not self.integrations_encryption_key:
                logger.warning("INTEGRATIONS_ENCRYPTION_KEY is not set; integration secrets are encrypted with a key derived from SECRET_KEY")
            return
        if self.app_env == "production":
            raise RuntimeError("Insecure production configuration: " + "; ".join(problems))
        for problem in problems:
            logger.warning("Insecure configuration (allowed because APP_ENV=%s): %s", self.app_env, problem)


settings = Settings()
