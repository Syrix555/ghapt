#!/usr/bin/env python3
"""场景 5:CLI 层面 —— 并发锁、check 退出码语义、assets 子命令、--project 过滤。"""
import argparse
import fcntl
import io
import sys
import contextlib

from harness import (Checker, check_cmd, fakegh, gc, ghapt, new_env, sync, WORK)

RE_BIN = r"^toolbin_[0-9.]+_linux_x86_64\.tar\.gz$"
RE_DEB = r"^tooldeb_[0-9][^_]*_amd64\.deb$"


def main():
    fakegh.reset_api()
    c = Checker("cli")
    fakegh.set_release("testorg", "toolbin", "v1.2.3", [fakegh.asset("toolbin_1.2.3_linux_x86_64.tar.gz")])
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    env, cfg = new_env("cli_lock", {
        "toolbin": {"repo": "testorg/toolbin", "type": "binary", "asset.amd64": RE_BIN},
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_DEB}})

    print("\n=== 1. flock:并发时第二个实例必须退出 ===")
    lock = (env / "state.json").with_suffix(".lock")
    with open(lock, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        r = sync(cfg)
        r2 = gc(cfg)
    print(f"  sync: exit={r.code} :: {r.err.strip()[:60]}")
    print(f"  gc  : exit={r2.code} :: {r2.err.strip()[:60]}")
    c.check("持锁时 sync 退出并给出明确原因",
            r.code != 0 and "另一个 ghapt sync 正在运行" in r.both, f"exit={r.code}")
    c.check("持锁时 gc 同样被挡住", r2.code != 0 and "另一个 ghapt 实例正在运行" in r2.both, f"exit={r2.code}")
    r = sync(cfg)
    c.check("锁释放后 sync 正常完成", r.code == 0, f"exit={r.code}")
    c.check("锁文件落在 state_file 同目录(state.json → state.lock)",
            lock.exists() and lock.name == "state.lock", str(lock))

    print("\n=== 2. --project 过滤 ===")
    env2, cfg2 = new_env("cli_project", {
        "toolbin": {"repo": "testorg/toolbin", "type": "binary", "asset.amd64": RE_BIN},
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_DEB}})
    r = sync(cfg2, project="toolbin")
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("只同步指定项目", "[new] toolbin" in r.out and "tooldeb" not in r.out)
    c.check("未同步的项目没有写进 state.json",
            "toolbin" in (env2 / "state.json").read_text()
            and "tooldeb" not in (env2 / "state.json").read_text())
    c.check("只同步一个项目也会重建索引(索引里有 toolbin)",
            "toolbin" in (env2 / "repo/dists/stable/main/binary-amd64/Packages").read_text())
    r = sync(cfg2, project="nope")
    c.check("不存在的项目名给出明确报错",
            r.code != 0 and "没有 [project:nope]" in r.both, r.both.strip()[:70])

    print("\n=== 2b. 配置里有坏段时的 --project 行为 ===")
    env2b, cfg2b = new_env("cli_project_broken", {
        "good": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_DEB},
        "broken": {"type": "deb", "asset.amd64": "^x$"}})      # 缺 repo
    r = sync(cfg2b, project="good")
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 指名同步好项目时成功退出(坏段只警告,不牵连)",
            r.code == 0 and "[new] good" in r.out, f"exit={r.code}")
    c.check("★ 坏段仍然被警告出来(不会因为 --project 就静默忽略)",
            "project:broken" in r.err, r.err.strip().splitlines()[:1])
    r = sync(cfg2b, project="broken")
    c.check("★ 指名同步坏段时明确报错",
            r.code != 0 and "配置有问题" in r.both, f"exit={r.code}")

    print("\n=== 3. check 的退出码语义(README:0=无更新 10=有更新 2=查询失败)===")
    r = check_cmd(cfg2)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("有项目没收录 → 10", r.code == 10, f"exit={r.code}")
    sync(cfg2)
    r = check_cmd(cfg2)
    c.check("全部最新 → 0", r.code == 0, f"exit={r.code}")

    env3, cfg3 = new_env("cli_check_fail", {
        "ghost": {"repo": "testorg/does-not-exist", "type": "deb", "asset.amd64": RE_DEB}})
    r = check_cmd(cfg3)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("查询失败 → 2", r.code == 2, f"exit={r.code}")

    print("\n=== 4. check 同时有'有更新'和'查询失败'时的优先级 ===")
    env4, cfg4 = new_env("cli_check_both", {
        "toolbin": {"repo": "testorg/toolbin", "type": "binary", "asset.amd64": RE_BIN},
        "ghost": {"repo": "testorg/does-not-exist", "type": "deb", "asset.amd64": RE_DEB}})
    r = check_cmd(cfg4)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★[P3 已修] 有查询失败时返回 2(优先于 10):'状态未知'比'已知有更新'更该被看到",
            r.code == 2, f"exit={r.code}")
    c.check("★ 摘要行同时给出两个计数(便于监控区分)",
            "1 个有更新" in r.out and "1 个查询失败" in r.out,
            [l for l in r.out.splitlines() if l.startswith("[check]")][:1])

    print("\n=== 5. assets 子命令 ===")
    ghapt.API_ROOT = fakegh.url(fakegh.API)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ghapt.cmd_assets(argparse.Namespace(repo="testorg/tooldeb", prerelease=False))
    print("  " + out.getvalue().strip().replace("\n", "\n  "))
    c.check("打印 tag 与该 release 的资产名(写正则用)",
            out.getvalue().splitlines()[0] == "# testorg/tooldeb v1.0.0"
            and "tooldeb_1.0.0_amd64.deb" in out.getvalue(), out.getvalue().strip()[:80])

    print(f"\n场景结果:{len(c.results) - len(c.failed)}/{len(c.results)} 通过")
    return c


if __name__ == "__main__":
    sys.exit(1 if main().failed else 0)

import sys as _sys; _sys.dont_write_bytecode = True   # 不往仓库里丢 __pycache__