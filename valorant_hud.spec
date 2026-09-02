# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置（单一通用版）：paddle 官方 wheel 为 multi-arch 打包，
# 同一产物同时覆盖 40 系（sm_89）与 50 系（sm_120）显卡。
# 离线模型：打包前先把 ~/.paddlex/official_models 下的
#   PP-OCRv6_small_det / PP-OCRv6_small_rec 拷进本仓库 ./paddlex_cache/official_models/
# （该目录被 .gitignore 忽略）。开发态运行会按名自动下载到 ~/.paddlex，无需此目录；
# 只有打「内置模型、离线可跑」的安装包才需要。打包命令（已激活的 Python 环境）：
#   python -m PyInstaller --noconfirm --clean --distpath dist --workpath build valorant_hud.spec
import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('icon.png', '.'), ('config\\config.json', 'config'), ('images', 'images'), ('paddlex_cache\\official_models\\PP-OCRv6_small_det', 'paddlex_cache\\official_models\\PP-OCRv6_small_det'), ('paddlex_cache\\official_models\\PP-OCRv6_small_rec', 'paddlex_cache\\official_models\\PP-OCRv6_small_rec')]
binaries = []
hiddenimports = []
datas += copy_metadata('paddlepaddle-gpu')
datas += copy_metadata('paddleocr')
datas += copy_metadata('paddlex')
datas += copy_metadata('imagesize')
datas += copy_metadata('opencv-contrib-python')
datas += copy_metadata('pyclipper')
datas += copy_metadata('pypdfium2')
datas += copy_metadata('python-bidi')
datas += copy_metadata('shapely')
tmp_ret = collect_all('paddleocr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('paddlex')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('paddle')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('nvidia')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['valorant_hud.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ValorantHud',
    icon='icon.png',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ValorantHud_v1.2.0',
)
