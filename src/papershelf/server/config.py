"""运行配置 —— 全部走环境变量（决策㉑：配置不进数据库、不做在线编辑）。

自托管单体的目标形态是「一个容器 + 一个数据卷 + 一组环境变量」，
所以配置在这里集中解析，其它模块只读 `Settings` 实例，不再各自 `os.environ`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 分享链接有效期硬上限（小时）—— 宿主 2026-09-11 指示：「最大设置 24 小时」。
# 放在 config 层（而非 db 层）是因为 db、路由、迁移三处都要引用，而 config 不 import 任何业务模块。
SHARE_MAX_HOURS = 24


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # ── 基础 ──
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("PAPERSHELF_DATA_DIR", "./data")))
    secret: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SECRET", "dev-insecure-secret"))
    base_url: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_BASE_URL", "http://localhost:8000"))
    admin_email: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_ADMIN_EMAIL", "") or "")
    # 首次引导管理员的密码：**必填**，见 security.ensure_admin 的安全约束
    admin_password: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_ADMIN_PASSWORD", "") or "")

    # ── LLM（决策⑧：服务端统一 Key，OpenAI 兼容）──
    llm_base_url: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_LLM_MODEL", "deepseek-chat"))
    # 成本护栏（待定项 5 的初值）：并发上限 + 单篇 token 预算
    max_concurrency: int = field(default_factory=lambda: _int("PAPERSHELF_MAX_CONCURRENCY", 2))
    token_budget_per_paper: int = field(default_factory=lambda: _int("PAPERSHELF_TOKEN_BUDGET", 400_000))
    max_conv_attempts: int = field(default_factory=lambda: _int("PAPERSHELF_MAX_ATTEMPTS", 3))
    # 常驻转换队列（server/queue.py）：启动恢复 `doing` + 轮询排空 `queued`。
    # 关掉它 = 退回"只在请求线程里跑"，重启即丢队列（2026-09-12 事故的成因）。
    # 测试默认关（避免后台线程与用例互相干扰），由 conftest 显式设置。
    queue_enabled: bool = field(default_factory=lambda: _bool("PAPERSHELF_QUEUE", True))
    queue_interval: float = field(default_factory=lambda: _float("PAPERSHELF_QUEUE_INTERVAL", 3.0))

    # ── 注册与邮箱（决策⑭⑮）──
    email_allowlist: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_EMAIL_DOMAIN_ALLOWLIST", "edu.cn"))
    smtp_host: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("PAPERSHELF_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SMTP_USER", ""))
    smtp_pass: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SMTP_PASS", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SMTP_FROM", ""))
    smtp_tls: bool = field(default_factory=lambda: _bool("PAPERSHELF_SMTP_TLS", True))
    # 发件人显示名（可选）：`formataddr` 拼成 `显示名 <地址>`。
    # `smtp_subject` 非空则**整条**覆盖默认主题（注册/重置共用一个）。
    smtp_from_name: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SMTP_FROM_NAME", ""))
    smtp_subject: str = field(default_factory=lambda: os.environ.get("PAPERSHELF_SMTP_SUBJECT", ""))

    # ── 验证码（⑭：注册与忘记密码都用它）──
    # 有效期 / 同邮箱重发冷却 / 单个验证码允许的失败次数（超限作废，需重新获取）
    code_ttl_seconds: int = field(default_factory=lambda: _int("PAPERSHELF_CODE_TTL", 600))
    code_cooldown_seconds: int = field(default_factory=lambda: _int("PAPERSHELF_CODE_COOLDOWN", 60))
    code_max_attempts: int = field(default_factory=lambda: _int("PAPERSHELF_CODE_MAX_ATTEMPTS", 5))

    # ── 分享（决策⑩⑪）──
    # 有效期**上限 24 小时**（宿主 2026-09-11：「分享链接有效期最大设置 24 小时」）。
    # 默认值也是硬上限：不设就取 24 小时，永久链接不再存在。
    share_default_hours: int = field(
        default_factory=lambda: _int("PAPERSHELF_SHARE_HOURS", SHARE_MAX_HOURS)
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "papershelf.db"

    @property
    def papers_dir(self) -> Path:
        return self.data_dir / "papers"

    @property
    def smtp_configured(self) -> bool:
        """⑮：SMTP 配全才开放自助注册；未配则自动降级为「管理员开号」。"""
        return bool(self.smtp_host and self.smtp_from)

    @property
    def open_registration(self) -> bool:
        return self.smtp_configured

    @property
    def allowed_domains(self) -> list[str]:
        return [d.strip().lower().lstrip("@") for d in self.email_allowlist.split(",") if d.strip()]

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.papers_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
