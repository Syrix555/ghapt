#!/usr/bin/env python3
"""场景 4:其余条目(P1-3 版本号映射、P2-1 完整性、P2-2 tar.zst、P2-3 架构校验、
P2-5 gc 抹库、P2-6 配置错误中断全部、P3 若干)。"""
import sys as _sys; _sys.dont_write_bytecode = True   # 不往仓库里丢 __pycache__
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from harness import (Checker, apt, apt_env, apt_update, check_cmd, fakegh, gc, ghapt,
                     indexed_versions, new_env, packages_text, pool_debs, sync, WORK)

RE_TOOLDEB = r"^tooldeb_[0-9][^_]*_amd64\.deb$"


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    fakegh.reset_api()
    c = Checker("others")
    A = fakegh.ASSETS

    # ── 1. P1-3a:预发布 tag 的版本号排到了正式版之上 ──────────────────
    print("\n=== 1. P1-3a:tag v1.2.3-rc1 → 版本 1.2.3.rc1,dpkg 认为它比 1.2.3 新 ===")
    def binrc_list(extra=()):
        base = [{"tag_name": "v1.2.3", "draft": False, "prerelease": False,
                 "created_at": "2026-01-01T00:00:00Z",
                 "assets": [fakegh.asset("binrc_1.2.3_linux_x86_64.tar.gz")]}]
        return [{"tag_name": "v1.2.3-rc1", "draft": False, "prerelease": True,
                 "created_at": "2026-02-01T00:00:00Z",
                 "assets": [fakegh.asset("binrc_1.2.3_linux_x86_64.tar.gz")]}] + base

    fakegh.set_release_list("testorg", "binrc",
                            binrc_list()[1:])       # 先只看到正式版
    env, cfg = new_env("others_prerelease", {
        "binrc": {"repo": "testorg/binrc", "type": "binary", "prerelease": "true",
                  "asset.amd64": r"^binrc_[0-9.]+_linux_x86_64\.tar\.gz$"}})
    sync(cfg)
    fakegh.set_release_list("testorg", "binrc", binrc_list())   # rc1 后来居上
    r = sync(cfg)
    print("  " + r.out.strip().replace("\n", "\n  "))
    v_rc = ghapt.deb_version_from_tag("v1.2.3-rc1", "v")
    print(f"  ghapt.deb_version_from_tag('v1.2.3-rc1','v') = {v_rc!r}")
    print(f"  dpkg --compare-versions {v_rc} lt 1.2.3 -> "
          f"{ghapt.dpkg_vercmp(v_rc, '1.2.3') == -1}")
    c.check("★ 预发布 tag 映射成 ~ 语义(1.2.3~rc1)",
            v_rc == "1.2.3~rc1", f"{v_rc!r}")
    c.check("★ dpkg 认为它低于正式版 1.2.3(修复前是 gt)",
            ghapt.dpkg_vercmp(v_rc, "1.2.3") == -1, f"{v_rc} lt 1.2.3")
    c.check("索引里同时有 1.2.3 和 1.2.3~rc1",
            sorted(indexed_versions(env, "binrc")) == ["1.2.3", "1.2.3~rc1"],
            str(indexed_versions(env, "binrc")))
    apt_env(env)
    code, out, err = apt_update(env)
    code, out, err = apt(env, "-s", "install", "binrc")
    sel = [l for l in (out + err).splitlines() if "Inst binrc" in l]
    print("  " + (sel[0] if sel else (out + err).strip()[:200]))
    c.check("★ 客户端选正式版 1.2.3,不再把预发布版当升级目标",
            bool(sel) and "1.2.3~rc1" not in sel[0].split("[")[0] and "1.2.3" in sel[0],
            sel[0] if sel else "")

    # ── 2. P1-3b:strip_prefix 没命中 → 版本号变成 0.xxx,静默不升级 ────
    print("\n=== 2. P1-3b:上游改用 'pfx-2.0.0' 这类 tag(默认 strip_prefix=v 不命中)===")
    tarball = fakegh.build_tar(A, "pfx_2.0.0_linux_x86_64.tar.gz", "pfx", b"#!/bin/sh\necho pfx\n")
    fakegh.set_release("testorg", "pfx", "v1.2.3", [fakegh.asset("pfx_2.0.0_linux_x86_64.tar.gz")])
    env2, cfg2 = new_env("others_stripprefix", {
        "pfx": {"repo": "testorg/pfx", "type": "binary",
                "asset.amd64": r"^pfx_[0-9.]+_linux_x86_64\.tar\.gz$"}})
    sync(cfg2)
    fakegh.set_release("testorg", "pfx", "pfx-2.0.0", [fakegh.asset("pfx_2.0.0_linux_x86_64.tar.gz")])
    r = sync(cfg2)
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("★ strip_prefix 未命中 → 明确报错并计入退出码(不再静默生成 0.pfx.2.0.0)",
            r.code != 0 and "strip_prefix" in r.both, f"exit={r.code}")
    c.check("★ 错误信息指出该怎么修", "把 strip_prefix 设成它" in r.both,
            [l.strip() for l in r.both.splitlines() if "strip_prefix" in l][:1])
    c.check("★ pool 里没有生成垃圾版本号的包",
            not any("0.pfx" in p for p in pool_debs(env2)), str(pool_debs(env2)))
    c.check("★ 索引仍只有 1.2.3(没有把垃圾版本发出去)",
            indexed_versions(env2, "pfx") == ["1.2.3"], str(indexed_versions(env2, "pfx")))
    apt_env(env2)
    apt_update(env2, log=False)
    code, out, err = apt(env2, "-s", "install", "pfx")
    sel = [l for l in (out + err).splitlines() if "Inst pfx" in l]
    print("  " + (sel[0] if sel else (out + err).strip()[:200]))
    c.check("客户端仍稳定使用 1.2.3", bool(sel) and "1.2.3" in sel[0], sel[0] if sel else "")

    print("\n  --- 配置改对之后(补上 strip_prefix)能正常同步 ---")
    cfg2.write_text(cfg2.read_text().replace(
        "[project:pfx]", "[project:pfx]\nstrip_prefix = pfx-"))
    r = sync(cfg2)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 补上 strip_prefix = pfx- 后正常收录 2.0.0",
            r.code == 0 and "2.0.0" in indexed_versions(env2, "pfx"),
            f"索引={indexed_versions(env2, 'pfx')}")

    # ── 3. P2-1a:size 不符会被重试并最终失败 ─────────────────────────
    print("\n=== 3. P2-1a:资产 size 与实际不符 → 重试后失败 ===")
    a = fakegh.asset("tooldeb_1.0.0_amd64.deb")
    a["size"] = a["size"] + 999
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [a])
    env3, cfg3 = new_env("others_sizefail", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    r = sync(cfg3)
    retries = r.err.count("网络错误")
    print(f"  重试次数={retries};最后一行 stderr: {r.err.strip().splitlines()[-1][:90]}")
    c.check("下载不完整会重试(observable)", retries >= 2, f"retries={retries}")
    c.check("最终该失败并计入退出码", r.code != 0 and "下载不完整" in r.both, f"exit={r.code}")

    # ── 4. P2-1b:同尺寸篡改必须被 GitHub digest 挡住 ──────────────────
    print("\n=== 4. P2-1b:同尺寸的字节篡改(尺寸不变,只比 size 挡不住)===")
    pristine = A / "rawtool_1.0.0_linux_x86_64"
    tampered = A / "tampered" / "rawtool_1.0.0_linux_x86_64"
    tampered.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(pristine.read_bytes())
    data[20000] ^= 0xFF                      # 翻一个字节,尺寸完全不变
    tampered.write_bytes(bytes(data))
    c.check("前提:篡改前后尺寸相同(所以 size 校验必然放行)",
            pristine.stat().st_size == tampered.stat().st_size,
            f"{pristine.stat().st_size} bytes")

    rec = fakegh.asset("rawtool_1.0.0_linux_x86_64", path=tampered)
    rec["digest"] = "sha256:" + sha(pristine)   # API 公布的是原始文件的 digest
    fakegh.set_release("testorg", "rawtool", "v1.0.0", [rec])
    env4, cfg4 = new_env("others_tamper", {
        "rawtool": {"repo": "testorg/rawtool", "type": "binary",
                    "asset.amd64": r"^rawtool_[0-9.]+_linux_x86_64$"}})
    r = sync(cfg4)
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("★[P2-1 已修] 内容与 GitHub digest 不符 → 同步失败",
            r.code != 0 and "sha256 不符" in r.both, f"exit={r.code}")
    c.check("★ 错误信息同时给出期望与实际哈希(便于排查是镜像还是上游问题)",
            sha(pristine)[:16] in r.both and sha(tampered)[:16] in r.both,
            [l.strip() for l in r.both.splitlines() if "sha256" in l or "期望" in l][:2])
    c.check("★ 篡改内容没有被打包发布",
            not (env4 / "repo/pool/main/r/rawtool/rawtool_1.0.0_amd64.deb").exists(),
            str(pool_debs(env4)))
    c.check("★ pool 里没有任何 rawtool 包(项目级原子性)",
            not any("rawtool" in p for p in pool_debs(env4)))

    print("\n  --- 对照:digest 与内容一致时正常收录 ---")
    fakegh.set_release("testorg", "rawtool", "v1.0.0", [
        fakegh.asset("rawtool_1.0.0_linux_x86_64")])   # 用原件,digest 自动正确
    r = sync(cfg4)
    c.check("digest 一致 → 正常打包发布",
            r.code == 0 and "rawtool_1.0.0_amd64.deb" in str(pool_debs(env4)),
            str(pool_debs(env4)))
    deb = env4 / "repo/pool/main/r/rawtool/rawtool_1.0.0_amd64.deb"
    inner = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(deb)],
                           capture_output=True).stdout
    with __import__("tempfile").TemporaryDirectory() as td:
        subprocess.run(["tar", "-xf", "-", "-C", td], input=inner, capture_output=True)
        payload = open(f"{td}/usr/bin/rawtool", "rb").read()
    c.check("★ 发布出去的内容与原件逐字节一致",
            hashlib.sha256(payload).hexdigest() == sha(pristine))
    apt_env(env4)
    code, out, err = apt_update(env4, log=False)
    c.check("客户端可正常使用", code == 0, f"exit={code}")

    print("\n  --- API 没给 digest 时(老资产/其他来源)行为如何 ---")
    rec2 = fakegh.asset("rawtool_1.0.0_linux_x86_64")
    rec2.pop("digest", None)
    fakegh.set_release("testorg", "rawtool", "v1.0.0", [rec2])
    env4b, cfg4b = new_env("others_nodigest", {
        "rawtool": {"repo": "testorg/rawtool", "type": "binary",
                    "asset.amd64": r"^rawtool_[0-9.]+_linux_x86_64$"}})
    r = sync(cfg4b)
    c.check("没有 digest 时不报错(退化为仅校验 size)",
            r.code == 0, f"exit={r.code}; " + (r.err.strip()[:80] or ""))

    # ── 5. P2-2:.tar.zst 现在能正确解包 ─────────────────────────────
    print("\n=== 5. P2-2:上游发 .tar.zst(tarfile 原生不支持 zstd)===")
    fakegh.set_release("testorg", "toolzst", "v1.0.0", [
        fakegh.asset("toolzst_1.0.0_linux_x86_64.tar.zst")])
    env5, cfg5 = new_env("others_tarzst", {
        "toolzst": {"repo": "testorg/toolzst", "type": "binary",
                    "asset.amd64": r"^toolzst_[0-9.]+_linux_x86_64\.tar\.zst$"}})
    r = sync(cfg5)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★[P2-2 已修] .tar.zst 能被正常解包并收录",
            r.code == 0 and "[new] toolzst" in r.out, f"exit={r.code}")
    deb5 = env5 / "repo/pool/main/t/toolzst/toolzst_1.0.0_amd64.deb"
    inner = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(deb5)],
                           capture_output=True).stdout
    with __import__("tempfile").TemporaryDirectory() as td:
        subprocess.run(["tar", "-xf", "-", "-C", td], input=inner, capture_output=True)
        blob = open(f"{td}/usr/bin/toolzst", "rb").read()
    is_elf_now = blob[:4] == b"\x7fELF"
    print(f"  /usr/bin/toolzst 前 4 字节 = {blob[:4].hex()} (ELF={is_elf_now})")
    c.check("★ 装进 /usr/bin 的是解包后的可执行文件,不再是 zstd 压缩包本身",
            blob[:4] == b"\x7fELF")
    c.check("★ 内容与上游 tarball 里的二进制一致",
            hashlib.sha256(blob).hexdigest() == hashlib.sha256(Path("/bin/true").read_bytes()).hexdigest())
    apt_env(env5)
    code, out, err = apt_update(env5, log=False)
    c.check("客户端可正常使用", code == 0, f"exit={code}")

    print("\n  --- 兜底:不认识的压缩格式必须报错,而不是当裸二进制打包 ---")
    weird = A / "weirdtool_1.0.0_linux_x86_64.tar.lz4"
    weird.write_bytes(b"\x04\x22\x4d\x18" + b"fake lz4 payload" * 20)
    fakegh.set_release("testorg", "weirdtool", "v1.0.0",
                       [fakegh.asset("weirdtool_1.0.0_linux_x86_64.tar.lz4")])
    env5b, cfg5b = new_env("others_weirdfmt", {
        "weirdtool": {"repo": "testorg/weirdtool", "type": "binary",
                      "asset.amd64": r"^weirdtool_[0-9.]+_linux_x86_64\.tar\.lz4$"}})
    r = sync(cfg5b)
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("★ 不认识的格式 → 明确报错(修复前会静默装进 /usr/bin)",
            r.code != 0 and "压缩包" in r.both and "lz4" in r.both, f"exit={r.code}")
    c.check("★ 没有产出坏包", not pool_debs(env5b), str(pool_debs(env5b)))

    # ── 6. P2-3:asset.<arch> 写了不在 architectures 里的架构 ──────────
    print("\n=== 6. P2-3:asset.<arch> 与 architectures 交叉校验 ===")
    fakegh.build_tar(A, "archtypo_1.0.0_linux_x86_64", "archtypo", b"#!/bin/sh\necho x\n")
    fakegh.set_release("testorg", "archtypo", "v1.0.0",
                       [fakegh.asset("archtypo_1.0.0_linux_x86_64")])
    env6, cfg6 = new_env("others_archtypo", {
        "archtypo": {"repo": "testorg/archtypo", "type": "binary",
                     "asset.i386": r"^archtypo_[0-9.]+_linux_x86_64$"}})
    r = sync(cfg6)
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("★[P2-3 已修] asset.i386 不在 architectures 里 → 配置阶段就报错",
            r.code != 0 and "不在 architectures 里" in r.both, f"exit={r.code}")
    c.check("★ 错误信息给出当前 architectures 与后果",
            "amd64 arm64" in r.both and "不会被索引" in r.both,
            [l.strip() for l in r.both.splitlines() if "architectures" in l][:1])
    c.check("★ 没有产出无声孤儿(修复前 pool 里会躺着一个永远不被索引的包)",
            not pool_debs(env6), str(pool_debs(env6)))

    print("\n  --- 把架构加进 architectures 之后就能正常工作 ---")
    cfg6.write_text(cfg6.read_text().replace(
        "architectures = amd64 arm64", "architectures = amd64 arm64 i386"))
    r = sync(cfg6)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 补上 i386 后正常收录并进入索引",
            r.code == 0 and "1.0.0" in indexed_versions(env6, "archtypo", "i386"),
            f"i386 索引={indexed_versions(env6, 'archtypo', 'i386')}")

    print("\n  --- 非法正则 / 空正则也在配置阶段拦下 ---")
    for label, pat, want in [("正则语法错", "^archtypo_[0-9+$", "无法编译"),
                             ("正则留空", "", "是空的")]:
        env6b, cfg6b = new_env(f"others_badre_{abs(hash(label)) % 1000}", {
            "archtypo": {"repo": "testorg/archtypo", "type": "binary",
                         "asset.amd64": pat}})
        r = sync(cfg6b)
        c.check(f"★ {label} → 明确报错", r.code != 0 and want in r.both,
                f"exit={r.code}; " + (r.err.strip().splitlines() or ["(无)"])[0][:80])

    # ── 7. P2-5:state.json 丢失后 gc 会把整个仓库当垃圾清掉 ───────────
    print("\n=== 7. P2-5:state.json 异常 + gc —— 必须拒绝执行,绝不误删 ===")
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    fakegh.set_release("testorg", "toolbin", "v1.2.3", [fakegh.asset("toolbin_1.2.3_linux_x86_64.tar.gz")])
    env7, cfg7 = new_env("others_gcstateloss", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB},
        "toolbin": {"repo": "testorg/toolbin", "type": "binary",
                    "asset.amd64": r"^toolbin_[0-9.]+_linux_x86_64\.tar\.gz$"}})
    sync(cfg7)
    apt_env(env7)
    apt_update(env7, log=False)
    before = pool_debs(env7)

    # 四种"state 不可信"的形态,全部必须拒绝
    cases = [
        ("state 文件丢失", lambda: (env7 / "state.json").unlink()),
        ("state 文件损坏", lambda: (env7 / "state.json").write_text("{ broken")),
        ("state 是合法但空的 {}", lambda: (env7 / "state.json").write_text("{}")),
        ("state 合法但不含已配置项目",
         lambda: (env7 / "state.json").write_text('{"ghost": {"tag": "v1", "files": {}}}')),
    ]
    for label, mutate in cases:
        mutate()
        r = gc(cfg7)
        c.check(f"★ {label} → gc 拒绝执行且 pool 一个文件都没少",
                r.code != 0 and pool_debs(env7) == before,
                f"exit={r.code} pool={len(pool_debs(env7))}/{len(before)}; " +
                (r.both.strip().splitlines() or ["(无输出)"])[0][:90])
        r = gc(cfg7, dry_run=True)
        c.check(f"   {label} → --dry-run 同样拒绝(不给误导性清单)",
                r.code != 0 and "移除 pool/" not in r.out, f"exit={r.code}")

    print("\n  --- 客户端侧:仓库始终完好 ---")
    code, out, err = apt_update(env7, log=False)
    c.check("apt-get update 一直正常", code == 0, f"exit={code}")
    code, out, err = apt(env7, "-s", "install", "tooldeb")
    c.check("★ 客户端始终能装到 tooldeb(修复前这里会被清空成 Unable to locate)",
            code == 0 and "Inst tooldeb" in (out + err),
            [l for l in (out + err).splitlines() if "Inst tooldeb" in l][:1])

    print("\n  --- 恢复 state 之后,正常的 gc 仍然工作 ---")
    sync(cfg7)      # 重新收录,state 恢复
    env8, cfg8 = new_env("others_gc_ok", {
        "toolbin": {"repo": "testorg/toolbin", "type": "binary",
                    "asset.amd64": r"^toolbin_[0-9.]+_linux_x86_64\.tar\.gz$"}})
    shutil.rmtree(env8 / "repo")
    shutil.copytree(env7 / "repo", env8 / "repo")
    shutil.copy(env7 / "state.json", env8 / "state.json")
    r = gc(cfg8)
    print("  " + r.out.strip().replace("\n", "\n  "))
    c.check("★ 正常 gc 仍然能清掉已移除的项目(没有被保险误伤)",
            r.code == 0 and not any("tooldeb" in p for p in pool_debs(env8))
            and any("toolbin" in p for p in pool_debs(env8)), str(pool_debs(env8)))
    c.check("★ 清理后索引不再列 tooldeb,仓库仍可用",
            "tooldeb" not in packages_text(env8))
    apt_env(env8)
    code, out, err = apt_update(env8, log=False)
    c.check("gc 后客户端仍可 update", code == 0, f"exit={code}")

    # ── 8. P2-6:配置里一个段写错 → 全部项目都不同步 ───────────────────
    print("\n=== 8. P2-6:一个配置段写错,不该拖垮其它项目 ===")
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    env8, cfg8 = new_env("others_badconfig", {
        "good": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB},
        "broken": {"type": "deb", "asset.amd64": "^x$"},          # 缺 repo = 必填
        "broken2": {"repo": "testorg/x", "type": "nonsense",      # type 非法
                    "asset.amd64": "^x$"},
    })
    r = sync(cfg8)
    print("  " + r.out.strip().replace("\n", "\n  "))
    print("  [stderr] " + r.err.strip().replace("\n", "\n  "))
    c.check("★[P2-6 已修] 正常项目照常同步(不再整体 traceback 退出)",
            "[new] good" in r.out and r.code != 0, f"exit={r.code}")
    c.check("★ 坏段被逐个点名报出(而不是只报第一个)",
            "project:broken" in r.both and "project:broken2" in r.both)
    c.check("★ 退出码非 0(坏配置不会被静默忽略)",
            r.code != 0 and "good" in (r.err + r.out), f"exit={r.code}")
    c.check("★ 正常项目的包确实发布了",
            "tooldeb_1.0.0_amd64.deb" in str(pool_debs(env8)), str(pool_debs(env8)))
    apt_env(env8)
    code, out, err = apt_update(env8, log=False)
    c.check("客户端可正常使用", code == 0, f"exit={code}")

    print("\n  --- check / gc 需要完整清单,遇到坏段直接退出 ---")
    r = check_cmd(cfg8)
    c.check("★ check 拒绝在坏配置上运行(否则会给出不完整的版本报告)",
            r.code != 0 and "无法解析" in r.both, f"exit={r.code}")
    r = gc(cfg8)
    c.check("★ gc 同样拒绝(删除操作更不能建立在残缺的项目清单上)",
            r.code != 0 and "无法解析" in r.both, f"exit={r.code}")

    print("\n  --- 全部项目都坏掉时,明确说清没有可同步的项目 ---")
    env8b, cfg8b = new_env("others_allbroken", {
        "b1": {"type": "deb", "asset.amd64": "^x$"},
    })
    r = sync(cfg8b)
    c.check("★ 全坏 → 明确报错并退出,不静默成功",
            r.code != 0 and "没有可同步的项目" in r.both, f"exit={r.code}")

    print("\n  --- [settings] 段写错也给出干净报错(不再 traceback) ---")
    env8c, cfg8c = new_env("others_badsettings", {
        "good": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    cfg8c.write_text(cfg8c.read_text().replace("keep_versions = 2", "keep_versions = 两个"))
    r = sync(cfg8c)
    c.check("★ keep_versions 非数字 → [settings] 段报错而非 ValueError traceback",
            r.code != 0 and "settings" in r.both and "Traceback" not in r.both,
            f"exit={r.code}; " + (r.both.strip().splitlines() or ["(无)"])[0][:90])

    # ── 9. P3:keep_versions = 0 的语义 ───────────────────────────────
    print("\n=== 9. P3:keep_versions 的语义 ===")
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    env9, cfg9 = new_env("others_keep0", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}},
        keep_versions=0)
    r = sync(cfg9)
    print("  " + r.err.strip().replace("\n", "\n  "))
    c.check("★[P3 已修] keep_versions=0 被拒绝(旧行为是'一个都不清',与字面相反)",
            r.code != 0 and "keep_versions" in r.both and ">= 1" in r.both,
            f"exit={r.code}")
    c.check("★ 报错说明该用什么替代(而不是只拒绝)",
            "大数" in r.both and "gc" in r.both,
            [l.strip() for l in r.both.splitlines() if "keep_versions" in l][:1])

    print("\n  --- keep_versions=1 仍然正常工作(每个包留最新 1 个)---")
    cfg9.write_text(cfg9.read_text().replace("keep_versions = 0", "keep_versions = 1"))
    for tag, name in (("v1.0.0", "tooldeb_1.0.0_amd64.deb"),
                      ("v2.0.0", "tooldeb_2.0.0_amd64.deb"),
                      ("v3.0.0", "tooldeb_3.0.0_amd64.deb")):
        fakegh.set_release("testorg", "tooldeb", tag, [fakegh.asset(name)])
        sync(cfg9)
    c.check("keep_versions=1:pool 里只剩最新一版",
            [p.split("/")[-1] for p in pool_debs(env9)] == ["tooldeb_3.0.0_amd64.deb"],
            str([p.split('/')[-1] for p in pool_debs(env9)]))
    c.check("索引里也只有 3.0.0", indexed_versions(env9, "tooldeb") == ["3.0.0"])

    # ── 10. P3:pool 写入必须是原子替换 ───────────────────────────────
    print("\n=== 10. P3:pool 写入改成原子替换(os.replace),不再是原地截断 ===")
    env10, cfg10 = new_env("others_inplace", {
        "tooldeb": {"repo": "testorg/tooldeb", "type": "deb", "asset.amd64": RE_TOOLDEB}})
    fakegh.set_release("testorg", "tooldeb", "v1.0.0", [fakegh.asset("tooldeb_1.0.0_amd64.deb")])
    sync(cfg10)
    target = env10 / "repo/pool/main/t/tooldeb/tooldeb_1.0.0_amd64.deb"
    ino_before, sha_before = target.stat().st_ino, sha(target)
    other = fakegh.build_deb(A / "samever", "tooldeb", "1.0.0", "amd64", pad=9999)
    fakegh.set_release("testorg", "tooldeb", "v1.0.0",
                       [fakegh.asset("tooldeb_1.0.0_amd64.deb", path=other)])
    sync(cfg10, force=True)          # 同一个版本号、不同内容
    ino_after = target.stat().st_ino
    print(f"  inode: {ino_before} → {ino_after};sha256 变了: {sha(target) != sha_before}")
    c.check("内容确实被换了(sha256 变了)", sha(target) != sha_before)
    c.check("★[P3 已修] inode 变了 → 是 rename 替换,并发客户端不会读到半截文件",
            ino_after != ino_before, f"inode {ino_before} → {ino_after}")
    c.check("★ 没有留下 .new 临时文件",
            not list((env10 / "repo/pool").rglob("*.new")),
            str(list((env10 / "repo/pool").rglob("*.new"))))
    apt_env(env10)
    code, out, err = apt_update(env10, log=False)
    c.check("替换后仓库仍可用", code == 0, f"exit={code}")

    # ── 11. P3:dpkg 依赖 ─────────────────────────────────────────────
    print("\n=== 11. P3:dpkg 依赖(README 只说了 dpkg-deb)===")
    old_path = os.environ["PATH"]
    os.environ["PATH"] = "/nonexistent"
    try:
        ghapt.dpkg_vercmp("1.0", "2.0")
        res = "没有报错"
    except Exception as e:
        res = f"{type(e).__name__}: {e}"
    finally:
        os.environ["PATH"] = old_path
    print(f"  PATH 里没有 dpkg 时 dpkg_vercmp() → {res}")
    c.check("dpkg 缺失时是清晰的 FileNotFoundError(不是静默错误结果)",
            "FileNotFoundError" in res, res)
    setup = (Path(__file__).resolve().parent.parent / "setup.sh").read_text()
    c.check("★[P3 已修] setup.sh 现在显式安装 dpkg(不只是 gnupg)",
            "dpkg" in setup.split("apt-get install")[1].split("\n")[0],
            [l.strip() for l in setup.splitlines() if "apt-get install" in l])
    c.check("★ setup.sh 启动时检查必需的外部命令",
            "command -v" in setup or "检查依赖" in setup,
            [l.strip() for l in setup.splitlines() if "command -v" in l][:2])

    # ── 12. P2-10:非法版本号的比较行为 ──────────────────────────────
    print("\n=== 12. P3:dpkg_vercmp 对非法版本不再假装'更大' ===")
    for bad in ("bad", "", "...", "1.0 2.0"):
        try:
            v = ghapt.dpkg_vercmp(bad, "1.0")
            c.check(f"★ {bad!r} 被判为非法", False, f"没报错,返回 {v}")
        except RuntimeError:
            c.check(f"★ {bad!r} 被判为非法并报错", True)
    c.check("合法版本排序不受影响(回归)",
            ghapt.dpkg_vercmp("1.0.0", "2.0.0") == -1
            and ghapt.dpkg_vercmp("1.2.3~rc1", "1.2.3") == -1
            and ghapt.dpkg_vercmp("1.0.0", "1.0.0") == 0)
    c.check("--validate-version 能区分'合法/语法错/硬错'",
            ghapt.valid_version("1.0.0") and not ghapt.valid_version("bad")
            and not ghapt.valid_version("1.0 2.0"))
    bad_stage = A / "badver" / "DEBIAN"
    bad_stage.mkdir(parents=True, exist_ok=True)
    (bad_stage / "control").write_text("Package: badver\nVersion: bad\nArchitecture: amd64\n"
                                       "Maintainer: t <t@l>\nDescription: x\n")
    out = subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                          str(A / "badver"), str(A / "badver.deb")],
                         capture_output=True, text=True)
    c.check("可达性:dpkg-deb 本身也拒绝 'Version: bad'(deb 模式下不可达)",
            out.returncode != 0, (out.stderr or "").strip().splitlines()[:1])

    print(f"\n场景结果:{len(c.results) - len(c.failed)}/{len(c.results)} 通过")
    return c


if __name__ == "__main__":
    sys.exit(1 if main().failed else 0)
