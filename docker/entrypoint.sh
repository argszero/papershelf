#!/bin/sh
# papershelf 容器入口 —— 只做「启动前的显式检查」，不做任何静默修补。
#
# 设计取向（与 security.ensure_admin 一致的失败哲学）：**配置错了要响亮地死在启动时**，
# 而不是带着不安全的默认值悄悄跑起来、让宿主在几天后才发现实例可被伪造会话。
set -eu

log() { printf '[entrypoint] %s\n' "$*"; }
die() { printf '[entrypoint] ✖ %s\n' "$*" >&2; exit 1; }

DATA_DIR="${PAPERSHELF_DATA_DIR:-/data}"
export PAPERSHELF_DATA_DIR="$DATA_DIR"

# ── 数据目录可写？(挂载点权限错了是最常见的启动失败，先说清楚) ──
mkdir -p "$DATA_DIR/papers" 2>/dev/null || true
[ -w "$DATA_DIR" ] || die "数据目录不可写：$DATA_DIR
    容器内以 uid=$(id -u) 运行。若挂载的是宿主机目录，请先 chown：
      sudo chown -R 10001:10001 <宿主目录>"

# ── 会话签名密钥 ──
if [ -z "${PAPERSHELF_SECRET:-}" ]; then
    cat >&2 <<'EOF'
[entrypoint] ✖ 未设置 PAPERSHELF_SECRET。
    它会签名会话凭据；用开发默认值意味着任何人都能伪造登录态。
    生成一个：
      python -c 'import secrets; print(secrets.token_urlsafe(32))'
    然后 docker run 加 -e PAPERSHELF_SECRET=<值>，或写进 .env 用 --env-file .env。
EOF
    exit 1
fi

case "$(printf '%s' "$PAPERSHELF_SECRET" | wc -c | tr -d ' ')" in
    1[6-9]|[2-9][0-9]|[1-9][0-9][0-9]*) : ;;   # ≥16 字符，够用
    *) die "PAPERSHELF_SECRET 太短（建议 ≥32 字节随机值）。" ;;
esac

# ── 管理员引导：邮箱与密码必须成对（§7：不许静默默认密码） ──
if [ -n "${PAPERSHELF_ADMIN_EMAIL:-}" ] && [ -z "${PAPERSHELF_ADMIN_PASSWORD:-}" ]; then
    die "给了 PAPERSHELF_ADMIN_EMAIL 但没给 PAPERSHELF_ADMIN_PASSWORD。
    首次引导必须显式给密码（不允许静默默认密码）。详见 .env.example。"
fi

# ── LLM：转换功能的前提；缺失不阻止起服务（仍可读/建计划/开号），但要讲明白 ──
if [ -z "${PAPERSHELF_LLM_BASE_URL:-}" ] || [ -z "${PAPERSHELF_LLM_API_KEY:-}" ]; then
    log "⚠ 未配置 LLM（PAPERSHELF_LLM_BASE_URL / _API_KEY）——实例能起，但导入的文献无法转换。"
fi

if [ -z "${PAPERSHELF_BASE_URL:-}" ]; then
    log "⚠ 未设置 PAPERSHELF_BASE_URL（默认 http://localhost:8000）——激活链接与分享链接会用它拼绝对地址，反代部署请填真实域名。"
fi

log "数据目录：$DATA_DIR"
log "启动：$*"
exec "$@"
