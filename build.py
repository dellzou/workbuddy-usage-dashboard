#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键打包 + 投递。

为什么需要这层：PyInstaller 产物落在 dist/，而用户双击的是仓库外的
outputs/WorkBuddy用量看板.exe。只重打 dist 不覆盖 outputs，用户看到的
还是老界面（2026-11-06 真踩过一次：代码改了、exe 也重打了，但打开还是旧的）。

用法：
    python build.py            # 打包并覆盖 outputs/
    python build.py --keep     # 只打包，不覆盖

注意：Windows 上目标 exe 被占用（看板还开着）时无法覆盖，脚本会重试几次
后放弃并提示先关闭。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "WorkBuddy用量看板.exe"
SRC = os.path.join(HERE, "dist", NAME)
OUT_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "outputs"))
DST = os.path.join(OUT_DIR, NAME)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build():
    # 直接给命令行参数，不依赖 .spec：spec 被 .gitignore 排除，
    # 换台机器 clone 下来就没有了，用参数才能保证处处可复现。
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--console", "--clean", "--noconfirm",
        "--name", "WorkBuddy用量看板",
        "--icon", "app.ico",
        "--add-data", "web;web",
        "main.py",
    ]
    print("▶ 打包中 ...")
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print("✗ PyInstaller 失败，退出码 %d" % rc)
        return rc
    if not os.path.isfile(SRC):
        print("✗ 没有找到产物：%s" % SRC)
        return 1
    return 0


def deliver():
    os.makedirs(OUT_DIR, exist_ok=True)
    for attempt in range(1, 4):
        try:
            shutil.copy2(SRC, DST)
            break
        except PermissionError:
            print("  目标被占用（看板可能还开着），%d/3 重试 ..." % attempt)
            time.sleep(3)
    else:
        print("✗ 覆盖失败：请先关掉正在运行的看板再执行")
        return 1

    # 复制后必须复核，否则“以为投递成功、其实还是旧文件”这种坑会再出现
    a, b = sha256(SRC), sha256(DST)
    size = os.path.getsize(DST)
    print("\n  产物  %s  (%.2f MB)" % (SRC, os.path.getsize(SRC) / 1048576))
    print("  交付  %s  (%.2f MB)" % (DST, size / 1048576))
    print("  sha256 %s" % b)
    if a != b:
        print("✗ 两边哈希不一致，投递没生效")
        return 1
    print("  ✓ 一致")
    return 0


def main():
    rc = build()
    if rc == 0 and "--keep" not in sys.argv:
        rc = deliver()
    return rc


if __name__ == "__main__":
    sys.exit(main())
