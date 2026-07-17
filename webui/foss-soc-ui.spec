# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the FOSS SOC Engine Web UI.
# Builds a single self-contained executable (FOSS-SOC-UI[.exe]) that bundles
# Python + Flask + the engine code + the HTML/CSS/JS. The editable rules/ and
# config.yaml are NOT bundled - they live next to the executable so operators
# can change them without rebuilding (app.py reads them from the exe's folder).
#
# Build with:   python webui/build_exe.py        (recommended; assembles a
#                                                  ready-to-zip release folder)
# or directly:  pyinstaller webui/foss-soc-ui.spec

import os

ROOT = os.path.dirname(SPECPATH)  # repo root (SPECPATH = the webui/ folder)

a = Analysis(
    [os.path.join(SPECPATH, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(SPECPATH, 'templates'), 'templates'),
        (os.path.join(SPECPATH, 'static'), 'static'),
    ],
    hiddenimports=[
        'core.engine', 'core.schema', 'core.registry', 'core.ecs_schema',
        'core.timeparse',
        'utils.geoip', 'utils.fastjson',
        'test_config', 'preflight', 'ecs_helper',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim heavy optional libs we never need in the UI build. (redis / geoip2 /
    # kafka are picked up only if installed; the app degrades without them.)
    excludes=['tkinter', 'numpy', 'pandas', 'matplotlib', 'scipy', 'PIL'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FOSS-SOC-UI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # keep a console so users see the URL + can Ctrl+C
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
