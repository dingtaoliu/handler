"""Integrations: apps that plug inputs, outputs, tools and settings into the core.

See docs/rebuild/integrations.md. `base` holds the interface, `registry` the
in-tree registry and per-instance loading.
"""

from .base import (
    BackgroundTask,
    Integration,
    IntegrationConfig,
    IntegrationContext,
    PromptFragment,
    SettingsSection,
    ThreadTypeSpec,
    UIManifestEntry,
)
from .registry import (
    IntegrationError,
    IntegrationRegistry,
    LoadedIntegrations,
    default_registry,
    enabled_names,
)

__all__ = [
    "BackgroundTask", "Integration", "IntegrationConfig", "IntegrationContext",
    "IntegrationError", "IntegrationRegistry", "LoadedIntegrations", "PromptFragment",
    "SettingsSection", "ThreadTypeSpec", "UIManifestEntry", "default_registry",
    "enabled_names",
]
