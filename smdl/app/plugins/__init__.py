"""SMDL plugin auto-loader.

Each *.py file in this directory is imported at SMDL startup. The import is
the registration mechanism — plugins call register_pattern() /
register_rewrite() / register_cloudflare_host() / register_platform_label()
at module top-level, and the interceptor + live_downloader pick them up.

Public release ships this directory empty (only __init__.py and README.md).
Drop your own plugin .py files in to extend URL detection or add site-
specific extractor patches. See README.md for the contract.

Failures during plugin import are logged but don't crash the core — a
broken plugin won't take the whole bot down with it.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

logger = logging.getLogger(__name__)


def _load_all() -> None:
    for mod_info in pkgutil.iter_modules(__path__):
        # Skip anything starting with underscore (convention for private helpers)
        if mod_info.name.startswith("_"):
            continue
        full_name = f"{__name__}.{mod_info.name}"
        try:
            importlib.import_module(full_name)
            logger.info("plugin loaded: %s", mod_info.name)
        except Exception as e:
            logger.warning("plugin failed: %s — %s", mod_info.name, e)


_load_all()
