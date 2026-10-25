# ghapt 代码审查记录

审查对象：`/home/syrix/ghapt`

审查日期：2026-10-25

本报告记录当前实现、修复状态、验证证据和仍然存在的运行环境限制。历史报告中的现象如果已经修复，会明确标为“修复前行为”。

## Git 状态

仓库已经重新建立为唯一的标准 Git 工作树，当前已有提交：

```text
5b6b630 chore: establish audit baseline
b6971ca fix: address high-priority repository consistency issues
3f289b0 fix: complete medium-priority robustness fixes
293683f fix: complete repository hardening and deployment safeguards
```

本轮继续修复和文档同步没有创建 commit，用户可以在检查工作区后自行提交。

## 当前已修复的问题

### 高优先级

- **幂等同步失败**：修复前无变化的 `sync_project()` 返回 `False`，调用方把它当可迭代对象处理，第二次 `sync` 会失败。现在无变化返回空列表，并覆盖第二次同步退出码。
- **多架构半提交**：所有架构先下载、校验和准备，之后再按项目提交到 pool；后续架构失败会回滚本轮已经写入的文件。
- **发布半提交**：索引、Release、两种签名和公钥都在 staging 树准备完成后才切换。发布失败时会恢复旧发布树并清理临时目录。
- **签名未验证**：`index_matches()` 现在验证 `Release.gpg`、`InRelease`、签名指纹，以及 InRelease 解出的 Release 内容。签名损坏、密钥不匹配或公钥缺失都会触发重发布。
- **deb 版本来源错误**：`type=deb` 直接使用并校验包内版本，不再要求 Git tag 匹配 `strip_prefix`。`type=binary` 仍从 tag 生成 Debian 版本，并将预发布后缀映射为 `~`。
- **deb 元数据错配**：收录前校验包内 `Package`、`Version`、`Architecture` 与项目配置和资产架构的关系，避免错误资产进入错误索引。

### 中优先级

- **状态文件结构不受控**：严格校验 `state.json` 的顶层、项目记录、tag、文件路径和架构映射。
- **哈希缓存不受控**：严格校验 `index-cache.json` 的顶层、路径、文件属性和 MD5/SHA1/SHA256 字段；坏条目被丢弃，不会直接污染索引。
- **预发布分页缺失**：预发布模式会跟随 GitHub `Link` 头分页，直到找到可用 release 或确认列表结束。
- **配置错误直接 traceback**：配置解析、重复 section/option、编码错误和字段校验错误会转换成可读的配置错误。
- **损坏 deb 直接 traceback**：索引生成和清理阶段会报告具体文件和子进程 stderr，并以可控错误停止发布。
- **路径穿越和符号链接**：suite、component、package、架构、state/cache/lock 和 pool 路径均做安全校验；仓库路径链中的符号链接会被拒绝。
- **component 污染**：索引和清理只扫描当前配置的 `pool/<component>`，其他 component 的包不会混入当前索引。
- **pool 结构不规范**：只接受 `pool/<component>/<首字符>/<package>/<package>_<version>_<arch>.deb`；首字符目录支持合法的数字包名，例如 `7zip`，错误层级、嵌套目录、临时文件和符号链接会被拒绝。
- **旧 suite 清理缺少路径保护**：删除前重新检查父目录和目标路径，旧 suite 符号链接只删除链接本身，不跟随到仓库外。

### 其他已修复或加固项

- GitHub asset 的 `sha256:<64 位十六进制>` digest 在下载后校验；没有 digest 时兼容地退化为 size 校验，格式错误会使同步失败。
- `.tar.zst` 和 `.tzst` 通过系统 `zstd` 解包；未知压缩容器不会被静默当成可执行文件发布。
- pool、state 和缓存写入采用同目录临时文件、`fsync()` 和 `os.replace()`。
- `state` 锁使用 `flock`，并拒绝预先存在的符号链接。
- `gc` 对缺失/损坏 state、丢失仍在配置项目记录和异常 pool 结构进行保护，避免误删整个仓库。
- `keep_versions` 要求至少为 1。
- `check` 在“有更新”和“查询失败”同时发生时返回查询失败码 2。
- `ensure_key()` 校验配置 UID、指纹、有效期、撤销状态和签名能力。
- `setup.sh` 将可能包含 token 的 `/etc/ghapt/config.ini` 和 `/etc/ghapt/env` 设置为 `ghapt:ghapt`、`0600`；递归修正 `/srv/ghapt` 所有权时不跟随内部符号链接；nginx 配置安装失败时统一回滚。
- 可选 `github_mirror` 会把 API、分页和 release 资产 URL 改写到用户配置的 HTTPS 镜像；只改写明确支持的 GitHub 域名，并继续校验下载大小和 SHA256 digest。

## 测试结果

离线回归单元测试当前通过：

```text
python3 -m unittest -v tests.test_high_priority tests.test_medium_priority
24 tests, OK
```

静态检查通过：

```text
python3 -m py_compile ghapt.py tests/*.py
bash -n setup.sh
git diff --check
```

完整场景测试使用真实的 `dpkg-deb`、`gpg`、APT 和本地 file API 夹具。当前运行结果应以 `tests/REPORT.md` 和 `tests/logs/*.log` 为准。当前环境中 GnuPG 组件实际存在，但 Codex 沙箱禁止 `gpg-agent` 创建 Unix domain socket：

```text
gpg: failed to start agent '/usr/bin/gpg-agent'
gpg: can't connect to the agent
绑定套接字到 '/tmp/.../S.gpg-agent' 时出现错误：不允许的操作
```

这不是缺少 `gpg`、`gpg-agent` 或 pinentry，也不是 pinentry 交互失败；失败发生在 agent 为 `S.gpg-agent` 等 socket 执行 `bind` 时，属于当前沙箱的 seccomp/IPC 限制。`--pinentry-mode loopback` 也无法绕过 agent socket 创建。真实服务器仍需验证 systemd 服务的运行时沙箱和 GPG 行为。

最近一次完整场景运行记录为 `34 PASS / 21 FAIL`，6 个场景中 5 个非零退出。失败都发生在需要 GPG 签名的路径，并共享上述 agent socket `bind` 限制；这次结果不能等同于 21 个业务缺陷。

`tests/run_all.py` 只统计日志中已经出现的 `[PASS]` 和 `[FAIL]`。场景在 traceback 后尚未执行的断言不会自动计为失败，因此旧版报告中“115/153/159/170/183 项全部通过”的数字不能作为当前完整验证证据；原场景表本身也存在 `35 + 24 + 10 + 33 + 13 = 115` 与正文数字互相矛盾的问题。

## 当前仍存在的限制

- **未实现 Acquire-By-Hash**：发布目录在切换过程中仍有极短的目录替换窗口。APT 可能在这个窗口观察到新旧索引混合并报告 `Hash Sum mismatch`；APT 通常会保留旧索引并允许下次重试。若需要消除这个窗口，应实现 `Acquire-By-Hash` 目录和对应 Release 元数据。
- **state 保存失败的恢复语义**：索引发布成功后才保存 state。若 state 保存失败，命令返回失败、旧版本暂不删除，仓库索引已经更新。修复 state 后再次运行会重新收敛；期间需要关注日志和磁盘占用。
- **发布目录切换不是无窗口切换**：staging 树降低了索引文件互相不一致的时间和范围，但目录重命名本身仍可能产生极短的观察窗口。
- **缓存的理论碰撞**：缓存同时使用 size、mtime、ctime、device、inode 和三种摘要，正常文件替换可以被识别；如果所有身份和时间属性都被恶意恢复，任何基于元数据的缓存仍存在极端碰撞可能。
- **真实部署尚未在本环境完整端到端验证**：包括真实 GitHub HTTP API、CDN 重定向、限流、systemd 运行时沙箱、nginx reload、`setup.sh` 的 root 流程和 Python 3.8 运行时。当前测试使用本地 file API 夹具，systemd 只做过静态单元校验。

## 结论

此前审查提出的高优先级和中优先级问题已经落实到源码并有回归测试覆盖。本轮还补齐了 pool 结构校验、数字开头 Debian 包名、旧 suite 路径保护、nginx 失败回滚、递归 chown 安全和相关文档。剩余工作主要是 Acquire-By-Hash、真实部署环境验证，以及 state 保存失败和发布目录切换窗口的运维监控。
