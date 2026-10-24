# ghapt 代码审查记录

审查对象：`/home/syrix/ghapt`

审查日期：2026-10-24

这份文档记录审查时观察到的行为、复现证据、测试限制和待修复项目。代码修复会在独立提交中进行，避免把审查基线和修复结果混在一起。

## Git 管理说明

工作区中的 `.git` 是只读挂载，无法在其中创建 Git 元数据。因此本项目使用 `.git-meta/.git` 作为工作树的 Git 后端。命令需要显式指定：

```bash
git --git-dir=.git-meta/.git --work-tree=. status
git --git-dir=.git-meta/.git --work-tree=. diff
git --git-dir=.git-meta/.git --work-tree=. log --oneline
```

`.git-meta/` 已加入 `.gitignore`，不会进入项目内容。后续如果运行环境允许把只读的 `.git` 替换为普通目录，可以再迁移到标准布局。

## 验证范围和限制

已执行：

```text
python3 -m py_compile ghapt.py tests/*.py
```

语法检查通过。

完整测试当前结果为 `36 PASS / 19 FAIL`，且只有 `5/6` 个场景正常返回。失败的共同根因是当前 Codex 沙箱无法让 GnuPG agent 绑定 Unix socket：

```text
gpg: failed to start agent '/usr/bin/gpg-agent'
gpg: can't connect to the agent
绑定套接字到 '/tmp/.../S.gpg-agent' 时出现错误：不允许的操作
```

系统中实际存在 `gpg`、`gpg-agent`、`gpgconf`、`gpg-connect-agent` 和 pinentry，版本为 GnuPG `2.2.27`。因此这不是缺少可执行文件，也不是 pinentry 交互导致的失败；是沙箱的 seccomp/IPC 限制拒绝了 Unix domain socket 的 bind。`--pinentry-mode loopback` 也不能绕过 socket 创建失败。真实部署还应结合 `systemd/ghapt-sync.service` 的沙箱设置验证。

测试运行器只统计日志中已经写出的 `[PASS]` 和 `[FAIL]`，场景在 traceback 后尚未执行的断言不会自动计入失败。因此 `tests/REPORT.md` 声称的“183 项全部通过”不能作为当前版本的完整验证证据。该报告的场景表合计为 `35 + 24 + 10 + 33 + 13 = 115`，与正文中的 `153`、`159`、`170`、`183` 互相矛盾。

## 高优先级问题

### P1：幂等 sync 第二次运行会失败

`sync_project()` 在项目已经是最新时返回布尔值 `False`，而 `cmd_sync()` 无条件执行 `touched += sync_project(...)`。第二次同步打印“已是最新”后会触发 `TypeError: 'bool' object is not iterable`，最终以非零退出。多个项目中只要有一个项目无变化，整体同步也可能失败。

相关位置：`ghapt.py` 中的 `sync_project()` 和 `cmd_sync()`。

修复方向：无变化时统一返回空列表，或者由调用方明确处理布尔值；增加第二次 `sync` 的退出码断言。

### P1：publish() 不是事务操作

`publish()` 为索引、Release 和签名生成临时文件，却逐个调用 `os.replace()`。如果中途替换失败，仓库会出现新旧索引混合、Release 与索引不匹配、签名对应旧内容以及残留 `.new` 文件的状态。公钥导出发生在索引替换之后，导出失败也可能留下“索引已更新、公钥未更新”的半提交。

已通过注入 `os.replace()` 异常复现：`Packages` 已是新内容，而 `Packages.gz`、`Release`、`Release.gpg` 和 `InRelease` 仍为旧内容。

修复方向：使用 staging 目录完成整套发布后整体切换，或实现可恢复的发布协议；统一清理临时文件；公钥也使用临时文件加原子替换；增加逐个替换失败和公钥导出失败的测试。

### P1：索引一致性检查不验证 GPG 签名

`index_matches()` 只检查 `Release`、`Release.gpg`、`InRelease` 是否存在，比较 Release 中的哈希表，并比较 Packages 文件字节。它不验证 detached signature、clearsigned signature、签名密钥或签名内容。

已构造正确的 Packages 和 Release 哈希，但把两个签名替换为垃圾内容；`index_matches()` 仍返回 `(True, '')`。之后 sync 会跳过重新签名，APT 客户端才会发现签名无效。

修复方向：使用当前仓库密钥验证 `Release.gpg` 和 `InRelease`，签名无效时强制发布。

### P1/P2：deb 模式错误地依赖 Git tag 生成 Debian 版本

`sync_project()` 对所有项目无条件调用 `deb_version_from_tag()`。`type=deb` 的版本应从 `.deb` 内部读取，tag 主要用于选择 release；一个合法的 `release-1.0.0` tag 会因为默认 `strip_prefix=v` 而在下载前失败。

修复方向：仅对 `type=binary` 从 tag 生成重打包版本；deb 模式直接使用包内版本，并按项目策略校验。

### P1/P2：deb 包内部元数据没有和配置校验

`place_deb()` 以包内的 `Package`、`Version` 和 `Architecture` 命名和归类，但没有检查它们是否匹配配置的项目包名和 asset 架构。配置声明的包可能实际发布成另一个包，配置的 `amd64` asset 也可能包含 `arm64` 包，state 仍会错误记录为 `amd64`。

修复方向：校验 `Package == project.package`，校验 `Architecture` 与 asset 架构键相符或明确允许 `all`，并校验包内版本符合项目策略。

### P1/P2：多架构收录仍可能半提交

下载和构建虽然已经按项目完成两阶段处理，但 `place_deb()` 仍逐架构写入 pool。第一个架构写入成功、第二个架构失败时，第一个包已经落盘，而 state 尚未更新。

已复现：`amd64` 包出现在 pool，`state.json` 仍为空。

修复方向：所有架构先写入项目级 staging，全部成功后再统一提交；或者在项目提交失败时删除本轮已经写入的 pool 文件。

## 中优先级问题

### P2：state.json 只检查 JSON 语法，不检查结构

以下内容可能被接受并在后续流程中触发异常或错误状态：

```json
[]
```

```json
null
```

```json
{"project": {"files": []}}
```

应校验顶层、项目记录、`tag`、`files`、架构键和仓库内相对路径的类型与格式。

### P2：损坏的 index-cache 可能 traceback 或污染索引

缓存顶层为 `[]` 或 `null` 时会在 `.items()` 处抛异常。当前只检查值是长度为 5 的 list，不检查 size、mtime、摘要长度和十六进制格式。伪造 size/mtime 可能使错误摘要直接进入 Packages 和 Release。

应对缓存逐条严格验证，异常条目丢弃，顶层非 dict 退化为空缓存。

### P2：预发布模式只读取 GitHub 前 10 个 release

请求使用 `per_page=10`，没有处理 GitHub Link 分页。如果前 10 个都是 draft 或不符合条件，而第 11 个才是可用 release，会错误报告没有 release。

应循环处理分页，直到找到可用 release 或确认列表结束。

### P2：配置解析异常没有转换成可控错误

`ConfigParser.read()` 及相关解析过程没有完整捕获 `configparser.Error`。语法错误、缺少 section、重复 section 或重复 option 可能直接 traceback。

应转换为清晰的配置错误和稳定的非零退出码。

### P2：pool 中损坏的 deb 会导致 traceback

`generate_indexes()` 扫描 pool 后直接读取每个 `.deb` 的 control 信息。损坏包会抛异常，且该过程发生在发布异常处理之前，可能把用户带入 traceback。

应报告具体路径并阻止发布，确保错误可控。

### P2：路径组成部分缺少安全校验

`component`、`suite`、项目包名和架构等值参与路径拼接，没有限制绝对路径、`..` 和路径分隔符。已用 `component = ../../outside` 复现仓库目录外写入风险。

应限制为安全的单一路径组成部分，并对最终路径 `resolve()` 后确认仍在 `repo_dir` 内。

### P2：切换 component 后旧 component 会污染新索引

索引生成扫描整个 `repo/pool`，但索引路径使用当前 component。配置从一个 component 切换到另一个后，旧 component 的 deb 可能进入新 component 的 Packages，而 Filename 仍指向旧 component。

应只扫描当前 component，或在配置迁移时明确处理旧 component。

## 其他风险和改进项

- 发布索引后删除旧版本或保存 state 失败，可能导致状态文件与实际 pool 不一致。
- Release 的 Origin、Label、Description 等元数据变化不会单独触发重发布。
- `ghapt.gpg` 或 `ghapt.asc` 被删除/损坏时，如果索引仍匹配，普通 sync 不会重新导出公钥。
- `save_state()` 和 `save_hash_cache()` 使用固定临时文件名，外部并发进程可能互相覆盖。
- 仅使用 mtime 和 size 的缓存无法检测同大小且 mtime 恰好相同的内容替换。
- suite 修改后旧的 `dists/<old-suite>` 不会自动删除。
- `prune_pool_plan()` 对异常命名的 `.deb` 可能抛 `IndexError`。
- `place_deb()` 写入临时文件时若中途异常，清理依赖后续流程，应显式清理。
- `ensure_key()` 只选第一个 secret key，没有确认它对应配置的 key name/email，也没有检查签名能力。
- `setup.sh` 将可能包含 token 的配置安装为 `0644`，同机其他用户可读取。

## 已确认的历史修复

以下修复在当前源码中确实存在：

- 所有架构先下载、校验、构建，再写入 pool。
- 校验 GitHub `sha256:<hex>` digest。
- 使用系统 `zstd` 处理 `.tar.zst` 和 `.tzst`。
- 未知压缩容器不再静默作为裸程序发布。
- `asset.<arch>` 与 `settings.architectures` 交叉校验。
- pool 写入使用 `.new` 加 `os.replace()`。
- 子进程 stderr 会进入错误信息。
- GC 对缺失/损坏 state 及配置中的项目包有保护。
- `keep_versions >= 1`。
- `check` 在查询失败和有更新同时出现时返回查询失败码。
- 预发布 tag 使用 Debian `~` 语义。
- `strip_prefix` 不匹配时明确报错。

## 修复顺序

当前计划按以下顺序推进：

1. 修复幂等 sync 的返回值协议。
2. 修复 deb 模式的版本来源和包元数据校验。
3. 修复多架构 pool 提交的一致性。
4. 加强 state/cache、配置错误和损坏 deb 的输入校验。
5. 增加 GPG 签名验证和发布失败清理。
6. 修复分页、路径边界和 component 隔离。
7. 为已修复问题补充回归测试，并在具备可用 GPG agent 的环境中重新运行完整测试。

本轮文档和 Git 基线建立时尚未修改业务代码。
