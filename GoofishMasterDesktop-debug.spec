# -*- mode: python ; coding: utf-8 -*-
"""
GoofishMasterDesktop 打包脚本（PyInstaller onedir）
生成 dist/GoofishMasterDesktop/GoofishMasterDesktop.exe：
  - 入口 launcher.py（编排器）
  - 冻结模式下以 `--service <name>` 复用同一 exe 拉起各微服务（避免 4 份依赖重复打包）
  - common/config/services/knowledge-base 以数据文件形式随包分发
  - 关键原生/复杂包强制 collect_all，确保动态导入不缺件
用法：
  .venv/Scripts/python.exe -m PyInstaller GoofishMasterDesktop-debug.spec
"""
import sys
from pathlib import Path

SPECPATH = Path(SPECPATH)  # PyInstaller 注入：spec 文件所在目录 = 项目根
ROOT = SPECPATH

block_cipher = None

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "common"), "common"),
        # config/ 不打包：冻结态读的是 APP_DIR/config，且会内嵌本机 secret_key
        (str(ROOT / "services"), "services"),
        (str(ROOT / "knowledge-base"), "knowledge-base"),
    ],
    hiddenimports=[
        "asyncpg", "qdrant_client", "lark_oapi", "openai", "fastapi", "uvicorn",
        "redis", "httpx", "websockets", "segno", "pyotp", "cryptography", "yaml",
        "aiofiles", "requests", "python_socks", "pydantic_settings", "PIL",
        "playwright", "json", "os", "secrets", "sys",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["webview", "pystray", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)

# 强制收集复杂/原生包（含子模块），防止冻结后动态 import 缺件导致启动崩溃。
# 不用 collect_all（它内部 copy_metadata 会返回 dist-info 目录，无法直接加为 DATA），
# 而是分别收集动态库 / 数据文件 / 子模块，跳过元数据目录（运行时不需要）。
from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)

_COLLECT = [
    "lark_oapi", "qdrant_client", "asyncpg", "openai", "playwright", "fastapi",
    "uvicorn", "redis", "httpx", "websockets", "segno", "pyotp", "cryptography",
    "yaml", "aiofiles", "requests", "python_socks", "pydantic_settings", "PIL",
]
for _pkg in _COLLECT:
    try:
        a.binaries += collect_dynamic_libs(_pkg)
        if _pkg == "asyncpg":
            # Cython 包：其 .pyx/.pxd 源码会与 pgproto 子包目录同名冲突，
            # 运行时只用编译后的 .pyd，故跳过数据文件收集。
            a.hiddenimports += collect_submodules(_pkg)
        else:
            for _d in collect_data_files(_pkg):
                _s, _dd = _d  # 2-tuple (src, dest)
                a.datas.append((_dd, _s, "DATA"))
            a.hiddenimports += collect_submodules(_pkg)
    except Exception as _e:  # 某些包 collect 失败不应阻断整体构建
        print(f"[spec] collect {_pkg} skipped: {_e}")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="GoofishMasterDesktop",
    icon=str(ROOT / "app.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,   # 调试期保留控制台；正式发布可改 False（日志已落 data/logs）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GoofishMasterDesktop",
)
