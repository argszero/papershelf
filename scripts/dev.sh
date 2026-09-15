#!/bin/sh
# 本地测试启动脚本（宿主用）。生产不放这个文件。
#
# 用法：
#   sh scripts/dev.sh           # 前台跑（Ctrl-C 停）
#   sh scripts/dev.sh &         # 后台跑，日志在同目录 .dev.log
#
# 端口 8012，数据目录 /tmp/pslocal/data（生产数据副本，随便造）。
set -eu
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "缺少 .env（本机测试配置），请先创建" >&2
  exit 1
fi

# 载入 .env（简单版：忽略注释与空行，逐行 export）
set -a
. ./.env
set +a

PY="${PY:-/opt/homebrew/Caskroom/miniconda/base/envs/dl/bin/python}"
echo "启动：http://localhost:8012  (日志级别 ${PAPERSHELF_LOG_LEVEL}, 数据 ${PAPERSHELF_DATA_DIR})"
echo "每篇文献日志：${PAPERSHELF_DATA_DIR}/logs/p<id>.log"
PYTHONPATH=src exec "$PY" -m papershelf.cli serve --host 127.0.0.1 --port 8012
