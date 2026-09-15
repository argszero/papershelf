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

    # ── 原文抽取校对（VLM agent，管线 ①c）──
    # 宿主 2026-09-14：「LLM 需要用于提取后的校对更新，要校对文字、格式，所有能校对的都要校对」，
    # 且要求**以 agent 方式**做（给工具：读页图 / 读抽取结果 / 改抽取结果，让它自己逐页核对）。
    # 代价实测：3 页论文 ≈ 11 轮 / 3–6 万 tokens（整页图 + 推理模型），37 页 ≈ 与整篇翻译同级，
    # 所以给一个显式开关；关掉即退回纯几何解析。
    proofread: bool = field(default_factory=lambda: _bool("PAPERSHELF_PROOFREAD", True))
    # 渲染 DPI：太低看不清上下标（校对就白做），太高图像太大拖慢调用。130 实测可用。
    proofread_dpi: int = field(default_factory=lambda: _int("PAPERSHELF_PROOFREAD_DPI", 130))
    # **单次** LLM 调用的输出预算：推理模型给不足会把预算全烧在思考上、content 返回空串
    # （实测 8192 必空）。注意这是"一次调用"的预算，不是整篇的。
    proofread_max_tokens: int = field(default_factory=lambda: _int("PAPERSHELF_PROOFREAD_MAX_TOKENS", 32000))
    # agent 轮数上限：一次工具调用算一轮。3 页论文实测 11 轮，37 页给足余量。
    proofread_max_rounds: int = field(default_factory=lambda: _int("PAPERSHELF_PROOFREAD_MAX_ROUNDS", 400))
    # 整篇校对的累计 token 预算（最后一道成本护栏：轮数没超、但模型开始兜圈子时挡在这里）。
    proofread_token_budget: int = field(
        default_factory=lambda: _int("PAPERSHELF_PROOFREAD_TOKEN_BUDGET", 1_500_000)
    )
    # 不配视觉模型也能开机（校对失败只记日志、不阻塞转换）。
    proofread_model: str = field(
        default_factory=lambda: os.environ.get("PAPERSHELF_PROOFREAD_MODEL", "")
    )
    # 思考预算：`minimal`（默认）/ `off` / `low` / `auto`。
    # 池子里的视觉模型是**推理模型**，同一请求实测（2026-09-14）：
    #   auto → completion 2001 tokens / 10.0s；`minimal` → 693 / 3.7s；`off` → 80 / 1.1s。
    # 但**要看 agent 整篇的效果，不看单次调用** —— 3 页真实论文实测（改了 12 块的同一篇）：
    #   auto 16 轮 / 106,709 tokens；`off` 13 轮 / 81,369；`minimal` 8 轮 / 60,665。
    # agent 的成本 ≈ 轮数 × 每轮输出，所以这一个参数直接决定"37 页能不能跑完"。
    # 默认 `minimal`（留一点推理余量，质量与 `auto` 一致但便宜一半）；`off` 留给成本吃紧时。
    proofread_thinking: str = field(
        default_factory=lambda: os.environ.get("PAPERSHELF_PROOFREAD_THINKING", "minimal")
    )
    # 常驻转换队列（server/queue.py）：启动恢复 `doing` + 轮询排空 `queued`。
    # 关掉它 = 退回"只在请求线程里跑"，重启即丢队列（2026-09-12 事故的成因）。
    # 测试默认关（避免后台线程与用例互相干扰），由 conftest 显式设置。
    queue_enabled: bool = field(default_factory=lambda: _bool("PAPERSHELF_QUEUE", True))
    queue_interval: float = field(default_factory=lambda: _float("PAPERSHELF_QUEUE_INTERVAL", 3.0))

    # ── 日志（运维可观测性）──
    # 踩过（2026-09-14 生产）：「一直显示转换中」汇报上来之后，容器日志里**一行应用日志都没有** ——
    # 代码里写了 `log.info`（队列启动/切片进度/元数据…），但从未 `basicConfig`，
    # 根 logger 没有 handler，`lastResort` 只放 WARNING 及以上 → 所有 INFO 被静默丢弃。
    # 一个跑了 7 分钟、花了 19 万 token 的转换，对外完全不可见。
    log_level: str = field(default_factory=lambda: (os.environ.get("PAPERSHELF_LOG_LEVEL") or "INFO").upper())
    # 每篇文献单独落一份 `<data_dir>/logs/p<id>.log`：事后按文献排障不必翻容器全量日志。
    log_per_paper: bool = field(default_factory=lambda: _bool("PAPERSHELF_LOG_PER_PAPER", True))
    # uvicorn 的 HTTP access log（每请求一行）：默认留着重定向到 DEBUG 级，
    # 想看时把 `PAPERSHELF_LOG_LEVEL=DEBUG` 即可，不必改代码。
    log_access: bool = field(default_factory=lambda: _bool("PAPERSHELF_LOG_ACCESS", True))

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
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

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
        self.logs_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
