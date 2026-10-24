#!/usr/bin/env python3
"""ghapt — 把 GitHub releases 同步成自托管、带 GPG 签名的 APT 仓库。

只依赖 Python 3.8+ 标准库,外加系统自带的 dpkg-deb 与 gpg。

每个项目两种收录模式:
  type = deb     release 里直接提供 .deb,原样收录
  type = binary  release 只有二进制 tarball/zip,自动重打包成 .deb

用法:
  ghapt.py sync  [-c config.ini] [--project NAME] [--force]
  ghapt.py check [-c config.ini]
  ghapt.py assets owner/repo [--prerelease]
"""

import argparse
import configparser
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path, PurePosixPath

API_ROOT = "https://api.github.com"
USER_AGENT = "ghapt/1.0"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")
# tarfile 原生不支持 zstd(Python 3.14 才有),用系统 zstd 先解一层
ZSTD_SUFFIXES = (".tar.zst", ".tzst")
ELF_MAGIC = b"\x7fELF"
PATH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._~-]*$")


def _config_path_part(value, label):
    """Validate a configuration value used as one path component."""
    if not isinstance(value, str) or not PATH_PART_RE.fullmatch(value):
        raise ConfigError(
            f"{label} 不是安全的单一路径组成部分: {value!r}")
    return value


# ── 配置 ──────────────────────────────────────────────────────────────

class ConfigError(ValueError):
    """配置文件里的某个值/段有问题(区别于运行期故障)。"""


class Settings:
    def __init__(self, cp):
        s = cp["settings"] if cp.has_section("settings") else {}
        self.repo_dir = Path(s.get("repo_dir", "/srv/ghapt/repo"))
        self.state_file = Path(s.get("state_file", str(self.repo_dir.parent / "state.json")))
        self.gpg_home = Path(s.get("gpg_home", str(self.repo_dir.parent / "gnupg")))
        self.suite = _config_path_part(s.get("suite", "stable"), "suite")
        self.component = _config_path_part(s.get("component", "main"), "component")
        self.architectures = s.get("architectures", "amd64 arm64").split()
        if not self.architectures:
            raise ConfigError("architectures 不能为空")
        if len(set(self.architectures)) != len(self.architectures):
            raise ConfigError("architectures 不能包含重复项")
        for arch in self.architectures:
            _config_path_part(arch, "architecture")
        self.origin = s.get("origin", "ghapt")
        self.label = s.get("label", "ghapt")
        self.description = s.get("description", "Self-hosted APT repo synced from GitHub releases")
        self.maintainer = s.get("maintainer", "ghapt <ghapt@localhost>")
        self.keep_versions = self._int_setting(s, "keep_versions", 2, minimum=1)
        self.key_name = s.get("key_name", "ghapt archive signing key")
        self.key_email = s.get("key_email", "ghapt@localhost")
        self.token = s.get("token", "") or os.environ.get("GITHUB_TOKEN", "")

    @staticmethod
    def _int_setting(sec, key, default, minimum=None):
        raw = sec.get(key)
        if raw is None or not str(raw).strip():
            return default
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ConfigError(f"{key} 必须是整数(当前 {raw!r})")
        if minimum is not None and value < minimum:
            # keep_versions=0 的旧行为是"一个都不清",与字面意思相反;
            # 真按字面执行则会把刚发布的包也删掉,所以这里只接受 >= 1
            raise ConfigError(
                f"{key} 必须 >= {minimum}(当前 {value})。"
                f"它表示每个包在 pool 里保留几个版本;"
                f"想彻底不清理就设一个大数,想清空某个包请直接从配置里删项目再跑 gc。")
        return value


class Project:
    def __init__(self, name, sec, architectures=None):
        self.name = name
        self.repo = sec.get("repo", "").strip()
        if "/" not in self.repo:
            raise ConfigError(f"缺少有效的 repo = owner/name")
        self.type = sec.get("type", "deb").strip()
        if self.type not in ("deb", "binary"):
            raise ConfigError(f"type 必须是 deb 或 binary(当前 {self.type!r})")
        self.package = _config_path_part(
            sec.get("package", name).strip(), "package")
        self.assets = {k[len("asset."):]: sec[k].strip()
                       for k in sec if k.startswith("asset.")}
        if not self.assets:
            raise ConfigError(f"至少需要一条 asset.<arch> = <正则>")
        for arch in self.assets:
            _config_path_part(arch, "asset 架构")

        # asset.<arch> 的键必须出现在 architectures 里。
        # binary 模式下这个键会被直接写进 control 的 Architecture,
        # 于是打出一个 architectures 不认的包:文件躺在 pool 里占地方,
        # 却进不了任何索引,客户端永远看不到 —— 而且全程没有任何报错。
        if architectures is not None:
            unknown = sorted(a for a in self.assets if a not in architectures)
            if unknown:
                raise ConfigError(
                    f"asset.{unknown[0]} 不在 architectures 里"
                    f"(当前 architectures = {' '.join(architectures)})。"
                    f"{'这些' if len(unknown) > 1 else '该'}架构不会被索引,"
                    f"包只会白占 pool 空间;请改对键名,或把该架构加进 "
                    f"[settings] architectures。")

        # 正则要能编译,且必须能匹配到东西(re.search("") 会匹配一切)
        for arch, pattern in self.assets.items():
            if not pattern:
                raise ConfigError(f"asset.{arch} 是空的")
            try:
                re.compile(pattern)
            except re.error as e:
                raise ConfigError(f"asset.{arch} 的正则无法编译: {e}({pattern!r})")

        self.strip_prefix = sec.get("strip_prefix", "v")
        self.prerelease = sec.get("prerelease", "false").lower() in ("1", "true", "yes")
        self.description = sec.get(
            "description", f"{self.package} (packaged from github.com/{self.repo} by ghapt)")
        self.binaries = (sec.get("binaries", "") or self.package).split()
        self.depends = sec.get("depends", "").strip()
        self.section = sec.get("section", "utils")


def load_config(path, strict=True):
    """读配置。

    单个 [project:*] 段写错时:strict=True 直接退出(用于 check/gc —— 它们
    需要一份完整的项目清单);strict=False 跳过坏段并继续(用于 sync ——
    某个项目配置写错不应该让其它项目都同步不了)。
    """
    cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    try:
        if not cp.read(path, encoding="utf-8"):
            sys.exit(f"读不到配置文件: {path}")
    except (configparser.Error, UnicodeError) as e:
        sys.exit(f"配置文件无法解析: {path}: {e}")

    try:
        st = Settings(cp)
    except (ValueError, TypeError) as e:
        sys.exit(f"[settings] 段配置有问题: {e}")

    projects, broken = [], []
    for sec in cp.sections():
        if not sec.startswith("project:"):
            continue
        name = sec.split(":", 1)[1].strip()
        try:
            projects.append(Project(name, cp[sec], st.architectures))
        except ConfigError as e:
            broken.append((name, str(e)))

    for name, msg in broken:
        print(f"[config] [project:{name}] 配置有问题: {msg}", file=sys.stderr)
    if broken and strict:
        sys.exit(f"配置里有 {len(broken)} 个项目段无法解析,已退出"
                 f"(涉及: {', '.join(n for n, _ in broken)})")
    return st, projects, broken


# ── GitHub API 与下载 ─────────────────────────────────────────────────

def with_retry(fn, attempts=3, delay=3):
    for i in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404) or i == attempts - 1:
                raise
        except OSError as e:  # URLError / SSL / socket 超时等
            if i == attempts - 1:
                raise
            print(f"    网络错误,{delay}s 后重试: {e}", file=sys.stderr)
        time.sleep(delay)


def gh_json_response(path, token):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token

    def go():
        url = path if "://" in path else API_ROOT + path
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp), resp.headers
    return with_retry(go)


def gh_json(path, token):
    return gh_json_response(path, token)[0]


def _next_link(headers):
    raw = headers.get("Link", "") if headers else ""
    for part in raw.split(","):
        match = re.search(r"<([^>]+)>\s*;\s*rel=\"([^\"]+)\"", part)
        if match and "next" in {x.strip() for x in match.group(2).split()}:
            return match.group(1)
    return None


def latest_release(project, token):
    if project.prerelease:
        path = f"/repos/{project.repo}/releases?per_page=10"
        seen = set()
        while path and path not in seen:
            seen.add(path)
            rels, headers = gh_json_response(path, token)
            if not isinstance(rels, list):
                raise RuntimeError("GitHub releases API 返回了非列表数据")
            usable = [r for r in rels if not r.get("draft")]
            if usable:
                return usable[0]
            path = _next_link(headers)
        raise RuntimeError("没有找到任何 release")
    return gh_json(f"/repos/{project.repo}/releases/latest", token)


def parse_digest(digest):
    """GitHub 的 asset digest(`sha256:<hex>`)→ hex,非 sha256/缺失则返回 None。

    2025-06 起 release 资产自带不可变 digest,校验它几乎不花成本,
    能挡住"尺寸相同但内容被换掉"的情况(只比 size 是挡不住的)。
    """
    if not digest or not isinstance(digest, str):
        return None
    algo, _, hexdigest = digest.partition(":")
    if algo.lower() != "sha256" or len(hexdigest) != 64:
        return None
    return hexdigest.lower()


def sha256_of(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_elf(path: Path):
    """是不是 ELF 可执行文件。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == ELF_MAGIC
    except OSError:
        return False


# 常见压缩/归档容器的魔数。用来识别"解包失败、把压缩包本身当成了程序"。
# 注意只列容器格式:shell 脚本、Python 脚本、ELF 都是合法的可执行产物,
# 不能因为"不是 ELF"就拒绝(很多项目发的就是脚本)。
CONTAINER_MAGICS = (
    (b"\x1f\x8b", "gzip"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"PK\x03\x04", "zip"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"LZIP", "lzip"),
    (b"\x1f\x9d", "compress"),
)


def container_kind(path: Path):
    """这个文件是不是一个压缩/归档容器?是则返回格式名,否则 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            for magic, name in CONTAINER_MAGICS:
                if head.startswith(magic):
                    return name
            f.seek(257)
            if f.read(5) == b"ustar":
                return "tar"
    except OSError:
        return None
    return None


def download(url, dest: Path, expect_size=None, expect_sha256=None):
    """下载资产。资产 URL 会 302 到 CDN,带上多余的认证头反而会被拒,这里只带 UA。

    校验顺序:size(便宜)→ sha256(权威)。两者不符都当作可重试的网络问题。
    """
    def go():
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 256)
        if expect_size is not None and tmp.stat().st_size != expect_size:
            tmp.unlink(missing_ok=True)
            raise OSError(f"下载不完整: {url}")
        if expect_sha256 is not None:
            actual = sha256_of(tmp)
            if actual != expect_sha256:
                tmp.unlink(missing_ok=True)
                raise OSError(
                    f"下载内容与 GitHub 公布的 sha256 不符: {url}\n"
                    f"    期望 {expect_sha256}\n    实际 {actual}")
        os.replace(tmp, dest)
    with_retry(go)


# ── deb 工具 ──────────────────────────────────────────────────────────

def parse_control(text):
    """control 文本 → [[字段名, 值]],多行值(如 Description)的续行保留在值里。"""
    fields = []
    for line in text.rstrip("\n").split("\n"):
        if not line:
            continue
        if line[0] in " \t" and fields:
            fields[-1][1] += "\n" + line
        else:
            name, _, val = line.partition(":")
            fields.append([name.strip(), val.strip()])
    return fields


def run_checked(cmd, binary=False, **kw):
    """跑子进程,失败时把 stderr 带进异常消息。

    `check=True` 抛出的 CalledProcessError 本身**不含 stderr**,而 ghapt 的
    deb 内容、gpg 签名都依赖这些工具;沙箱里 gpg 失败时,日志只有
    "returned non-zero exit status 2",真正的原因(dpkg-deb/gpg 的报错)
    全都看不到。这里统一补上。

    binary=True 用于输出是二进制的情况(如 `gpg --export`),此时不做文本解码。
    """
    if binary:
        proc = subprocess.run(cmd, capture_output=True, **kw)
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
        err = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode != 0:
        if len(err) > 600:
            err = err[:600] + " ...(截断)"
        raise RuntimeError(
            f"命令失败 ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"    {err or '(无输出)'}")
    return proc


def deb_control(path):
    out = run_checked(["dpkg-deb", "-f", str(path)]).stdout
    fields = parse_control(out)
    return fields, dict(fields)


class VersionError(ValueError):
    """tag 无法映射成一个可用的 debian 版本号(配置问题,不是运行时故障)。"""


def deb_version_from_tag(tag, strip_prefix, project_name=None):
    """把 release tag 映射成 debian 版本号。

    两条容易出错、且**失败方向相反**的规则:

    * `-` 后面跟字母 → `~`(debian 的预发布语义)。`v1.2.3-rc1` 得到 `1.2.3~rc1`,
      排序上 **低于** `1.2.3` —— 否则 apt 会把预发布版当成比正式版还新的升级目标。
      `-` 后面跟数字(如 `v1.2.3-1` 的 debian revision)保持原有行为,转成 `.`。
    * 版本号必须以数字开头。`strip_prefix` 没命中时会走到这里,例如上游把 tag 从
      `v2.0.0` 改成 `pfx-2.0.0`、而配置里还是默认的 `strip_prefix = v`。
      旧行为是默默生成 `0.pfx.2.0.0`(比现有版本还低),于是日志报"已同步新版本"
      但客户端永远不升级;现在直接报错并提示怎么改。
    """
    v = tag
    if strip_prefix and tag.startswith(strip_prefix):
        v = tag[len(strip_prefix):]
    v = re.sub(r"-(?=[A-Za-z])", "~", v)          # 预发布:1.2.3-rc1 → 1.2.3~rc1
    v = re.sub(r"[^A-Za-z0-9.+~]", ".", v).strip(".")  # 其余非法字符转 '.'
    if not v or not v[0].isdigit():
        who = f"[project:{project_name}] " if project_name else ""
        prefix_hint = (f"如果这个项目的 tag 以别的前缀开头,把 strip_prefix 设成它"
                       f"(当前是 {strip_prefix!r})"
                       if strip_prefix else "该 tag 无法直接用作版本号")
        raise VersionError(
            f"{who}tag {tag!r} 去掉前缀 {strip_prefix!r} 后得到 {v!r},不是以数字开头的"
            f"合法版本号。{prefix_hint};或者给项目加一个显式的版本号映射。")
    return v


def valid_version(v):
    """版本号是否是合法的 debian 版本。

    用 `dpkg --validate-version`:它对"语法错误"(如 `bad`、`...`)返回 1,
    对"硬错误"(如含空格)返回 2,合法返回 0。
    不能用 `--compare-versions v eq v` —— 它对非法版本同样返回 0,区分不出来。
    """
    r = subprocess.run(["dpkg", "--validate-version", v],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def dpkg_vercmp(a, b):
    """比较两个 debian 版本号。

    dpkg 对"比较为假"和"版本号非法"都返回 1,单看 returncode 区分不了,
    所以非法版本会被静默当成"更大"(结果就是垃圾版本永远排在最前、
    永远不被 keep_versions 清理)。这里先校验,非法直接报错。
    """
    for v in (a, b):
        if not valid_version(v):
            raise RuntimeError(
                f"非法 debian 版本号 {v!r}(版本号必须以数字开头,"
                f"只能含 [A-Za-z0-9.+~-] 与 ':' 形式的 epoch)")
    if a == b:
        return 0
    lt = subprocess.run(["dpkg", "--compare-versions", a, "lt", b],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return -1 if lt.returncode == 0 else 1


def file_hashes(path: Path):
    md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            for h in (md5, sha1, sha256):
                h.update(chunk)
    return size, md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest()


# ── binary 模式:从压缩包提取可执行文件并打成 deb ─────────────────────

def _extract_from_tar(tf, wanted, dest, found):
    for m in tf:
        base = os.path.basename(m.name)
        if m.isfile() and base in wanted and base not in found:
            with tf.extractfile(m) as src, open(dest / base, "wb") as f:
                shutil.copyfileobj(src, f)
            found[base] = dest / base


def _zstd_to_tar(asset: Path, workdir: Path) -> Path:
    """用系统 zstd 把 .tar.zst 解成 .tar(tarfile 自己不认识 zstd)。

    zstd 缺失或解压失败时给出明确指引,而不是让它落进"裸二进制"分支。
    """
    tar_path = workdir / (asset.name.rsplit(".", 1)[0] + ".tar")
    try:
        run_checked(["zstd", "-d", "-q", "-f", "-o", str(tar_path), str(asset)])
    except FileNotFoundError:
        raise RuntimeError(
            f"{asset.name} 是 zstd 压缩包,但系统里没有 zstd 命令。"
            f"请先安装(apt install zstd),或换用其他格式的资产。")
    return tar_path


def extract_binaries(asset: Path, wanted, dest: Path):
    found = {}
    lower = asset.name.lower()
    if lower.endswith(ARCHIVE_SUFFIXES):
        with tarfile.open(asset, "r:*") as tf:
            _extract_from_tar(tf, wanted, dest, found)
    elif lower.endswith(ZSTD_SUFFIXES):
        with tarfile.open(_zstd_to_tar(asset, dest.parent), "r:") as tf:
            _extract_from_tar(tf, wanted, dest, found)
    elif lower.endswith(".zip"):
        with zipfile.ZipFile(asset) as zf:
            for info in zf.infolist():
                base = os.path.basename(info.filename)
                if not info.is_dir() and base in wanted and base not in found:
                    with zf.open(info) as src, open(dest / base, "wb") as f:
                        shutil.copyfileobj(src, f)
                    found[base] = dest / base
    elif lower.endswith(".gz"):
        with gzip.open(asset, "rb") as src, open(dest / wanted[0], "wb") as f:
            shutil.copyfileobj(src, f)
        found[wanted[0]] = dest / wanted[0]
    else:  # 裸二进制
        shutil.copy2(asset, dest / wanted[0])
        found[wanted[0]] = dest / wanted[0]

    missing = [w for w in wanted if w not in found]
    if missing:
        raise RuntimeError(f"资产里找不到可执行文件: {', '.join(missing)}")

    # 无论走哪条分支,最后都确认拿到的不是"压缩包本身"。
    # 否则不认识的新格式(.tar.zst 曾如此)会被当成裸二进制原样装进 /usr/bin,
    # 客户端装上一个跑不起来的"程序",而这边一切日志都正常。
    # 只拦容器格式:shell/python 脚本、ELF 都是合法产物。
    for name, p in found.items():
        kind = container_kind(p)
        if kind:
            raise RuntimeError(
                f"{asset.name} 里取出的 {name!r} 是一个 {kind} 压缩包,不是可执行文件。"
                f"多半是这个压缩格式还没被支持(当前支持 tar.gz/tgz/tar.xz/tar.bz2/tar/"
                f"tar.zst/tzst/zip/gz 和裸二进制);请检查该资产的打包方式,"
                f"或在配置里改用其它资产。")
        os.chmod(p, 0o755)


def build_deb(project, version, arch, asset: Path, workdir: Path, st: Settings) -> Path:
    stage = workdir / f"stage-{arch}"
    bindir = stage / "usr" / "bin"
    bindir.mkdir(parents=True)
    extract_binaries(asset, project.binaries, bindir)
    installed_kb = sum(p.stat().st_size for p in bindir.iterdir()) // 1024 + 1

    control = [
        f"Package: {project.package}",
        f"Version: {version}",
        f"Architecture: {arch}",
        f"Maintainer: {st.maintainer}",
        f"Installed-Size: {installed_kb}",
        f"Section: {project.section}",
        "Priority: optional",
        f"Homepage: https://github.com/{project.repo}",
    ]
    if project.depends:
        control.append(f"Depends: {project.depends}")
    control.append(f"Description: {project.description}")
    (stage / "DEBIAN").mkdir()
    (stage / "DEBIAN" / "control").write_text("\n".join(control) + "\n")

    out = workdir / f"{project.package}_{version}_{arch}.deb"
    run_checked(["dpkg-deb", "--build", "--root-owner-group", "-Zxz",
                 str(stage), str(out)])
    return out


# ── pool 管理 ─────────────────────────────────────────────────────────

def prune_pool_plan(pooldir: Path, pkg, arch, keep):
    """算出该清理哪几个旧版本,但**不删除**任何文件(删除必须等新索引发布成功)。"""
    if keep <= 0:
        return []

    pkg = _path_part(pkg, "Package")
    arch = _path_part(arch, "Architecture")
    files = []
    for p in pooldir.glob("*.deb"):
        version, file_arch = _pool_deb_parts(p, pkg)
        if file_arch == arch:
            files.append((p, version))
    files.sort(key=cmp_to_key(lambda a, b: dpkg_vercmp(a[1], b[1])))
    return [p for p, _version in files[:-keep]]


def prune_candidates(st):
    """当前 component 范围内按 keep_versions 应当清理的文件(仍然不删除)。"""
    component = _path_part(st.component, "component")
    pool = st.repo_dir / "pool" / component
    if st.keep_versions <= 0 or not pool.is_dir():
        return []
    out = []
    for pooldir in sorted({p.parent for p in pool.rglob("*.deb")}):
        pkg = _path_part(pooldir.name, "Package")
        arches = set()
        for p in pooldir.glob("*.deb"):
            _version, arch = _pool_deb_parts(p, pkg)
            arches.add(arch)
        for arch in sorted(arches):
            out += prune_pool_plan(pooldir, pkg, arch, st.keep_versions)
    return out


def _path_part(value, label):
    """Validate a value that is used as one directory or file-name component."""
    if (not isinstance(value, str) or not value or value in (".", "..")
            or os.path.isabs(value) or "/" in value or "\\" in value):
        raise RuntimeError(f"{label} 不是安全的单一路径组成部分: {value!r}")
    return value


def _pool_deb_parts(path, pkg):
    """Parse the canonical pool filename and return (version, architecture)."""
    prefix = f"{pkg}_"
    suffix = ".deb"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        raise RuntimeError(
            f"pool 中的 deb 文件名格式错误: {path}"
            f"(期望 {pkg}_<version>_<architecture>.deb)")
    body = name[len(prefix):-len(suffix)]
    try:
        encoded_version, arch = body.rsplit("_", 1)
    except ValueError as e:
        raise RuntimeError(
            f"pool 中的 deb 文件名格式错误: {path}"
            f"(缺少版本或架构)") from e
    if not encoded_version or not arch:
        raise RuntimeError(
            f"pool 中的 deb 文件名格式错误: {path}"
            f"(版本或架构为空)")
    arch = _path_part(arch, "Architecture")
    version = encoded_version.replace("%3a", ":")
    if not valid_version(version):
        raise RuntimeError(
            f"pool 中的 deb 文件名包含非法版本 {version!r}: {path}")
    return version, arch


def _pool_target(st, metadata):
    pkg = _path_part(metadata["Package"], "Package")
    ver = metadata["Version"]
    if not isinstance(ver, str) or not valid_version(ver):
        raise RuntimeError(f"Version 不是合法 Debian 版本: {ver!r}")
    arch = _path_part(metadata["Architecture"], "Architecture")
    component = _path_part(st.component, "component")
    pooldir = st.repo_dir / "pool" / component / pkg[0].lower() / pkg
    target = pooldir / f"{pkg}_{ver.replace(':', '%3a')}_{arch}.deb"
    root = st.repo_dir.resolve()
    if root not in target.resolve().parents:
        raise RuntimeError(f"deb 目标路径超出仓库目录: {target}")
    return target, target.relative_to(st.repo_dir).as_posix()


def validate_deb_metadata(deb, project, asset_arch, expected_version=None):
    """Check that an incoming deb belongs to the configured project."""
    try:
        _, metadata = deb_control(deb)
    except (KeyError, RuntimeError) as e:
        raise RuntimeError(f"无法读取 deb 元数据 {deb.name}: {e}") from e
    for field in ("Package", "Version", "Architecture"):
        if not metadata.get(field):
            raise RuntimeError(f"{deb.name} 缺少 control 字段 {field}")
    if metadata["Package"] != project.package:
        raise RuntimeError(
            f"{deb.name} 的 Package={metadata['Package']!r},"
            f"配置期望 {project.package!r}")
    if metadata["Architecture"] not in (asset_arch, "all"):
        raise RuntimeError(
            f"{deb.name} 的 Architecture={metadata['Architecture']!r},"
            f"资产架构键期望 {asset_arch!r} (允许 all)")
    if not valid_version(metadata["Version"]):
        raise RuntimeError(
            f"{deb.name} 的 Version={metadata['Version']!r} 不是合法 Debian 版本")
    if expected_version is not None and metadata["Version"] != expected_version:
        raise RuntimeError(
            f"{deb.name} 的 Version={metadata['Version']!r},"
            f"重打包期望 {expected_version!r}")
    return metadata


def _stage_and_commit_debs(st, entries):
    """Stage every deb for a project, then commit all pool files as one unit."""
    st.repo_dir.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".ghapt-project-", dir=str(st.repo_dir)))
    records = []
    committed = []
    targets = {}
    try:
        for arch, deb, metadata in entries:
            target, rel = _pool_target(st, metadata)
            if target in targets:
                previous_arch = targets[target]
                raise RuntimeError(
                    f"多个资产架构({previous_arch}、{arch})映射到同一个 pool 文件: {rel}")
            targets[target] = arch
            staged = stage_root / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(deb, staged)
            records.append((arch, staged, target, rel))

        # Prepare all backups before changing pool. If a copy fails, no target
        # has been changed yet.
        backup_root = stage_root / ".backups"
        backup_root.mkdir()
        backups = {}
        changes = {}
        for arch, staged, target, rel in records:
            same = target.exists() and file_hashes(target)[3] == file_hashes(staged)[3]
            changes[arch] = not same
            if same:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[target] = backup

        try:
            for record in records:
                arch, staged, target, rel = record
                if changes[arch]:
                    os.replace(staged, target)
                    committed.append((target, backups.get(target)))
        except BaseException as e:
            rollback_errors = []
            for target, backup in reversed(committed):
                try:
                    if backup is not None and backup.exists():
                        os.replace(backup, target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    f"pool 提交失败且回滚也失败: {e}; "
                    f"{'; '.join(rollback_errors)}") from e
            raise

        return changes, {arch: rel for arch, _staged, _target, rel in records}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def place_deb(deb: Path, st: Settings, project=None, asset_arch=None,
              expected_version=None):
    """把一个 deb 收进 pool(兼容旧调用方,项目同步使用批量提交)。"""
    _, metadata = deb_control(deb)
    if project is not None:
        if asset_arch is None:
            raise ValueError("asset_arch is required when project is supplied")
        validate_deb_metadata(deb, project, asset_arch, expected_version)
    changes, paths = _stage_and_commit_debs(st, [(asset_arch, deb, metadata)])
    arch = asset_arch if asset_arch is not None else metadata["Architecture"]
    return changes[arch], paths[arch]


# ── 同步单个项目 ──────────────────────────────────────────────────────

def sync_project(project, st, state, force):
    rel = latest_release(project, st.token)
    tag = rel.get("tag_name") or ""
    prev = state.get(project.name, {})
    files_ok = bool(prev.get("files")) and all(
        (st.repo_dir / f).exists() for f in prev["files"].values())
    if not force and prev.get("tag") == tag and files_ok:
        print(f"[ok] {project.name}: 已是最新 ({tag})")
        return []

    assets = rel.get("assets", [])
    picked = {}
    for arch, pattern in project.assets.items():
        matches = [a for a in assets if re.search(pattern, a["name"])]
        if len(matches) != 1:
            names = "\n    ".join(a["name"] for a in assets) or "(空)"
            raise RuntimeError(
                f"{arch} 的正则 {pattern!r} 匹配到 {len(matches)} 个资产,应为 1 个。"
                f"该 release 的资产列表:\n    {names}")
        picked[arch] = matches[0]

    version = (deb_version_from_tag(tag, project.strip_prefix, project.name)
               if project.type == "binary" else None)
    with tempfile.TemporaryDirectory(prefix="ghapt-") as tmp:
        tmpdir = Path(tmp)
        # 两阶段:先把该项目的所有架构都下载/打包成功,再动 pool。
        # 否则 arm64 失败时 amd64 已经落盘、而 state 又不会推进,
        # 就会留下一个永远进不了索引的孤儿文件(见 tests/REPORT.md P1-1 A/B)。
        staged = []
        for arch, asset in picked.items():
            print(f"    下载 {asset['name']} ({asset.get('size', 0) // 1024} KiB)")
            dest = tmpdir / asset["name"]
            digest = parse_digest(asset.get("digest"))
            if digest is None and asset.get("digest"):
                print(f"    (该资产的 digest 不是 sha256,跳过内容校验: "
                      f"{asset['digest']!r})", file=sys.stderr)
            download(asset["browser_download_url"], dest, asset.get("size"), digest)
            deb = dest if project.type == "deb" else build_deb(
                project, version, arch, dest, tmpdir, st)
            metadata = validate_deb_metadata(
                deb, project, arch, expected_version=version)
            staged.append((arch, deb, metadata))

        changed_by_arch, relpaths = _stage_and_commit_debs(st, staged)
        changed, new_files, touched = False, {}, []
        for arch, _deb, _metadata in staged:
            c = changed_by_arch[arch]
            relpath = relpaths[arch]
            changed |= c
            new_files[arch] = relpath
            if c:
                touched.append(relpath)

    state[project.name] = {"tag": tag, "files": new_files}
    if changed:
        print(f"[new] {project.name}: {tag} → " +
              ", ".join(Path(f).name for f in new_files.values()))
    else:
        # 内容与 pool 里已有的完全一致(例如重跑、state 丢失后重来),
        # 不能谎报 [new]:索引是否更新由后面的内容比对决定。
        print(f"[same] {project.name}: {tag} 已在 pool 中,内容未变")
    return touched


# ── 仓库索引生成与签名 ────────────────────────────────────────────────

def gz_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def rfc2822_now():
    now = datetime.now(timezone.utc)
    return (f"{DAYS[now.weekday()]}, {now.day:02d} {MONTHS[now.month - 1]} "
            f"{now.year} {now:%H:%M:%S} UTC")


HASH_CACHE_NAME = "index-cache.json"


def load_hash_cache(st):
    """{相对路径: [size, mtime_ns, md5, sha1, sha256]}

    每次 sync 都要重新生成索引才能判断"pool 与已发布索引是否一致",而生成索引
    要给每个 deb 算三种哈希。缓存按 (size, mtime_ns) 命中,避免每天重读全部包。
    缓存损坏/丢失只是退化为重算,不影响正确性。
    """
    try:
        raw = json.loads((st.state_file.parent / HASH_CACHE_NAME).read_text())
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    cache = {}
    for rel, value in raw.items():
        if not isinstance(rel, str) or not _is_safe_repo_deb_path(st, rel):
            continue
        if _valid_hash_cache_entry(value):
            cache[rel] = value
    return cache


def _valid_hash_cache_entry(value):
    if not isinstance(value, list) or len(value) != 5:
        return False
    size, mtime_ns, md5, sha1, sha256 = value
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0
            or isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int)
            or mtime_ns < 0):
        return False
    for digest, length in ((md5, 32), (sha1, 40), (sha256, 64)):
        if (not isinstance(digest, str) or len(digest) != length
                or re.fullmatch(r"[0-9a-fA-F]+", digest) is None):
            return False
    return True


def _is_safe_repo_deb_path(st, value):
    if (not isinstance(value, str) or not value or "\\" in value
            or not value.endswith(".deb")):
        return False
    rel = PurePosixPath(value)
    if (rel.is_absolute() or rel.as_posix() != value
            or not rel.parts or rel.parts[0] != "pool"
            or any(part in ("", ".", "..") for part in rel.parts)):
        return False
    root = st.repo_dir.resolve()
    target = (st.repo_dir / Path(*rel.parts)).resolve(strict=False)
    return target != root and root in target.parents


def atomic_write_text(path, text):
    """Write text through a unique sibling temporary file and replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    open_fd = fd
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            open_fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if open_fd != -1:
            os.close(open_fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def save_hash_cache(st, cache):
    try:
        p = st.state_file.parent / HASH_CACHE_NAME
        atomic_write_text(p, json.dumps(cache, sort_keys=True) + "\n")
    except OSError as e:
        print(f"    (哈希缓存写入失败,忽略: {e})", file=sys.stderr)


def hashes_of(path: Path, rel, cache):
    stt = path.stat()
    hit = cache.get(rel)
    if (_valid_hash_cache_entry(hit)
            and hit[0] == stt.st_size and hit[1] == stt.st_mtime_ns):
        return stt.st_size, hit[2], hit[3], hit[4]
    size, md5, sha1, sha256 = file_hashes(path)
    cache[rel] = [size, stt.st_mtime_ns, md5, sha1, sha256]
    return size, md5, sha1, sha256


def generate_indexes(st, exclude=(), cache=None):
    """扫描 pool,在内存里生成全部索引。返回 (dists 目录, {相对路径: 内容}, Release 内容)。

    exclude 里的文件按"已删除"处理:这样可以先发布不含它们的索引、成功后再真正删除,
    保证任何时刻已发布的索引都不会指向不存在的文件。
    """
    excluded = {str(p) for p in exclude}
    cache = {} if cache is None else cache
    seen = set()
    debs = []
    component = _path_part(st.component, "component")
    pool = st.repo_dir / "pool" / component
    if pool.is_dir():
        for p in sorted(pool.rglob("*.deb")):
            relpath = p.relative_to(st.repo_dir).as_posix()
            if str(p) in excluded or relpath in excluded:
                continue
            try:
                fields, d = deb_control(p)
                size, md5, sha1, sha256 = hashes_of(p, relpath, cache)
            except (OSError, RuntimeError, ValueError) as e:
                raise RuntimeError(f"无法读取 pool 中的 deb {p}: {e}") from e
            seen.add(relpath)
            debs.append((relpath, fields, d, size, md5, sha1, sha256))
    for gone in [k for k in cache if k not in seen]:
        del cache[gone]  # 缓存只保留当前 pool 里存在的文件

    index_files = {}
    for arch in st.architectures:
        stanzas = []
        for relpath, fields, d, size, md5, sha1, sha256 in debs:
            if d.get("Architecture") not in (arch, "all"):
                continue
            lines = [f"{k}: {v}" for k, v in fields]
            lines += [f"Filename: {relpath}", f"Size: {size}",
                      f"MD5sum: {md5}", f"SHA1: {sha1}", f"SHA256: {sha256}"]
            stanzas.append("\n".join(lines))
        data = ("\n\n".join(stanzas) + ("\n" if stanzas else "")).encode()
        base = f"{st.component}/binary-{arch}"
        index_files[f"{base}/Packages"] = data
        index_files[f"{base}/Packages.gz"] = gz_bytes(data)
        index_files[f"{base}/Release"] = (
            f"Archive: {st.suite}\nOrigin: {st.origin}\nLabel: {st.label}\n"
            f"Component: {st.component}\nArchitecture: {arch}\n").encode()

    release = [
        f"Origin: {st.origin}",
        f"Label: {st.label}",
        f"Suite: {st.suite}",
        f"Codename: {st.suite}",
        f"Date: {rfc2822_now()}",
        f"Architectures: {' '.join(st.architectures)}",
        f"Components: {st.component}",
        f"Description: {st.description}",
    ]
    entries = sorted(index_files.items())
    for header, hasher in (("MD5Sum", hashlib.md5),
                           ("SHA1", hashlib.sha1), ("SHA256", hashlib.sha256)):
        release.append(header + ":")
        release += [f" {hasher(data).hexdigest()} {len(data):>10} {rel}"
                    for rel, data in entries]
    release_data = ("\n".join(release) + "\n").encode()
    return st.repo_dir / "dists" / st.suite, index_files, release_data


def parse_release_hashes(text):
    """解析 Release 的 MD5Sum/SHA1/SHA256 段。

    返回 {(字段, 相对路径): (hexdigest, size)}。哈希段里出现坏行时
    直接报错,由调用方把它当成"需要重建",而不是让异常穿透到 CLI。
    """
    out, field = {}, None
    lengths = {"MD5Sum": 32, "SHA1": 40, "SHA256": 64}
    for line in text.splitlines():
        if not line.startswith((" ", "\t")):
            field = line[:-1] if line.endswith(":") else None
            continue
        if field not in lengths:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError(f"{field} 段包含格式错误的行")
        digest, raw_size, rel = parts
        if (len(digest) != lengths[field]
                or re.fullmatch(r"[0-9a-fA-F]+", digest) is None):
            raise ValueError(f"{field} 段包含格式错误的摘要")
        try:
            size = int(raw_size)
        except ValueError as e:
            raise ValueError(f"{field} 段包含非整数文件大小") from e
        if size < 0 or not rel or Path(rel).is_absolute():
            raise ValueError(f"{field} 段包含格式错误的文件路径或大小")
        out[(field, rel)] = (digest.lower(), size)
    return out


def _gpg_status_fingerprint(status):
    """Extract the signature and primary-key fingerprints from gpg status."""
    for line in status.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "[GNUPG:]" and parts[1] == "VALIDSIG":
            candidates = [parts[2]]
            if len(parts) >= 12:
                candidates.append(parts[-1])
            return {value.lower() for value in candidates if value}
    return set()


def _verify_detached_signature(st, fpr, signature, signed):
    keyring = st.repo_dir / "ghapt.gpg"
    if not keyring.is_file():
        return False, "缺少 ghapt.gpg 公钥"
    proc = subprocess.run(
        ["gpg", "--batch", "--no-default-keyring", "--keyring", str(keyring),
         "--status-fd", "1", "--verify", str(signature), str(signed)],
        capture_output=True, text=True, env=gpg_env(st))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"Release.gpg 签名无效{': ' + detail if detail else ''}"
    if fpr.lower() not in _gpg_status_fingerprint(proc.stdout):
        return False, "Release.gpg 不是当前仓库密钥签名"
    return True, ""


def _verify_inrelease_signature(st, fpr, inrelease, release):
    keyring = st.repo_dir / "ghapt.gpg"
    if not keyring.is_file():
        return False, "缺少 ghapt.gpg 公钥"
    proc = subprocess.run(
        ["gpg", "--batch", "--no-default-keyring", "--keyring", str(keyring),
         "--status-fd", "2", "--output", "-", "--decrypt", str(inrelease)],
        capture_output=True, env=gpg_env(st))
    status = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = status.strip()
        return False, f"InRelease 签名无效{': ' + detail if detail else ''}"
    if fpr.lower() not in _gpg_status_fingerprint(status):
        return False, "InRelease 不是当前仓库密钥签名"
    try:
        release_bytes = release.read_bytes()
    except OSError as e:
        return False, f"Release 不可读: {e}"
    if proc.stdout != release_bytes:
        return False, "InRelease 解出的 Release 内容不一致"
    return True, ""


def index_matches(dist, index_files, st=None, fpr=None):
    """已发布的索引,是否与"当前 pool 生成的索引"完全一致?返回 (是否一致, 原因)。

    比较两件事:索引文件本身的字节,以及 Release 里对它们的哈希声明。
    Release 的 Date 每次生成都会变,所以这里比的是哈希表而不是 Release 文本 ——
    内容没变时不重新签名,仓库保持真正的幂等。

    这个判定是"pool == 已发布索引"这条不变式在每次 sync 被强制执行的地方:
    只要两者不一致(新增、删除、内容变化、外部改动、上次发布失败),就会重发。
    """
    for name in ("Release", "Release.gpg", "InRelease"):
        if not (dist / name).exists():
            return False, f"缺少 {name}"
    want = {}
    for field, hasher in (("MD5Sum", hashlib.md5), ("SHA1", hashlib.sha1),
                          ("SHA256", hashlib.sha256)):
        for rel, data in index_files.items():
            want[(field, rel)] = (hasher(data).hexdigest(), len(data))
    try:
        have = parse_release_hashes((dist / "Release").read_text())
    except (OSError, UnicodeError, ValueError) as e:
        return False, f"Release 不可读: {e}"
    if have != want:
        return False, "Release 声明的哈希与 pool 当前内容不一致"
    for rel, data in index_files.items():
        try:
            if (dist / rel).read_bytes() != data:
                return False, f"索引文件内容不一致: {rel}"
        except OSError as e:
            return False, f"索引文件不可读: {rel} ({e})"
    if st is None or not fpr:
        return False, "无法验证索引签名: 未提供仓库密钥"
    for name in ("ghapt.gpg", "ghapt.asc"):
        if not (st.repo_dir / name).is_file():
            return False, f"无法验证签名: 缺少公钥 {name}"
    ok, why = _verify_detached_signature(
        st, fpr, dist / "Release.gpg", dist / "Release")
    if not ok:
        return False, why
    ok, why = _verify_inrelease_signature(
        st, fpr, dist / "InRelease", dist / "Release")
    if not ok:
        return False, why
    return True, ""


def gpg_env(st):
    env = dict(os.environ)
    env["GNUPGHOME"] = str(st.gpg_home)
    return env


def ensure_key(st):
    st.gpg_home.mkdir(parents=True, exist_ok=True)
    os.chmod(st.gpg_home, 0o700)
    env = gpg_env(st)

    def first_fpr():
        out = subprocess.run(["gpg", "--batch", "--with-colons", "--list-secret-keys"],
                             capture_output=True, text=True, env=env).stdout
        for line in out.splitlines():
            if line.startswith("fpr:"):
                return line.split(":")[9]
        return None

    fpr = first_fpr()
    if fpr:
        return fpr
    print("[init] 首次运行,生成仓库签名密钥 (RSA 4096),约需几秒...")
    batch = (f"%no-protection\nKey-Type: RSA\nKey-Length: 4096\nKey-Usage: sign\n"
             f"Name-Real: {st.key_name}\nName-Email: {st.key_email}\n"
             f"Expire-Date: 0\n%commit\n")
    run_checked(["gpg", "--batch", "--gen-key"], input=batch, env=env)
    fpr = first_fpr()
    if not fpr:
        raise RuntimeError("生成 GPG 密钥失败")
    return fpr


def publish(st, fpr, dist, index_files, release_data):
    """构建完整发布树后整体切换,失败时恢复旧树和公钥。

    Unix 无法把一个非空目录直接替换到另一个非空目录,所以提交协议是:
    旧 dist 改名到备份 -> 新 dist 改名到正式位置 -> 原子替换两个公钥。
    任一步失败都尽力撤销已经完成的步骤,并把回滚失败明确报告出来。
    """
    env = gpg_env(st)
    st.repo_dir.mkdir(parents=True, exist_ok=True)
    dist.parent.mkdir(parents=True, exist_ok=True)
    publish_root = Path(tempfile.mkdtemp(
        prefix=".ghapt-publish-", dir=str(st.repo_dir)))
    new_dist = publish_root / "dist"
    new_dist.mkdir()
    key_stage = publish_root / "keys"
    key_stage.mkdir()
    backup_dist = publish_root / "old-dist"
    backup_keys = {name: publish_root / f"old-{name}"
                   for name in ("ghapt.asc", "ghapt.gpg")}
    key_paths = {name: st.repo_dir / name
                 for name in ("ghapt.asc", "ghapt.gpg")}
    installed_keys = set()
    dist_installed = False
    dist_backed_up = False
    key_backups = set()

    try:
        for rel, data in sorted(index_files.items()):
            final = new_dist / rel
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(data)
        release = new_dist / "Release"
        release.write_bytes(release_data)
        inrelease = new_dist / "InRelease"
        signature = new_dist / "Release.gpg"
        run_checked(["gpg", "--batch", "--yes", "-u", fpr,
                     "--digest-algo", "SHA256", "--clearsign",
                     "-o", str(inrelease), str(release)], env=env)
        run_checked(["gpg", "--batch", "--yes", "-u", fpr,
                     "--digest-algo", "SHA256", "-abs",
                     "-o", str(signature), str(release)], env=env)

        for name, opts in (("ghapt.asc", ["--armor"]), ("ghapt.gpg", [])):
            data = run_checked(["gpg", "--batch", "--export", *opts, fpr],
                               binary=True, env=env).stdout
            if not isinstance(data, bytes) or not data:
                raise RuntimeError(f"gpg --export 没有生成 {name}")
            (key_stage / name).write_bytes(data)

        if dist.exists():
            os.replace(dist, backup_dist)
            dist_backed_up = True
        os.replace(new_dist, dist)
        dist_installed = True

        for name, final in key_paths.items():
            old = backup_keys[name]
            if final.exists():
                os.replace(final, old)
                key_backups.add(name)
            os.replace(key_stage / name, final)
            installed_keys.add(name)
    except Exception as e:
        rollback_errors = []
        for name in reversed(("ghapt.gpg", "ghapt.asc")):
            final = key_paths[name]
            try:
                if name in installed_keys:
                    final.unlink(missing_ok=True)
                if name in key_backups:
                    os.replace(backup_keys[name], final)
            except OSError as rollback_error:
                rollback_errors.append(f"{final}: {rollback_error}")
        try:
            if dist_installed and dist.exists():
                shutil.rmtree(dist)
            if dist_backed_up and backup_dist.exists():
                os.replace(backup_dist, dist)
        except OSError as rollback_error:
            rollback_errors.append(f"{dist}: {rollback_error}")
        suffix = (f"; 回滚也失败: {'; '.join(rollback_errors)}"
                  if rollback_errors else "; 已恢复旧发布内容")
        raise RuntimeError(f"publish 失败: {e}{suffix}") from e
    finally:
        shutil.rmtree(publish_root, ignore_errors=True)


# ── 状态文件 ──────────────────────────────────────────────────────────

class StateError(RuntimeError):
    """state.json 缺失或损坏。继续操作可能误删整个仓库,所以直接拒绝。"""


def _validate_state(st, raw):
    if not isinstance(raw, dict):
        raise StateError("state 顶层必须是 JSON 对象")
    for project_name, record in raw.items():
        if not isinstance(project_name, str) or not project_name:
            raise StateError("state 项目名必须是非空字符串")
        if not isinstance(record, dict):
            raise StateError(f"state 项目 {project_name!r} 必须是 JSON 对象")
        if not isinstance(record.get("tag"), str):
            raise StateError(f"state 项目 {project_name!r} 的 tag 必须是字符串")
        files = record.get("files")
        if not isinstance(files, dict):
            raise StateError(f"state 项目 {project_name!r} 的 files 必须是 JSON 对象")
        for arch, rel in files.items():
            if (not isinstance(arch, str)
                    or PATH_PART_RE.fullmatch(arch) is None):
                raise StateError(
                    f"state 项目 {project_name!r} 的架构键不安全: {arch!r}")
            if not _is_safe_repo_deb_path(st, rel):
                raise StateError(
                    f"state 项目 {project_name!r} 的文件路径不安全: {rel!r}")
    return raw


def load_state(st, strict=False):
    """读 state.json。

    strict=True(会做删除操作时)对"缺失/损坏"报错而不是当成空:
    把缺失当成空会让 gc 认为"所有包都不属于任何项目",从而清空整个仓库。
    """
    try:
        text = st.state_file.read_text()
    except FileNotFoundError:
        if strict:
            raise StateError(
                f"state 文件不存在: {st.state_file}。"
                f"为免误删整个仓库,拒绝继续。确认要重建的话,请先备份 pool,"
                f"再用 sync 重新收录(或临时把该文件写成 {{}})。")
        return {}
    except (OSError, UnicodeError) as e:
        if strict:
            raise StateError(
                f"state 文件无法读取: {st.state_file} ({e})。"
                f"为免误删整个仓库,拒绝继续。请检查权限或从备份恢复。")
        print(f"警告: state 文件无法读取({e}),按空状态处理", file=sys.stderr)
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        if strict:
            raise StateError(
                f"state 文件损坏: {st.state_file} ({e})。"
                f"为免误删整个仓库,拒绝继续。可从备份恢复,或用 sync 重新收录。")
        print(f"警告: state 文件无法解析({e}),按空状态处理", file=sys.stderr)
        return {}
    try:
        return _validate_state(st, raw)
    except StateError as e:
        if strict:
            raise
        print(f"警告: state 文件结构无效({e}),按空状态处理", file=sys.stderr)
        return {}


def save_state(st, state):
    atomic_write_text(
        st.state_file, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


# ── 子命令 ────────────────────────────────────────────────────────────

def publish_if_needed(st, dist, index_files, release_data, force=False):
    """索引与 pool 一致就什么都不做,否则签名发布。返回是否真的发布了。"""
    fpr = None
    if (dist / "Release").is_file():
        fpr = ensure_key(st)
    ok, why = index_matches(dist, index_files, st=st, fpr=fpr)
    if ok and not force:
        print("[done] 索引与 pool 一致,未改动。")
        return []
    print(f"    索引需要重建:{why}" if not ok else "    按 --force 要求重建索引")
    fpr = fpr or ensure_key(st)
    publish(st, fpr, dist, index_files, release_data)
    print(f"[done] 仓库索引已更新并签名 ({st.repo_dir})")
    return True


def cmd_sync(args):
    st, projects, broken = load_config(args.config, strict=False)
    # 配置写坏的项目也要计入失败,否则"日志里报了错但退出码是 0"会骗过监控
    failures = [name for name, _ in broken]
    if args.project:
        if args.project not in [p.name for p in projects] + [n for n, _ in broken]:
            sys.exit(f"配置里没有 [project:{args.project}]")
        projects = [p for p in projects if p.name == args.project]
        failures = [n for n in failures if n == args.project]
    if not projects and not failures:
        sys.exit("配置里没有任何 [project:*]")
    if not projects:
        sys.exit(f"以下项目配置有问题,没有可同步的项目: {', '.join(failures)}")

    st.repo_dir.mkdir(parents=True, exist_ok=True)
    st.state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(st.state_file.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("另一个 ghapt sync 正在运行,退出。")

        print(f"ghapt: {len(projects)} 个项目 → {st.repo_dir}")
        state = load_state(st)
        touched = []
        for p in projects:
            try:
                touched += sync_project(p, st, state, args.force)
            except Exception as e:  # 单个项目失败不影响其他项目
                failures.append(p.name)
                print(f"[fail] {p.name}: {e}", file=sys.stderr)

        # 索引永远由"当前 pool 的内容"重新生成,再与已发布内容比对;
        # 因此即使某个项目失败、或上次发布中断,下一次 sync 也会自动收敛。
        cache = load_hash_cache(st)
        for rel in touched:
            cache.pop(rel, None)   # 刚被覆盖写的文件,缓存一律作废后重算
        try:
            prune = prune_candidates(st)
            dist, index_files, release_data = generate_indexes(
                st, exclude=prune, cache=cache)
        except Exception as e:
            print(f"[fail] 生成索引失败,未发布仓库: {e}", file=sys.stderr)
            save_hash_cache(st, cache)
            sys.exit(1)
        try:
            publish_if_needed(st, dist, index_files, release_data, args.force)
        except Exception as e:
            # 发布失败:仓库保持原样 —— 旧索引仍有效、旧版本一个都没删、state 不推进。
            print(f"[fail] 索引发布失败,仓库保持原样(未删旧版本、未更新 state): {e}",
                  file=sys.stderr)
            save_hash_cache(st, cache)
            sys.exit(1)

        # 只有新索引已经就位,才允许删除它不再引用的旧版本
        for old in prune:
            print(f"    清理旧版本: {old.name}")
            old.unlink(missing_ok=True)
        save_state(st, state)
        save_hash_cache(st, cache)
        if failures:
            sys.exit(f"以下项目同步失败: {', '.join(failures)}")


def cmd_check(args):
    st, projects, _ = load_config(args.config)
    state = load_state(st)
    updates = failures = 0
    for p in projects:
        try:
            tag = latest_release(p, st.token).get("tag_name")
        except Exception as e:
            failures += 1
            print(f"? {p.name}: 查询失败 ({e})")
            continue
        cur = state.get(p.name, {}).get("tag")
        updates += cur != tag
        print(f"{'=' if cur == tag else '↑'} {p.name}: 已收录 {cur or '(无)'} / 最新 {tag}")
    if updates or failures:
        print(f"[check] {updates} 个有更新,{failures} 个查询失败")
    # 退出码:0=无更新;10=有更新(无失败);2=有查询失败(优先于 10)。
    # 查询失败意味着"状态未知",比"已知有更新"更需要人看一眼:
    # 如果返回 10,监控脚本会把"有更新"当成正常情况,从而漏掉查询失败。
    if failures:
        sys.exit(2)
    if updates:
        sys.exit(10)


def check_gc_safe(st, projects, victims):
    """删除前的保险:不许删掉"仍在配置里"的项目的包。

    只靠 strict 读 state 挡不住"内容是合法 JSON 但丢了这个项目的记录"的情况
    (例如被清成 `{}`、或只留下别的项目的条目)。那种 state 会让 gc 把这些包
    当成"已移除项目的遗留"而删光。

    判据用包名与配置里的 `package` 对照,而不是"已发布索引是否引用该文件":
    正常 gc 删的恰恰是当前索引还列着的包(先重发索引、再删文件),
    用索引判断会把正常的 gc 全部拦死;而"换了配置、还没同步新项目就 gc"
    这种正常流程也不会被误伤(被删的包不属于任何已配置项目)。
    """
    if not victims:
        return
    expected = {p.package for p in projects}
    doomed = sorted({d.name for d in victims} & expected)
    if doomed:
        raise StateError(
            f"这些包属于配置里仍然存在的项目,却被判定为可清理: {', '.join(doomed)}。"
            f"通常意味着 {st.state_file} 丢失了这些项目的记录。"
            f"为免误删仓库内容,拒绝继续。请先检查/恢复 state(或先用 sync 重新收录)。")


def cmd_gc(args):
    st, projects, _ = load_config(args.config)
    st.state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(st.state_file.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("另一个 ghapt 实例正在运行,退出。")

        try:
            state = load_state(st, strict=True)
        except StateError as e:
            sys.exit(f"[gc] {e}")

        names = {p.name for p in projects}
        pool = st.repo_dir / "pool" / st.component
        stale = sorted(k for k in state if k not in names)
        keep_dirs = {(st.repo_dir / rel).parent
                     for name in names
                     for rel in state.get(name, {}).get("files", {}).values()}
        victims = [d for d in sorted(pool.glob("*/*"))
                   if d.is_dir() and d not in keep_dirs] if pool.is_dir() else []
        try:
            check_gc_safe(st, projects, victims)
        except StateError as e:
            sys.exit(f"[gc] {e}")

        if not victims and not stale:
            print("[gc] 没有需要清理的内容")
            return
        for d in victims:
            print(f"[gc] 移除 {d.relative_to(st.repo_dir)}/ "
                  f"({len(list(d.glob('*.deb')))} 个包)")
        for k in stale:
            print(f"[gc] 移除 state 条目: {k}")
        if args.dry_run:
            print("[gc] --dry-run,未实际改动")
            return

        cache = load_hash_cache(st)
        if victims:
            # 与 sync 相同的顺序:先发布"把这些包排除掉"的索引,成功后再删文件。
            doomed = [p for d in victims for p in d.glob("*.deb")]
            try:
                dist, index_files, release_data = generate_indexes(
                    st, exclude=doomed, cache=cache)
            except Exception as e:
                print(f"[gc] 生成索引失败,未删除任何文件: {e}",
                      file=sys.stderr)
                save_hash_cache(st, cache)
                sys.exit(1)
            try:
                publish_if_needed(st, dist, index_files, release_data)
            except Exception as e:
                print(f"[gc] 索引发布失败,未删除任何文件(仓库保持原样): {e}",
                      file=sys.stderr)
                sys.exit(1)

        for d in victims:
            shutil.rmtree(d)
            try:
                d.parent.rmdir()  # 首字母目录空了就顺手删掉
            except OSError:
                pass
        for k in stale:
            del state[k]
        save_state(st, state)
        save_hash_cache(st, cache)
        print("[gc] 已清理完毕" +(",索引已重建并签名" if victims else ""))


def cmd_assets(args):
    class P:
        repo = args.repo
        prerelease = args.prerelease
    rel = latest_release(P, os.environ.get("GITHUB_TOKEN", ""))
    print(f"# {args.repo} {rel.get('tag_name')}")
    for a in rel.get("assets", []):
        print(a["name"])


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="/etc/ghapt/config.ini",
                        help="配置文件路径 (默认 /etc/ghapt/config.ini)")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sync", parents=[common], help="同步所有项目并重建仓库索引")
    sp.add_argument("--project", help="只同步这一个项目")
    sp.add_argument("--force", action="store_true", help="版本没变也重新下载并重建索引")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("check", parents=[common],
                        help="只查版本,不下载(退出码 0=无更新 10=有更新 2=查询失败)")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("gc", parents=[common],
                        help="清理已从配置移除的项目(pool + state),并重建索引")
    sp.add_argument("--dry-run", action="store_true", help="只显示将清理什么,不实际改动")
    sp.set_defaults(func=cmd_gc)

    sp = sub.add_parser("assets", help="列出仓库最新 release 的资产名(帮助写正则)")
    sp.add_argument("repo", help="owner/name")
    sp.add_argument("--prerelease", action="store_true", help="包含预发布版本")
    sp.set_defaults(func=cmd_assets)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
