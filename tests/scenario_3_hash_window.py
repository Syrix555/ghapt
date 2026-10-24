#!/usr/bin/env python3
"""场景 3:P1-2 —— "原子发布"的说法是否成立。

ghapt.py:437-439 / README:144-148 声称按 索引 → Release → Release.gpg → InRelease
的顺序替换就不会让客户端报 Hash Sum mismatch。做法:

  1) 正常同步出 1.0.0,把 InRelease/Release/Release.gpg 备份成"旧"版本
  2) 同步出 2.0.0(终态一致),再把旧的 InRelease/Release/Release.gpg 拷回去
     —— 这就是 publish() 在两次 os.replace 之间真实停留过的字节状态
  3) 全新客户端 apt-get update → 看是否报 Hash Sum mismatch
  4) 反向状态(新 InRelease + 旧 Packages)同样测一遍 → 证明"换个顺序"救不了
  5) 对照:终态一致时 apt 正常
"""
import sys as _sys; _sys.dont_write_bytecode = True   # 不往仓库里丢 __pycache__
import shutil
import sys

from harness import (Checker, apt, apt_env, apt_update, fakegh, indexed_versions,
                     new_env, sync, WORK)

RE_TOOLDEB = r"^tooldeb_[0-9][^_]*_amd64\.deb$"
SUITE = "stable"
INDEX_FILES = ["dists/stable/Release", "dists/stable/Release.gpg", "dists/stable/InRelease",
               "dists/stable/main/binary-amd64/Packages",
               "dists/stable/main/binary-amd64/Packages.gz",
               "dists/stable/main/binary-amd64/Release"]


def snapshot(env):
    return {f: (env / "repo" / f).read_bytes() for f in INDEX_FILES}


def restore(env, snap, files=None):
    for f in (files or INDEX_FILES):
        (env / "repo" / f).write_bytes(snap[f])


def fresh_client(env):
    lst = env / "client" / "state" / "lists"
    for p in lst.glob("*"):
        if p.is_file():
            p.unlink()
    return lst


def main():
    fakegh.reset_api()
    c = Checker("hash_mismatch")

    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    env, cfg = new_env("hash_window", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    sync(cfg)
    old = snapshot(env)
    apt_env(env)
    code, out, err = apt_update(env)
    c.check("前提:1.0.0 的仓库状态客户端可正常更新", code == 0, f"exit={code}")

    # 同步出 2.0.0 → 终态(全部文件都是新的)
    fakegh.set_release("testorg", "tooldeb", "v2.0.0", [fakegh.asset("tooldeb_2.0.0_amd64.deb")])
    sync(cfg)
    new = snapshot(env)
    index_only = ["dists/stable/main/binary-amd64/Packages",
                  "dists/stable/main/binary-amd64/Packages.gz",
                  "dists/stable/main/binary-amd64/Release"]
    c.check("前提:2.0.0 已进入索引", "2.0.0" in indexed_versions(env, "tooldeb"))

    print("\n=== 中间状态 W1:publish() 已经换掉索引,但还没换 InRelease ===")
    restore(env, old, ["dists/stable/Release", "dists/stable/Release.gpg", "dists/stable/InRelease"])
    fresh_client(env)
    code, out, err = apt_update(env)
    lines = (out + err).splitlines()
    shown = [l for l in lines
             if l.startswith(("E:", "W:", "Get:", "Err"))
             or "mismatch" in l.lower() or "Hashes of expected" in l]
    print("  " + "\n  ".join(shown[:14]))
    c.check("★ 客户端报 Hash Sum mismatch(README 声称不会发生)",
            code != 0 and "Hash Sum mismatch" in (out + err), f"exit={code}")
    c.check("apt 明确指出是期望哈希与实际不符",
            "Hashes of expected file" in (out + err) or "does not match" in (out + err),
            [l.strip() for l in lines if "mismatch" in l][:1])

    print("\n=== 中间状态 W2:反过来先换 InRelease、索引还是旧的 ===")
    restore(env, new, ["dists/stable/Release", "dists/stable/Release.gpg", "dists/stable/InRelease"])
    restore(env, old, index_only)
    fresh_client(env)
    code, out, err = apt_update(env)
    c.check("★ 反方向同样报 Hash Sum mismatch(所以'换顺序'救不了,只有 by-hash 能救)",
            code != 0 and "Hash Sum mismatch" in (out + err), f"exit={code}")

    print("\n=== 对照:终态一致(全部新文件)===")
    restore(env, new)
    fresh_client(env)
    code, out, err = apt_update(env)
    c.check("终态一致时 apt 正常", code == 0 and "mismatch" not in (out + err), f"exit={code}")
    code, out, err = apt(env, "-s", "install", "tooldeb")
    sel = [l for l in (out + err).splitlines() if "Inst tooldeb" in l]
    c.check("客户端选中 2.0.0", bool(sel) and "2.0.0" in sel[0], sel[0] if sel else "")

    print("\n=== 仓库是否具备 by-hash 能力(业界标准解法)===")
    inrel = (env / "repo" / "dists/stable/InRelease").read_text()
    c.check("InRelease 里没有 Acquire-By-Hash", "Acquire-By-Hash" not in inrel)
    c.check("仓库里没有 by-hash 目录",
            not list((env / "repo" / "dists").rglob("by-hash")),
            "Debian/Ubuntu 官方仓库都提供 dists/<suite>/<comp>/binary-<arch>/by-hash/SHA256/<hash>")

    print("\n=== 附加:只留 Release + Release.gpg(删掉 InRelease)的验证路径 ===")
    (env / "repo" / "dists/stable/InRelease").unlink()
    fresh_client(env)
    code, out, err = apt_update(env)
    print("  " + "\n  ".join((out + err).strip().splitlines()[:6]))
    c.check("apt 走 Release + Release.gpg 路径依然成功(两个签名产物都可用)",
            code == 0, f"exit={code}")

    print(f"\n场景结果:{len(c.results) - len(c.failed)}/{len(c.results)} 通过")
    return c


if __name__ == "__main__":
    sys.exit(1 if main().failed else 0)
