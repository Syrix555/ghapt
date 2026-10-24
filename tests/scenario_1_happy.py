#!/usr/bin/env python3
"""场景 1:端到端快乐路径(deb 模式 + binary 重打包模式 + 真实 apt 客户端)。"""
import shutil
import subprocess
import sys

from harness import (Checker, apt, apt_env, apt_update, dist, fakegh, gc,
                     gpg_verify, inrelease, indexed_versions, new_env,
                     packages_text, pool_debs, sync, check_cmd, verify_release_hashes)

RE_TOOLDEB = r"^tooldeb_[0-9][^_]*_amd64\.deb$"
RE_TOOLBIN = r"^toolbin_[0-9.]+_linux_x86_64\.tar\.gz$"


def clone_env(src, name):
    env, cfg = new_env(name, {})
    shutil.rmtree(env / "repo")
    shutil.copytree(src / "repo", env / "repo")
    return env, cfg


def main():
    fakegh.reset_api()
    c = Checker("happy_path")

    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    fakegh.set_release("testorg", "toolbin", "v1.2.3", [fakegh.asset("toolbin_1.2.3_linux_x86_64.tar.gz")])

    env, cfg = new_env("happy_path", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB},
        "toolbin": {"repo": "testorg/toolbin", "type": "binary",
                    "description": "fixture binary tool", "asset.amd64": RE_TOOLBIN},
    })

    print("\n=== 1. 首次 sync(deb 收录 + binary 重打包,真实 gpg 生成密钥并签名)===")
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    if r.err.strip():
        print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("首次 sync 退出码 0", r.code == 0, f"exit={r.code}")
    c.check("两个项目都收录成功", "[new] tooldeb" in r.out and "[new] toolbin" in r.out)
    c.check("生成了签名 InRelease", inrelease(env).exists())
    c.check("导出了公钥 ghapt.gpg / ghapt.asc",
            (env / "repo" / "ghapt.gpg").exists() and (env / "repo" / "ghapt.asc").exists())

    print("\n=== 2. pool 与索引内容 ===")
    print(f"  pool: {pool_debs(env)}")
    pk = packages_text(env)
    for line in pk.splitlines():
        if line[:1] not in (" ", "\t"):
            print(f"  {line}")
    c.check("deb 模式:上游版本号原样收录 (1.0.0)",
            "Package: tooldeb" in pk and "Version: 1.0.0" in pk)
    c.check("binary 模式:tag 去掉 v 前缀 → 1.2.3",
            "Package: toolbin" in pk and "Version: 1.2.3" in pk)
    c.check("binary 重打包出的 deb 落在 pool 里(命名遵循 pool 规范)",
            any("pool/main/t/toolbin/toolbin_1.2.3_amd64.deb" == p for p in pool_debs(env)),
            str(pool_debs(env)))

    print("\n=== 3. 重打包质量:control 字段与可执行文件本体 ===")
    deb = env / "repo" / "pool" / "main" / "t" / "toolbin" / "toolbin_1.2.3_amd64.deb"
    ctl = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True).stdout
    print("  " + ctl.strip().replace("\n", "\n  "))
    c.check("control 含 Package/Version/Architecture/Homepage",
            all(k in ctl for k in ("Package: toolbin", "Version: 1.2.3",
                                   "Architecture: amd64", "Homepage: https://github.com/testorg/toolbin")))
    listing = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True).stdout
    print("  " + listing.strip().replace("\n", "\n  "))
    c.check("deb 里有 /usr/bin/toolbin 且可执行", "-rwxr-xr-x" in listing and "./usr/bin/toolbin" in listing)
    import hashlib
    extracted = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(deb)], capture_output=True).stdout
    with __import__("tempfile").TemporaryDirectory() as td:
        p = subprocess.run(["tar", "-xO", "-C", td, "./usr/bin/toolbin"], input=extracted,
                           capture_output=True)
    c.check("提取出的二进制与上游 tarball 里的字节一致(未损坏)",
            hashlib.sha256(p.stdout).hexdigest() == hashlib.sha256(open("/bin/true", "rb").read()).hexdigest())

    print("\n=== 4. 索引自洽性:Release 哈希 / Filename 指向 ===")
    checked, problems, filenames = verify_release_hashes(env)
    c.check(f"Release 里 {checked} 条哈希记录全部与磁盘文件一致", not problems, "; ".join(problems))
    c.check("Packages 的 Filename 都指向 pool 里真实存在的文件",
            all(f.startswith("pool/main/") for f in filenames), str(filenames))
    import gzip
    gz = (dist(env) / "Packages.gz").read_bytes()
    c.check("Packages.gz 解压后与 Packages 字节一致(apt 侧哈希校验的前提)",
            gzip.decompress(gz) == packages_text(env).encode())

    print("\n=== 5. 独立校验 GPG 签名(gpg --verify,不经 apt)===")
    code, err = gpg_verify(env)
    print(f"  gpg --verify InRelease -> exit {code}: {err.splitlines()[-1] if err else ''}")
    c.check("InRelease 签名有效", code == 0)
    code2, err2 = gpg_verify(env, env / "repo" / "dists" / "stable" / "Release.gpg")
    print(f"  gpg --verify Release.gpg -> exit {code2}: {err2.splitlines()[-1] if err2 else ''}")
    c.check("Release.gpg 分离签名有效", code2 == 0)

    print("\n=== 6. 客户端 apt update(真实签名 + 哈希校验)===")
    apt_env(env)
    code, out, err = apt_update(env)
    print("  " + "\n  ".join((out + err).strip().splitlines()[:10]))
    c.check("apt-get update 成功", code == 0, f"exit={code}")
    warnings = [l for l in (out + err).splitlines() if l.startswith(("W:", "E:"))]
    c.check("apt 输出里没有任何 W:/E: 告警(签名与哈希校验零异议)", not warnings, str(warnings))

    print("\n=== 7. 客户端解析安装计划 ===")
    for pkg, want in (("tooldeb", "1.0.0"), ("toolbin", "1.2.3")):
        code, out, err = apt(env, "-s", "install", pkg)
        sel = [l for l in (out + err).splitlines() if f"Inst {pkg} " in l]
        print("  " + (sel[0] if sel else (out + err).strip()[:180]))
        c.check(f"apt 能解析 {pkg} 且选中 {want}", bool(sel) and want in sel[0], sel[0] if sel else "")

    print("\n=== 8. 负向测试:换成错误的公钥,apt 必须拒绝 ===")
    env2, _ = clone_env(env, "happy_path_wrongkey")
    bad = env2 / "repo" / "wrongkey.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--no-default-keyring", "--keyring", str(bad),
                    "--passphrase", "", "--quick-generate-key", "wrong@localhost", "rsa2048",
                    "sign", "0"], capture_output=True)
    apt_env(env2, key="wrongkey.gpg")
    code, out, err = apt_update(env2)
    print("  " + "\n  ".join((err or out).strip().splitlines()[-3:]))
    c.check("错误 signing key 被拒(反证签名确实在校验)", code != 0, f"exit={code}")

    print("\n=== 9. 幂等:再跑一次 sync 不应改动索引 ===")
    before, mtime = inrelease(env).read_bytes(), inrelease(env).stat().st_mtime
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("第二次 sync 退出码仍为 0", r.code == 0, f"exit={r.code}")
    c.check("两个项目都报 '已是最新'", r.out.count("已是最新") == 2)
    c.check("InRelease 字节与 mtime 均未变(真幂等)",
            inrelease(env).read_bytes() == before and inrelease(env).stat().st_mtime == mtime)

    print("\n=== 10. check 退出码语义 ===")
    r = check_cmd(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("无更新 → 退出码 0", r.code == 0, f"exit={r.code}")

    print("\n=== 11. 上游发新版本 → 升级与 keep_versions 保留策略 ===")
    fakegh.set_release("testorg", "tooldeb", "v2.0.0", [fakegh.asset("tooldeb_2.0.0_amd64.deb")])
    fakegh.set_release("testorg", "toolbin", "v1.2.4", [fakegh.asset("toolbin_1.2.4_linux_x86_64.tar.gz")])
    r = check_cmd(cfg)
    c.check("check 检测到更新 → 退出码 10", r.code == 10, f"exit={r.code}")
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("索引里出现新版本 2.0.0 / 1.2.4",
            "2.0.0" in indexed_versions(env, "tooldeb") and "1.2.4" in indexed_versions(env, "toolbin"),
            f"tooldeb={indexed_versions(env, 'tooldeb')} toolbin={indexed_versions(env, 'toolbin')}")
    c.check("keep_versions=2:上一版 1.0.0 仍在 pool 且仍在索引里(README 的承诺)",
            "tooldeb_1.0.0_amd64.deb" in [p.split("/")[-1] for p in pool_debs(env)]
            and "1.0.0" in indexed_versions(env, "tooldeb"))
    apt_update(env)
    code, out, err = apt(env, "-s", "install", "tooldeb")
    sel = [l for l in (out + err).splitlines() if "Inst tooldeb" in l]
    print("  " + (sel[0] if sel else (out + err).strip()[:200]))
    c.check("客户端升级后选中 2.0.0", bool(sel) and "2.0.0" in sel[0], sel[0] if sel else "")

    print("\n=== 12. 上游改资产命名 → 单项目失败隔离 ===")
    fakegh.set_release("testorg", "tooldeb", "v3.0.0",
                       [fakegh.asset("tooldeb-3.0.0-linux-amd64.deb",
                                     path=fakegh.ASSETS / "tooldeb_2.0.0_amd64.deb")])
    fakegh.set_release("testorg", "toolbin", "v1.2.5", [fakegh.asset("toolbin_1.2.4_linux_x86_64.tar.gz")])
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("tooldeb 记为 fail 且整体退出码非 0", r.code != 0 and "[fail] tooldeb" in r.both, f"exit={r.code}")
    if r.err.strip():
        print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("错误信息里打印了该 release 的资产列表(README 承诺)",
            "资产列表" in r.both and "tooldeb-3.0.0-linux-amd64.deb" in r.both)
    c.check("toolbin 未受影响(仍完成同步)", "[new] toolbin" in r.out)
    c.check("失败项目不污染已发布索引(索引里仍是 2.0.0 / 1.2.4)",
            "3.0.0" not in indexed_versions(env, "tooldeb")
            and "1.2.5" in indexed_versions(env, "toolbin"),
            f"tooldeb={indexed_versions(env, 'tooldeb')} toolbin={indexed_versions(env, 'toolbin')}")

    print("\n=== 13. gc:从配置里删掉一个项目 ===")
    env3, cfg3 = new_env("happy_path_gc", {
        "toolbin": {"repo": "testorg/toolbin", "type": "binary", "asset.amd64": RE_TOOLBIN},
    })
    shutil.rmtree(env3 / "repo")
    shutil.copytree(env / "repo", env3 / "repo")
    shutil.copy(env / "state.json", env3 / "state.json")
    r = gc(cfg3, dry_run=True)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("gc --dry-run 列出要删的目录且不实际改动",
            "dry-run" in r.out and (env3 / "repo" / "pool" / "main" / "t" / "tooldeb").exists())
    r = gc(cfg3)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("gc 真实清理:tooldeb 目录被移除,toolbin 保留",
            not (env3 / "repo" / "pool" / "main" / "t" / "tooldeb").exists()
            and any("toolbin" in p for p in pool_debs(env3)), str(pool_debs(env3)))
    c.check("gc 后索引已重建(不再列出 tooldeb)",
            "tooldeb" not in packages_text(env3) and "toolbin" in packages_text(env3))
    apt_env(env3)
    code, out, err = apt_update(env3)
    c.check("gc 重建后的仓库客户端仍可用(apt update 通过)", code == 0, f"exit={code}")

    print(f"\n场景结果:{len(c.results) - len(c.failed)}/{len(c.results)} 通过")
    return c


if __name__ == "__main__":
    sys.exit(1 if main().failed else 0)

import sys as _sys; _sys.dont_write_bytecode = True   # 不往仓库里丢 __pycache__
