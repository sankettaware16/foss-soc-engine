#!/bin/bash

# FOSS SOC Engine - Installation Script
#!/bin/bash
set -e

echo "[+] Installing Python dependencies"
# Installing with --break-system-packages as requested for system-wide install
pip3 install -r requirements.txt --break-system-packages

echo "[+] Creating required directories"
mkdir -p logs database

# ---------------------------------------------------------
# GeoIP Database Setup
# ---------------------------------------------------------

GEOIP_DB="database/GeoLite2-City.mmdb"

if [ ! -f "$GEOIP_DB" ]; then
    echo "[+] GeoLite2 City database not found"
    echo "[+] Downloading GeoLite2 City database"

    if [ -z "$MAXMIND_LICENSE_KEY" ]; then
        echo "[!] MAXMIND_LICENSE_KEY is not set"
        echo "[!] Please export your MaxMind license key:"
        echo "    export MAXMIND_LICENSE_KEY=YOUR_KEY"
        exit 1
    fi

    curl -L \
      "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key=${MAXMIND_LICENSE_KEY}&suffix=tar.gz" \
      -o /tmp/GeoLite2-City.tar.gz

    tar -xzf /tmp/GeoLite2-City.tar.gz -C /tmp

    mv /tmp/GeoLite2-City_*/GeoLite2-City.mmdb database/

    rm -rf /tmp/GeoLite2-City*
    echo "[+] GeoIP database installed"
else
    echo "[+] GeoIP database already present"
fi

# ---------------------------------------------------------
# ASN Database Setup (who owns the IP: ISP / cloud / hosting)
# Same offline one-time download; enriches source.as.* fields.
# ---------------------------------------------------------

ASN_DB="database/GeoLite2-ASN.mmdb"

if [ ! -f "$ASN_DB" ]; then
    echo "[+] GeoLite2 ASN database not found"
    echo "[+] Downloading GeoLite2 ASN database"

    if [ -z "$MAXMIND_LICENSE_KEY" ]; then
        echo "[!] MAXMIND_LICENSE_KEY is not set"
        echo "[!] Please export your MaxMind license key:"
        echo "    export MAXMIND_LICENSE_KEY=YOUR_KEY"
        exit 1
    fi

    curl -L \
      "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-ASN&license_key=${MAXMIND_LICENSE_KEY}&suffix=tar.gz" \
      -o /tmp/GeoLite2-ASN.tar.gz

    tar -xzf /tmp/GeoLite2-ASN.tar.gz -C /tmp

    mv /tmp/GeoLite2-ASN_*/GeoLite2-ASN.mmdb database/

    rm -rf /tmp/GeoLite2-ASN*
    echo "[+] ASN database installed"
else
    echo "[+] ASN database already present"
fi

echo "[+] Installation complete"
