#!/usr/bin/env python3
"""ghapt — 把 GitHub releases 同步成自托管、带 GPG 签名的 APT 仓库。

只依赖 Python 3.8+ 标准库,外加系统自带的 dpkg、dpkg-deb 与 gpg。
跟踪 `.tar.zst`/`.tzst` 资产时还需要系统 `zstd`。

每个项目两种收录模式:
  type = deb     release 里直接提供 .deb,原样收录
  type = binary  release 只有二进制 tarball/zip,自动重打包成 .deb

用法:
  ghapt.py sync  [-c config.ini] [--project NAME] [--force]
  ghapt.py check [-c config.ini]
  ghapt.py assets owner/repo [--prerelease] [-c config.ini] [--mirror URL]
"""

import argparse
import configparser
from contextlib import contextmanager
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
from email.utils import parseaddr
from functools import cmp_to_key
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

API_ROOT = "https://api.github.com"
USER_AGENT = "ghapt/1.0"
SUPPORTED_GITHUB_HOSTS = frozenset({
    "github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "gist.github.com",
    "api.github.com",
    "codeload.github.com",
    "github.githubassets.com",
})
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")
# tarfile 原生不支持 zstd(Python 3.14 才有),用系统 zstd 先解一层
ZSTD_SUFFIXES = (".tar.zst", ".tzst")
ELF_MAGIC = b"\x7fELF"
PATH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._~-]*$")
GITHUB_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
DEB_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


def _config_path_part(value, label):
    """Validate a configuration value used as one path component."""
    if not isinstance(value, str) or not PATH_PART_RE.fullmatch(value):
        raise ConfigError(
            f"{label} 不是安全的单一路径组成部分: {value!r}")
    return value


def _config_single_line(value, label, allow_empty=False):
    """Validate a config value that is written into a line-oriented file."""
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{label} 不能为空")
    if "\r" in value or "\n" in value:
        raise ConfigError(f"{label} 不能包含换行")
    return value


def _config_github_repo(value, label):
    value = _config_single_line(value, label)
    if GITHUB_REPO_RE.fullmatch(value) is None:
        raise ConfigError(
            f"{label} 必须是安全的 GitHub owner/name: {value!r}")
    return value


def _config_email(value, label):
    value = _config_single_line(value, label)
    if EMAIL_RE.fullmatch(value) is None:
        raise ConfigError(f"{label} 不是合法的单行 email: {value!r}")
    return value


def _config_github_mirror(value):
    """Validate the HTTPS prefix used to proxy GitHub URLs."""
    value = (value or "").strip()
    if not value:
        return ""
    _config_single_line(value, "github_mirror")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as e:
        raise ConfigError(f"github_mirror 不是合法的 HTTPS URL: {value!r}") from e
    if (parsed.scheme.lower() != "https" or not parsed.netloc
            or not hostname or port is None and ":" in parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ConfigError(
            "github_mirror 必须是没有用户信息、查询参数或 fragment 的 HTTPS URL")
    return value.rstrip("/")


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
        self.origin = _config_single_line(s.get("origin", "ghapt"), "origin")
        self.label = _config_single_line(s.get("label", "ghapt"), "label")
        self.description = _config_single_line(
            s.get("description", "Self-hosted APT repo synced from GitHub releases"),
            "description")
        self.maintainer = _config_single_line(
            s.get("maintainer", "ghapt <ghapt@localhost>"), "maintainer")
        self.keep_versions = self._int_setting(s, "keep_versions", 2, minimum=1)
        self.key_name = _config_single_line(
            s.get("key_name", "ghapt archive signing key"), "key_name")
        self.key_email = _config_email(
            s.get("key_email", "ghapt@localhost"), "key_email")
        token = s.get("token", "") or os.environ.get("GITHUB_TOKEN", "")
        self.token = _config_single_line(token, "token", allow_empty=True)
        mirror = s.get("github_mirror", "") or os.environ.get("GITHUB_MIRROR", "")
        self.github_mirror = _config_github_mirror(mirror)

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
        if not name:
            raise ConfigError("项目名不能为空")
        self.repo = _config_github_repo(
            sec.get("repo", "").strip(), f"[project:{name}] repo")
        self.type = sec.get("type", "deb").strip()
        if self.type not in ("deb", "binary"):
            raise ConfigError(f"type 必须是 deb 或 binary(当前 {self.type!r})")
        self.package = sec.get("package", name).strip()
        if DEB_PACKAGE_RE.fullmatch(self.package) is None:
            raise ConfigError(
                f"[project:{name}] package 不是合法 Debian 包名: {self.package!r}")
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
            pattern = _config_single_line(
                pattern, f"[project:{name}] asset.{arch}")
            self.assets[arch] = pattern
            try:
                re.compile(pattern)
            except re.error as e:
                raise ConfigError(f"asset.{arch} 的正则无法编译: {e}({pattern!r})")

        self.strip_prefix = _config_single_line(
            sec.get("strip_prefix", "v"), f"[project:{name}] strip_prefix",
            allow_empty=True)
        self.prerelease = sec.get("prerelease", "false").lower() in ("1", "true", "yes")
        self.description = _config_single_line(
            sec.get("description",
                    f"{self.package} (packaged from github.com/{self.repo} by ghapt)"),
            f"[project:{name}] description")
        binaries = _config_single_line(
            sec.get("binaries", "") or self.package,
            f"[project:{name}] binaries")
        self.binaries = binaries.split()
        for binary in self.binaries:
            _config_path_part(binary, f"[project:{name}] binaries")
        self.depends = _config_single_line(
            sec.get("depends", "").strip(),
            f"[project:{name}] depends", allow_empty=True)
        self.section = _config_single_line(
            sec.get("section", "utils"), f"[project:{name}] section")


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


def mirror_github_url(url, mirror=""):
    """Prefix supported HTTPS GitHub URLs with a configured mirror."""
    if not mirror:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if (parsed.scheme.lower() != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in SUPPORTED_GITHUB_HOSTS):
        return url
    return mirror.rstrip("/") + "/" + url


def gh_json_response(path, token, mirror=""):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token

    def go():
        url = path if "://" in path else API_ROOT + path
        url = mirror_github_url(url, mirror)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp), resp.headers
    return with_retry(go)


def gh_json(path, token, mirror=""):
    if mirror:
        return gh_json_response(path, token, mirror)[0]
    return gh_json_response(path, token)[0]


def _next_link(headers):
    raw = headers.get("Link", "") if headers else ""
    for part in raw.split(","):
        match = re.search(r"<([^>]+)>\s*;\s*rel=\"([^\"]+)\"", part)
        if match and "next" in {x.strip() for x in match.group(2).split()}:
            return match.group(1)
    return None


def latest_release(project, token, mirror=""):
    if project.prerelease:
        path = f"/repos/{project.repo}/releases?per_page=10"
        seen = set()
        while path and path not in seen:
            seen.add(path)
            if mirror:
                rels, headers = gh_json_response(path, token, mirror)
            else:
                rels, headers = gh_json_response(path, token)
            if not isinstance(rels, list):
                raise RuntimeError("GitHub releases API 返回了非列表数据")
            for index, release in enumerate(rels):
                _validate_release(release, f"GitHub release[{index}]")
                if not release.get("draft", False):
                    return release
            path = _next_link(headers)
        raise RuntimeError("没有找到任何 release")
    if mirror:
        release = gh_json(f"/repos/{project.repo}/releases/latest", token, mirror)
    else:
        release = gh_json(f"/repos/{project.repo}/releases/latest", token)
    _validate_release(release, "GitHub latest release", require_assets=True)
    return release


def _validate_release(release, label, require_assets=False):
    if not isinstance(release, dict):
        raise RuntimeError(f"{label} 不是 JSON 对象")
    tag = release.get("tag_name")
    if (not isinstance(tag, str) or not tag.strip()
            or "\r" in tag or "\n" in tag):
        raise RuntimeError(f"{label} 缺少合法的 tag_name")
    draft = release.get("draft", False)
    if not isinstance(draft, bool):
        raise RuntimeError(f"{label} 的 draft 不是布尔值")
    assets = release.get("assets")
    if assets is None and draft and not require_assets:
        assets = []
    if not isinstance(assets, list):
        raise RuntimeError(f"{label} 的 assets 不是列表")
    for index, asset in enumerate(assets):
        _validate_asset(asset, f"{label} asset[{index}]")
    return release


def _validate_asset(asset, label):
    if not isinstance(asset, dict):
        raise RuntimeError(f"{label} 不是 JSON 对象")
    _safe_asset_filename(asset.get("name"))
    url = asset.get("browser_download_url")
    if (not isinstance(url, str) or not url.strip()
            or "\r" in url or "\n" in url):
        raise RuntimeError(f"{label} 缺少合法的 browser_download_url")
    size = asset.get("size")
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise RuntimeError(f"{label} 的 size 不是非负整数")
    if "digest" in asset and asset["digest"] is not None:
        parse_digest(asset["digest"])
    return asset


def parse_digest(digest):
    """GitHub 的 asset digest(`sha256:<hex>`)→ hex,缺失则返回 None。

    2025-06 起 release 资产自带不可变 digest,校验它几乎不花成本,
    能挡住"尺寸相同但内容被换掉"的情况(只比 size 是挡不住的)。
    """
    if digest is None:
        return None
    if not isinstance(digest, str):
        raise RuntimeError(f"GitHub asset digest 不是字符串: {digest!r}")
    match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
    if match is None:
        raise RuntimeError(
            f"GitHub asset digest 格式不受支持: {digest!r}"
            f"(期望 sha256:<64 位十六进制>)")
    return match.group(1).lower()


def sha256_of(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_asset_filename(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or os.path.isabs(value) or "/" in value or "\\" in value):
        raise RuntimeError(f"GitHub 资产名不是安全的文件名: {value!r}")
    return value


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
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.name}.", suffix=".part", dir=str(dest.parent))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, \
                    os.fdopen(fd, "wb") as f:
                fd = -1
                shutil.copyfileobj(resp, f, 1024 * 256)
                f.flush()
                os.fsync(f.fileno())
            tmp = Path(tmp_name)
            if expect_size is not None and tmp.stat().st_size != expect_size:
                raise OSError(f"下载不完整: {url}")
            if expect_sha256 is not None:
                actual = sha256_of(tmp)
                if actual != expect_sha256:
                    raise OSError(
                        f"下载内容与 GitHub 公布的 sha256 不符: {url}\n"
                        f"    期望 {expect_sha256}\n    实际 {actual}")
            os.replace(tmp, dest)
        finally:
            if fd != -1:
                os.close(fd)
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
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
        if p.is_symlink():
            raise RuntimeError(f"pool 路径包含符号链接文件: {p}")
        version, file_arch = _pool_deb_parts(p, pkg)
        if file_arch == arch:
            files.append((p, version))
    files.sort(key=cmp_to_key(lambda a, b: dpkg_vercmp(a[1], b[1])))
    return [p for p, _version in files[:-keep]]


def prune_candidates(st):
    """当前 component 范围内按 keep_versions 应当清理的文件(仍然不删除)。"""
    pool = _component_pool(st)
    if st.keep_versions <= 0 or not pool.is_dir():
        return []
    out = []
    for pooldir in sorted({p.parent for p in _pool_deb_files(st, pool)}):
        pkg = _path_part(pooldir.name, "Package")
        arches = set()
        for p in _pool_deb_files(st, pooldir):
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


def _ensure_repo_root(st, create=False):
    """Reject symlinks in the repository root path before creating anything."""
    root = Path(st.repo_dir)
    for candidate in (root, *root.parents):
        if candidate.is_symlink():
            raise RuntimeError(f"仓库路径不能包含符号链接: {candidate}")
    if root.exists() and not root.is_dir():
        raise RuntimeError(f"仓库根目录不是目录: {root}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_repo_path(st, path, label, expect_dir=False):
    """Validate that an existing path chain stays inside the repository."""
    path = Path(path)
    repo_root = _ensure_repo_root(st)
    root = repo_root.resolve()
    try:
        relative = path.relative_to(repo_root)
    except ValueError as e:
        raise RuntimeError(f"{label} 不在仓库目录内: {path}") from e

    current = repo_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"{label} 路径包含符号链接: {current}")
    resolved = path.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"{label} 解析后超出仓库目录: {path}")
    if expect_dir and path.exists() and not path.is_dir():
        raise RuntimeError(f"{label} 不是目录: {path}")
    return path


def _ensure_repo_file(st, path, label):
    """Validate a repository file without following a file symlink."""
    path = _ensure_repo_path(st, path, label)
    if path.is_symlink():
        raise RuntimeError(f"{label} 不能是符号链接: {path}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"{label} 不是普通文件: {path}")
    return path


def _component_pool(st):
    component = _path_part(st.component, "component")
    pool_root = _ensure_repo_path(
        st, st.repo_dir / "pool", "pool", expect_dir=True)
    return _ensure_repo_path(
        st, pool_root / component, "component pool", expect_dir=True)


def _dists_root(st):
    dists = st.repo_dir / "dists"
    if dists.is_symlink():
        raise RuntimeError(f"dists 路径包含符号链接: {dists}")
    return _ensure_repo_path(st, dists, "dists", expect_dir=True)


def _suite_dist(st):
    suite = _path_part(st.suite, "suite")
    dists = _dists_root(st)
    return _ensure_repo_path(
        st, dists / suite, "当前 suite", expect_dir=True)


def _pool_layout(st):
    """Validate the configured component and return its package directories.

    A pool file is accepted only at:
    pool/<component>/<first-letter>/<package>/<package>_<version>_<arch>.deb
    """
    pool = _component_pool(st)
    if not pool.is_dir():
        return [], {}
    package_dirs = []
    debs_by_package = {}
    for first in sorted(pool.iterdir()):
        if first.is_symlink():
            raise RuntimeError(f"pool 路径包含符号链接: {first}")
        if first.is_file():
            if first.name.endswith(".deb"):
                raise RuntimeError(f"pool deb 位于错误层级: {first}")
            continue
        if not first.is_dir():
            raise RuntimeError(f"pool 路径对象类型无效: {first}")
        if re.fullmatch(r"[a-z0-9]", first.name) is None:
            raise RuntimeError(
                f"pool 首字母目录格式错误: {first}"
                "(期望单个小写字母或数字)")
        _ensure_repo_path(st, first, "pool package prefix", expect_dir=True)
        for package_dir in sorted(first.iterdir()):
            if package_dir.is_symlink():
                raise RuntimeError(f"pool 路径包含符号链接: {package_dir}")
            if package_dir.is_file():
                if package_dir.name.endswith(".deb"):
                    raise RuntimeError(f"pool deb 位于错误层级: {package_dir}")
                continue
            if not package_dir.is_dir():
                raise RuntimeError(f"pool 路径对象类型无效: {package_dir}")
            if (DEB_PACKAGE_RE.fullmatch(package_dir.name) is None
                    or package_dir.name[0] != first.name):
                raise RuntimeError(
                    f"pool 包目录名称与首字母目录不一致: {package_dir}")
            _ensure_repo_path(
                st, package_dir, "pool package", expect_dir=True)
            package_files = []
            for item in sorted(package_dir.iterdir()):
                if item.is_symlink():
                    raise RuntimeError(f"pool 路径包含符号链接: {item}")
                if item.is_dir():
                    raise RuntimeError(f"pool deb 位于嵌套目录: {item}")
                if not item.is_file():
                    raise RuntimeError(f"pool 路径对象类型无效: {item}")
                if item.name.endswith(".deb"):
                    _pool_deb_parts(item, package_dir.name)
                    package_files.append(item)
                elif (item.name.endswith(".deb.new")
                      or item.name.endswith(".deb.part")):
                    raise RuntimeError(f"pool 中有未清理的临时文件: {item}")
            package_dirs.append(package_dir)
            debs_by_package[package_dir] = sorted(package_files)
    return package_dirs, debs_by_package


def _pool_deb_files(st, root):
    """Return regular deb files from one canonical pool tree."""
    root = _ensure_repo_path(st, root, "pool", expect_dir=True)
    pool = _component_pool(st)
    package_dirs, debs_by_package = _pool_layout(st)
    if root == pool:
        return sorted(
            deb for package_dir in package_dirs
            for deb in debs_by_package[package_dir])
    if root in debs_by_package:
        return list(debs_by_package[root])
    raise RuntimeError(f"pool 扫描起点不是当前 component 的规范目录: {root}")


def _pool_package_dirs(st):
    """Enumerate package directories in the configured component only."""
    package_dirs, _debs_by_package = _pool_layout(st)
    return package_dirs


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
    pkg = metadata["Package"]
    if not isinstance(pkg, str) or DEB_PACKAGE_RE.fullmatch(pkg) is None:
        raise RuntimeError(f"Package 不是合法 Debian 包名: {pkg!r}")
    ver = metadata["Version"]
    if not isinstance(ver, str) or not valid_version(ver):
        raise RuntimeError(f"Version 不是合法 Debian 版本: {ver!r}")
    arch = _path_part(metadata["Architecture"], "Architecture")
    component = _path_part(st.component, "component")
    pooldir = st.repo_dir / "pool" / component / pkg[0].lower() / pkg
    target = pooldir / f"{pkg}_{ver.replace(':', '%3a')}_{arch}.deb"
    _ensure_repo_path(st, target, "deb 目标")
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


def validate_pool_deb_metadata(st, path, fields, metadata):
    """Validate metadata and filename before an existing pool deb is indexed."""
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{path} 的 control 元数据不是对象")
    for field in ("Package", "Version", "Architecture"):
        value = metadata.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"{path} 缺少 control 字段 {field}")
    if DEB_PACKAGE_RE.fullmatch(metadata["Package"]) is None:
        raise RuntimeError(
            f"{path} 的 Package 名称不合法: {metadata['Package']!r}")
    if not valid_version(metadata["Version"]):
        raise RuntimeError(
            f"{path} 的 Version 不合法: {metadata['Version']!r}")
    arch = metadata["Architecture"]
    if arch != "all":
        _path_part(arch, "Architecture")
        configured_architectures = getattr(st, "architectures", None)
        if (configured_architectures is not None
                and arch not in configured_architectures):
            raise RuntimeError(
                f"{path} 的 Architecture={arch!r} 不在配置的架构列表中")
    target, expected_rel = _pool_target(st, metadata)
    actual_rel = path.relative_to(st.repo_dir).as_posix()
    if path != target or actual_rel != expected_rel:
        raise RuntimeError(
            f"{path} 的 control 元数据与 pool 路径不一致"
            f"(期望 {expected_rel})")
    if not fields:
        raise RuntimeError(f"{path} 的 control 为空")
    return metadata


def _stage_and_commit_debs(st, entries):
    """Stage every deb for a project, then commit all pool files as one unit."""
    _ensure_repo_root(st, create=True)
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
    arch = asset_arch if asset_arch is not None else metadata["Architecture"]
    changes, paths = _stage_and_commit_debs(st, [(arch, deb, metadata)])
    return changes[arch], paths[arch]


# ── 同步单个项目 ──────────────────────────────────────────────────────

def sync_project(project, st, state, force):
    mirror = getattr(st, "github_mirror", "")
    if mirror:
        rel = latest_release(project, st.token, mirror)
    else:
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
            asset_name = _safe_asset_filename(asset.get("name"))
            print(f"    下载 {asset_name} ({asset.get('size', 0) // 1024} KiB)")
            dest = tmpdir / asset_name
            digest = parse_digest(asset.get("digest"))
            download(
                mirror_github_url(asset["browser_download_url"], mirror),
                dest,
                asset.get("size"),
                digest,
            )
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
    """{相对路径: [size, mtime_ns, ctime_ns, st_dev, st_ino, md5, sha1, sha256]}

    每次 sync 都要重新生成索引才能判断"pool 与已发布索引是否一致",而生成索引
    要给每个 deb 算三种哈希。缓存按文件身份和时间戳命中,避免每天重读全部包。
    缓存损坏/丢失只是退化为重算,不影响正确性。
    """
    try:
        cache_path = _data_file(
            st.state_file.parent / HASH_CACHE_NAME, "index-cache.json")
        raw = json.loads(cache_path.read_text())
    except (OSError, RuntimeError, ValueError, UnicodeError):
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
    if not isinstance(value, list) or len(value) != 8:
        return False
    size, mtime_ns, ctime_ns, st_dev, st_ino, md5, sha1, sha256 = value
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0
            or isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int)
            or mtime_ns < 0
            or isinstance(ctime_ns, bool) or not isinstance(ctime_ns, int)
            or ctime_ns < 0
            or isinstance(st_dev, bool) or not isinstance(st_dev, int)
            or st_dev < 0
            or isinstance(st_ino, bool) or not isinstance(st_ino, int)
            or st_ino < 0):
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
            or len(rel.parts) < 5
            or not rel.parts or rel.parts[0] != "pool"
            or rel.parts[1] != st.component
            or any(part in ("", ".", "..") for part in rel.parts)):
        return False
    try:
        _ensure_repo_path(
            st, st.repo_dir / Path(*rel.parts), "state deb")
    except (OSError, RuntimeError):
        return False
    return True


def _data_file(path, label, create_parent=False):
    """Validate a state/cache/lock path without following symlinks.

    These files may intentionally live outside the repository, so the
    repository-specific path helpers cannot be used here.
    """
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise RuntimeError(f"{label} 路径不能包含符号链接: {candidate}")
        if candidate != path and candidate.exists() and not candidate.is_dir():
            raise RuntimeError(f"{label} 的父路径不是目录: {candidate}")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
        for candidate in (path.parent, *path.parent.parents):
            if candidate.is_symlink():
                raise RuntimeError(
                    f"{label} 路径不能包含符号链接: {candidate}")
            if candidate.exists() and not candidate.is_dir():
                raise RuntimeError(f"{label} 的父路径不是目录: {candidate}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"{label} 不是普通文件: {path}")
    return path


def _state_lock_path(st):
    return Path(st.state_file).with_suffix(".lock")


@contextmanager
def state_lock(st, purpose):
    """Acquire the state lock without following a pre-existing symlink."""
    lock_path = _data_file(_state_lock_path(st), "state lock", create_parent=True)
    flags = os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as e:
        raise RuntimeError(f"{purpose} 无法打开 state lock {lock_path}: {e}") from e
    lock = None
    try:
        os.fchmod(fd, 0o600)
        lock = os.fdopen(fd, "r+")
        fd = -1
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(f"另一个 ghapt {purpose} 正在运行,退出。") from e
        yield lock
    finally:
        if lock is not None:
            lock.close()
        if fd != -1:
            os.close(fd)


def atomic_write_text(path, text):
    """Write text through a unique sibling temporary file and replace atomically."""
    path = _data_file(path, "数据文件", create_parent=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    open_fd = fd
    try:
        os.fchmod(fd, 0o600)
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
        p = _data_file(
            st.state_file.parent / HASH_CACHE_NAME,
            "index-cache.json",
            create_parent=True)
        atomic_write_text(p, json.dumps(cache, sort_keys=True) + "\n")
    except (OSError, RuntimeError) as e:
        print(f"    (哈希缓存写入失败,忽略: {e})", file=sys.stderr)


def hashes_of(path: Path, rel, cache):
    stt = path.stat()
    hit = cache.get(rel)
    if (_valid_hash_cache_entry(hit)
            and hit[0] == stt.st_size
            and hit[1] == stt.st_mtime_ns
            and hit[2] == stt.st_ctime_ns
            and hit[3] == stt.st_dev
            and hit[4] == stt.st_ino):
        return stt.st_size, hit[5], hit[6], hit[7]
    size, md5, sha1, sha256 = file_hashes(path)
    cache[rel] = [
        size, stt.st_mtime_ns, stt.st_ctime_ns, stt.st_dev, stt.st_ino,
        md5, sha1, sha256,
    ]
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
    pool = _component_pool(st)
    if pool.is_dir():
        for p in _pool_deb_files(st, pool):
            relpath = p.relative_to(st.repo_dir).as_posix()
            if str(p) in excluded or relpath in excluded:
                continue
            try:
                fields, d = deb_control(p)
                validate_pool_deb_metadata(st, p, fields, d)
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
    return _suite_dist(st), index_files, release_data


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
        rel_path = PurePosixPath(rel)
        if (size < 0 or not rel or "\\" in rel
                or rel_path.is_absolute()
                or rel_path.as_posix() != rel
                or any(part in ("", ".", "..") for part in rel_path.parts)):
            raise ValueError(f"{field} 段包含格式错误的文件路径或大小")
        key = (field, rel)
        if key in out:
            raise ValueError(f"{field} 段包含重复文件路径: {rel}")
        out[key] = (digest.lower(), size)
    return out


RELEASE_HASH_SECTIONS = {"MD5Sum", "SHA1", "SHA256"}


def parse_release_metadata(text):
    """Parse the non-hash fields from a Release file."""
    fields = {}
    for line in text.splitlines():
        if line in {f"{name}:" for name in RELEASE_HASH_SECTIONS}:
            break
        if not line or line[0] in " \t":
            continue
        name, separator, value = line.partition(":")
        if not separator or not name:
            raise ValueError("Release 包含格式错误的元数据行")
        if name in fields:
            raise ValueError(f"Release 重复元数据字段: {name}")
        fields[name] = value.lstrip()
    return fields


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


def _key_file_fingerprints(st, path):
    """Read fingerprints from an exported public-key file without importing it."""
    try:
        _ensure_repo_file(st, path, "公钥")
    except RuntimeError as e:
        return set(), str(e)
    try:
        proc = subprocess.run(
            ["gpg", "--batch", "--no-options", "--show-keys",
             "--with-colons", str(path)],
            capture_output=True, text=True, env=gpg_env(st))
    except OSError as e:
        return set(), str(e)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return set(), detail or f"gpg 返回 {proc.returncode}"
    fingerprints = set()
    for line in proc.stdout.splitlines():
        parts = line.split(":")
        if len(parts) > 9 and parts[0] == "fpr" and parts[9]:
            fingerprints.add(parts[9].lower())
    if not fingerprints:
        return set(), "文件中没有可识别的公钥指纹"
    return fingerprints, ""


def _verify_detached_signature(st, fpr, signature, signed):
    keyring = st.repo_dir / "ghapt.gpg"
    try:
        _ensure_repo_file(st, keyring, "ghapt.gpg")
        _ensure_repo_file(st, signature, "Release.gpg")
        _ensure_repo_file(st, signed, "Release")
    except RuntimeError as e:
        return False, str(e)
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
    try:
        _ensure_repo_file(st, keyring, "ghapt.gpg")
        _ensure_repo_file(st, inrelease, "InRelease")
        _ensure_repo_file(st, release, "Release")
    except RuntimeError as e:
        return False, str(e)
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


def index_matches(dist, index_files, st=None, fpr=None, release_data=None):
    """已发布的索引,是否与"当前 pool 生成的索引"完全一致?返回 (是否一致, 原因)。

    比较两件事:索引文件本身的字节,以及 Release 里对它们的哈希声明。
    Release 的 Date 每次生成都会变,所以这里比的是哈希表而不是 Release 文本 ——
    内容没变时不重新签名,仓库保持真正的幂等。

    这个判定是"pool == 已发布索引"这条不变式在每次 sync 被强制执行的地方:
    只要两者不一致(新增、删除、内容变化、外部改动、上次发布失败),就会重发。
    """
    if st is not None:
        try:
            _ensure_repo_path(st, dist, "发布目录", expect_dir=True)
        except RuntimeError as e:
            return False, str(e)
    for name in ("Release", "Release.gpg", "InRelease"):
        path = dist / name
        if st is not None:
            try:
                _ensure_repo_file(st, path, name)
            except RuntimeError as e:
                return False, str(e)
        if not path.is_file():
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
    if release_data is not None:
        try:
            expected_metadata = parse_release_metadata(
                release_data.decode("utf-8"))
            actual_metadata = parse_release_metadata(
                (dist / "Release").read_text())
        except (OSError, UnicodeError, ValueError) as e:
            return False, f"Release 元数据不可读: {e}"
        expected_metadata.pop("Date", None)
        actual_metadata.pop("Date", None)
        if actual_metadata != expected_metadata:
            return False, "Release 元数据与当前配置不一致"
    for rel, data in index_files.items():
        path = dist / rel
        if st is not None:
            try:
                _ensure_repo_file(st, path, f"索引 {rel}")
            except RuntimeError as e:
                return False, str(e)
        try:
            if not path.is_file():
                return False, f"索引文件缺失: {rel}"
            if path.read_bytes() != data:
                return False, f"索引文件内容不一致: {rel}"
        except OSError as e:
            return False, f"索引文件不可读: {rel} ({e})"
    if st is None or not fpr:
        return False, "无法验证索引签名: 未提供仓库密钥"
    for name in ("ghapt.gpg", "ghapt.asc"):
        key_path = st.repo_dir / name
        try:
            _ensure_repo_file(st, key_path, name)
        except RuntimeError as e:
            return False, f"无法验证签名: {e}"
        if not key_path.is_file():
            return False, f"无法验证签名: 缺少公钥 {name}"
        fingerprints, why = _key_file_fingerprints(st, key_path)
        if not fingerprints:
            return False, f"无法验证签名: 公钥 {name} 无效 ({why})"
        if fpr.lower() not in fingerprints:
            return False, f"无法验证签名: 公钥 {name} 不是当前仓库密钥"
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


def _gpg_colon_unescape(value):
    raw = bytearray()
    pos = 0
    while pos < len(value):
        match = re.match(r"\\x([0-9A-Fa-f]{2})", value[pos:])
        if match:
            raw.append(int(match.group(1), 16))
            pos += match.end()
        else:
            char = value[pos]
            raw.extend(char.encode("utf-8"))
            pos += 1
    return raw.decode("utf-8", "replace")


def _gpg_record_expired(parts, now=None):
    if len(parts) <= 6 or not parts[6]:
        return False
    try:
        expiry = int(parts[6])
    except ValueError:
        return True
    if expiry == 0:
        return False
    return expiry <= int(time.time() if now is None else now)


def _gpg_record_usable(parts):
    """Reject revoked, expired, disabled, and otherwise unusable key records."""
    if len(parts) < 12 or parts[1].lower() in {"", "i", "r", "e", "d"}:
        return False
    if _gpg_record_expired(parts):
        return False
    return True


def _select_signing_key(colon_output, key_name, key_email):
    """Return a matching primary fingerprint from gpg --with-colons output."""
    candidates = []
    current = None
    pending_fingerprint = None
    for line in colon_output.splitlines():
        parts = line.split(":")
        record_type = parts[0] if parts else ""
        if record_type == "sec":
            current = {
                "primary": parts,
                "fpr": None,
                "uids": [],
                "signing": set(),
                "usable": _gpg_record_usable(parts),
            }
            candidates.append(current)
            pending_fingerprint = ("primary", parts)
        elif record_type == "ssb" and current is not None:
            pending_fingerprint = ("subkey", parts)
            if (_gpg_record_usable(parts)
                    and "s" in (parts[11] if len(parts) > 11 else "").lower()):
                current["signing"].add(True)
        elif record_type == "fpr" and current is not None and len(parts) > 9:
            if pending_fingerprint is not None:
                key_type, _key_record = pending_fingerprint
                if key_type == "primary" and current["fpr"] is None:
                    fpr = parts[9].lower()
                    if FINGERPRINT_RE.fullmatch(fpr):
                        current["fpr"] = fpr
                pending_fingerprint = None
        elif record_type == "uid" and current is not None and len(parts) > 9:
            if _gpg_record_usable(parts):
                current["uids"].append(_gpg_colon_unescape(parts[9]))

    wanted_email = key_email.casefold()
    for candidate in candidates:
        primary = candidate["primary"]
        if not candidate["usable"] or not candidate["fpr"]:
            continue
        if "s" not in (primary[11] if len(primary) > 11 else "").lower():
            if not candidate["signing"]:
                continue
        for uid in candidate["uids"]:
            name, email = parseaddr(uid)
            if name == key_name and email.casefold() == wanted_email:
                return candidate["fpr"]
    return None


def _ensure_gpg_home(st):
    home = Path(st.gpg_home)
    for candidate in (home, *home.parents):
        if candidate.is_symlink():
            raise RuntimeError(f"GPG home 路径不能包含符号链接: {candidate}")
    if home.exists() and not home.is_dir():
        raise RuntimeError(f"GPG home 不是目录: {home}")
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home


def ensure_key(st):
    _ensure_gpg_home(st)
    env = gpg_env(st)

    def list_secret_keys():
        try:
            proc = subprocess.run(
                ["gpg", "--batch", "--no-options", "--with-colons",
                 "--list-secret-keys"],
                capture_output=True, text=True, env=env)
        except OSError as e:
            raise RuntimeError(f"查询 GPG secret key 失败: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"查询 GPG secret key 失败 ({proc.returncode}): "
                f"{detail or '(无输出)'}")
        return proc.stdout

    fpr = _select_signing_key(list_secret_keys(), st.key_name, st.key_email)
    if fpr:
        return fpr
    print("[init] 首次运行,生成仓库签名密钥 (RSA 4096),约需几秒...")
    batch = (f"%no-protection\nKey-Type: RSA\nKey-Length: 4096\nKey-Usage: sign\n"
             f"Name-Real: {st.key_name}\nName-Email: {st.key_email}\n"
             f"Expire-Date: 0\n%commit\n")
    run_checked(["gpg", "--batch", "--gen-key"], input=batch, env=env)
    fpr = _select_signing_key(list_secret_keys(), st.key_name, st.key_email)
    if not fpr:
        raise RuntimeError(
            f"已生成 GPG 密钥,但找不到可用于签名且 UID 匹配 "
            f"{st.key_name!r} <{st.key_email}> 的密钥")
    return fpr


def publish(st, fpr, dist, index_files, release_data):
    """构建完整发布树后整体切换,失败时恢复旧树和公钥。

    Unix 无法把一个非空目录直接替换到另一个非空目录,所以提交协议是:
    旧 dist 改名到备份 -> 新 dist 改名到正式位置 -> 原子替换两个公钥。
    任一步失败都尽力撤销已经完成的步骤,并把回滚失败明确报告出来。
    """
    env = gpg_env(st)
    _ensure_repo_root(st, create=True)
    _ensure_repo_path(st, dist.parent, "发布目录父级", expect_dir=True)
    _ensure_repo_path(st, dist, "发布目录", expect_dir=True)
    key_paths = {
        name: _ensure_repo_file(st, st.repo_dir / name, name)
        for name in ("ghapt.asc", "ghapt.gpg")
    }
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
    installed_keys = set()
    dist_installed = False
    dist_backed_up = False
    key_backups = set()

    try:
        for rel, data in sorted(index_files.items()):
            rel_path = PurePosixPath(rel) if isinstance(rel, str) else None
            if (rel_path is None or rel_path.is_absolute()
                    or rel_path.as_posix() != rel
                    or any(part in ("", ".", "..") for part in rel_path.parts)):
                raise RuntimeError(f"索引路径不安全: {rel!r}")
            final = new_dist.joinpath(*rel_path.parts)
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
            key_file = key_stage / name
            key_file.write_bytes(data)
            os.chmod(key_file, 0o644)

        _prepare_public_tree(new_dist)

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


def _prepare_public_tree(root):
    """Apply stable public permissions and reject links in the published tree."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"发布 staging 根目录无效: {root}")
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(f"发布树包含符号链接目录: {path}")
            if not path.is_dir():
                raise RuntimeError(f"发布树目录无效: {path}")
            os.chmod(path, 0o755)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(f"发布树包含符号链接文件: {path}")
            if not path.is_file():
                raise RuntimeError(f"发布树文件无效: {path}")
            os.chmod(path, 0o644)
    os.chmod(root, 0o755)


# ── 状态文件 ──────────────────────────────────────────────────────────

class StateError(RuntimeError):
    """state.json 缺失或损坏。继续操作可能误删整个仓库,所以直接拒绝。"""


def _validate_state(st, raw):
    if not isinstance(raw, dict):
        raise StateError("state 顶层必须是 JSON 对象")
    for project_name, record in raw.items():
        if (not isinstance(project_name, str) or not project_name.strip()
                or "\r" in project_name or "\n" in project_name):
            raise StateError("state 项目名必须是非空单行字符串")
        if not isinstance(record, dict):
            raise StateError(f"state 项目 {project_name!r} 必须是 JSON 对象")
        tag = record.get("tag")
        if (not isinstance(tag, str) or not tag.strip()
                or "\r" in tag or "\n" in tag):
            raise StateError(
                f"state 项目 {project_name!r} 的 tag 必须是非空单行字符串")
        files = record.get("files")
        if not isinstance(files, dict) or not files:
            raise StateError(
                f"state 项目 {project_name!r} 的 files 必须是非空 JSON 对象")
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
        state_file = _data_file(st.state_file, "state 文件")
        text = state_file.read_text()
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
    _validate_state(st, state)
    atomic_write_text(
        st.state_file, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def old_suite_paths(st):
    dists = st.repo_dir / "dists"
    if dists.is_symlink():
        raise RuntimeError(f"dists 路径包含符号链接: {dists}")
    if not dists.exists():
        return []
    if not dists.is_dir():
        raise RuntimeError(f"dists 不是普通目录: {dists}")
    _ensure_repo_path(st, dists, "dists", expect_dir=True)
    return sorted(
        (entry for entry in dists.iterdir() if entry.name != st.suite),
        key=lambda path: path.name)


def cleanup_old_suites(st, dry_run=False):
    """Remove suite directories no longer selected by the configuration."""
    paths = old_suite_paths(st)
    failures = []
    for path in paths:
        if dry_run:
            print(f"    将清理旧 suite: {path.name}")
            continue
        try:
            # Check the parent chain before deleting anything. A suite symlink
            # is removed as a link, never followed into its target.
            _ensure_repo_path(st, path.parent, "旧 suite 父目录", expect_dir=True)
            if _path_part(path.name, "旧 suite") != path.name:
                raise RuntimeError(f"旧 suite 名称不安全: {path.name!r}")
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                _ensure_repo_path(st, path, "旧 suite", expect_dir=True)
                shutil.rmtree(path)
            else:
                raise RuntimeError(f"旧 suite 类型无法处理: {path}")
            print(f"    清理旧 suite: {path.name}")
        except (OSError, RuntimeError) as e:
            failures.append(f"{path}: {e}")
    if failures:
        raise RuntimeError("旧 suite 清理失败: " + "; ".join(failures))
    return paths


# ── 子命令 ────────────────────────────────────────────────────────────

def publish_if_needed(st, dist, index_files, release_data, force=False):
    """索引与 pool 一致就什么都不做,否则签名发布。返回是否真的发布了。"""
    fpr = None
    try:
        _ensure_repo_path(st, dist, "发布目录", expect_dir=True)
        release_path = _ensure_repo_file(st, dist / "Release", "Release")
    except RuntimeError as e:
        release_path = None
        print(f"    现有发布目录不安全,将重建: {e}", file=sys.stderr)
    if release_path is not None and release_path.is_file():
        fpr = ensure_key(st)
    ok, why = index_matches(
        dist, index_files, st=st, fpr=fpr, release_data=release_data)
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

    try:
        _ensure_repo_root(st, create=True)
        with state_lock(st, "sync"):
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

            # state 先落盘,再删除不再被新索引引用的旧版本。这样即使删除
            # 中途失败,下一次 sync/gc 仍能根据 pool 和 state 继续收敛。
            try:
                save_state(st, state)
            except (OSError, TypeError, ValueError) as e:
                print(f"[fail] 索引已发布,但 state 保存失败: {e}。"
                      f"旧版本未删除,请修复 {st.state_file} 后重试。",
                      file=sys.stderr)
                save_hash_cache(st, cache)
                sys.exit(1)

            cleanup_failures = []
            for old in prune:
                try:
                    if old.is_symlink():
                        raise RuntimeError(f"旧版本路径是符号链接: {old}")
                    _ensure_repo_file(st, old, "旧版本")
                    old.unlink(missing_ok=True)
                    print(f"    清理旧版本: {old.name}")
                except (OSError, RuntimeError) as e:
                    cleanup_failures.append(f"{old}: {e}")

            try:
                cleanup_old_suites(st)
            except RuntimeError as e:
                cleanup_failures.append(str(e))

            save_hash_cache(st, cache)
            if cleanup_failures:
                print("[fail] 发布后的清理未完成: " + "; ".join(cleanup_failures),
                      file=sys.stderr)
                sys.exit(1)
            if failures:
                sys.exit(f"以下项目同步失败: {', '.join(failures)}")
    except (OSError, RuntimeError, StateError) as e:
        sys.exit(f"[sync] {e}")


def cmd_check(args):
    st, projects, _ = load_config(args.config)
    try:
        state = load_state(st, strict=True)
    except (OSError, RuntimeError, StateError) as e:
        sys.exit(f"[check] {e}")
    updates = failures = 0
    for p in projects:
        try:
            if st.github_mirror:
                tag = latest_release(
                    p, st.token, st.github_mirror).get("tag_name")
            else:
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
    try:
        _ensure_repo_root(st)
        with state_lock(st, "gc"):
            try:
                state = load_state(st, strict=True)
            except StateError as e:
                sys.exit(f"[gc] {e}")

            names = {p.name for p in projects}
            stale = sorted(k for k in state if k not in names)
            keep_dirs = set()
            for name in names:
                for rel in state.get(name, {}).get("files", {}).values():
                    path = _ensure_repo_path(
                        st, st.repo_dir / Path(*PurePosixPath(rel).parts),
                        "state deb", expect_dir=False)
                    keep_dirs.add(path.parent)
            try:
                package_dirs = _pool_package_dirs(st)
                victims = [d for d in package_dirs if d not in keep_dirs]
                old_suites = old_suite_paths(st)
            except (OSError, RuntimeError) as e:
                sys.exit(f"[gc] 扫描仓库失败: {e}")

            try:
                check_gc_safe(st, projects, victims)
            except StateError as e:
                sys.exit(f"[gc] {e}")

            if not victims and not stale and not old_suites:
                print("[gc] 没有需要清理的内容")
                return
            for d in victims:
                try:
                    count = len(_pool_deb_files(st, d))
                except (OSError, RuntimeError) as e:
                    sys.exit(f"[gc] 无法检查待清理目录 {d}: {e}")
                print(f"[gc] 移除 {d.relative_to(st.repo_dir)}/ ({count} 个包)")
            for k in stale:
                print(f"[gc] 移除 state 条目: {k}")
            for path in old_suites:
                print(f"[gc] 清理旧 suite: {path.name}")
            if args.dry_run:
                print("[gc] --dry-run,未实际改动")
                return

            cache = load_hash_cache(st)
            if victims:
                # 与 sync 相同的顺序:先发布"把这些包排除掉"的索引,成功后再删文件。
                try:
                    doomed = [p for d in victims for p in _pool_deb_files(st, d)]
                except (OSError, RuntimeError) as e:
                    print(f"[gc] 无法准备待清理文件,未删除任何文件: {e}",
                          file=sys.stderr)
                    save_hash_cache(st, cache)
                    sys.exit(1)
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
                    save_hash_cache(st, cache)
                    sys.exit(1)

            next_state = dict(state)
            for k in stale:
                del next_state[k]
            try:
                save_state(st, next_state)
            except (OSError, TypeError, ValueError) as e:
                print(f"[gc] state 保存失败,未删除任何文件: {e}", file=sys.stderr)
                save_hash_cache(st, cache)
                sys.exit(1)

            delete_failures = []
            for d in victims:
                try:
                    if d.is_symlink():
                        raise RuntimeError(f"待清理目录是符号链接: {d}")
                    _ensure_repo_path(st, d, "待清理目录", expect_dir=True)
                    shutil.rmtree(d)
                    try:
                        d.parent.rmdir()  # 首字母目录空了就顺手删掉
                    except OSError:
                        pass
                except (OSError, RuntimeError) as e:
                    delete_failures.append(f"{d}: {e}")

            try:
                cleanup_old_suites(st)
            except RuntimeError as e:
                delete_failures.append(str(e))

            save_hash_cache(st, cache)
            if delete_failures:
                print("[gc] 清理未完全成功: " + "; ".join(delete_failures),
                      file=sys.stderr)
                sys.exit(1)
            print("[gc] 已清理完毕" + (",索引已重建并签名" if victims else ""))
    except (OSError, RuntimeError, StateError) as e:
        sys.exit(f"[gc] {e}")


def cmd_assets(args):
    try:
        repo = _config_github_repo(args.repo, "repo")
    except ConfigError as e:
        sys.exit(f"配置错误: {e}")

    token = os.environ.get("GITHUB_TOKEN", "")
    mirror = os.environ.get("GITHUB_MIRROR", "")
    config_path = getattr(args, "config", None)
    if config_path:
        try:
            st, _projects, _broken = load_config(config_path, strict=False)
        except SystemExit:
            raise
        token = st.token
        mirror = st.github_mirror
    mirror_override = getattr(args, "mirror", None)
    if mirror_override is not None:
        try:
            mirror = _config_github_mirror(mirror_override)
        except ConfigError as e:
            sys.exit(f"配置错误: {e}")

    class P:
        pass

    project = P()
    project.repo = repo
    project.prerelease = args.prerelease
    try:
        if mirror:
            rel = latest_release(project, token, mirror)
        else:
            rel = latest_release(project, token)
    except Exception as e:
        sys.exit(f"查询 release 失败: {e}")
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
    sp.add_argument("-c", "--config",
                    help="读取 token/github_mirror 的配置文件(可选)")
    sp.add_argument("--mirror",
                    help="临时覆盖 github_mirror,必须是 HTTPS URL")
    sp.set_defaults(func=cmd_assets)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
