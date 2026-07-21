#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一條命令徹底脫離當前會話跑成守護程式（double-fork + setsid），
使其父程式變為 init(PPID=1)，IDE/agent 會話回收也不會殺掉它。

用法：
  python3 scripts/daemonize.py <logfile> <cmd> [args...]
列印子程式 PID 後立即退出；命令的輸出重定向到 logfile。
"""
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: daemonize.py <logfile> <cmd> [args...]", file=sys.stderr)
        return 2
    logfile = sys.argv[1]
    cmd = sys.argv[2:]

    # 第一次 fork：父程式記錄孫子 PID 後退出
    r, w = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(w)
        child_pid = os.read(r, 32).decode().strip()
        os.close(r)
        print(child_pid)
        return 0

    os.close(r)
    os.setsid()  # 成為新會話領導者，脫離控制終端與原會話

    # 第二次 fork：確保不是會話領導者，無法再獲得控制終端
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # 孫子程式：重定向 IO 到日誌，exec 目標命令
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
