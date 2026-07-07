#!/bin/bash
# Sentinel Print container entrypoint: configure the network CUPS queue for the
# EPSON L1250, start cupsd, then run the upload-and-print portal in foreground.
set -u

PRINTER="${PRINTER_NAME:-EPSON_L1250}"
DEVICE_URI="${PRINTER_URI:-ipp://192.168.50.145/ipp/print}"
MODEL="${PRINTER_MODEL:-everywhere}"   # driverless IPP Everywhere (pwg-raster).
                                       # Set PRINTER_MODEL to an escpr PPD for the
                                       # native Epson path (driver baked in image).

mkdir -p /run/cups
# Start the scheduler (daemonizes).
/usr/sbin/cupsd

# Wait for the scheduler socket.
for i in $(seq 1 30); do
  lpstat -r >/dev/null 2>&1 && break
  sleep 0.5
done

# (Re)create the queue. Retry a few times in case the printer is momentarily
# unreachable at container start — do NOT hard-fail the container on this.
if ! lpstat -p "$PRINTER" >/dev/null 2>&1; then
  for i in $(seq 1 10); do
    if lpadmin -p "$PRINTER" -E -v "$DEVICE_URI" -m "$MODEL" 2>/tmp/lpadmin.err; then
      cupsaccept "$PRINTER"; cupsenable "$PRINTER"; lpadmin -d "$PRINTER"
      echo "[sentinel-print] queue '$PRINTER' -> $DEVICE_URI ($MODEL) ready"
      break
    fi
    echo "[sentinel-print] lpadmin attempt $i failed: $(cat /tmp/lpadmin.err 2>/dev/null)"
    sleep 5
  done
fi

lpstat -t 2>/dev/null || true

# Portal in foreground keeps the container alive; restart-policy handles crashes.
echo "[sentinel-print] starting portal on :6632"
exec python3 /opt/sentinel/sentinel_print_web.py
