"""日志与可观测性回归 —— 钉住 2026-09-14 的「转换过程完全不可见」缺陷。

宿主反馈：「转换过程，应该有输出日志，我们应该有完善的日志系统。方便运维」。
诊断实况：一篇 502 块 / 19.6 万 token / 跑了 7 分钟的转换，`docker logs` 里
**一行应用日志都没有**；根因不是"没写日志"，而是**从来没人配置日志**：

    root.handlers == []        ← 没有任何 handler
    root.level == WARNING
    logging.lastResort = StreamHandler(WARNING)

于是 `log.info(...)`（队列启动、切片进度、阶段耗时…）全部被静默丢弃，只有
`log.warning` 及以上碰巧可见 —— 生产日志里那唯一一行「19 块待校对」正是 WARNING。

本文件按"缺陷面"分三段：
1. 根因guard：**不配置就吞**、**配置了就出**（子进程实测，不依赖 pytest 的 capture）；
2. 每篇文献独立日志文件（`logs/p<id>.log`）；
3. 入口护栏：`serve` / `create_app` 必须真的调用 `setup_logging`（别被人删掉）。
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ── 1. 根因：INFO 日志的去留取决于**有没有配 handler** ────────────────────────


def _run_py(code: str) -> subprocess.CompletedProcess:
    """在**独立子进程**里跑一段代码（避免 pytest 自己的 handler 干扰结论）。"""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )


def test_info_log_is_swallowed_without_setup():
    """⭐ 缺陷本体：不配置日志时，`papershelf.*` 的 INFO **一个字都出不来**。

    这条是整个修复的判据 —— 它必须是"红过"的（修复前 `log.info` 就是这么消失的）。
    """
    r = _run_py(
        "import logging\n"
        "logging.getLogger('papershelf.converter').info('这是一条进度日志')\n"
    )
    assert r.returncode == 0
    assert "这是一条进度日志" not in r.stdout + r.stderr, (
        "未配置 handler 时 INFO 竟然可见 —— 那 2026-09-14 的现场现象就不是这个原因，判据需重查"
    )


def test_info_log_appears_after_setup_logging():
    """配上 `setup_logging` 之后同一条 INFO 必须可见（并且带时间戳与 logger 名）。"""
    r = _run_py(
        "from papershelf.server.logging_setup import setup_logging\n"
        "import logging\n"
        "setup_logging('INFO')\n"
        "logging.getLogger('papershelf.converter').info('这是一条进度日志')\n"
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "这是一条进度日志" in out, f"配置后仍看不到 INFO：{out!r}"
    assert "[papershelf.converter]" in out, "日志里必须带 logger 名（出问题时要知道是哪一层在说话）"


def test_setup_logging_respects_level():
    """级别可调：`WARNING` 时 INFO 必须闭嘴（否则"日志级别"是个摆设）。"""
    r = _run_py(
        "from papershelf.server.logging_setup import setup_logging\n"
        "import logging\n"
        "setup_logging('WARNING')\n"
        "lg = logging.getLogger('papershelf.converter')\n"
        "lg.info('不应出现')\n"
        "lg.warning('应当出现')\n"
    )
    out = r.stdout + r.stderr
    assert "不应出现" not in out
    assert "应当出现" in out


def test_setup_logging_is_idempotent():
    """重复调用不得叠加 handler（每次调用都挂一个 → 同一条日志打 N 遍）。"""
    from papershelf.server.logging_setup import setup_logging

    setup_logging("INFO")
    root = logging.getLogger()
    n1 = len([h for h in root.handlers if getattr(h, "_papershelf_", False)])
    setup_logging("INFO")
    n2 = len([h for h in root.handlers if getattr(h, "_papershelf_", False)])
    assert n1 == n2 == 1, f"handler 叠加了：{n1} → {n2}"


def test_access_log_is_demoted_below_debug():
    """HTTP access log 默认压到 DEBUG：它此前把容器日志刷成 3752 行，真信号全被淹没。"""
    from papershelf.server.logging_setup import setup_logging

    setup_logging("INFO")
    assert logging.getLogger("uvicorn.access").level >= logging.WARNING
    setup_logging("DEBUG")
    assert logging.getLogger("uvicorn.access").level == logging.INFO


# ── 2. 每篇文献一份日志 ────────────────────────────────────────────────────


def test_paper_log_writes_only_papershelf_records(tmp_path):
    """`logs/p<id>.log` 只收 `papershelf.*`，不把第三方库的噪音抄进来。"""
    from papershelf.server.logging_setup import paper_log

    logs = tmp_path / "logs"
    with paper_log(42, logs):
        logging.getLogger("papershelf.converter").info("阶段①完成")
        logging.getLogger("httpx").info("HTTP Request: GET /v1/models")
    text = (logs / "p42.log").read_text(encoding="utf-8")
    assert "阶段①完成" in text
    assert "HTTP Request" not in text, "第三方日志混进了单篇日志（会把有用的行淹掉）"


def test_paper_log_removes_handler_after_exit(tmp_path):
    """退出后必须摘掉 handler：否则同线程下一篇文献的日志会串进上一个文件。"""
    from papershelf.server.logging_setup import paper_log

    logs = tmp_path / "logs"
    with paper_log(1, logs):
        logging.getLogger("papershelf.converter").info("第一篇")
    with paper_log(2, logs):
        logging.getLogger("papershelf.converter").info("第二篇")
    assert "第二篇" not in (logs / "p1.log").read_text(encoding="utf-8")
    assert "第一篇" not in (logs / "p2.log").read_text(encoding="utf-8")


def test_paper_log_disabled_creates_nothing(tmp_path):
    """`PAPERSHELF_LOG_PER_PAPER=false` 时不建文件（给"别把磁盘写满"留出口）。"""
    from papershelf.server.logging_setup import paper_log

    logs = tmp_path / "logs"
    with paper_log(3, logs, enabled=False):
        logging.getLogger("papershelf.converter").info("不该落盘")
    assert not (logs / "p3.log").exists()


def test_paper_log_failure_never_breaks_conversion(tmp_path):
    """日志落盘失败**绝不能**影响转换：`logs` 位置是个**文件**（mkdir 必失败）也要照跑。"""
    from papershelf.server.logging_setup import paper_log

    bad = tmp_path / "logs"
    bad.write_text("我是个文件，不是目录", encoding="utf-8")
    with paper_log(9, bad):
        logging.getLogger("papershelf.converter").info("转换照常")
    # 走到这里没抛异常就是通过


# ── 3. 入口护栏：配置必须真的被调用 ─────────────────────────────────────────


def test_serve_configures_logging_before_uvicorn():
    """`serve` 必须先 `setup_logging` 再 `uvicorn.run`。

    ⚠️ 顺序不能反：uvicorn 只配自己的 logger，从不碰 root；晚一步配
    就等于把启动阶段的日志（最容易出错的一段）丢掉。
    """
    src = (ROOT / "src" / "papershelf" / "cli.py").read_text(encoding="utf-8")
    i_setup = src.index("setup_logging(")
    i_run = src.index("uvicorn.run(")
    assert i_setup < i_run, "`setup_logging` 必须在 `uvicorn.run` 之前调用"


def test_create_app_configures_logging():
    """`create_app` 也要配（覆盖 `uvicorn papershelf.server.app:app` 与测试直接建 app）。"""
    src = (ROOT / "src" / "papershelf" / "server" / "app.py").read_text(encoding="utf-8")
    assert "setup_logging(" in src


def test_startup_summary_does_not_leak_secrets():
    """启动摘要**绝不打印密钥**（只报"有无"）—— 生产容器日志是任何人都能 dump 的。"""
    src = (ROOT / "src" / "papershelf" / "server" / "app.py").read_text(encoding="utf-8")
    start = src.index("启动配置：")
    block = src[start:src.index("\n\n", start)]
    for secret in ("llm_api_key", "smtp_pass", "settings.secret", "admin_password"):
        assert secret not in block, f"启动摘要里出现了敏感字段 {secret}"
    assert "settings.llm_base_url" in block and "settings.llm_model" in block


# ── 4. 转换过程必须逐阶段可见（"一直显示转换中，正常吗？"的正面回答）───────────


def test_run_emits_visible_stage_logs(settings, caplog, monkeypatch):
    """⭐ 一次成功的转换，日志里必须能读出**四个阶段 + 块数 + 耗时**。

    这就是宿主那条汇报的根因面：**不是没跑，而是跑得看不见**。
    用替身把解析/公式/元数据/翻译全换掉（不烧 token），只验证"过程可见"。
    ⚠️ 替身一律走 `monkeypatch.setattr` —— 直接赋值会**污染后续用例**
    （实测：`cv._run` 被永久换掉后，`test_server` 的"未配 LLM 必须失败"就变成了 done）。
    """
    import logging

    from papershelf.pipeline.model import Block as PBlock, Doc as PDoc
    from papershelf.server import converter as cv
    from papershelf.server.config import get_settings

    get_settings(refresh=True)
    settings.llm_base_url = "https://example.invalid/v1"   # 只为过前置检查，不会真的联网
    settings.llm_api_key = "test-key"
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    doc = PDoc(meta={"pages": 3}, blocks=[
        PBlock(id="b-0001", type="p", en="Hello world.", zh="你好，世界。", zh_source="mt"),
    ])
    pdf = settings.data_dir / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")     # 存在即可：`parse_pdf` 被替身接管

    class _FakeLz:
        tokens_used = 0

        def latexize(self, blocks, **kw):
            return {"blocks": 0, "chunks": 0, "retried": 0, "failed": 0, "done": 0}

    class _FakeMx:
        tokens_used = 0

        def extract(self, blocks, **kw):
            return {"title_en": "Fake Title"}

    class _FakeTr:
        tokens_used = 0
        needs_review: list[str] = []

        def __init__(self, cfg, glossary):
            pass

        @staticmethod
        def ordered(blocks):
            return blocks

        def translate_blocks(self, blocks, **kw):
            return ["你好，世界。"]

    from papershelf.pipeline.validate import Report
    from papershelf.server.db import connect, init_db

    init_db(settings)
    conn = connect(settings)          # `_run` 要查 plan_settings.glossary，给个真连接

    monkeypatch.setattr(cv, "parse_pdf", lambda *a, **k: doc)
    monkeypatch.setattr(cv, "MetadataExtractor", lambda cfg: _FakeMx())
    monkeypatch.setattr(cv, "Translator", _FakeTr)
    monkeypatch.setattr(cv, "Latexizer", lambda cfg: _FakeLz())
    monkeypatch.setattr(cv, "validate", lambda *a, **k: Report())
    with caplog.at_level(logging.INFO, logger="papershelf"):
        cv._run(conn, {"id": 1, "source": "upload", "pdf_path": str(pdf),
                       "plan_id": 1, "source_ref": None}, settings, None)  # type: ignore[arg-type]
    conn.close()

    text = "\n".join(r.getMessage() for r in caplog.records)
    for stage in ("① 解析完成", "② 公式 LaTeX 化", "②b 元数据抽取", "③ 翻译完成", "④ 标记保真校验"):
        assert stage in text, f"缺少阶段日志「{stage}」，完整输出：\n{text}"
    assert "用时" in text, "阶段日志必须带耗时（运维要判断'慢在哪一段'）"
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) >= 5


def test_convert_paper_logs_start_and_finish(settings, caplog, monkeypatch):
    """`convert_paper` 必须有**开始**与**完成**两条（含 paper id 与用时）。"""
    import logging

    from papershelf.server import converter as cv
    from papershelf.server.db import connect
    from papershelf.server.security import create_user

    conn = connect(settings)
    try:
        create_user(conn, email="log@x.edu.cn", password="password123", status_="active")
        pl = conn.execute("INSERT INTO plans (user_id,name,created_at,updated_at) "
                          "VALUES (1,'p',datetime('now'),datetime('now'))").lastrowid
        pid = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,datetime('now'),datetime('now'))""",
            (pl, "t", "upload", "unread", "queued")).lastrowid
        conn.commit()
    finally:
        conn.close()

    from papershelf.pipeline.model import Block as PBlock, Doc as PDoc
    from papershelf.server.config import get_settings

    get_settings(refresh=True)
    monkeypatch.setattr(cv, "_run",
                        lambda *a, **k: (PDoc(meta={}, blocks=[PBlock(id="b-0001", type="p", en="a")]), 7))

    with caplog.at_level(logging.INFO, logger="papershelf"):
        cv.convert_paper(int(pid))

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"paper={pid} 开始转换" in text
    assert f"paper={pid} 转换完成" in text
    assert "7 tokens" in text


def test_queue_round_logs_state_when_busy(settings, monkeypatch, caplog):
    """队列每轮打**一次状态快照**（有几篇 queued / doing），空转时不打。

    为什么要有：宿主看到的界面只有一个「转换中」，"队列还在动吗"在日志里必须可查；
    而每 3 秒打一行空转日志会把容器日志刷成噪声 —— 所以"有活才打"是设计的一部分。
    """
    import logging

    from papershelf.server.db import connect
    from papershelf.server.queue import ConversionQueue
    from papershelf.server.security import create_user

    conn = connect(settings)
    try:
        create_user(conn, email="q@x.edu.cn", password="password123", status_="active")
        pl = conn.execute("INSERT INTO plans (user_id,name,created_at,updated_at) "
                          "VALUES (1,'p',datetime('now'),datetime('now'))").lastrowid
        conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,datetime('now'),datetime('now'))""",
            (pl, "t", "upload", "unread", "queued"))
        conn.commit()
    finally:
        conn.close()

    q = ConversionQueue(settings)
    with caplog.at_level(logging.INFO, logger="papershelf.queue"):
        q._log_round()
    assert any("队列状态" in r.getMessage() and "queued=1" in r.getMessage()
               for r in caplog.records), "有排队文献时，轮询应报出状态快照"

    # 空转（没有 queued/doing）时不得刷日志
    conn = connect(settings)
    try:
        conn.execute("UPDATE papers SET conv_state='done'")
        conn.commit()
    finally:
        conn.close()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="papershelf.queue"):
        q._log_round()
    assert not [r for r in caplog.records if "队列状态" in r.getMessage()]

