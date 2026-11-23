"""Integrations: apps that plug inputs, outputs, tools and settings into the core.

See docs/rebuild/integrations.md. `base` holds the interface, `registry` the
in-tree registry and per-instance loading.

The names below load on first use (PEP 562), so a reader process that only
parses mail (`mailbox.message`) doesn't import the registry and every
integration with it.
"""

from importlib import import_module

_EXPORTS = {
    **dict.fromkeys(("Integration", "IntegrationConfig",
                     "IntegrationContext", "PromptFragment", "SettingsSection",
                     "ThreadTypeSpec", "TimerKind", "UIManifestEntry"), ".base"),
    **dict.fromkeys(("IntegrationError", "IntegrationRegistry", "LoadedIntegrations",
                     "default_registry", "enabled_names"), ".registry"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
