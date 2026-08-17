# -*- mode: python ; coding: utf-8 -*-
"""
Сборка пакета proof_recorder в .exe (точка входа — run_proof_recorder.py).

Собирается в ПАПКУ (COLLECT), а не в один файл, и это осознанно: внутри лежит
драйвер Playwright — Node.js плюс сотни файлов. В варианте --onefile всё это
распаковывается во временную папку при КАЖДОМ запуске, и старт занимает
десятки секунд.

Важное про данные рядом с программой. Профиль браузера (.browser_profile),
proxy_config.json, proof_settings.json и cookies.json программа ищет рядом с
самим exe (см. _app_dir в proof_recorder.py), а НЕ внутри сборки. Поэтому:
  * профиль, прогретый руками, переживает обновление программы;
  * чтобы раздать программу коллегам, копируется вся папка dist/ProofRecorder.

Собрать:
    .venv\\Scripts\\pyinstaller.exe ProofRecorder.spec --noconfirm
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Playwright и patchright — оба, и это не перестраховка: сам прогон работает
# на playwright (см. proof_recorder.py), а bot.py, который мы импортируем ради
# прокси и определения брендов, тянет patchright. Без обоих драйверов exe
# упадёт на импорте.
for pkg in ("playwright", "patchright"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# imageio_ffmpeg возит ffmpeg.exe внутри пакета — без сбора данных запись
# видео и сжатие просто не запустятся.
d, b, h = collect_all("imageio_ffmpeg")
datas += d
binaries += b
hiddenimports += h

# oxipng — скомпилированный модуль (Rust), PyInstaller не всегда находит его
# сам по импорту.
d, b, h = collect_all("oxipng")
datas += d
binaries += b
hiddenimports += h

# pngquant.exe ставится пакетом pngquant-cli в Scripts окружения и лежит вне
# любого пакета, поэтому кладём его руками. Не нашёлся — не страшно: сжатие
# скриншотов откатится на oxipng без потерь (см. _shrink_screenshot).
_pngquant = os.path.join(os.path.dirname(os.sys.executable), "pngquant.exe")
if os.path.isfile(_pngquant):
    binaries += [(_pngquant, ".")]

hiddenimports += collect_submodules("openpyxl")
# proof_recorder теперь пакет: подтягиваем все его подмодули (в т.ч. переехавшие
# внутрь proof_sheet и proof_xlsx). bot.py остаётся отдельным модулем в корне.
hiddenimports += collect_submodules("proof_recorder")
hiddenimports += [
    "mss", "mss.tools", "mss.windows",
    "win32gui", "win32con", "win32process", "win32api",
    "psutil", "requests", "cv2", "numpy",
    "bot",
]

a = Analysis(
    ["run_proof_recorder.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Тяжёлые пакеты, которые тянутся транзитивно, но в работе не нужны.
    excludes=["matplotlib", "scipy", "pandas", "PyQt5", "PySide2", "IPython",
              "notebook", "pytest", "setuptools"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ProofRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=False намеренно: сжатый упаковщиком exe антивирусы ловят заметно
    # охотнее, а выигрыш в размере тут роли не играет — рядом всё равно лежит
    # драйвер Playwright.
    upx=False,
    console=False,          # окно программы своё, консоль поверх него не нужна
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ProofRecorder",
)
