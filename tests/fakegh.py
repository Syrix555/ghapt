#!/usr/bin/env python3
"""测试夹具:构造一个假的 GitHub API(file:// 传输层)+ 假 release 资产。

不依赖网络:ghapt 的 API_ROOT 被替换为 file:// 目录树,urlopen/json.load/
download 走的仍然是 ghapt.py 里的真实代码,只有 socket 传输被换成文件读取。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
API = TESTS / "fakeapi"
ASSETS = TESTS / "assets"


def url(p: Path) -> str:
    return "file://" + str(Path(p).resolve())


# ── 构造假资产 ────────────────────────────────────────────────────────

def build_deb(outdir: Path, pkg: str, version: str, arch: str,
              desc: str = "fixture package built by ghapt tests",
              pad: int = 0) -> Path:
    """用真实 dpkg-deb 打一个 .deb(模拟上游 release 里现成的 deb)。"""
    outdir.mkdir(parents=True, exist_ok=True)
    stage = outdir / f"stage-{pkg}-{version}-{arch}"
    (stage / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (stage / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    body = f"#!/bin/sh\necho '{pkg} {version} ({arch})'\n" + ("#" * pad + "\n" if pad else "")
    exe = stage / "usr" / "bin" / pkg
    exe.write_text(body)
    os.chmod(exe, 0o755)
    (stage / "DEBIAN" / "control").write_text(
        f"Package: {pkg}\nVersion: {version}\nArchitecture: {arch}\n"
        f"Maintainer: fixture <fixture@localhost>\nSection: utils\n"
        f"Priority: optional\nDescription: {desc}\n")
    out = outdir / f"{pkg}_{version}_{arch}.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz",
                    str(stage), str(out)], check=True, capture_output=True)
    shutil.rmtree(stage)
    return out


def build_tar(outdir: Path, name: str, inner: str, payload: bytes,
              compress: str = "gz") -> Path:
    """装箱:<inner> 文件 → <name>(.tar.gz / .tar.zst / 裸文件)。"""
    outdir.mkdir(parents=True, exist_ok=True)
    stage = outdir / "_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    src = stage / inner
    src.write_bytes(payload)
    os.chmod(src, 0o755)
    out = outdir / name
    if name.endswith(".tar.zst"):
        subprocess.run(["tar", "--zstd", "-cf", str(out), "-C", str(stage), inner],
                       check=True, capture_output=True)
    elif name.endswith(".tar.gz"):
        subprocess.run(["tar", "-czf", str(out), "-C", str(stage), inner],
                       check=True, capture_output=True)
    else:  # 裸二进制
        shutil.copy2(src, out)
    shutil.rmtree(stage)
    return out


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asset(name: str, path: Path = None, size: int = None) -> dict:
    """一条 release asset 记录。size 可被故意写错,用来测完整性校验。"""
    path = Path(path) if path else ASSETS / name
    real = path.stat().st_size
    return {"name": name,
            "size": real if size is None else size,
            "browser_download_url": url(path),
            # GitHub 2025-06 起提供 digest,ghapt 目前不读它 —— 测试里照样写上
            "digest": "sha256:" + sha256(path)}


# ── 假 API 文件树 ─────────────────────────────────────────────────────

def _rel_dir(owner: str, name: str) -> Path:
    d = API / "repos" / owner / name / "releases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_release(owner: str, name: str, tag: str, assets, prerelease: bool = False):
    """写入 /repos/<owner>/<name>/releases/latest。"""
    d = _rel_dir(owner, name)
    rel = {"tag_name": tag, "draft": False, "prerelease": prerelease,
           "created_at": "2026-01-01T00:00:00Z", "assets": assets}
    (d / "latest").write_text(json.dumps(rel, indent=2))
    return rel


def set_release_list(owner: str, name: str, releases):
    """写入 /repos/<owner>/<name>/releases?per_page=10(prerelease=true 模式用)。

    注意 file:// 下 query 不会被剥掉,所以这是一个文件名里真的带 '?' 的文件。
    """
    d = API / "repos" / owner / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "releases?per_page=10").write_text(json.dumps(releases, indent=2))


def reset_api():
    if API.exists():
        shutil.rmtree(API)
    API.mkdir(parents=True)


if __name__ == "__main__":
    sys.exit("这是夹具模块,请用 tests/run_tests.py")
