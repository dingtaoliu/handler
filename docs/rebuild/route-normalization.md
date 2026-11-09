# Crontal route description normalization

Item 6 resolves description paths against the existing per-instance cached route
catalog before fetching a contract. Brace templates, Flask converter templates,
and concrete URLs with standard converters now share the registered path and
contract-cache entry. Query strings are stripped; harmless trailing-slash aliases
are accepted when unambiguous. HTTP methods and parameter names must match the
published route. Exact static routes win over parameter routes. Ambiguous matches
return a useful error rather than guessing.

Standard concrete converters: int, float, string/default, path and UUID. Custom
converters remain authoritative in Crontal; their concrete paths pass through.
If discovery is unavailable, concrete/exact URLs can still use the existing backend
resolver; template aliases require discovery. Hidden endpoints remain hidden.

This only normalizes contract lookup (`crontal_api_describe` and automatic schema
help on validation errors). It never rewrites an executing read/write URL,
substitutes resource IDs, broadens policy or changes approval requirements.
No Crontal backend change or migration is required.

Tests cover multi-parameter contact-list paths, files, template/concrete cache
identity, methods, numeric mismatch, static specificity, hidden endpoints and
ambiguity. Focused Crontal suite: 55 passed.

Released v57, runtime `4668eab`, image `626e2f1c4455`, backup b035, rollback v56.
Full suite 1,247 passed/2 skipped; module Pyright clean and all three instance
health/auth checks passed.
