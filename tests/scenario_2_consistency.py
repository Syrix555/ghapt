#!/usr/bin/env python3
"""场景 2:P1-1 修复后的不变式 —— pool 与已发布索引必须永远一致。

修复前这个文件复现的是"分叉 / 粘性 / 404";现在它断言的是反面:
  1 项目级两阶段落盘:任一架构失败,整个项目的包都不进 pool
  2 索引永远由 pool 重新生成并比对,失败/中断后下一次 sync 自动收敛
  3 发布失败不删旧版本、不推进 state、旧索引继续可用
  4 先发布成功、再删旧版本(gc 同理)
  5 外部改动(往 pool 里塞包、改坏索引、删掉 InRelease)会被下一次 sync 收敛
  6 日志不再自相矛盾
"""
import sys as _sys; _sys.dont_write_bytecode = True   # 不往仓库里丢 __pycache__
import shutil
import subprocess
import sys

from harness import (Checker, apt, apt_env, apt_update, fakegh, gc, ghapt,
                     inrelease, indexed_versions, new_env, packages_text,
                     pool_debs, sync, verify_release_hashes)

RE_MULTIA_A = r"^multia_[0-9][^_]*_amd64\.deb$"
RE_MULTIA_R = r"^multia_[0-9][^_]*_arm64\.deb$"
RE_TOOLDEB = r"^tooldeb_[0-9][^_]*_amd64\.deb$"
RE_TOOLBIN = r"^toolbin_[0-9.]+_linux_x86_64\.tar\.gz$"


def has(env, name):
    return any(p.endswith("/" + name) for p in pool_debs(env))


def no_dangling(env):
    """任何时刻都必须成立:索引引用的文件全部存在且哈希正确(不会 404)。"""
    _, problems, _ = verify_release_hashes(env)
    return not problems


def consistent(env):
    """同步成功之后成立:索引不仅无悬空引用,而且 pool 里的每个包都已进索引。"""
    _, problems, filenames = verify_release_hashes(env)
    if problems:
        return False
    indexed = set()
    for arch in ("amd64", "arm64"):
        for line in packages_text(env, arch).splitlines():
            if line.startswith("Filename:"):
                indexed.add(line.split(":", 1)[1].strip())
    for rel in pool_debs(env):
        if rel in indexed:
            continue
        arch = subprocess.run(["dpkg-deb", "-f", str(env / "repo" / rel), "Architecture"],
                              capture_output=True, text=True).stdout.strip()
        if arch in ("amd64", "arm64", "all"):
            return False        # 架构在声明范围内却没进索引 = 孤儿
    return set(filenames) <= set(pool_debs(env))


def broken_arm64():
    bad = fakegh.ASSETS / "broken" / "multia_2.0.0_arm64.deb"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not a debian package at all\n" * 8)
    return bad


def main():
    fakegh.reset_api()
    c = Checker("consistency")

    # ── 1/2/3/4. 多架构部分失败 → 项目级原子性 ────────────────────────
    print("\n=== 1. 首次 sync v1.0.0(双架构)===")
    fakegh.set_release("testorg", "multia", "v1.0.0", [
        fakegh.asset("multia_1.0.0_amd64.deb"), fakegh.asset("multia_1.0.0_arm64.deb")])
    env, cfg = new_env("consistency_multiarch", {
        "multia": {"repo": "testorg/multia", "type": "deb",
                   "asset.amd64": RE_MULTIA_A, "asset.arm64": RE_MULTIA_R},
    })
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("v1.0.0 双架构收录成功",
            r.code == 0 and indexed_versions(env, "multia") == ["1.0.0"]
            and indexed_versions(env, "multia", "arm64") == ["1.0.0"],
            f"amd64={indexed_versions(env, 'multia')} arm64={indexed_versions(env, 'multia', 'arm64')}")
    before_pool = pool_debs(env)

    print("\n=== 2. 上游发 v2.0.0,arm64 资产是坏文件(amd64 正常、且先下载)===")
    fakegh.set_release("testorg", "multia", "v2.0.0", [
        fakegh.asset("multia_2.0.0_amd64.deb"),
        fakegh.asset("multia_2.0.0_arm64.deb", path=broken_arm64())])
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("整体报失败(退出码非 0)", r.code != 0, f"exit={r.code}")
    c.check("amd64 确实已经下载过(失败发生在下载之后)", "下载 multia_2.0.0_amd64.deb" in r.out)
    c.check("★[P2-4 已修] 失败信息带上 dpkg-deb 的真实 stderr(修复前只有 exit status 2)",
            "并非 Debian 格式" in r.both or "not a Debian" in r.both,
            [l.strip() for l in r.both.splitlines() if "dpkg-deb" in l][:2])
    c.check("★ pool 里一个 2.0.0 文件都没有(项目级两阶段落盘)",
            "multia_2.0.0_amd64.deb" not in [p.split("/")[-1] for p in pool_debs(env)],
            str([p.split("/")[-1] for p in pool_debs(env)]))
    c.check("★ pool 与失败前完全一致(没有任何半套文件)",
            pool_debs(env) == before_pool, f"{before_pool} → {pool_debs(env)}")
    c.check("★ 已发布索引仍是 1.0.0,没有把半套 release 发出去",
            indexed_versions(env, "multia") == ["1.0.0"]
            and indexed_versions(env, "multia", "arm64") == ["1.0.0"],
            f"amd64={indexed_versions(env, 'multia')}")
    c.check("state 未推进", '"v1.0.0"' in (env / "state.json").read_text())
    c.check("★ 索引不含悬空引用(核心不变式:客户端永远不会 404)", no_dangling(env),
            "索引里的文件全部存在且哈希正确")

    print("\n=== 3. 失败期间重复同步 3 次:不产生任何孤儿,也不自愈成半套 ===")
    for i in range(3):
        r = sync(cfg)
        print(f"  第 {i + 2} 次 sync: exit={r.code} pool={[p.split('/')[-1] for p in pool_debs(env)]}")
        c.check(f"第 {i + 2} 次:索引仍然无悬空引用", no_dangling(env))
    c.check("★ 3 次失败同步后 pool 里依然没有 2.0.0",
            "multia_2.0.0_amd64.deb" not in [p.split("/")[-1] for p in pool_debs(env)])

    print("\n=== 4. 上游修好 arm64 之后:一次 sync 自动收敛(不需要 --force)===")
    fakegh.set_release("testorg", "multia", "v2.0.0", [
        fakegh.asset("multia_2.0.0_amd64.deb"), fakegh.asset("multia_2.0.0_arm64.deb")])
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("修好后一次 sync 就成功", r.code == 0, f"exit={r.code}")
    c.check("★ 两个架构同时进索引(一起发布,不存在只发一半)",
            indexed_versions(env, "multia") == ["1.0.0", "2.0.0"]
            and indexed_versions(env, "multia", "arm64") == ["1.0.0", "2.0.0"],
            f"amd64={indexed_versions(env, 'multia')} arm64={indexed_versions(env, 'multia', 'arm64')}")
    c.check("★ pool == 索引", consistent(env))
    apt_env(env)
    code, out, err = apt_update(env, log=False)
    c.check("客户端可正常 update", code == 0, f"exit={code}")

    # ── 5. publish 失败:仓库保持原样 ────────────────────────────────
    print("\n=== 5. publish 失败(模拟 gpg/磁盘/沙箱错误)===")
    fakegh.reset_api()
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    env2, cfg2 = new_env("consistency_publish_fail", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}},
        keep_versions=2)
    sync(cfg2)
    apt_env(env2)
    apt_update(env2, log=False)
    fakegh.set_release("testorg", "tooldeb", "v2.0.0", [fakegh.asset("tooldeb_2.0.0_amd64.deb")])
    sync(cfg2)
    print(f"  两轮正常同步后:索引={indexed_versions(env2, 'tooldeb')} "
          f"pool={[p.split('/')[-1] for p in pool_debs(env2)]}")

    fakegh.set_release("testorg", "tooldeb", "v3.0.0", [fakegh.asset("tooldeb_3.0.0_amd64.deb")])
    orig_publish = ghapt.publish

    def boom(*a, **k):
        raise RuntimeError("模拟 publish 失败(gpg/磁盘/沙箱)")

    ghapt.publish = boom
    try:
        r = sync(cfg2)
    finally:
        ghapt.publish = orig_publish
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("失败被捕获并以非 0 退出(不再抛 traceback)", r.code == 1, f"exit={r.code}")
    c.check("错误信息说清'仓库保持原样'",
            "仓库保持原样" in r.both, [l.strip() for l in r.both.splitlines() if "保持原样" in l][:1])
    c.check("★ 旧版本 1.0.0 没有被删(先发布后清理)",
            has(env2, "tooldeb_1.0.0_amd64.deb"), str(pool_debs(env2)))
    c.check("★ state 没有推进(仍是 v2.0.0)",
            '"v2.0.0"' in (env2 / "state.json").read_text())
    c.check("★ 已发布索引仍是 [1.0.0, 2.0.0],与 pool 一致(没留 404 的坑)",
            indexed_versions(env2, "tooldeb") == ["1.0.0", "2.0.0"],
            f"索引={indexed_versions(env2, 'tooldeb')}")
    c.check("★ 索引无悬空引用", no_dangling(env2))
    c.check("★ 失败期间多出的 3.0.0 只是'待发布',索引并未谎称它有",
            "3.0.0" not in indexed_versions(env2, "tooldeb"))

    print("\n  --- 客户端侧:失败的发布没有影响任何已发布内容 ---")
    code, out, err = apt_update(env2)
    c.check("apt-get update 正常", code == 0, f"exit={code}")
    code, out, err = apt(env2, "download", "tooldeb=1.0.0")
    print("  " + " ".join((out + err).split())[:130])
    c.check("★ apt-get download tooldeb=1.0.0 成功(修复前这里硬 404)",
            code == 0, f"exit={code}")

    print("\n  --- publish 恢复后:下一次普通 sync 自动收敛,无需 --force ---")
    r = sync(cfg2)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("一次普通 sync 就发布 3.0.0,并清掉超出 keep_versions 的 1.0.0",
            indexed_versions(env2, "tooldeb") == ["2.0.0", "3.0.0"]
            and not has(env2, "tooldeb_1.0.0_amd64.deb"),
            f"索引={indexed_versions(env2, 'tooldeb')} "
            f"pool={[p.split('/')[-1] for p in pool_debs(env2)]}")
    c.check("★ pool == 索引(发布成功后孤儿已被收编)", consistent(env2))
    c.check("★ 3.0.0 已进索引", "3.0.0" in indexed_versions(env2, "tooldeb"))
    c.check("state 推进到 v3.0.0", '"v3.0.0"' in (env2 / "state.json").read_text())

    print("\n  --- 再跑两次:幂等,索引不被重写 ---")
    mt = inrelease(env2).stat().st_mtime
    for _ in range(2):
        r = sync(cfg2)
    c.check("无变化时索引 mtime 不变(幂等)",
            inrelease(env2).stat().st_mtime == mt and "未改动" in r.out)

    # ── 6. 先发布后清理 ───────────────────────────────────────────
    print("\n=== 6. 清理顺序:旧版本只在索引切换之后才消失 ===")
    env3, cfg3 = new_env("consistency_prune_order", {
        "toolbin": {"repo": "testorg/toolbin", "type": "binary",
                    "asset.amd64": RE_TOOLBIN}}, keep_versions=2)
    fakegh.set_release("testorg", "toolbin", "v1.2.3",
                       [fakegh.asset("toolbin_1.2.3_linux_x86_64.tar.gz")])
    sync(cfg3)
    fakegh.set_release("testorg", "toolbin", "v1.2.4",
                       [fakegh.asset("toolbin_1.2.4_linux_x86_64.tar.gz")])
    sync(cfg3)
    fakegh.set_release("testorg", "toolbin", "v1.2.5",
                       [fakegh.asset("toolbin_1.2.5_linux_x86_64.tar.gz")])
    r = sync(cfg3)
    print("  " + r.out.strip().replace("\n", "\n  "))
    lines = [l.strip() for l in r.out.splitlines()]
    i_pub = next((i for i, l in enumerate(lines) if "索引已更新" in l), -1)
    i_del = next((i for i, l in enumerate(lines) if "清理旧版本" in l), -1)
    c.check("★ 日志顺序:先发布索引,再删旧版本", 0 <= i_pub < i_del,
            f"发布@{i_pub} 删除@{i_del}: " + " → ".join(lines[max(0, i_pub):i_del + 1]))
    c.check("keep_versions=2:pool 里留 1.2.4 + 1.2.5(1.2.3 已清)",
            sorted(p.split("/")[-1] for p in pool_debs(env3))
            == ["toolbin_1.2.4_amd64.deb", "toolbin_1.2.5_amd64.deb"], str(pool_debs(env3)))
    c.check("索引里也是 1.2.4 + 1.2.5", indexed_versions(env3, "toolbin") == ["1.2.4", "1.2.5"])

    # ── 7. drift 收敛 ─────────────────────────────────────────────
    print("\n=== 7. 往 pool 里手工塞包 → 下一次 sync 纳入索引 ===")
    stray = env3 / "repo/pool/main/t/toolbin/toolbin_9.9.9_amd64.deb"
    shutil.copy2(fakegh.build_deb(fakegh.ASSETS / "stray", "toolbin", "9.9.9", "amd64"), stray)
    c.check("前提:手工塞进 pool 的包此刻不在索引里",
            "9.9.9" not in indexed_versions(env3, "toolbin"))
    r = sync(cfg3)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 下一次 sync 自动把它纳入索引(pool == 索引 被强制)",
            "9.9.9" in indexed_versions(env3, "toolbin"),
            f"索引={indexed_versions(env3, 'toolbin')}")

    print("\n=== 8. 索引被外部改坏 → 自动重发 ===")
    pk = env3 / "repo/dists/stable/main/binary-amd64/Packages"
    pk.write_text(pk.read_text() + "\nPackage: ghost\nVersion: 1.0\n")
    r = sync(cfg3)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 检测到索引被改坏并重发",
            "索引需要重建" in r.out and "ghost" not in packages_text(env3),
            f"索引={indexed_versions(env3, 'toolbin')}")

    print("\n=== 9. 删掉 InRelease → 重新签名 ===")
    inrelease(env3).unlink()
    r = sync(cfg3)
    c.check("★ InRelease 丢失 → 重签", inrelease(env3).exists() and "索引需要重建" in r.out)

    # ── 8. state 丢失:日志自洽 ────────────────────────────────────
    print("\n=== 10. state.json 丢失后重新同步:日志不再自相矛盾 ===")
    env4, cfg4 = new_env("consistency_stateloss", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    sync(cfg4)
    before = inrelease(env4).stat().st_mtime
    (env4 / "state.json").unlink()
    r = sync(cfg4)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 报 [same] 而不是 [new](内容没变就不谎报新增)",
            "[same] tooldeb" in r.out and "[new]" not in r.out)
    c.check("★ 不再出现'说新增又说没改动'的矛盾",
            not ("[new]" in r.out and "未改动" in r.out))
    c.check("索引确实没被动(mtime 不变)", inrelease(env4).stat().st_mtime == before)
    c.check("state 已写回", '"v1.0.0"' in (env4 / "state.json").read_text())
    r = sync(cfg4)
    c.check("下一次同步只剩 '已是最新',且 pool == 索引",
            "已是最新" in r.out and consistent(env4))

    # ── 9. gc 的发布/删除顺序 ─────────────────────────────────────
    print("\n=== 11. gc:先发布再删目录 ===")
    env5, cfg5 = new_env("consistency_gc", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    shutil.rmtree(env5 / "repo")
    shutil.copytree(env / "repo", env5 / "repo")     # 里面 track 的是 multia
    shutil.copy(env / "state.json", env5 / "state.json")
    r = gc(cfg5, dry_run=True)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("--dry-run 列出 multia 且不删任何东西",
            "multia" in r.out and "dry-run" in r.out
            and (env5 / "repo/pool/main/m/multia").exists())
    r = gc(cfg5)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("gc 后 multia 目录消失", not (env5 / "repo/pool/main/m/multia").exists())
    c.check("★ gc 后索引不再列 multia,且 pool == 索引",
            "Package: multia" not in packages_text(env5) and consistent(env5),
            f"索引={indexed_versions(env5, 'multia')}")

    print("\n=== 12. gc 时发布失败:一个文件都不许删 ===")
    env6, cfg6 = new_env("consistency_gc_fail", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    shutil.rmtree(env6 / "repo")
    shutil.copytree(env / "repo", env6 / "repo")
    shutil.copy(env / "state.json", env6 / "state.json")
    before_files = sorted(p.name for p in (env6 / "repo/pool/main").rglob("*.deb"))
    ghapt.publish = boom
    try:
        r = gc(cfg6)
    finally:
        ghapt.publish = orig_publish
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("gc 发布失败以非 0 退出", r.code == 1, f"exit={r.code}")
    c.check("★ 失败时一个文件都没删(不会留下索引指向空目录)",
            sorted(p.name for p in (env6 / "repo/pool/main").rglob("*.deb")) == before_files,
            f"{before_files}")

    print(f"\n场景结果:{len(c.results) - len(c.failed)}/{len(c.results)} 通过")
    return c


if __name__ == "__main__":
    sys.exit(1 if main().failed else 0)
