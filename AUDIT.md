# ghapt 代码审查记录

审查对象：`/home/syrix/ghapt`

审查日期：2026-10-24

这份文档记录审查时观察到的行为、复现证据、测试限制、已完成修复和待修复项目。

## Git 管理说明

仓库已经迁移为标准 Git 布局，唯一的 Git 元数据目录是项目根目录下的 `.git`。审查基线提交为：

```text
5b6b630 chore: establish audit baseline
```

本轮修复暂时保留在工作区，尚未创建修复提交。若当前运行环境仍将 `.git/index` 作为只读文件暴露，修改源码和测试不受影响，但 `git add`、提交和更新索引需要在可写的 Git 工作区中执行。

## 本轮修复状态

已完成并补充回归测试：

- 无变化的 `sync_project()` 返回空列表，第二次 `sync` 不再因把 `False` 加入列表而失败。
- `type=deb` 直接使用包内版本，不再要求 Git tag 匹配 `strip_prefix`。
- 收录 `.deb` 前校验 `Package`、`Architecture` 和 Debian `Version`，binary 模式还校验重打包版本。
- 多架构包先全部准备，再以项目为单位提交到 pool；提交失败时回滚已经写入的架构。
- 拒绝多个资产架构映射到同一个 pool 文件，并在底层收录路径再次校验 Debian 版本。
- `index_matches()` 验证 `Release.gpg`、`InRelease`、签名指纹和 InRelease 解出的 `Release` 内容。
- `publish()` 在 staging 树中生成索引、签名和公钥，切换失败时恢复旧发布树并清理临时目录。
- 严格校验 `state.json` 和 `index-cache.json` 的结构、路径和摘要字段。
- 预发布 release 跟随 GitHub `Link` 分页，配置语法错误和损坏 deb 转换为可控失败。
- 限制路径组成部分、隔离 component、校验 pool deb 文件名，并使用唯一临时文件原子写入状态和缓存。

新增的 `tests/test_high_priority.py` 覆盖上述修复和重复 pool 目标边界，共 7 项回归测试；`tests/scenario_1_happy.py` 也检查第二次 `sync` 的退出码。
新增的 `tests/test_medium_priority.py` 覆盖中优先级修复，共 6 项回归测试。

## 验证范围和限制

已执行：

```text
python3 -m py_compile ghapt.py tests/*.py
python3 -m unittest -v tests.test_high_priority
python3 -m unittest -v tests.test_medium_priority
git diff --check
```

语法检查、两组回归测试和 diff 检查均通过：高优先级 `7/7`，中优先级 `6/6`。

完整测试当前结果为 `36 PASS / 19 FAIL`，且只有 `5/6` 个场景正常返回。失败的共同根因是当前 Codex 沙箱无法让 GnuPG agent 绑定 Unix socket：

```text
gpg: failed to start agent '/usr/bin/gpg-agent'
gpg: can't connect to the agent
绑定套接字到 '/tmp/.../S.gpg-agent' 时出现错误：不允许的操作
```

系统中实际存在 `gpg`、`gpg-agent`、`gpgconf`、`gpg-connect-agent` 和 pinentry，版本为 GnuPG `2.2.27`。因此这不是缺少可执行文件，也不是 pinentry 交互导致的失败；是沙箱的 seccomp/IPC 限制拒绝了 Unix domain socket 的 bind。`--pinentry-mode loopback` 也不能绕过 socket 创建失败。真实部署还应结合 `systemd/ghapt-sync.service` 的沙箱设置验证。

测试运行器只统计日志中已经写出的 `[PASS]` 和 `[FAIL]`，场景在 traceback 后尚未执行的断言不会自动计入失败。因此 `tests/REPORT.md` 声称的“183 项全部通过”不能作为当前版本的完整验证证据。该报告的场景表合计为 `35 + 24 + 10 + 33 + 13 = 115`，与正文中的 `153`、`159`、`170`、`183` 互相矛盾。

## 高优先级问题

### P1：幂等 sync 第二次运行会失败（已修复）

`sync_project()` 在项目已经是最新时返回布尔值 `False`，而 `cmd_sync()` 无条件执行 `touched += sync_project(...)`。第二次同步打印“已是最新”后会触发 `TypeError: 'bool' object is not iterable`，最终以非零退出。多个项目中只要有一个项目无变化，整体同步也可能失败。

相关位置：`ghapt.py` 中的 `sync_project()` 和 `cmd_sync()`。

修复结果：无变化时返回空列表，并增加第二次 `sync` 退出码必须为 0 的断言。

### P1：publish() 不是事务操作（已修复）

`publish()` 为索引、Release 和签名生成临时文件，却逐个调用 `os.replace()`。如果中途替换失败，仓库会出现新旧索引混合、Release 与索引不匹配、签名对应旧内容以及残留 `.new` 文件的状态。公钥导出发生在索引替换之后，导出失败也可能留下“索引已更新、公钥未更新”的半提交。

已通过注入 `os.replace()` 异常复现：`Packages` 已是新内容，而 `Packages.gz`、`Release`、`Release.gpg` 和 `InRelease` 仍为旧内容。

修复结果：使用 staging 目录生成完整发布树和公钥，切换失败时恢复旧 `dist` 和公钥，并删除 staging。测试覆盖切换失败后的恢复和清理。目录切换仍会产生极短的“旧目录已改名、新目录尚未改名”窗口；若部署要求发布期间绝对不能出现目录缺失，应进一步改为稳定 symlink 或版本目录指针。

### P1：索引一致性检查不验证 GPG 签名（已修复）

`index_matches()` 只检查 `Release`、`Release.gpg`、`InRelease` 是否存在，比较 Release 中的哈希表，并比较 Packages 文件字节。它不验证 detached signature、clearsigned signature、签名密钥或签名内容。

已构造正确的 Packages 和 Release 哈希，但把两个签名替换为垃圾内容；`index_matches()` 仍返回 `(True, '')`。之后 sync 会跳过重新签名，APT 客户端才会发现签名无效。

修复结果：使用仓库导出的公钥验证两种签名，检查签名指纹，并比较 InRelease 解出的内容与 `Release`。签名损坏、密钥缺失或签名不是当前仓库密钥时会强制重新发布。

### P1/P2：deb 模式错误地依赖 Git tag 生成 Debian 版本（已修复）

`sync_project()` 对所有项目无条件调用 `deb_version_from_tag()`。`type=deb` 的版本应从 `.deb` 内部读取，tag 主要用于选择 release；一个合法的 `release-1.0.0` tag 会因为默认 `strip_prefix=v` 而在下载前失败。

修复结果：只有 `type=binary` 从 tag 生成版本；`type=deb` 使用并校验包内版本。

### P1/P2：deb 包内部元数据没有和配置校验（已修复）

`place_deb()` 以包内的 `Package`、`Version` 和 `Architecture` 命名和归类，但没有检查它们是否匹配配置的项目包名和 asset 架构。配置声明的包可能实际发布成另一个包，配置的 `amd64` asset 也可能包含 `arm64` 包，state 仍会错误记录为 `amd64`。

修复结果：校验 `Package == project.package`、`Architecture` 是否匹配 asset 架构键或为 `all`，以及 Debian 版本格式；binary 模式另外要求包内版本等于 tag 映射版本。

### P1/P2：多架构收录仍可能半提交（已修复）

下载和构建虽然已经按项目完成两阶段处理，但 `place_deb()` 仍逐架构写入 pool。第一个架构写入成功、第二个架构失败时，第一个包已经落盘，而 state 尚未更新。

已复现：`amd64` 包出现在 pool，`state.json` 仍为空。

修复结果：所有架构先复制到项目级 staging，目标文件替换失败时恢复本轮已经提交的文件，并清理 staging。

## 中优先级问题

### P2：state.json 只检查 JSON 语法，不检查结构（已修复）

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

修复结果：严格模式要求顶层为对象，项目记录、`tag`、`files`、架构键和文件路径均通过结构及路径校验；路径必须是仓库内 `pool/` 下的 `.deb` 文件。普通同步模式遇到损坏状态时给出警告并按空状态处理，`check`/`gc` 等严格流程继续拒绝执行。

### P2：损坏的 index-cache 可能 traceback 或污染索引（已修复）

缓存顶层为 `[]` 或 `null` 时会在 `.items()` 处抛异常。当前只检查值是长度为 5 的 list，不检查 size、mtime、摘要长度和十六进制格式。伪造 size/mtime 可能使错误摘要直接进入 Packages 和 Release。

修复结果：缓存顶层非对象时退化为空缓存，并逐条检查路径、size、mtime、MD5、SHA1 和 SHA256 的类型、范围及格式；非法条目被丢弃，命中缓存前再次验证条目结构。

### P2：预发布模式只读取 GitHub 前 10 个 release（已修复）

请求使用 `per_page=10`，没有处理 GitHub Link 分页。如果前 10 个都是 draft 或不符合条件，而第 11 个才是可用 release，会错误报告没有 release。

修复结果：解析 GitHub `Link` 响应头并跟随 `rel="next"`，直到找到非 draft release 或确认分页结束。

### P2：配置解析异常没有转换成可控错误（已修复）

`ConfigParser.read()` 及相关解析过程没有完整捕获 `configparser.Error`。语法错误、缺少 section、重复 section 或重复 option 可能直接 traceback。

修复结果：捕获 `configparser.Error` 和 UTF-8 解码错误，转换为带配置文件路径的稳定错误和非零退出码。

### P2：pool 中损坏的 deb 会导致 traceback（已修复）

`generate_indexes()` 扫描 pool 后直接读取每个 `.deb` 的 control 信息。损坏包会抛异常，且该过程发生在发布异常处理之前，可能把用户带入 traceback。

修复结果：索引生成捕获 deb 读取和哈希异常，错误中包含具体路径；`sync`/`gc` 在发布或删除前停止并返回非零，不产生未处理 traceback。

### P2：路径组成部分缺少安全校验（已修复）

`component`、`suite`、项目包名和架构等值参与路径拼接，没有限制绝对路径、`..` 和路径分隔符。已用 `component = ../../outside` 复现仓库目录外写入风险。

修复结果：`suite`、`component`、`package`、架构键等配置值限制为安全的单一路径组成部分，pool 和状态/缓存路径还会在解析后确认位于仓库范围内。

### P2：切换 component 后旧 component 会污染新索引（已修复）

索引生成扫描整个 `repo/pool`，但索引路径使用当前 component。配置从一个 component 切换到另一个后，旧 component 的 deb 可能进入新 component 的 Packages，而 Filename 仍指向旧 component。

修复结果：索引生成和清理计划只扫描当前配置的 `pool/<component>`，旧 component 的包不会进入新 component 的索引，也不会被当前 component 的清理逻辑误删。

### P2：异常命名的 pool deb 可能导致清理阶段崩溃（已修复）

`prune_pool_plan()` 和 `prune_candidates()` 原先通过字符串切分推断版本和架构。pool 中出现命名异常的 `.deb` 时，清理逻辑可能抛出 `IndexError`，并且错误不指向具体文件。

修复结果：统一解析 `<package>_<version>_<architecture>.deb`，校验包名、架构和 Debian 版本；异常文件会阻止清理并报告具体路径。

### P2：状态和缓存写入使用固定临时文件（已修复）

`state.json` 和 `index-cache.json` 原先使用固定的 `.tmp` 文件名。外部并发进程或残留临时文件可能互相覆盖，写入中断也可能留下不完整文件。

修复结果：使用同目录唯一临时文件，写入并 `fsync()` 后通过 `os.replace()` 原子替换；异常时清理临时文件。

## 其他风险和改进项

- 发布索引后删除旧版本或保存 state 失败，可能导致状态文件与实际 pool 不一致。
- Release 的 Origin、Label、Description 等元数据变化不会单独触发重发布。
- `ghapt.gpg` 或 `ghapt.asc` 被删除/损坏时，如果索引仍匹配，普通 sync 不会重新导出公钥。
- 仅使用 mtime 和 size 的缓存无法检测同大小且 mtime 恰好相同的内容替换。
- suite 修改后旧的 `dists/<old-suite>` 不会自动删除。
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

## 后续工作

剩余风险主要集中在发布后 state/pool 的故障恢复、缓存的极端碰撞、旧 suite 清理、公钥选择与能力校验、pool 临时文件清理，以及 `setup.sh` 的配置权限。完整端到端 GPG 测试仍需要具备可用 Unix socket 的环境；当前沙箱限制已在“验证范围和限制”中记录。

本轮已修改 `ghapt.py`、新增 `tests/test_medium_priority.py` 并更新本审查文档；未创建 commit。
