#!/usr/bin/env python3
"""
Build the FOSS SOC Engine Web UI into a single self-contained executable and
assemble a ready-to-distribute folder.

    python webui/build_exe.py

What you get (under  release/FOSS-SOC-UI/ ):

    FOSS-SOC-UI.exe      <- double-click to run (no Python needed)
    config.yaml          <- editable
    database/internal_ips.yaml <- editable internal IP map (IP Map tab)
    rules/               <- editable parser rules
    examples/            <- sample logs
    database/            <- drop GeoLite2-City.mmdb here (optional)
    WEB_UI_GUIDE.md      <- how to use it (copied from docs/web-ui-guide.md)

Zip that folder and hand it to a tester: "unzip, double-click the .exe".
The .exe is OS-specific - build it on the OS you want to ship to (run this on
Windows to get the Windows .exe, on Linux for a Linux binary, etc).
"""

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))   # webui/
ROOT = os.path.dirname(HERE)                          # repo root
SPEC = os.path.join(HERE, "foss-soc-ui.spec")
BUILD_OUT = os.path.join(ROOT, "build_out")
DISTPATH = os.path.join(BUILD_OUT, "dist")
WORKPATH = os.path.join(BUILD_OUT, "build")
RELEASE = os.path.join(ROOT, "release", "FOSS-SOC-UI")

EXE_NAME = "FOSS-SOC-UI.exe" if os.name == "nt" else "FOSS-SOC-UI"


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        print("PyInstaller not found - installing it...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def run_pyinstaller():
    print("\n=== Building executable with PyInstaller ===")
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--distpath", DISTPATH, "--workpath", WORKPATH, SPEC,
    ])


def copytree(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)


def assemble():
    print("\n=== Assembling release folder ===")
    exe_src = os.path.join(DISTPATH, EXE_NAME)
    if not os.path.exists(exe_src):
        sys.exit(f"Build failed: {exe_src} not found")

    if os.path.exists(RELEASE):
        shutil.rmtree(RELEASE)
    os.makedirs(RELEASE)

    shutil.copy2(exe_src, os.path.join(RELEASE, EXE_NAME))
    copytree(os.path.join(ROOT, "rules"), os.path.join(RELEASE, "rules"))
    copytree(os.path.join(ROOT, "examples"), os.path.join(RELEASE, "examples"))

    cfg = os.path.join(ROOT, "config.yaml")
    if os.path.exists(cfg):
        shutil.copy2(cfg, os.path.join(RELEASE, "config.yaml"))

    os.makedirs(os.path.join(RELEASE, "database"), exist_ok=True)

    # Internal IP map starter (lives in database/ with the GeoIP files).
    # Always GENERATED, never copied: the repo's map file is .gitignore'd
    # site-local data (a builder's real network plan must never end up
    # inside a distributable).
    with open(os.path.join(RELEASE, "database", "internal_ips.yaml"), "w",
              encoding="utf-8") as f:
        f.write(
            "# Internal IP map - which of YOUR ranges is which building/room/"
            "lab.\n"
            "# Edit it in the UI ('IP Map' tab: visual editor, validation,\n"
            "# test lookups) or copy examples/internal_ips.example.yaml over\n"
            "# this file. Empty list = feature idle, nothing is enriched.\n"
            "networks: []\n")
    # leave a hint so testers know what goes there
    with open(os.path.join(RELEASE, "database", "PUT-GeoLite2-mmdb-FILES-HERE.txt"),
              "w", encoding="utf-8") as f:
        f.write("GeoIP/ASN enrichment is optional. To enable it, place your\n"
                "MaxMind files in this folder and set geoip.enabled: true in\n"
                "config.yaml:\n"
                "  GeoLite2-City.mmdb  -> source.geo.* (country/city/coords)\n"
                "  GeoLite2-ASN.mmdb   -> source.as.*  (which ISP/cloud owns the IP)\n"
                "The UI works fine without them.\n")

    for src_rel, dst_name in (("docs/web-ui-guide.md", "WEB_UI_GUIDE.md"),
                              ("README.md", "README.md"),
                              ("LICENSE", "LICENSE")):
        p = os.path.join(ROOT, src_rel)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(RELEASE, dst_name))

    print("\n  Done.")
    print(f"  Release folder: {RELEASE}")
    print(f"  Run it:         {os.path.join(RELEASE, EXE_NAME)}")
    print("  Zip that folder and share it. Testers just unzip + double-click.\n")


def main():
    ensure_pyinstaller()
    run_pyinstaller()
    assemble()


if __name__ == "__main__":
    main()
