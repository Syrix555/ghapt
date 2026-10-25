# ghapt 测试报告

本文件记录当前测试入口、实际运行结果和环境限制。它取代早期把历史检查数量误写成“全部通过”的报告。

## 运行方式

```bash
python3 tests/run_all.py
```

测试使用本地 `file://` API 夹具，因此不需要访问真实 GitHub，也不需要 root。测试仍然调用仓库的真实 Python 代码，并使用真实的 `dpkg-deb`、`gpg`、APT 和压缩工具生成或验证产物。每个场景的完整输出写入 `tests/logs/*.log`，隔离工作区写入 `tests/work/*`。

## 当前验证结果

单元回归测试：

```text
python3 -m unittest -v tests.test_high_priority tests.test_medium_priority
20 tests, OK
```

其他静态检查：

```text
python3 -m py_compile ghapt.py tests/*.py
bash -n setup.sh
git diff --check
```

完整场景测试的结果必须以最近一次 `python3 tests/run_all.py` 的终端输出和日志为准。当前运行环境的共同失败原因是 GnuPG agent 无法绑定 Unix socket：

```text
gpg: failed to start agent '/usr/bin/gpg-agent'
gpg: can't connect to the agent
绑定套接字到 '/tmp/.../S.gpg-agent' 时出现错误：不允许的操作
```

系统中存在 `gpg`、`gpg-agent`、`gpgconf`、`gpg-connect-agent` 和 pinentry。失败原因是当前 Codex 沙箱在 socket `bind` 阶段返回“不允许的操作”，不是缺少 GnuPG 组件，也不是 pinentry 需要输入密码。`--pinentry-mode loopback` 不能绕过 agent socket 的创建。真实服务器上的 systemd 运行时仍需单独验证。

最近一次完整场景运行记录为：

```text
34 PASS / 21 FAIL
6 个场景中 5 个非零退出
```

这次运行的失败都发生在需要 GPG 签名的路径，并共享上面的 agent socket `bind` 限制；不能据此判断业务断言全部失败。

## runner 的统计边界

`tests/run_all.py` 对每个脚本执行一次，并只统计输出中已经出现的 `[PASS]` 和 `[FAIL]`：

- 脚本返回非零会计入失败场景；
- traceback 之后尚未执行的检查不会自动生成 `[FAIL]`；
- `make_fixtures.py` 本身不输出 PASS/FAIL，不应被当成业务检查；
- 因此 `PASS + FAIL` 是日志标记数量，不等于所有计划断言数量。

早期报告中的场景表为 `35 + 24 + 10 + 33 + 13 = 115`，正文却先后写成 `153`、`159`、`170` 和 `183`。这些数字不能同时成立，也不能用来证明当前版本全绿。

## 已覆盖的修复

当前单元测试覆盖以下关键行为：

- 无变化的同步返回成功，并可重复执行；
- deb 模式使用包内版本并校验包名、版本和架构；
- 多架构 pool 提交失败时回滚；
- Release 两种签名、公钥指纹和内容一致性校验；
- state/cache/lock 的结构校验、路径保护、符号链接拒绝和原子写入；
- GitHub 预发布 release 分页；
- 配置语法错误和损坏 deb 的可控错误；
- 当前 component 隔离；
- pool 规范布局、数字开头的合法包名、嵌套目录/临时文件/符号链接拒绝；
- 旧 suite 符号链接只删除链接本身；
- 发布失败后的 staging 清理和旧发布树恢复。

源码和配置还实现了以下保护：GitHub asset SHA256 digest 校验、`.tar.zst`/`.tzst` 解包、未知压缩容器拒绝、GPG key 能力和身份校验、`gc` 对损坏 state 的保护、旧版本延迟清理、pool 写入原子替换、nginx 配置失败回滚以及 setup 配置文件 `0600` 权限。

## 未完成的验证

以下内容需要正常服务器环境或真实外部服务，当前离线场景不应声称已经通过：

- 真实 GitHub API、HTTP 错误/重定向、CDN 下载和 API 限流；
- systemd 的 `ProtectSystem`、`SystemCallFilter`、`PrivateTmp` 和 GPG agent 运行时行为；
- `setup.sh` 的 root 安装流程、用户/权限变更和 nginx reload；
- nginx 实际静态托管、HTTPS 和客户端网络访问；
- Python 3.8 的实际运行，而不是静态兼容性检查；
- 发布过程中真实并发客户端观察到的目录切换窗口。

`Acquire-By-Hash` 尚未实现，因此发布目录切换期间仍可能出现短暂的 APT `Hash Sum mismatch`；客户端通常会保留旧索引并在后续重试。这个限制不应写成“零窗口发布”或“完全不会出现 hash mismatch”。

## 历史复现的解释

早期场景中记录的 pool/index 分叉、坏签名被误认为一致、deb tag 版本依赖、state 丢失导致 gc 误删、digest 未校验和 `.tar.zst` 被当作裸文件等，均是修复前行为。当前实现已针对这些路径增加代码和回归测试；如果完整场景在具备可用 GPG agent 的环境中仍失败，应以新的日志重新判断，而不能引用历史复现数字代替当前结果。
