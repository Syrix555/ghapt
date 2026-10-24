#!/usr/bin/env python3
"""生成全部测试夹具(真实 dpkg-deb / tar / zstd),不联网。"""
import shutil
from pathlib import Path

import fakegh
from fakegh import ASSETS, build_deb, build_tar

ELF = Path("/bin/true").read_bytes()  # 拿真 ELF 当二进制载荷


def main():
    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    ASSETS.mkdir(parents=True)

    # 1) deb 模式:上游直接发 .deb,两个版本
    build_deb(ASSETS, "tooldeb", "1.0.0", "amd64", pad=0)
    build_deb(ASSETS, "tooldeb", "2.0.0", "amd64", pad=2048)

    # 2) 多架构项目(用于"部分失败"场景)
    build_deb(ASSETS, "multia", "1.0.0", "amd64")
    build_deb(ASSETS, "multia", "1.0.0", "arm64")

    # 2b) 多架构新版本 + 第三个版本(部分失败 / publish 失败场景)
    build_deb(ASSETS, "multia", "2.0.0", "amd64", pad=512)
    build_deb(ASSETS, "multia", "2.0.0", "arm64", pad=512)
    build_deb(ASSETS, "tooldeb", "3.0.0", "amd64", pad=4096)

    # 3) binary 模式:上游只发 tarball
    build_tar(ASSETS, "toolbin_1.2.3_linux_x86_64.tar.gz", "toolbin", ELF)
    build_tar(ASSETS, "toolbin_1.2.4_linux_x86_64.tar.gz", "toolbin", ELF + b"\n" * 64)
    build_tar(ASSETS, "toolbin_1.2.5_linux_x86_64.tar.gz", "toolbin", ELF + b"\n" * 96)

    # 4) binary 模式 + 预发布 tag(版本号映射场景)
    build_tar(ASSETS, "binrc_1.2.3_linux_x86_64.tar.gz", "binrc", ELF)

    # 4b) 裸二进制资产(无任何压缩/校验和,用于"同尺寸篡改"场景)
    build_tar(ASSETS, "rawtool_1.0.0_linux_x86_64", "rawtool", ELF)

    # 5) binary 模式 + ghapt 不认识的压缩格式(.tar.zst)
    build_tar(ASSETS, "toolzst_1.0.0_linux_x86_64.tar.zst", "toolzst", ELF)

    for p in sorted(ASSETS.iterdir()):
        print(f"  {p.name:44} {p.stat().st_size:>9} B")
    print(f"夹具资产 {len(list(ASSETS.iterdir()))} 个 → {ASSETS}")


if __name__ == "__main__":
    main()

import sys as _sys; _sys.dont_write_bytecode = True   # 不往仓库里丢 __pycache__