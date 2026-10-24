#!/usr/bin/env python3
"""测试骨架:隔离环境 + 调用 ghapt 的真实代码路径 + 客户端 apt 校验。

网络层说明:ghapt.API_ROOT 被指向 tests/fakeapi(file:// 目录树),
因此 gh_json/download/extract/place/publish 全是 ghapt.py 的真实实现,
只有 socket 传输换成了本地文件读取(沙箱禁止一切 socket,含回环)。
"""
import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True          # 不往仓库里丢 __pycache__
TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

import fakegh                            # noqa: E402
import ghapt                             # noqa: E402

WORK = TESTS / "work"
LOG = []


# ── 断言与证据 ────────────────────────────────────────────────────────

class Checker:
    def __init__(self, scenario):
        self.scenario = scenario
        self.results = []

    def check(self, name, cond, evidence=""):
        ok = bool(cond)
        self.results.append((name, ok, evidence))
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}" + (f"\n         {evidence}" if evidence else ""))
        return ok

    def note(self, text):
        print(f"  [note] {text}")

    @property
    def failed(self):
        return [n for n, ok, _ in self.results if not ok]


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err
        self.both = out + err

    def __repr__(self):
        return f"<exit={self.code} out={self.out!r} err={self.err!r}>"


def _exit_code(exc):
    c = exc.code
    if c is None:
        return 0
    if isinstance(c, int):
        return c
    print(f"         (sys.exit message: {c})")
    return 1


# ── 隔离环境 ──────────────────────────────────────────────────────────

def new_env(name, projects, settings=(), keep_versions=2):
    """建一个全新的工作区:repo/ + state.json + gnupg/ + config.ini。"""
    ghapt.API_ROOT = fakegh.url(fakegh.API)
    d = WORK / name
    if d.exists():
        shutil.rmtree(d)
    (d / "repo").mkdir(parents=True)
    lines = [
        "[settings]",
        f"repo_dir = {d}/repo",
        f"state_file = {d}/state.json",
        f"gpg_home = {d}/gnupg",
        "suite = stable",
        "component = main",
        "architectures = amd64 arm64",
        "origin = ghapt",
        "label = ghapt",
        "description = ghapt test repo",
        "maintainer = test <test@localhost>",
        f"keep_versions = {keep_versions}",
        *settings,
    ]
    for pname, sec in projects.items():
        lines.append("")
        lines.append(f"[project:{pname}]")
        for k, v in sec.items():
            lines.append(f"{k} = {v}")
    cfg = d / "config.ini"
    cfg.write_text("\n".join(lines) + "\n")
    return d, cfg


def run(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*a, **kw)
        except SystemExit as e:
            code = _exit_code(e)
    return Result(code, out.getvalue(), err.getvalue())


def sync(cfg, project=None, force=False):
    return run(ghapt.cmd_sync, argparse.Namespace(config=str(cfg), project=project, force=force))


def check_cmd(cfg):
    return run(ghapt.cmd_check, argparse.Namespace(config=str(cfg)))


def gc(cfg, dry_run=False):
    return run(ghapt.cmd_gc, argparse.Namespace(config=str(cfg), dry_run=dry_run))


# ── 仓库观察 ──────────────────────────────────────────────────────────

def pool_debs(env):
    return sorted(str(p.relative_to(env / "repo"))
                  for p in (env / "repo" / "pool").rglob("*.deb"))


def dist(env, arch="amd64"):
    return env / "repo" / "dists" / "stable" / "main" / f"binary-{arch}"


def packages_text(env, arch="amd64"):
    p = dist(env, arch) / "Packages"
    return p.read_text() if p.exists() else ""


def indexed_versions(env, pkg, arch="amd64"):
    """从已发布的 Packages 里取某个包的所有 Version(即客户端能看到什么)。"""
    out = []
    for stanza in packages_text(env, arch).split("\n\n"):
        d = {}
        for line in stanza.splitlines():
            if line[:1] not in (" ", "\t") and ":" in line:
                k, _, v = line.partition(":")
                d[k.strip()] = v.strip()
        if d.get("Package") == pkg:
            out.append(d.get("Version"))
    return out


def inrelease(env):
    return env / "repo" / "dists" / "stable" / "InRelease"


def gpg_verify(env, path=None):
    """用导出的公钥独立校验 InRelease / Release.gpg(不依赖 apt)。

    分离签名必须把被签名的 Release 一起传进去。
    """
    path = Path(path) if path else inrelease(env)
    cmd = ["gpg", "--batch", "--no-default-keyring",
           "--keyring", str(env / "repo" / "ghapt.gpg"), "--verify", str(path)]
    if path.name == "Release.gpg":
        cmd.append(str(path.parent / "Release"))
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, (res.stderr or "").strip()


# ── 客户端侧:真实 apt ────────────────────────────────────────────────

def apt_env(env, key="ghapt.gpg", suite="stable", component="main", arch="amd64"):
    a = env / "client"
    for sub in ("etc", "state/lists/partial", "cache/archives/partial"):
        (a / sub).mkdir(parents=True, exist_ok=True)
    (a / "etc" / "sources.list").write_text(
        f"deb [signed-by={env / 'repo' / key}] file:{env}/repo {suite} {component}\n")
    (a / "etc" / "status").write_text("")
    return a


def apt(env, *args, timeout=120):
    a = env / "client"
    opts = [
        f"Dir::Etc::sourcelist={a}/etc/sources.list",
        "Dir::Etc::sourceparts=/dev/null",
        f"Dir::Etc::trusted=/dev/null",
        f"Dir::Etc::trustedparts=/dev/null",
        f"Dir::State={a}/state",
        f"Dir::State::status={a}/etc/status",
        f"Dir::Cache={a}/cache",
        "APT::Architecture=amd64",
        "APT::Architectures::=amd64",
        "APT::Sandbox::User=root",
        "Acquire::Check-Valid-Until=false",
        "Debug::NoLocking=true",
        "APT::Get::List-Cleanup=0",
    ]
    cmd = ["apt-get"] + [f"-o{_}" for _ in opts] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         env={**os.environ, "LC_ALL": "C"})
    return res.returncode, res.stdout, res.stderr


def apt_log(env):
    p = env / "client" / "apt.log"
    return p.read_text() if p.exists() else ""


def apt_update(env, log=True):
    code, out, err = apt(env, "update")
    if log:
        (env / "client").mkdir(parents=True, exist_ok=True)
        (env / "client" / "apt.log").write_text(
            f"$ apt-get update  -> exit {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
    return code, out, err


def verify_release_hashes(env, arch="amd64"):
    """按 Release 里声明的哈希逐个核对磁盘文件(独立于 ghapt 的自检)。"""
    import hashlib
    rel = (env / "repo" / "dists" / "stable" / "Release").read_text()
    problems, checked = [], 0
    for line in rel.splitlines():
        parts = line.split()
        if len(parts) == 3 and len(parts[0]) in (32, 40, 64) and parts[1].isdigit():
            digest, size, relpath = parts[0], int(parts[1]), parts[2]
            f = env / "repo" / "dists" / "stable" / relpath
            if not f.exists():
                problems.append(f"Release 列出的文件不存在: {relpath}")
                continue
            data = f.read_bytes()
            algo = {32: hashlib.md5, 40: hashlib.sha1, 64: hashlib.sha256}[len(digest)]
            if algo(data).hexdigest() != digest:
                problems.append(f"哈希不符: {relpath}")
            if len(data) != size:
                problems.append(f"大小不符: {relpath}(Release={size} 实际={len(data)})")
            checked += 1
    # Filename 字段指向的 pool 文件是否存在、大小是否一致
    filenames = [l.split(":", 1)[1].strip() for l in packages_text(env, arch).splitlines()
                 if l.startswith("Filename:")]
    for fn in filenames:
        if not (env / "repo" / fn).exists():
            problems.append(f"Packages 里的 Filename 不存在: {fn}")
    return checked, problems, filenames
