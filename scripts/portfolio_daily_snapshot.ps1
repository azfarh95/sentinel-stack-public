# portfolio_daily_snapshot.ps1
# Take a daily portfolio snapshot via the portfolio-mcp container, then notify
# via Telegram with net-worth change vs previous snapshot.
#
# Triggered by Windows Scheduled Task "Portfolio Daily Snapshot" (07:00 daily).

$ErrorActionPreference = "Stop"
$LogFile = "$env:LOCALAPPDATA\portfolio_daily.log"

function Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Log "=== Run started ==="

# 1) Verify container is healthy
$health = docker inspect portfolio-mcp --format "{{.State.Health.Status}}" 2>$null
if ($health -ne "healthy") {
    Log "portfolio-mcp not healthy ($health) - abort"
    exit 1
}

# 2) Run snapshot + diff inside the container, capture JSON
$pyScript = @'
import asyncio, json
from app import database as db
from app import moralis
from datetime import timedelta

async def main():
    db.init_db()
    snap = await moralis.wallet_snapshot("0xYOUR_WALLET_ADDRESS", dust_threshold_usd=0.01)
    s = db.SessionLocal()
    try:
        hidden = {(r.chain, (r.token_address or "").lower()) for r in s.query(db.HiddenToken).all()}
        snap["positions"] = [p for p in snap["positions"] if (p["chain"], (p["token_address"] or "").lower()) not in hidden]
        liquid = sum(p["usd_value"] for p in snap["positions"])
        manual_rows = s.query(db.ManualPosition).all()
        manual_usd = sum(r.usd_value for r in manual_rows)
        total = liquid + manual_usd

        # Persist
        row = db.Snapshot(address=snap["address"], captured_at=db.now_utc(),
                          total_usd=total, chain_count=snap["chain_count"],
                          token_count=len(snap["positions"]))
        s.add(row); s.flush()
        for p in snap["positions"]:
            s.add(db.Position(snapshot_id=row.id, chain=p["chain"],
                              token_address=p["token_address"], symbol=p["symbol"],
                              decimals=p["decimals"], raw_balance=p["raw_balance"],
                              usd_price=p["usd_price"], usd_value=p["usd_value"]))
        s.commit()
        new_id = row.id

        # Compare to ~24h ago
        cutoff = db.now_utc() - timedelta(days=1)
        prev = (s.query(db.Snapshot)
                  .filter(db.Snapshot.address == snap["address"],
                          db.Snapshot.captured_at <= cutoff)
                  .order_by(db.Snapshot.captured_at.desc()).first())
        delta = (total - prev.total_usd) if prev else None
        prev_total = prev.total_usd if prev else None
    finally:
        s.close()

    top = sorted(snap["positions"], key=lambda x: -x["usd_value"])[:5]
    out = {
        "snapshot_id": new_id,
        "captured_at": str(db.now_utc()),
        "liquid_usd": round(liquid, 2),
        "manual_usd": round(manual_usd, 2),
        "total_usd": round(total, 2),
        "prev_total_usd": prev_total,
        "delta_usd": (round(delta, 2) if delta is not None else None),
        "top_5_liquid": [{"chain": p["chain"], "symbol": p["symbol"], "usd": round(p["usd_value"], 2)} for p in top],
    }
    print(json.dumps(out))

asyncio.run(main())
'@

$json = $pyScript | docker exec -i portfolio-mcp python 2>&1 | Select-Object -Last 1
$data = $null
try { $data = $json | ConvertFrom-Json } catch {
    Log "Failed to parse snapshot JSON: $json"
    exit 1
}
Log "snapshot id=$($data.snapshot_id) total=`$$($data.total_usd) delta=`$$($data.delta_usd)"

# 3) Telegram report - always send (this is opt-in tracking, not noisy)
$delta = $data.delta_usd
$arrow = if ($null -eq $delta) { "first run" } elseif ($delta -gt 0) { "[+]" } elseif ($delta -lt 0) { "[-]" } else { "flat" }
$deltaStr = if ($null -eq $delta) { "(no prior snapshot)" } else { ("{0,+10:N2}" -f $delta) }
$top = ($data.top_5_liquid | ForEach-Object { "- $($_.chain): $($_.symbol)  `$$($_.usd)" }) -join "`n"
$body = @"
NET WORTH:  USD $($data.total_usd)
24h delta:  $deltaStr  $arrow

Liquid:  `$$($data.liquid_usd)
Manual:  `$$($data.manual_usd)  (staking, LPs)

Top 5 liquid positions:
$top
"@

# --- Phase 3: mirror to Firefly III ---
$pat = $null
$patPath = "$env:TEMP\firefly_pat.txt"
if (Test-Path $patPath) { $pat = (Get-Content $patPath -Raw -Encoding UTF8).Trim() }
if ($pat) {
    try {
        $h = @{Authorization="Bearer $pat"; Accept="application/json"; "Content-Type"="application/json"}
        $acct = Invoke-RestMethod -Uri "http://127.0.0.1:8180/api/v1/accounts/95" -Headers $h -TimeoutSec 10
        $current = [decimal]$acct.data.attributes.current_balance
        $target = [decimal]$data.total_usd
        $diff = $target - $current
        if ([math]::Abs($diff) -ge 0.01) {
            $abs = [math]::Round([math]::Abs($diff), 2)
            $type = if ($diff -gt 0) { "deposit" } else { "withdrawal" }
            $today = (Get-Date).ToString("yyyy-MM-dd")
            $sourceName = if ($diff -gt 0) { "<Crypto Market>" } else { "Crypto Portfolio" }
            $destName  = if ($diff -gt 0) { "Crypto Portfolio" } else { "<Crypto Market>" }
            $txPayload = @{
                error_if_duplicate_hash = $false
                apply_rules = $false
                fire_webhooks = $false
                transactions = @(@{
                    type = $type
                    date = $today
                    amount = ("{0:F2}" -f $abs)
                    currency_code = "USD"
                    description = "Portfolio sync: snapshot delta"
                    source_name = $sourceName
                    destination_name = $destName
                    notes = "Auto-generated by portfolio_daily_snapshot.ps1. Snapshot total=`$$($target). Previous Firefly balance=`$$($current). Delta=`$$([math]::Round($diff,2))."
                })
            } | ConvertTo-Json -Depth 6
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:8180/api/v1/transactions" -Method Post -Headers $h -Body $txPayload -TimeoutSec 15
            Log "Firefly sync: $type `$$abs (txn id=$($r.data.id))"
        } else {
            Log "Firefly sync: no change (balance already `$$current)"
        }
    } catch {
        Log "Firefly sync FAILED: $_"
    }
} else {
    Log "Firefly sync skipped: no PAT at $patPath"
}

# --- Telegram report ---
$envFile = "C:\Users\azfar\metamcp-local\.env.local"
$token = (Get-Content $envFile | Select-String "^TELEGRAM_BOT_TOKEN=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() } | Select-Object -First 1)
$chatId = "YOUR_TELEGRAM_CHAT_ID"

$full = "[Daily portfolio snapshot]`n`n$body"
try {
    $resp = curl.exe -s -F "chat_id=$chatId" --form-string "text=$full" "https://api.telegram.org/bot$token/sendMessage"
    $ok = ($resp | ConvertFrom-Json).ok
    if ($ok) { Log "Telegram (@YourSentinelBot) sent" } else { Log "Telegram FAIL: $resp" }
} catch {
    Log "Telegram notify FAILED: $_"
}

Log "=== Run finished ==="
