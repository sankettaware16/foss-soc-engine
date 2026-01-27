#!/bin/bash

# FOSS SOC Engine - Installation Script

echo "[+] Installing Python dependencies..."
# Installing with --break-system-packages as requested for system-wide install
pip3 install -r requirements.txt --break-system-packages

echo "[+] Creating required directories..."
# Create local log directory for engine.log, stats.json, dlq.json
mkdir -p logs

# Create database directory for GeoLite2 file
mkdir -p database

# Create output directory for parsed logs (requires sudo if in /var/log)
# We use -p to avoid errors if it already exists
sudo mkdir -p /var/log/foss_soc_output/
sudo chmod 777 /var/log/foss_soc_output/

echo "[+] Checking for GeoIP Database..."
if [ -f "database/GeoLite2-City.mmdb" ]; then
    echo "   [OK] GeoIP database found."
else
    echo "   [WARNING] database/GeoLite2-City.mmdb is missing."
    echo "   Please move your .mmdb file into the 'database/' folder."
fi

echo ""
echo "Setup Complete. Run 'python3 main.py' to start the engine."
