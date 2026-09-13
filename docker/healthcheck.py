#!/usr/bin/env python3
"""容器健康检查 —— 打 /api/health（不依赖 curl / wget，slim 镜像里没有）。

判定与「服务是否可用」对齐：
  · HTTP 200 且 ok=true          → 健康
  · 其余（连不上、5xx、返非 JSON）→ 不健康

刻意**不**把 llm_configured 当作不健康的理由：没配 LLM 的实例依然可以登录、建计划、
开号、读已有文献 —— 那是「功能降级」，不是「进程挂了」。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("PAPERSHELF_HEALTHCHECK_PORT", "8000")
URL = f"http://127.0.0.1:{PORT}/api/health"


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=4) as resp:
            if resp.status != 200:
                print(f"unhealthy: HTTP {resp.status}", file=sys.stderr)
                return 1
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1

    if not body.get("ok"):
        print(f"unhealthy: {body}", file=sys.stderr)
        return 1

    if not body.get("llm_configured"):
        print("healthy (LLM 未配置：转换功能不可用)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
