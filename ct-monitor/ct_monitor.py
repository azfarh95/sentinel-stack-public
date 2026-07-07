#!/usr/bin/env python3
"""Certificate Transparency monitor (DEFI-012 / DNS hardening, 2026-06-21).

Polls crt.sh for the Sentinel domains and Telegram-alerts on NEW certs — a cheap
hijack/mis-issuance tripwire (someone issuing a cert for your domain shows up here).
First run per domain SEEDS the baseline (no alerts). State in ct_state.json next to
this file. Robust to crt.sh flakiness: retries + only seeds/updates a domain when a
fetch actually returns data (a failed fetch leaves that domain's state untouched).
No Cloudflare creds or paid plan needed.
"""
import json
import os
import re
import time
import pathlib
import urllib.request
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "ct_state.json"
ENV = pathlib.Path(r"C:\Users\azfar\metamcp-local\.env.local")
DOMAINS = ["sentinelsuite.xyz", "your-domain.example.com"]
UA = "sentinel-ct-monitor/1.0"
CHAT = os.environ.get("CT_ALERT_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
MAX_ALERTS = 15


def envval(key):
    try:
        t = ENV.read_text(encoding="utf-8", errors="ignore")
        m = re.search(rf'^\s*{key}\s*=\s*(.+?)\s*$', t, re.M)
        return m.group(1).strip().strip('"').strip("'") if m else None
    except Exception:
        return None


def fetch(url, tries=2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read().decode("utf-8", "ignore")
            if data.strip().startswith("["):
                return json.loads(data)
        except Exception:
            pass
        if i + 1 < tries:
            time.sleep(2)
    return None


def certs_for(domain):
    """Union of crt.sh entries for the apex + wildcard/subdomain query. {} if all failed."""
    out = {}
    for q in (domain, "%25." + domain):
        res = fetch(f"https://crt.sh/?q={q}&output=json")
        if res:
            for c in res:
                cid = str(c.get("id") or "")
                if cid:
                    out[cid] = {"issuer": c.get("issuer_name", ""),
                                "name": c.get("name_value", ""),
                                "cn": c.get("common_name", ""),
                                "not_before": c.get("not_before", "")}
    return out


def tg(text):
    tok = envval("TELEGRAM_BOT_TOKEN")
    if not tok:
        print("no TELEGRAM_BOT_TOKEN; alert skipped")
        return
    body = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body),
            timeout=20)
    except Exception as e:
        print("tg send failed:", type(e).__name__)


def main():
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}
    alerts = []
    for d in DOMAINS:
        found = certs_for(d)
        if not found:
            print(f"{d}: fetch failed/empty — skipped (state untouched)")
            continue
        seen = set(state.get(d, []))
        if not seen:
            state[d] = sorted(found.keys())
            print(f"{d}: seeded {len(found)} certs (baseline; no alert)")
            continue
        fresh = [cid for cid in found if cid not in seen]
        for cid in fresh:
            c = found[cid]
            alerts.append(f"🔐 NEW cert · {d}\n  CN: {c['cn'] or (c['name'][:60])}\n"
                          f"  issuer: {c['issuer'][:60]}\n  since: {c['not_before'][:19]}\n"
                          f"  https://crt.sh/?id={cid}")
        if fresh:
            state[d] = sorted(seen | set(found.keys()))
            print(f"{d}: {len(fresh)} NEW cert(s)")
        else:
            print(f"{d}: no new certs ({len(found)} known)")
    STATE.write_text(json.dumps(state, indent=2))
    if alerts:
        tg("⚠️ Certificate Transparency alert — confirm YOU (Cloudflare) issued these; "
           "an unexpected issuer = possible domain hijack:\n\n" + "\n\n".join(alerts[:MAX_ALERTS]))
        print(f"alerted on {len(alerts)} new cert(s)")
    else:
        print("no new certs across all domains")


if __name__ == "__main__":
    main()
