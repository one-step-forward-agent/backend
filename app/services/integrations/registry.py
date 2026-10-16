from app.services.integrations.apple import AppleCalendarIntegration
from app.services.integrations.base import IntegrationProvider
from app.services.integrations.google import GoogleIntegration
from app.services.integrations.jira import JiraIntegration
from app.services.integrations.notion import NotionIntegration
from app.services.integrations.yandex import YandexCalendarIntegration

# Jira and Notion can be connected from the Integrations page; signing in with them stays off (2026-10-13:
# their sign-in failed for most accounts).
PROVIDERS: dict[str, type[IntegrationProvider]] = {
    provider.slug: provider
    for provider in (GoogleIntegration, YandexCalendarIntegration, AppleCalendarIntegration, JiraIntegration, NotionIntegration)
}
