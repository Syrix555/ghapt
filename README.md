# ghapt — 把 GitHub Releases 变成你自己的 APT 源

很多 GitHub 开源项目只在 Releases 页面发布 `.deb` 或二进制包，没有 apt 源，无法自动更新。ghapt 在自己的服务器上定时抓取这些项目的最新 release，整理成标准的、带 GPG 签名的 APT 仓库。客户端加一次源，之后 `apt upgrade` 就会顺带升级这些工具。

```text
GitHub Releases --(每日 systemd timer)--> ghapt.py sync
                                             | type=deb    直接收录 .deb
                                             | type=binary 自动重打包成 .deb
                                             v
                                  /srv/ghapt/repo (pool + dists + GPG 签名)
                                             |
                                          nginx 静态托管
                                             |
                                      客户端 apt update
```

只依赖 Python 3.8+ 标准库和系统命令 `dpkg`、`dpkg-deb`、`gpg`。跟踪 `.tar.zst` 或 `.tzst` 资产时还需要 `zstd`。`setup.sh` 会安装并检查必需命令。

## 文件一览

| 文件 | 用途 |
|---|---|
| `ghapt.py` | 同步工具本体（单文件） |
| `config.ini` | 跟踪项目和仓库参数 |
| `setup.sh` | Debian/Ubuntu 服务器部署脚本 |
| `systemd/ghapt-sync.{service,timer}` | 每日自动同步（service 带 systemd 沙箱加固） |
| `nginx/ghapt.conf` | 静态站点配置示例 |
| `tests/` | 离线回归测试和测试报告 |

## 服务器部署

把整个目录传到服务器后运行：

```bash
rsync -av ghapt/ 你的服务器:~/ghapt/
ssh 你的服务器
cd ~/ghapt
sudo bash setup.sh --with-nginx
```

不希望脚本修改 nginx 时，去掉 `--with-nginx`，然后参考 `nginx/ghapt.conf` 手动配置站点。

脚本会安装依赖、创建 `ghapt` 系统用户、安装程序和配置、启用每日 systemd 定时任务，并运行第一次同步来生成签名密钥。配置和环境文件的权限为 `ghapt:ghapt`、`0600`：

- `/opt/ghapt/ghapt.py`：程序；
- `/etc/ghapt/config.ini`：项目配置；
- `/etc/ghapt/env`：可选的 `GITHUB_TOKEN`；
- `/srv/ghapt/repo`：仓库数据；
- `/srv/ghapt/gnupg`：签名私钥，必须备份。

`--with-nginx` 会检查并保留已有站点。干净服务器上会停用 nginx 自带的标准 `default` 站点；如果已有站点使用 `default_server`，ghapt 会去掉自己的默认监听标记并以非默认站点安装。配置替换、软链创建、`nginx -t` 或 reload 任一步失败，脚本都会尝试恢复原配置。

签名依赖 GnuPG agent 的 Unix socket。若服务启动后出现 `gpg-agent` socket 错误，应先检查 systemd 服务的 `AF_UNIX`、`PrivateTmp` 和其他沙箱设置。当前离线测试环境禁止 socket bind，不能代替真实服务器上的运行时验证。

仓库目录切换后，建议检查：

```bash
journalctl -u ghapt-sync -n 50
```

有域名时建议使用 HTTPS：

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx
```

纯 HTTP 也可以使用，但应理解为内网或自用场景。

## 客户端接入

每台客户端执行一次：

```bash
curl -fsSL http://服务器地址/ghapt.gpg | sudo tee /usr/share/keyrings/ghapt.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/ghapt.gpg] http://服务器地址/ stable main" | sudo tee /etc/apt/sources.list.d/ghapt.list
sudo apt update
sudo apt install ripgrep lazygit
```

amd64 和 arm64 客户端都可以使用，前提是项目提供对应架构。

## 添加项目

先查看目标项目最新 release 的资产名：

```bash
python3 ghapt.py assets sharkdp/fd
```

然后在 `/etc/ghapt/config.ini` 添加配置。资产正则必须恰好匹配一个文件，建议使用 `^` 和 `$`，并排除 `.sha256`、`musl` 等不需要的变体：

```ini
[project:fd]
repo = sharkdp/fd
type = deb
asset.amd64 = ^fd_[0-9][^_]*_amd64\.deb$
asset.arm64 = ^fd_[0-9][^_]*_arm64\.deb$

[project:fzf]
repo = junegunn/fzf
type = binary
description = A command-line fuzzy finder
asset.amd64 = ^fzf-[0-9.]+-linux_amd64\.tar\.gz$
asset.arm64 = ^fzf-[0-9.]+-linux_arm64\.tar\.gz$
```

手动同步：

```bash
sudo systemctl start ghapt-sync.service
journalctl -u ghapt-sync -f
```

从配置删除项目后，先用 `gc --dry-run` 预览，再运行 `gc` 清理 pool、state 和索引。

### project 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `repo` | 必填 | GitHub `owner/name` |
| `type` | `deb` | `deb` 直接收录；`binary` 从 tarball、zip、gz 或裸文件提取程序后重打包 |
| `asset.<arch>` | 至少一个 | 资产文件名正则；架构键必须出现在 `[settings] architectures` 中 |
| `package` | 项目名 | binary 模式下的 Debian 包名 |
| `binaries` | `package` | binary 模式要提取的可执行文件名，空格分隔 |
| `description` | 自动生成 | Debian 包描述 |
| `depends` | 空 | Debian `Depends` 字段 |
| `strip_prefix` | `v` | binary 模式从 tag 去掉的前缀；去掉后必须以数字开头 |
| `prerelease` | `false` | `true` 跟踪最近创建的非 draft release，包括预发布版本 |

`.tar.zst` 和 `.tzst` 需要安装 `zstd`。tag 中 `-rc1` 这样的预发布后缀会映射为 Debian 的 `~rc1`，低于对应正式版本，不会把预发布错误地当成升级目标。`deb` 模式的版本来自 `.deb` 内部，不依赖 tag 前缀。

## 运维命令

```bash
python3 /opt/ghapt/ghapt.py check -c /etc/ghapt/config.ini
sudo systemctl start ghapt-sync.service
sudo runuser -u ghapt -- python3 /opt/ghapt/ghapt.py sync -c /etc/ghapt/config.ini --force
sudo runuser -u ghapt -- python3 /opt/ghapt/ghapt.py gc -c /etc/ghapt/config.ini --dry-run
sudo runuser -u ghapt -- python3 /opt/ghapt/ghapt.py gc -c /etc/ghapt/config.ini
```

`check` 的退出码为：`0` 无更新，`10` 有更新且查询全部成功，`2` 至少有一个查询失败（优先于 `10`）。同步会重试网络错误三次；项目配置错误会被点名，`sync` 会继续处理其他项目并以非零退出，`check` 和 `gc` 因为需要完整配置会直接拒绝。

默认每个包保留最近两个版本。`keep_versions` 必须至少为 1；超出保留数量的旧版本会被清理，仍持有更早索引的客户端可能因此得到 404。

## 可靠性与安全

- **索引来自当前 pool**：每次同步都会扫描当前 component 的规范 pool，重新生成 Packages、Release 和签名所需内容，再与已发布内容比较。外部增加、删除或修改 pool 文件会触发重新发布。
- **发布使用 staging 树**：完整索引、Release、`Release.gpg`、`InRelease` 和公钥都准备成功后，才切换发布目录；失败时尽力恢复旧目录并清理临时文件。目录切换本身仍有极短窗口。
- **发布成功后再保存 state**：旧版本会在新索引发布并保存 state 后才删除。若 state 保存失败，命令返回失败，旧版本暂时保留，修复 state 后再次运行会继续收敛。
- **pool 项目级提交**：一个项目的所有架构都准备成功后才写入 pool；某个架构失败不会留下其他架构的孤儿包。
- **下载校验**：先校验 asset size，再校验 GitHub 提供的 SHA256 digest。老 API 没有 digest 时兼容地退化为 size 校验；digest 字段格式错误会使同步失败。
- **签名校验**：同步会验证已有 Release 的两种 GPG 签名、公钥指纹和内容一致性。公钥或签名缺失、损坏或不匹配都会触发重新发布。
- **状态和缓存保护**：state、hash cache、锁文件拒绝符号链接，写入采用同目录临时文件和原子替换。`gc` 遇到缺失或损坏 state 会停止，避免把整个 pool 误判为垃圾。
- **pool 结构保护**：拒绝路径穿越、符号链接、错误层级、嵌套目录、临时残留和非法包文件名。数字包名使用数字首字符目录，例如 `7zip` 使用 `pool/main/7/7zip/`。
- **旧 suite 清理**：配置切换 suite 后，后续 `sync` 或 `gc` 会清理旧的 `dists/<suite>`，并在删除前检查路径安全。
- **Acquire-By-Hash 尚未实现**：APT 在发布目录切换的极短窗口内仍可能观察到混合索引并报告 `Hash Sum mismatch`。通常是一次可重试的更新失败，而不是写坏客户端索引。

## 测试和限制

运行离线回归测试：

```bash
python3 -m unittest -v tests.test_high_priority tests.test_medium_priority
python3 tests/run_all.py
```

当前单元回归测试为 `20/20`。完整场景使用本地 API 夹具，当前 Codex 沙箱禁止 GnuPG agent bind Unix socket，所以不能把该环境中的完整场景失败解释为代码缺少 GnuPG。真实 GitHub API、systemd 运行时沙箱、nginx、`setup.sh` root 流程和 Python 3.8 运行时仍需在对应环境验证。完整结果和统计规则见 `tests/REPORT.md`。
