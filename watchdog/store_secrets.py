"""
One-time setup: store Sentinel secrets in Windows Credential Manager.
Run once per secret. After storing, the corresponding config.json value
can be nulled out — the service will prefer Credential Manager.

Usage:
    py store_secrets.py <key_name> <value>

Examples:
    py store_secrets.py github_pat          ghp_xxxx
    py store_secrets.py bot_token           7552...:AAF_xxxx
    py store_secrets.py telegram_bot_token  7552...:AAF_xxxx
    py store_secrets.py mini_app_secret     d0412d...
    py store_secrets.py totp_secret         BGET6X...
    py store_secrets.py lm_api_key          sk-lm-xxxx
"""

import sys
import keyring

# Maps key_name → (service, description)
SECRETS = {
    # sentinel-watchdog
    "bot_token":          ("sentinel-watchdog", "Watchdog Telegram bot token"),
    "lm_api_key":         ("sentinel-watchdog", "LM Studio API key"),
    "github_pat":         ("sentinel-watchdog", "GitHub Personal Access Token"),
    # sentinel-miniapp
    "telegram_bot_token": ("sentinel-miniapp",  "Mini App Telegram bot token"),
    "mini_app_secret":    ("sentinel-miniapp",  "Mini App shared secret (X-Sentinel-Token)"),
    "totp_secret":        ("sentinel-miniapp",  "TOTP 2FA seed"),
}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in SECRETS:
        print("Usage: py store_secrets.py <key_name> <value>\n")
        print("Known keys:")
        for k, (svc, desc) in SECRETS.items():
            stored = "x" if keyring.get_password(svc, k) else " "
            print(f"  [{stored}] {k:<22}  {desc}  (service: {svc})")
        sys.exit(1)

    key_name = sys.argv[1]
    value    = " ".join(sys.argv[2:]).strip()

    if not value:
        print("ERROR: empty value.")
        sys.exit(1)

    service, desc = SECRETS[key_name]
    keyring.set_password(service, key_name, value)
    print(f"Stored: {desc}")
    print(f"  Service  : {service}")
    print(f"  Key      : {key_name}")
    print(f"  Length   : {len(value)} chars")
    print(f"\nYou can now null out '{key_name}' in the relevant config.json.")


if __name__ == "__main__":
    main()

