FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
# cups + escpr (native Epson driver, available for later PRINTER_MODEL switch)
# + cups-filters/ghostscript for PDF/image rasterization, + flask for the portal.
RUN apt-get update && apt-get install -y --no-install-recommends \
      cups \
      cups-client \
      cups-filters \
      printer-driver-escpr \
      ghostscript \
      poppler-utils \
      python3 \
      python3-flask \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY cupsd.conf /etc/cups/cupsd.conf
COPY sentinel_print_web.py /opt/sentinel/sentinel_print_web.py
COPY entrypoint.sh /entrypoint.sh
# Strip any CRLF picked up from the Windows host, then make executable.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 631 6632
ENTRYPOINT ["/entrypoint.sh"]
