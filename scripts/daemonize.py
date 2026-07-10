#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条命令彻底脱离当前会话跑成守护进程（double-fork + setsid），
使其父进程变为 init(PPID=1)，IDE/agent 会话回收也不会杀掉它。

用法：
  python3 scripts/daemonize.py <logfile> <cmd> [args...]
打印子进程 PID 后立即退出；命令的输出重定向到 logfile。
"""
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: daemonize.py <logfile> <cmd> [args...]", file=sys.stderr)
        return 2
    logfile = sys.argv[1]
    cmd = sys.argv[2:]

    # 第一次 fork：父进程记录孙子 PID 后退出
    r, w = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(w)
        child_pid = os.read(r, 32).decode().strip()
        os.close(r)
        print(child_pid)
        return 0

    os.close(r)
    os.setsid()  # 成为新会话领导者，脱离控制终端与原会话

    # 第二次 fork：确保不是会话领导者，无法再获得控制终端
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # 孙子进程：重定向 IO 到日志，exec 目标命令
    os.write(w, str(os.getpid()).encode())
    os.close(w)
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
    os.execvp(cmd[0], cmd)
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
