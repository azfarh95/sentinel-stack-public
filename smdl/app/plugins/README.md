# SMDL Plugins

Drop `*.py` files in this directory to extend the bot's URL detection,
add platform labels, register Cloudflare-protected hosts, or monkey-patch
yt-dlp extractors. Modules in this directory are auto-imported at startup.

## Contract

A plugin module is a standard Python module whose **top-level code**
registers extensions via four public functions:

```python
# Add URL detection regex (find_video_url returns ('myplatform', url))
from app.interceptor import register_pattern, register_rewrite

register_pattern(
    "myplatform",
    r"https?://(?:www\.)?myplatform\.com/[\w\-]+/?",
)

# Map a mirror domain to its canonical form before yt-dlp sees it
register_rewrite(
    matched_platform="mirror_site",
    canonical_platform="real_site",
    old_host="mirror.example.com",
    new_host="real.example.com",
)

# Declare hosts that need Chrome TLS impersonation (Cloudflare bot bypass)
from app.live_downloader import register_cloudflare_host, register_platform_label

register_cloudflare_host("myplatform.com")

# Friendly platform label for live recordings + log lines
register_platform_label("myplatform", "myplatform.com", "myplatform.io")
```

## Loading order

`interceptor.py` imports this package at module bottom — AFTER its
`register_*` functions are defined. So when your plugin runs
`from app.interceptor import register_pattern`, the function is callable.

## Failure handling

If a plugin raises during import (syntax error, missing dependency, etc.),
the auto-loader logs a warning and continues. A broken plugin won't take
the whole bot down.

## Module naming

- File names starting with `_` are skipped (use for shared helpers).
- Otherwise filename is irrelevant to plugin function — pick whatever's
  meaningful: `cam_sites.py`, `niche_extractors.py`, `local_test_servers.py`.

## Public vs Private

This directory exists to keep site-specific extensions out of the public
release. The public-side `.gitignore` excludes everything here except
`__init__.py` and `README.md`. Drop your own plugin files in; they live
in your private overlay or your local-only fork. See the project's
top-level OVERVIEW.md for the public/private architecture rationale.
