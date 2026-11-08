"""Allow python -m handler.runtime.documents as well as handler-read."""

from .cli import main

raise SystemExit(main())
