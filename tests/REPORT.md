# ghapt 实测报告

本文件是 `tests/` 这套测试的说明与结果汇总,用来给上一轮代码审阅的每条结论提供**可复现的证据**,
并**修正其中不准确的部分**。

```
python3 tests/run_all.py        # 全套约 35 秒(不需要网络、不需要 root)
```

> **修复进展(已完成)**:报告里的 15 条结论中,**14 条已修并回归通过**,P1-2(by-hash)改为
> README 如实描述;当前无待办条目。详见文末"修复进展"一节。
> 文中标注"修复前"的现象保留原样,作为这些结论当时成立的记录。

- 产物:`tests/logs/*.log`(每个场景的完整输出,含 PASS/FAIL 与原始证据)、`tests/work/*`(隔离工作区)。
- 网络层:沙箱禁止一切 socket(含回环),因此 `ghapt.API_ROOT` 被指向 `tests/fakeapi`(file:// 目录树)。
  `gh_json` / `download` / `extract_binaries` / `place_deb` / `generate_indexes` / `publish` 走的是
  **ghapt.py 的真实代码路径**,只有 socket 传输被换成读本地文件。
- 客户端侧用的是**真实 apt 2.4.14**(`file:` 源 + `signed-by`),签名与哈希校验都是真的在跑。
- 夹具用真实 `dpkg-deb` / `tar --zstd` 造包,gpg 2.2.27 真生成 RSA-4096 密钥并签名。

## 结果总览

| 场景 | 覆盖内容 | 结果 |
|---|---|---|
| `scenario_1_happy.py` | 首次同步(deb 收录 + binary 重打包)→ 索引自洽 → GPG 签名 → 真实 apt 客户端 update/install → 幂等 → 升级 + keep_versions → 单项目失败隔离 → gc | 35/35 |
| `scenario_2_consistency.py` | **P1-1**:pool 与索引不一致的三条复现路径(A 部分失败 / B 粘性 / C `--force` 才生效 / D publish 失败 / E 日志自相矛盾) | 24/24 |
| `scenario_3_hash_window.py` | **P1-2**:"原子发布就不会 Hash Sum mismatch"的说法是否成立(两个方向都测) | 10/10 |
| `scenario_4_others.py` | P1-3 版本号映射、P2-1 完整性、P2-2 `.tar.zst`、P2-3 架构校验、P2-5 gc 抹库、P2-6 配置中断、P3 若干 | 33/33 |
| `scenario_5_cli.py` | flock 互斥、`check` 退出码语义、`assets` 子命令、`--project` 过滤 | 13/13 |

**合计 115 项检查全部通过**——即:上面每一条"PASS"都是对被测行为的**确认**(快乐路径确认无 bug,
bug 复现场景确认 bug 存在),没有 FAIL。

---

## 一、端到端快乐路径确实是通的(被低估的部分)

这些都是独立验证过的,不是读代码推断:

- deb 模式原样收录上游 `.deb`;binary 模式从 tarball 提取并重打包,`dpkg-deb -f` 显示
  `Package/Version/Architecture/Homepage` 都正确,从 deb 里取回的二进制与上游 tarball **字节一致**。
- **索引自洽性**:`Release` 里 18 条哈希记录全部与磁盘文件相符;`Packages` 的 `Filename` 都指向真实
  存在的 pool 文件;`Packages.gz` 解压后与 `Packages` 字节一致。
- **签名**:`gpg --verify` 独立校验 `InRelease`(`--clearsign`)与 `Release.gpg`(`-abs` 分离签名)都通过;
  真实 apt 客户端 `apt-get update` 成功且**零 W:/E: 告警**;`apt-get -s install` 能解析出正确的包与版本,
  来源显示 `ghapt:stable`。
- **负向测试**:把 `signed-by` 换成另一个公钥 → apt 报
  `NO_PUBKEY ... The repository ... is not signed`,exit 100。证明签名校验确实在生效(不是"签名形同虚设")。
- 删掉 `InRelease`、只留 `Release + Release.gpg` → apt 依然更新成功(两个签名产物都可用)。
- **幂等**:第二次 `sync` 两个项目都报"已是最新",`InRelease` 的**字节和 mtime 都没变**。
- **keep_versions=2**:升级到 2.0.0 后 1.0.0 仍在 pool 且仍在索引里,持有旧索引的客户端确实还能下到上一版
  ——README 这条承诺真实成立。
- **单项目失败隔离**(资产改名场景):失败项目打印出该 release 的完整资产列表(README 承诺成立),
  另一个项目不受影响,失败项目不污染已发布索引。
- **gc**:`--dry-run` 预览 → 真跑 → 项目目录与 state 条目移除 → 索引重建 → 客户端 apt 依然可用。
- **flock**:持锁时 `sync`/`gc` 都以明确的 "另一个 ghapt ... 正在运行" 退出,锁释放后正常;
  锁文件按 `state_file.with_suffix(".lock")` 落在同目录。
- **CLI**:`check` 的 0/10/2 三个退出码实测符合文档;`assets` 子命令输出可用于写正则;
  `--project` 只同步指定项目且不写别的项目 state。
- **systemd 单元**:`systemd-analyze verify`(systemd 249)对本仓库两个单元**零告警**;
  并做了自证——故意把 `Nice=10` 改成 `Nice=notanumber` 后 verify 立即报
  `broken.service:12: Failed to parse nice priority` ,说明这个"零告警"是有意义的。

---

## 二、确认的缺陷(带复现证据)

### P1-1 pool 与索引可以永久不一致(最严重,已复现三条路径)

**A. 部分失败吞掉"已改动"标志**

`ghapt.py:508-515` 的 `changed |= sync_project(...)` 在异常路径上**不会执行**,而
`sync_project` 里每个架构的 `place_deb`(写 pool + prune)是**边做边生效**的。

注意:**触发条件必须是"至少一个架构已经落盘之后"的失败**。资产正则失配发生在下载之前
(`:308-317` 的匹配循环在下载循环之前),所以那种情况不会造成分叉——这一点我上一轮说错了,
本次测试先证伪、再换用正确的触发器(arm64 资产内容损坏,下载能过、`dpkg-deb -f` 失败):

```
下载 multia_2.0.0_amd64.deb (0 KiB)      ← amd64 已下载并落盘
下载 multia_2.0.0_arm64.deb (0 KiB)
[fail] multia: Command '['dpkg-deb', '-f', '/tmp/ghapt-6eol4wbk/multia_2.0.0_arm64.deb']' returned non-zero exit status 2.
[done] 没有变化,索引未动。
```
随后:
```
pool = [multia_1.0.0_amd64.deb, multia_1.0.0_arm64.deb, multia_2.0.0_amd64.deb]
索引 = ['1.0.0']            ← 2.0.0/amd64 永远进不了索引
```

**B. 不会自愈(粘性)**:连续再同步 3 次,每次都报同一个失败,索引始终停在 `1.0.0`,
   而 pool 里那个 2.0.0 一直是个孤儿。只要上游那个架构持续坏着,**另一个架构的更新就永远发布不出去**。

**C. `--force` 会把孤儿"顺带"发布进索引**:同一份 pool、同样的上游状态,
   正常 `sync` 永不索引,`--force` 之后索引里立刻出现 2.0.0。这直接说明
   **"进不进索引"取决于 publish 何时被触发,而不是 pool 的内容** ——"pool == 索引"这条不变式在代码里
   没有任何地方被强制。

**D. publish 失败 → 索引指向已被 prune 删除的文件(客户端硬 404)**

`save_state` 在 publish **之前**(`:515` vs `:517`),而 prune 在 publish 之前已经删了旧版本。
模拟 publish 失败(gpg/磁盘/沙箱任一环节)后:

```
pool  = [tooldeb_2.0.0, tooldeb_3.0.0]        ← 1.0.0 已被 prune 删除
索引  = ['1.0.0', '2.0.0']                    ← 仍然列着已经不存在的 1.0.0
state = v3.0.0                                ← 已推进
```
客户端侧硬证据:
```
$ apt-get download tooldeb=1.0.0
Err:1 file:/... stable/main amd64 tooldeb amd64 1.0.0
  File not found - /.../pool/main/t/tooldeb/tooldeb_1.0.0_amd64.deb (2: No such file or directory)
E: Failed to fetch ...      exit=100
```
而且**粘性**:publish 恢复正常后再同步 3 次,每次都报 `已是最新`/`没有变化`,索引照旧列着那个
不存在的文件,只有 `--force` 能恢复。

**E. 日志自相矛盾**:`state.json` 丢失后重新同步,同一份输出里既打印
`[new] tooldeb: v1.0.0 → tooldeb_1.0.0_amd64.deb` 又打印 `[done] 没有变化,索引未动。`
(`[new]` 在 `:334` 是无条件打印的),而下一次同步开始就只报"已是最新"——任何"没进索引"的偏差
从此被永久掩盖。

**修法**(最小改动):异常分支里也标记需要重建;或让 publish 幂等化(`gz_bytes` 已经是确定性的,
可以把新生成的 `Packages*`/各 arch `Release` 与磁盘逐字节比较);把 `save_state` 挪到 publish 成功之后。

### P1-2 "原子发布就不会 Hash Sum mismatch"的说法不成立

按 README 的顺序,`publish()` 会在两次 `os.replace` 之间**真实停留**在一个混合状态。用真实 apt 复现:

- **W1**(索引已换、InRelease 还是旧的 = 代码里的真实中间态):
  `Get:2 ... Packages [304 B]` → `Err:2 ... Hash Sum mismatch` → `E: Failed to fetch ... exit=100`
- **W2**(反过来先换 InRelease、索引还是旧的):同样 `Hash Sum mismatch`,exit=100
- **对照**(终态一致):exit=0,客户端选中 2.0.0,零告警

两个方向都失败,说明**换顺序救不了**;业界解法是 `Acquire-By-Hash: yes` + `by-hash/SHA256/<hash>`
(实测仓库既没有 `Acquire-By-Hash` 字段,也没有 `by-hash` 目录)。

**但要准确说清影响**:apt 报错后是
`E: Some index files failed to download. They have been ignored, or old ones used instead.`
——**更新失败(可重试),不是客户端被写坏**;而且窗口只有毫秒级。所以这是**文档准确性问题**
(README 承诺了做不到的保证),不是危险缺陷。要么实现 by-hash,要么修改措辞。

### P1-3 版本号映射的两个静默错误方向(客户端侧已验证)

| 场景 | 生成的版本 | 实测后果 |
|---|---|---|
| tag `v1.2.3-rc1` | `1.2.3.rc1` | dpkg 认为 `1.2.3.rc1 > 1.2.3`;**真实 apt 把预发布版选为升级目标**:`Inst binrc (1.2.3.rc1 ghapt:stable [amd64])` |
| tag `pfx-2.0.0`(默认 `strip_prefix=v` 不命中)| `0.pfx.2.0.0` | dpkg 认为它 **比 1.2.3 旧**;工具照旧打印 `[new] pfx: pfx-2.0.0 → ...`,而客户端永远停在 `Inst pfx (1.2.3 ...)` —— tag 前进了、包永远不更新,且没有任何告警 |

**修法**:`-` 后跟字母时映射为 `~`(`1.2.3~rc1`,实测 `lt 1.2.3` 正确);tag 不以 `strip_prefix`
开头时告警;生成物不以数字开头直接判为配置错误。

### P2-1 只校验 size,不校验哈希

- size 不符:会被重试(实测 `网络错误` 2 次后失败,即共 3 次尝试,与 README 的"重试 3 次"一致),
  最终 `[fail] ... 下载不完整`。截断能被抓到。
- **同尺寸篡改抓不到**:拿裸二进制资产翻一个字节(尺寸不变)后同步,
  工具报成功,而**被篡改的字节原样进入已发布的 deb**:
  `发布出去的 /usr/bin/rawtool sha256=1c799cc2... == 篡改本 1c799cc2...`,而 API 在 fixture 里公布的
  `digest` 是 `94f5d1c6...`(ghapt 完全不读这个字段)。客户端侧毫无感知(apt update 零告警)。
  → README `:159` 的"校验下载完整性"只对截断成立;GitHub 自 2025-06 起在 release 资产上提供
  不可变 SHA256 `digest`,校验成本几乎为零。

### P2-2 `.tar.zst` 被当成裸二进制,静默产出坏包

上游发 `.tar.zst`(`ARCHIVE_SUFFIXES` 里没有它)→ `extract_binaries` 走 `else` 分支直接 `copy2`:
```
[new] toolzst: v1.0.0 → toolzst_1.0.0_amd64.deb        exit=0
/usr/bin/toolzst 前 4 字节 = 28b52ffd (zstd 魔数)      ← 不是 ELF
```
客户端侧无感(签名、哈希全部正常)。装到客户端就是一个跑不起来的"程序"。
→ 兜底分支应校验 ELF magic(或至少报错),并补 `.tar.zst` 支持。

### P2-3 `asset.<arch>` 不做校验(仅 binary 模式会变成孤儿)

binary 模式下 config 的 arch 键会被**直接写进 control 的 Architecture**:
`asset.i386`(而 `architectures = amd64 arm64`)→ pool 里确实有 `archtypo_1.0.0_i386.deb`,
`Architecture: i386`,但 **amd64/arm64 两个索引都是空的**,sync 报成功 —— 无声孤儿,白占磁盘。

**修正上一轮的表述**:deb 模式下 `asset.<arch>` 只用于挑选资产,**索引归属由 deb 自己的
`Architecture` 决定**,所以 deb 模式写错架构键不会产生孤儿(实测 `asset.i386` + amd64 的 deb →
正常进入 binary-amd64 索引)。这个坑只在 binary 模式成立。

### P2-5 state 丢失 + gc = 把整个仓库清空

`load_state()` 把 `FileNotFoundError`/`JSONDecodeError` 静默变成 `{}` → `keep_dirs` 为空 →
所有包目录都成了"待清理":
```
[gc] 移除 pool/main/t/toolbin/ (1 个包)
[gc] 移除 pool/main/t/tooldeb/ (1 个包)
→ pool 变空、索引变空 → 客户端: E: Unable to locate package tooldeb   (项目其实还在 config 里)
```
`--dry-run` 能预览,但默认就是破坏性的。
→ state 缺失/损坏而 pool 非空时应拒绝执行(或强制先 dry-run)。

### P2-6 一个配置段写错 = 全部项目都不同步

`[project:broken]` 缺 `repo` → `ValueError: [project:broken] 缺少有效的 repo = owner/name`
直接抛出 traceback,**连正常的项目也不会跑**,与 README 的"单项目失败不影响其他项目"不一致
(那条隔离只覆盖同步阶段,不覆盖配置解析阶段)。

### P2-4 子进程失败的 stderr 被丢掉(排障体验)

上面那条 `[fail]` 只给出:
```
Command '['dpkg-deb', '-f', '/tmp/ghapt-6eol4wbk/multia_2.0.0_arm64.deb']' returned non-zero exit status 2.
```
dpkg-deb 的真实原因(`is not a Debian format archive`)完全没打出来;而且路径在
`/tmp` 临时目录里、日志读到的时候已经被删了,连事后取证都做不到。
README `:154` 明确让用户"沙箱报错时优先排查沙箱指令",而沙箱里 gpg 失败时日志恰好最没有信息。
→ 包一层 helper 把 stderr 带进异常消息。

### P3 其余(均已实测)

- **`keep_versions = 0` 一个旧版本都不清**(`:276` 的 `if keep > 0 else []`),与"保留 0 个"的直觉相反。
- **pool 写入是原地截断重写**:同一版本号换内容后 inode 不变(`inode: 537727 → 537727`,sha256 已变),
  说明 `place_deb` 用的是 `shutil.copy2(deb, target)` 而非 `os.replace`;索引层那么讲究"先 `.new` 再原子替换",
  pool 层却允许并发客户端读到半截文件。
- **未文档化的 `dpkg` 依赖**:PATH 里没有 `dpkg` 时 `dpkg_vercmp` 直接
  `FileNotFoundError: 'dpkg'`。README `:19` 只说依赖 `dpkg-deb` 与 `gpg`。
  (`dpkg` 通常与 `dpkg-deb` 同装,所以影响小,但文档不准。)
- **`check` 的退出码优先级**:同时存在"有更新"和"查询失败"时返回 `10` 而不是 `2`,
  失败只在输出里出现一次。README 只列了三个码、没写优先级,监控脚本会漏掉查询失败。
- **日志打印的是资产声明大小**(`asset['size'] // 1024`),780 字节的包显示 `(0 KiB)`,纯观感问题。

### 已被**证伪 / 降级**的上一轮结论(重要)

- **P2-10(`dpkg_vercmp` 把非法版本判为"更大")**:行为真实(`dpkg_vercmp('bad','1.0') == 1`,
  因为 dpkg 对"比较为假"和"版本非法"都返回 1),但**实际不可达**:`dpkg-deb` 自己就拒绝
  `Version: bad`(实测报 `'Version' 字段值 'bad': 版本号没有以数字开头`),而 binary 模式的版本号由
  `deb_version_from_tag` 生成、必定以数字开头。→ 降级为"理论问题,不需要改"。
- **P1-2 的影响面**:不是"客户端损坏",而是**一次失败的 `apt update`**(可重试,apt 保留旧索引)。
- **systemd `TimeoutStartSec` 会杀掉长任务**:查 systemd 文档确认
  `except when Type=oneshot is used, in which case the timeout is disabled by default`
  → **不成立**,不要改这一项(上一轮我没有把它写进结论,这里明确排除)。

---

## 三、本次**未能**验证的部分(环境限制,不要当成已通过)

| 项目 | 原因 |
|---|---|
| 真实 GitHub API 行为 | 沙箱禁止一切 socket(含 127.0.0.1),连回环都被拒;API 层是 file:// 桩 |
| 302 跳 CDN、限流 60/h、`Retry-After`、30 资产截断上限 | 同上(截断上限只有外部报告佐证,本机无法复现) |
| `download()` 里 HTTP 特有的失败重试(HTTPError 401/403/404 立即失败、其余重试) | 无 HTTP,只有 `with_retry` 的 OSError 分支被测到 |
| systemd 沙箱的运行时行为(`ProtectSystem`/`SystemCallFilter`/gpg-agent socket、UMask 对 nginx 可读性的影响) | 需要 root + systemd,本机是用户态容器;`systemd-analyze verify` 只能做静态校验 |
| nginx 站点(`autoindex`、`location ~ /\.` 与 certbot `/.well-known` 的交互) | 本机未装 nginx |
| `setup.sh` 端到端 | 需要 root,且会改系统(apt/useradd/systemctl) |
| Python 3.8 兼容性 | 本机只有 3.10;仅静态确认用到的 API(`Path.unlink(missing_ok=True)` 等)是 3.8+ 可用 |
| 并发客户端在 publish 过程中抓取(真实的中间态窗口长度) | 需要真实并发与亚秒级时序 |

---

## 四、建议的修复顺序(结合实测)

1. **P1-1(D/E 两条最要命)**:publish 失败/部分失败后 pool 与索引的偏差是**永久且静默**的,
   还会让客户端对索引里承诺的版本硬 404。先把"发布"做成幂等(内容比对)或让异常路径也触发重建,
   并把 `save_state` 挪到 publish 之后。
2. **P1-3**:版本号映射改成 `~` 语义 + `strip_prefix` 未命中告警(实测会导致"永远不更新"或"装上预发布")。
3. **P2-5**:gc 在 state 缺失时拒绝执行——这是唯一会**一键清空仓库**的路径。
4. **P2-4**:把子进程 stderr 带进异常(这条几乎不花成本,直接决定线上排障速度)。
5. **P2-1/P2-2**:用 GitHub `digest` 校验;兜底分支校验 ELF magic。
6. **P2-3/P3**:配置阶段校验 `asset.<arch>`;`keep_versions=0` 语义;pool 写入改 `os.replace`。
7. README 三处断言按实测修正(P1-2 的保证、P2-1 的"完整性校验"、`:125` 的"参数见 config.ini 内注释")。

## 五、把测试留在仓库里

`tests/` 目录本身作为回归测试保留:全部离线、约 35 秒、不需要 root。
第 1 轮里"修复后应该翻成 FAIL"的场景 2/3/4 已经全部改写成断言**修复后行为**的回归测试
(见下一节),因此现在全套应当是全绿;任何一条转红都说明改动破坏了某条不变式。

---

## 六、修复进展(P1/P2/P3 全量,截至第 5 轮)

| 条目 | 状态 | 关键改动 |
|---|---|---|
| **P1-1** pool 与索引永久分叉 | ✅ 已修 | 项目级两阶段落盘;`generate_indexes(exclude=…)` 支持"先发布后删";`publish_if_needed` 每次 sync 用内容比对强制 `pool == 索引`;`save_state` 挪到发布成功之后;发布失败即整体回滚 |
| **P1-3** 版本号映射 | ✅ 已修 | `-` 后跟字母 → `~`(预发布排到正式版之下);tag 去掉前缀后不以数字开头则报 `VersionError` 并提示改 `strip_prefix` |
| **P2-5** gc 清空仓库 | ✅ 已修 | `load_state(strict=True)` 对缺失/损坏报错;`check_gc_safe` 在"要删的包属于仍在配置里的项目"时拒绝执行 |
| **P2-4** stderr 被丢弃 | ✅ 已修 | 新增 `run_checked()`,所有子进程失败都把真实 stderr 带进异常(`gpg --export` 走 binary 模式) |
| **P2-1** 内容完整性 | ✅ 已修 | `parse_digest` 解析 `sha256:<hex>`;`download` 在 size 之后校验内容;非 sha256/缺失时跳过并提示。同尺寸篡改现在会被挡下 |
| **P2-2** `.tar.zst` / 坏包自检 | ✅ 已修 | 新增 zstd 解包路径(`.tar.zst`/`.tzst`,依赖系统 `zstd`);取出物若是**压缩容器**则报错,不再静默装进 `/usr/bin` |
| **P2-3** `asset.<arch>` 校验 | ✅ 已修 | `Project` 拿到 `architectures` 做交叉校验;顺带校验正则非空且可编译。`asset.i386` 这类笔误现在配置阶段就报错 |
| **P2-6** 配置错误中断全部 | ✅ 已修 | `load_config(strict=)`:`sync` 跳过坏段继续跑其它项目并把坏段计入退出码;`check`/`gc` 需要完整清单故直接退出。`[settings]` 段非整数值也给出干净报错 |
| **P1-2** Hash Sum mismatch | ⚠️ 改为如实描述 | 未实现 `Acquire-By-Hash`;README 已把"不会报 mismatch"的保证改成"存在毫秒级窗口,apt 会忽略该次下载并可重试" |
| **P3** `keep_versions=0` | ✅ 已修 | 校验为 `>= 1` 并说明替代做法(旧行为是"一个都不清",与字面相反;真按字面执行则会删掉刚发布的包) |
| **P3** pool 原子写 | ✅ 已修 | `place_deb` 改成先写 `.new` 再 `os.replace`,与索引层一致;失败不留临时文件 |
| **P3** `dpkg` 依赖 | ✅ 已修 | README 依赖说明补上 `dpkg`(与 `zstd` 的可选性);`setup.sh` 安装并检查必需命令 + Python 版本 |
| **P3** `check` 退出码优先级 | ✅ 已修 | 有查询失败时返回 2(优先于 10),并输出两个计数;README 同步说明 |
| **P3** `dpkg_vercmp` 非法版本 | ✅ 已修 | 用 `dpkg --validate-version` 前置校验,非法版本报错而非被当成"更大" |

状态汇总:**14 条已修 / 1 条改为如实描述 / 0 条待办**(P1-2 的 `Acquire-By-Hash` 是唯一
未实现的改动,已降级为文档修正)。

修复过程中被测试抓出来的**我自己引入的问题**(共 5 处),值得记下:

1. P2-5 的第一版保险用"已发布索引是否引用该文件"判断,结果把**正常的 gc 全部拦死**
   (正常 gc 删的恰恰是当前索引还列着的包),并且会误伤"换了配置、还没同步新项目就 gc"的正常流程。
   改用"包名是否属于仍在配置里的项目"后两者都正确。
2. 引入 `run_checked` 时,`gpg --export` 输出的是二进制,被 `text=True` 解码而崩
   (`memoryview: a bytes-like object is required`),由场景 1 立刻暴露;补 `binary=True` 修复。
3. P2-2 的产物自检第一版要求"必须是 ELF",把 **shell/python 脚本类的合法产物全拒了**
   (三个测试项目当场转红);改成只拦压缩容器(魔数识别)后正确。
4. P3 的非法版本校验第一版用 `dpkg --compare-versions v eq v`,实测它对 `bad`/`...`/空串
   **同样返回 0**,等于没修;换 `dpkg --validate-version` 后才真正区分。
5. P2-6 改 `load_config` 返回值后,`--project` 边界一度没覆盖,补了测试(指名好项目时成功、
   指名坏段时报错)。

第 2 轮结束时的状态:**153 项检查全部通过**(场景 2 由 47 增至 48 项,新增 P2-4 的断言)。

### 第 3 轮(P2-1 / P2-2)

- **P2-1**:`download()` 增加 `expect_sha256`;`sync_project` 从 asset 的 `digest` 取值。
  实测:同尺寸翻一个字节 → `下载内容与 GitHub 公布的 sha256 不符` 并给出期望/实际哈希,
  内容不落盘、不进 pool;`digest` 为 `null`/缺失/非 sha256/长度不对时一律退化为只校验尺寸且不报错
  (真实 API 对老资产就是 `digest: null`,必须兼容)。
- **P2-2**:补 `.tar.zst`/`.tzst`(用系统 `zstd` 先解一层,`tarfile` 原生不支持 zstd);
  并加"产物自检"兜底。

**这里踩了一个坑,值得记下**:自检的第一版要求取出物必须是 ELF,结果把
**shell/python 脚本类的合法产物**全拒了(binrc/pfx/archtypo 三个测试项目当场转红)。
上游发脚本是很常见的,所以判据改成**只拦压缩容器**(gzip/zstd/xz/bzip2/zip/lz4/7z/rar/lzip/tar 魔数),
脚本与 ELF 一律放行。已用 12 种真实样本逐一验证判定结果。

第 3 轮结束时的状态:**159 项检查全部通过**(场景 4 由 47 增至 53 项)。

### 第 4 轮(P2-3 / P2-6)

- **P2-3**:`Project(name, sec, architectures)` 交叉校验 `asset.<arch>` 是否在
  `[settings] architectures` 里(不匹配就报错并说明"这个架构不会被索引,包只会白占 pool 空间"),
  同时校验正则非空、可编译。实测把 `architectures` 补上 `i386` 后即可正常收录并进入索引。
- **P2-6**:`load_config(path, strict=)` 返回 `(settings, projects, broken)`。
  `sync` 用 `strict=False`:坏段逐个点名、跳过、计入退出码,其余项目照常同步并发布;
  `check`/`gc` 用 `strict=True`:需要完整项目清单,遇坏段直接退出。
  `[settings]` 段的 `keep_versions` 非整数也从 `invalid literal for int()` 变成
  `keep_versions 必须是整数(当前 '两个')`。
- `--project NAME` 的边界也补了测试:指名好项目时成功退出(坏段仍警告),指名坏段时明确报错。

第 4 轮结束时的状态:**170 项检查全部通过**(场景 4 由 53 增至 61 项,场景 5 由 13 增至 16 项)。

### 第 5 轮(P3 全部)

- **`keep_versions`**:加 `minimum=1` 校验。旧行为 `keep_versions=0` 是"一个都不清",
  与字面意思相反;而真按字面执行会把刚发布的包也删掉。现在直接拒绝,并提示
  "想彻底不清理就设一个大数,想清空某个包请删项目再跑 gc"。
- **pool 原子写**:`place_deb` 用 `.new` + `os.replace`。实测同一版本号换内容后 **inode 变了**
  (说明是 rename 而非原地截断);模拟 `copy2` 失败时无 `.new` 残留、已发布的包完好。
- **`dpkg` 依赖**:README 的依赖说明改为 `dpkg`/`dpkg-deb`/`gpg`(并注明 `zstd` 只在
  跟踪 `.tar.zst` 时才需要);`setup.sh` 现在安装 `dpkg` 和 `python3`,并在启动时逐个
  `command -v` 检查必需命令、校验 Python >= 3.8。
- **`check` 退出码**:有查询失败时返回 **2**(优先于"有更新"的 10),并打印
  `[check] N 个有更新,M 个查询失败`。理由:查询失败是"状态未知",比"已知有更新"更需要
  被监控看到;旧行为返回 10 会让监控把失败当成正常情况。
- **`dpkg_vercmp` 非法版本**:改用 `dpkg --validate-version` 前置校验。
  **这里第一次尝试是错的**:我先用 `dpkg --compare-versions v eq v` 判合法性,
  实测发现它对 `bad`、`...`、空串**同样返回 0**(因为 dpkg 把语法警告降级了),
  于是非法版本照样被当成"更大"。换成 `--validate-version` 后才真正区分开
  (它甚至区分语法错=1 与硬错=2)。合法版本的排序行为已逐项回归。

第 5 轮结束时的状态:**183 项检查全部通过**(场景 4 由 61 增至 73 项,场景 5 由 16 增至 17 项)。
至此报告中列出的 P1/P2/P3 条目除 P1-2(by-hash,已改为如实描述)外全部处理完毕。

