# ghapt — 把 GitHub Releases 变成你自己的 APT 源

很多 GitHub 开源项目只在 Releases 页面发 `.deb` 或二进制包,没有 apt 源,没法自动更新。
ghapt 在你自己的服务器上定时抓取这些项目的最新 release,自动整理成一个**标准的、带 GPG
签名的 APT 仓库**。客户端加一次源,之后 `apt upgrade` 就会顺带升级这些工具。

```
GitHub Releases ──(每日 systemd timer)──> ghapt.py sync
                                             │  type=deb    直接收录 .deb
                                             │  type=binary 自动重打包成 .deb
                                             ▼
                                  /srv/ghapt/repo (pool + dists + GPG 签名)
                                             │
                                          nginx 静态托管
                                             │
                            客户端 apt update / apt upgrade
```

只依赖 Python 3.8+ 标准库和系统自带的 `dpkg`、`dpkg-deb`、`gpg`(跟踪 `.tar.zst` 资产时另需 `zstd`),
没有第三方组件。`setup.sh` 会检查这些命令是否齐备。

## 文件一览

| 文件 | 用途 |
|---|---|
| `ghapt.py` | 同步工具本体(单文件) |
| `config.ini` | 跟踪哪些项目、仓库参数 |
| `setup.sh` | 服务器一键部署脚本 |
| `systemd/ghapt-sync.{service,timer}` | 每日自动同步(service 带 systemd 沙箱加固) |
| `nginx/ghapt.conf` | 静态站点配置示例 |

## 服务器部署

把整个目录传到服务器,跑部署脚本:

```bash
rsync -av ghapt/ 你的服务器:~/ghapt/
ssh 你的服务器
cd ~/ghapt
sudo bash setup.sh --with-nginx     # 完全不想让脚本碰 nginx 就去掉该参数,参考 nginx/ghapt.conf 手动加 server 块
```

`--with-nginx` 会自动适配服务器上的 nginx 现状:

- **干净服务器**(`sites-enabled/` 里只有自带的 `default` 欢迎页,或为空):ghapt 接管 80 端口,
  欢迎页站点被停用,直接用 IP 就能访问
- **已有站点占用 `default_server`**:ghapt 自动以非默认站点安装,**不动现有站点**;
  装完后需要编辑 `/etc/nginx/sites-available/ghapt.conf`,把 `server_name` 改成专用域名
  (或把 `listen` 改成独立端口,客户端源写 `http://IP:端口/`)
- 不确定自己属于哪种就看 `ls /etc/nginx/sites-enabled/`——注意 `sites-available/` 里的文件
  只是"备着",软链到 `sites-enabled/` 的才实际生效

脚本会:装 gnupg → 建 `ghapt` 系统用户 → 装到 `/opt/ghapt` + `/etc/ghapt/config.ini` →
启用每日定时任务 → 立刻跑第一次同步(自动生成签名密钥)→ 打印客户端接入命令。

- 仓库数据在 `/srv/ghapt/repo`,签名私钥在 `/srv/ghapt/gnupg`(**建议备份**,丢了客户端要重新信任新钥)
- 有域名的话建议上 HTTPS:`sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx`,
  然后客户端源里的 `http://` 改成 `https://`。纯 http apt 也能用(有签名保完整性),内网/自用可接受
- 首次部署后建议看一眼 `journalctl -u ghapt-sync`,确认同步和签名在沙箱下正常(见下文"可靠性与安全")

## 客户端接入(每台机器一次)

```bash
curl -fsSL http://服务器地址/ghapt.gpg | sudo tee /usr/share/keyrings/ghapt.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/ghapt.gpg] http://服务器地址/ stable main" | sudo tee /etc/apt/sources.list.d/ghapt.list
sudo apt update
sudo apt install ripgrep lazygit
```

之后日常 `sudo apt upgrade` 就会一起升级。amd64 和 arm64 客户端都支持(取决于每个项目提供哪些架构)。

> 如果 Ubuntu/Debian 官方源里也有同名包(比如 ripgrep),apt 会自动选版本高的一边;
> GitHub 上的版本几乎总是更新,所以默认就是用你这个源的。

## 添加 / 管理项目

**第一步**:看目标项目最新 release 里的文件叫什么名:

```bash
python3 ghapt.py assets sharkdp/fd
```

**第二步**:在 `/etc/ghapt/config.ini` 里加一段,正则要求**恰好匹配 1 个**资产
(建议 `^...$` 收紧两端,注意排除 `.sha256`、`musl` 变体这类干扰项):

```ini
# release 里直接有 .deb 的项目
[project:fd]
repo = sharkdp/fd
type = deb
asset.amd64 = ^fd_[0-9][^_]*_amd64\.deb$
asset.arm64 = ^fd_[0-9][^_]*_arm64\.deb$

# 只发二进制 tarball 的项目,自动重打包
[project:fzf]
repo = junegunn/fzf
type = binary
description = A command-line fuzzy finder
asset.amd64 = ^fzf-[0-9.]+-linux_amd64\.tar\.gz$
asset.arm64 = ^fzf-[0-9.]+-linux_arm64\.tar\.gz$
```

**第三步**:手动同步一次(不想等定时任务的话):

```bash
sudo systemctl start ghapt-sync.service && journalctl -u ghapt-sync -f
```

**删除项目**:从 config 里删掉对应的 `[project:x]` 段,然后跑一次 `gc`
(先加 `--dry-run` 预览要删什么),它会移除该项目的 pool 文件和 state 条目并重建索引。

### project 段完整参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `repo` | 必填 | GitHub `owner/name` |
| `type` | `deb` | `deb`=收录现成 deb;`binary`=从 tarball(tar.gz/xz/bz2/zst)/zip/gz/裸文件提取可执行文件打包 |
| `asset.<arch>` | 必填≥1 | 该架构资产文件名的正则,须恰好匹配 1 个。`<arch>` 必须出现在 `[settings] architectures` 里,否则配置阶段就会报错(binary 模式下这个键会直接写进包的 `Architecture`,写错会打出一个永远进不了索引的孤儿包) |
| `package` | 项目名 | binary 模式下的包名 |
| `binaries` | 同 package | binary 模式要从压缩包里提取的可执行文件名(可多个,空格分隔) |
| `description` | 自动生成 | binary 模式的包描述 |
| `depends` | 空 | binary 模式的 Depends 字段(大多数 Go/Rust 静态二进制不需要) |
| `strip_prefix` | `v` | 从 tag 生成版本号时去掉的前缀。**若去掉前缀后不是以数字开头(比如上游把 tag 从 `v2.0.0` 改成 `pfx-2.0.0`),同步会直接报错并提示改这个值**,而不是静默生成一个比现有版本还低的垃圾版本号 |
| `prerelease` | `false` | `true` 则跟踪**最近创建**的 release(含预发布)。注意语义是"最近创建"而非"版本号最高",并行维护多条发布线的项目需留意。tag 里的 `-rc1` 这类预发布后缀会映射成 `~rc1`,排序上低于正式版(不会被 apt 当成升级目标) |

`[settings]` 段的参数(架构列表、保留几个历史版本、GPG 密钥信息等)见 `config.ini` 内注释。

## 运维速查

```bash
python3 /opt/ghapt/ghapt.py check -c /etc/ghapt/config.ini    # 查版本不下载;退出码 0=无更新 10=有更新 2=有查询失败(2 优先于 10)
sudo systemctl start ghapt-sync.service                        # 立即同步
journalctl -u ghapt-sync -n 50                                 # 看日志
sudo systemctl edit --full ghapt-sync.timer                    # 改同步频率
sudo runuser -u ghapt -- python3 /opt/ghapt/ghapt.py sync -c /etc/ghapt/config.ini --force     # 强制重建
sudo runuser -u ghapt -- python3 /opt/ghapt/ghapt.py gc -c /etc/ghapt/config.ini --dry-run     # 预览可清理的已删项目
sudo runuser -u ghapt -- python3 /opt/ghapt/ghapt.py gc -c /etc/ghapt/config.ini               # 清理并重建索引
```

- **GitHub API 限流**:匿名 60 次/小时(每项目每次同步 1 次请求,默认每天一次,几十个项目也够)。
  不够就在 `/etc/ghapt/env` 里加 `GITHUB_TOKEN=ghp_xxx`(文件已 600 权限)
- **上游改了资产命名**:同步会报错并把当前资产列表打在日志里,照着改正则即可;单个项目失败不影响其他项目
- **配置写错了**:启动时就会逐个点名报出(缺 `repo`、`type` 拼错、`asset.<arch>` 不在
  `architectures` 里、正则写坏等)。`sync` 会**跳过坏段、继续同步其它项目**并把它们计入退出码;
  `check`/`gc` 因为需要完整的项目清单,会直接拒绝运行
- **上游换了 tag 前缀**:同步会报错并提示该把 `strip_prefix` 改成什么(见上文参数表)
- **磁盘占用**:每包默认保留最近 2 个版本(`keep_versions`),旧的自动清理。
  `keep_versions` 必须 ≥ 1("保留 0 个"等于把刚发布的包也删掉,现在会直接拒绝)
- **`gc` 会自我保护**:`state.json` 丢失/损坏、或它丢了某些项目的记录时,`gc` 会**拒绝执行**
  而不是把整个仓库当垃圾清掉。确认要重建就先备份 pool,再用 `sync` 重新收录

## 可靠性与安全

- **pool 与索引永远一致**:每次 sync 都用「当前 pool 的内容」重新生成索引,再与已发布的内容
  逐字节比对(索引文件本身 + `Release` 里的哈希声明);不一致才重新签名发布。因此
  新增/删除/外部改动/上次发布中断,下一次 sync 都会自动收敛,不需要手工 `--force`
- **先发布、再删旧版本**:`keep_versions` 的清理被推迟到新索引发布成功之后才执行,发布失败时
  一个文件都不删、`state` 也不推进。所以任何时刻已发布的索引都不会指向不存在的文件
  (修复前:发布失败后索引仍列着已被删掉的旧版本,客户端会 404)
- **项目级原子落盘**:一个项目的所有架构都下载并校验通过之后,才会写进 pool。
  某个架构失败时,已经下好的另一个架构也不会留下(修复前会留下永远进不了索引的孤儿文件)
- **下载校验**:先比 `size`,再比 GitHub 公布的资产 `digest`(sha256,2025-06 起提供)。
  尺寸相同但内容被换掉的情况因此能被挡住;API 没给 digest 时退化为只校验尺寸并提示。
  (修复前只比尺寸,同尺寸篡改会原样进包)
- **打包产物自检**:从压缩包取出的文件如果是压缩包本身(说明这个格式还没被支持),
  直接报错而不是装进 `/usr/bin`(修复前 `.tar.zst` 会被当成裸二进制打包,客户端装上一个跑不起来的"程序")
- **发布失败即整体回滚**:签名/发布出错时以非 0 退出并明确报错,仓库保持原样 ——
  旧索引继续可用,旧版本不删,state 不推进
- **原子替换**:所有索引文件先写成 `*.new` 并签名完成,再逐个 `os.replace`
  (索引 → `Release` → `Release.gpg` → `InRelease`),不会读到写了一半的文件。
  注意:替换的过程仍有毫秒级窗口,期间抓取的客户端可能报 `Hash Sum mismatch`
  (apt 会忽略该次下载并可重试);要做到零窗口需要 `Acquire-By-Hash`,目前未实现
- **单项目失败隔离**:某个项目下载/打包失败只记日志并计入退出码,不影响其他项目;网络抖动自动重试 3 次
- **配置校验前置**:`repo`/`type`/`asset.<arch>`(与 `architectures` 交叉校验)/正则可编译性/
  `keep_versions` 是否为整数,都在读配置时检查。修复前 `asset.i386` 这类笔误会静默产出一个
  占着 pool 却永不进索引的包,`keep_versions` 写错则直接抛 Python traceback
- **失败信息可排障**:子进程(dpkg-deb/gpg)出错时,它们的 stderr 会一并打进日志,
  而不是只留一句 `returned non-zero exit status 2`。沙箱里签名失败时能直接看到 gpg 的原因
- **`gc` 自我保护**:`state.json` 缺失、损坏、或丢了已配置项目的记录时,`gc` 拒绝执行
  (修复前:state 丢失会让它把整个 pool 当垃圾清空,客户端随即 `Unable to locate package`)
- **systemd 沙箱**:同步服务以专用系统用户 `ghapt` 运行,`ProtectSystem=strict`
  使除 `/srv/ghapt` 外整个文件系统只读,另有 `NoNewPrivileges`、`PrivateTmp`、
  空 capability 集、`SystemCallFilter=@system-service` 等加固。
  注意 gpg 签名依赖 gpg-agent 的 unix socket,`RestrictAddressFamilies` 必须保留 `AF_UNIX`;
  如果部署后签名报错,优先排查方向是沙箱指令(临时注释 service 里的加固段可快速定位)
- **旧版本保留**:pool 里每包保留最近 `keep_versions` 个版本,拿着旧索引的客户端在索引切换后
  仍能下到上一版的 deb。注意超出 `keep_versions` 的更老版本会被清理,缓存了更早索引的客户端
  届时会 404(这是保留策略本身的语义)
- **pool 写入原子替换**:和索引层一致,先写 `.new` 再 `os.replace`。
  即使某个版本号的内容被上游重发,并发下载的客户端也不会读到写了一半的文件
- **`check` 退出码**:有查询失败时返回 2(优先于"有更新"的 10)——
  查询失败意味着状态未知,比已知有更新更需要被监控看到

## 边界与注意

- **信任模型**:包内容来自上游 release 产物,ghapt 校验下载完整性(尺寸 + GitHub 公布的
  sha256 digest)并对仓库签名,但不改变"你信任该项目作者"这一前提——
  和手动去 release 页下载安装是同级别的信任
- binary 模式支持 `tar.gz`/`tgz`/`tar.xz`/`tar.bz2`/`tar`/`tar.zst`/`tzst`/`zip`/`gz` 和裸二进制;
  遇到别的格式(如 `.tar.lz4`)会明确报错而不是打包一个坏程序。`.tar.zst` 需要系统装有 `zstd`
- binary 模式只打包主程序到 `/usr/bin`,不含 man 手册和 shell 补全;要这些就找提供 deb 的项目或自行扩展
- 资产超过 ~30 个的 release,GitHub API 可能截断列表;常见项目都碰不到这个上限

---

已在 Ubuntu 22.04(Python 3.10 / dpkg 1.21 / gpg 2.2)上完成端到端验证:
真实下载 ripgrep(deb 模式)与 lazygit(binary 重打包模式)→ 生成签名仓库 →
`apt-get update` 通过签名校验 → `apt-get install` 正确解析出两个包 → 重复同步幂等 →
`check` 退出码(0/10)符合预期 → `gc` 正确清理已删项目且清理后仓库仍可正常使用。
systemd 单元通过 `systemd-analyze verify`;沙箱的运行时行为需在服务器首次同步时确认。

另有一套离线回归测试(不需要网络/root,约 30 秒):

```bash
python3 tests/run_all.py
```

用 `file://` 假 GitHub API 驱动真实代码路径,并用真实 `apt-get` 校验签名与哈希;
覆盖首次同步、幂等、升级与保留策略、部分失败、发布失败、外部改动收敛、gc 顺序等。详见 `tests/REPORT.md`。
