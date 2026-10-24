#!/usr/bin/env python3
"""一键重跑全部测试:python3 tests/run_all.py

产物:
  tests/logs/*.log  每个场景的完整输出(含 PASS/FAIL 与原始证据)
  tests/work/*      每个场景的隔离工作区(repo/state.json/gnupg/客户端 apt 目录)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LOGS = TESTS / "logs"
SCENARIOS = [
    ("make_fixtures.py", "夹具(真实 dpkg-deb/tar/zstd 造包)"),
    ("scenario_1_happy.py", "快乐路径:deb+binary 同步 → GPG 签名 → 真实 apt 客户端"),
    ("scenario_2_consistency.py", "一致性:P1-1 三条复现路径"),
    ("scenario_3_hash_window.py", "P1-2 Hash Sum mismatch 窗口"),
    ("scenario_4_others.py", "其余条目(版本映射/完整性/tar.zst/架构/gc/...)"),
    ("scenario_5_cli.py", "CLI:并发锁/check 退出码/assets/--project"),
]


def main():
    LOGS.mkdir(exist_ok=True)
    summary, failed = [], 0
    for script, desc in SCENARIOS:
        t0 = time.time()
        res = subprocess.run([sys.executable, script], cwd=TESTS,
                             capture_output=True, text=True, timeout=3600,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        out = res.stdout + res.stderr
        (LOGS / (script.replace(".py", ".log"))).write_text(out)
        passes = out.count("[PASS]")
        fails = out.count("[FAIL]")
        if res.returncode != 0:
            failed += 1
        summary.append((script, desc, passes, fails, res.returncode, time.time() - t0))
        print(f"{'OK ' if res.returncode == 0 else 'FAIL'} {script:30} "
              f"PASS={passes:3} FAIL={fails:2} exit={res.returncode} ({time.time() - t0:.0f}s)")

    print("\n" + "=" * 78)
    print(f"{'场景':32} {'描述':46} PASS/FAIL")
    print("-" * 78)
    tp = tf = 0
    for script, desc, p, f, rc, _ in summary:
        tp += p
        tf += f
        print(f"{script:32} {desc[:44]:46} {p}/{p + f}")
    print("-" * 78)
    print(f"合计:{tp} 通过 / {tf} 失败;失败场景数 {failed}/{len(SCENARIOS)}")
    print(f"完整日志:{LOGS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
