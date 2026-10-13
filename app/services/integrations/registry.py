from app.services.integrations.apple import AppleCalendarIntegration
from app.services.integrations.base import IntegrationProvider
from app.services.integrations.google import GoogleIntegration
from app.services.integrations.yandex import YandexCalendarIntegration

# Jira and Notion were taken out (2026-10-13): their sign-in failed for most accounts. Their modules stay for
# the events imported earlier, but they can no longer be connected or used to sign in.
PROVIDERS: dict[str, type[IntegrationProvider]] = {
    provider.slug: provider
    for provider in (GoogleIntegration, YandexCalendarIntegration, AppleCalendarIntegration)
}
