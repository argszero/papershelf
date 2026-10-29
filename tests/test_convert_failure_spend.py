"""「转换没跑完」不等于「一分钱没花」—— 失败路径必须记账，且必须落 `failed`。

## 两条不变量（本文件钉住的就是这两条）

1. **花掉的 token 跟产物走，不跟这次成没成走**：`tokens_used` 是成本护栏与用量面板
   唯一的数据来源。一篇 ②b/③ 都跑过、最后在阶段④结构失败（或 `save_doc` 撞约束）的文献，
   原先在库里记 **0** —— 用户看到「转换失败」，账上却是"白嫖"。
2. **任何异常都必须落到 `conv_state=failed`**（`convert_paper` 自己的承诺）：
   `save_doc` / `_writeback_meta` 原先在 `try` 外面，抛出的异常一路逃到
   `queue.run_once` 的 `_deliver`（只 `log.exception`）⇒ 这一行**永久 `doing`**、
   `conv_error` 是 NULL，界面永远「转换中」，只有重启的 `recover_stuck` 才捞得回来。

## 三条反向护栏（防止修过头）

- **402 那种「每一片都抛、`continue` 掉」的部分失败**仍然是 `done` + 块上 `needs_review`
  —— 它是**诚实的降级**，不能因为"要记账"就把它变成 `failed`（那会把几百块已译的产物报废）；
- 成功路径的账**不重复记**（`_writeback_meta` 记一次，失败分支才记）；
- ①c 抛出时只补**差额**（`try` 里已记过的部分不加第二遍）。

全部用例离线：`httpx.HTTPTransport.handle_request` 桩成抛错当铁丝网，①c 在不需要它的用例里
显式关掉，任何一次真实外呼都会变成 `AssertionError` 而不是一次网络请求。
"""

from __future__ import annotations

import httpx
import pytest


# ── 夹具 ────────────────────────────────────────────────────────────────
@pytest.fixture()
def no_egress(monkeypatch):
    """任何真实 HTTP 发送就地失败（`TestClient` 走 ASGITransport，不受影响）。"""

    def _boom(self, request, *a, **k):
        raise AssertionError(f"real network egress attempt: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _boom)


def _seed(make_user, client) -> int:
    """一个用户 + 计划 + 一篇 PDF 存在的文献行。返回 paper id。

    ⚠️ 必须 `conn.commit()`：`sqlite3` 默认开隐式事务，`close()` 会回滚，
    接口随即回 404「文献不存在」（上一轮 Recon 在这里白跑过一次）。
    """
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect

    email, pw = make_user()
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text

    s = get_settings()
    pdf = s.data_dir / "seed.pdf"
    pdf.write_bytes(b"%PDF-1.4 seeded")     # 只需存在：`parse_pdf` 被替身接管

    conn = connect(s)
    try:
        uid = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        plan = conn.execute(
            "INSERT INTO plans (user_id,name,created_at,updated_at) "
            "VALUES (?,?,datetime('now'),datetime('now'))", (uid, "p")).lastrowid
        pid = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,conv_state,pdf_path,
                                   created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (plan, "seed", "upload", "unread", "queued", str(pdf))).lastrowid
        conn.commit()
    finally:
        conn.close()
    return int(pid)


def _state(pid: int) -> dict:
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect

    conn = connect(get_settings())
    try:
        return dict(conn.execute(
            "SELECT conv_state, conv_error, tokens_used, pending_job FROM papers WHERE id=?",
            (pid,)).fetchone())
    finally:
        conn.close()


def _blocks(pid: int) -> list[dict]:
    from papershelf.server.config import get_settings
    from papershelf.server.db import connect
    from papershelf.server.repo import load_doc

    conn = connect(get_settings())
    try:
        doc = load_doc(conn, pid)
        return [{"id": b.id, "zh": b.zh,
                 "needs_review": bool((b.payload or {}).get("needs_review"))}
                for b in (doc.blocks if doc else [])]
    finally:
        conn.close()


def _fake_translator(texts_by_index: dict, spent: int):
    """把每块都"译"好的替身，并汇报自己的消耗（走不到 LLM）。"""

    class _FakeTr:
        tokens_used = spent
        needs_review: list[str] = []

        def __init__(self, cfg, glossary):
            pass

        @staticmethod
        def ordered(blocks):
            return list(blocks)

        def translate_blocks(self, blocks, **kw):
            return [texts_by_index.get(i, "中文译文。")
                    for i, _ in enumerate(self.ordered(blocks))]

    return _FakeTr


def _stub_pipeline(monkeypatch, *, meta_tokens: int, translator=None, blocks=()):
    """替掉四条管线里的三条（解析 / 公式 / 元数据），返回喂给 `parse_pdf` 的 Doc。"""
    from papershelf.pipeline.model import Doc as PDoc
    from papershelf.server import converter as cv

    doc = PDoc(meta={"pages": 1}, blocks=list(blocks))

    class _FakeLz:
        tokens_used = 0

        def __init__(self, cfg):
            pass

        def latexize(self, blocks, **kw):
            return {"blocks": 0, "chunks": 0, "retried": 0, "failed": 0, "done": 0}

    class _FakeMx:
        tokens_used = meta_tokens

        def __init__(self, cfg):
            pass

        def extract(self, blocks, **kw):
            return {"title_en": "Seeded Title"}

    monkeypatch.setattr(cv, "parse_pdf", lambda *a, **k: doc)
    monkeypatch.setattr(cv, "Latexizer", _FakeLz)
    monkeypatch.setattr(cv, "MetadataExtractor", _FakeMx)
    if translator is not None:
        monkeypatch.setattr(cv, "Translator", translator)
    return doc


def _ready(settings, *, proofread: bool = False) -> None:
    """把 `Settings` 调成"能过前置检查、但永远不会真联网"的样子。

    ⚠️ 直接改夹具持有的那个实例（`get_settings()` 返回的就是同一个对象）——
    不 `refresh()`，免得 `convert_paper` 里现取的设置与 `connect()` 用的设置变成两份。
    """
    settings.proofread = proofread
    settings.llm_base_url = "https://example.invalid/v1"   # 只为过 `_run` 的前置检查
    settings.llm_api_key = "test-key"


SPEND_META = 1500
SPEND_TR = 2400
EXPECTED_SPENT = SPEND_META + SPEND_TR          # ② 因无数学块而跳过 ⇒ 只有 ②b 与 ③


# ── 1. 阶段④结构性失败：花掉的照记，状态照落 ─────────────────────────────
def test_a_structural_failure_records_the_tokens_it_burned(
        client, settings, make_user, monkeypatch, no_egress):
    """`enforce_report` 的结构分支抛出后：`failed` + `conv_error` + **真数**。

    `enforce_report` 是**真的**，只把 `validate` 换成一份结构性报告（`missing`）——
    这正是校验器在两种渲染产出块身份对不上时给出的结果。
    """
    from papershelf.pipeline.model import Block as PBlock
    from papershelf.pipeline.validate import Report
    from papershelf.server import converter as cv

    _ready(settings)
    _stub_pipeline(monkeypatch, meta_tokens=SPEND_META,
                   translator=_fake_translator({}, SPEND_TR),
                   blocks=[PBlock(id="b-0001", type="p",
                                  en="A long enough English paragraph for translation.")])
    monkeypatch.setattr(cv, "validate", lambda *a, **k: Report(missing=["b-0001"]))

    pid = _seed(make_user, client)
    assert client.post(f"/api/papers/{pid}/convert").status_code == 202

    st = _state(pid)
    assert st["conv_state"] == "failed"
    assert "标记保真校验未通过" in (st["conv_error"] or ""), st["conv_error"]
    assert st["tokens_used"] == EXPECTED_SPENT, (
        f"花了 {EXPECTED_SPENT} tokens 却记了 {st['tokens_used']} —— 失败不等于没花钱")
    assert st["pending_job"] is None, "终态必须清掉 pending_job"


# ── 2. `save_doc` 抛出：不能再留一个永久 `doing` ──────────────────────────
def test_a_raising_save_doc_lands_failed_with_the_spend(
        client, settings, make_user, monkeypatch, no_egress):
    """重复块 id 让 `save_doc` 撞 `UNIQUE` 约束：异常必须落 `failed`，而不是逃出函数。

    这条用例是**两个缺陷的交点**：`save_doc` 原先在 `try` 外面（⇒ 僵尸 `doing`），
    而即便落了 `failed`，`tokens` 也在 `_run` 的栈帧里（⇒ 记 0）。
    """
    from papershelf.pipeline.model import Block as PBlock

    _ready(settings)
    _stub_pipeline(monkeypatch, meta_tokens=SPEND_META,
                   translator=_fake_translator({}, SPEND_TR),
                   blocks=[
                       PBlock(id="b-0001", type="p",
                              en="A long enough English paragraph to require translation."),
                       PBlock(id="b-0001", type="p",     # ← 重复 id：save_doc 抛 IntegrityError
                              en="A second paragraph that reuses the same block id."),
                   ])

    pid = _seed(make_user, client)
    assert client.post(f"/api/papers/{pid}/convert").status_code == 202

    st = _state(pid)
    assert st["conv_state"] == "failed", (
        f"conv_state={st['conv_state']!r} —— 异常逃出了 `convert_paper`，"
        f"这一行会永久停在 doing（只有重启的 recover_stuck 能捞回来）")
    assert "IntegrityError" in (st["conv_error"] or ""), st["conv_error"]
    assert st["tokens_used"] == EXPECTED_SPENT, st["tokens_used"]


# ── 3. 反向护栏：402 那种部分失败仍然是「已生成 + 待校对」───────────────
def test_a_partial_translation_outage_stays_done_with_needs_review(
        client, settings, make_user, monkeypatch, no_egress):
    """每一次 `_chat` 都抛（欠费/402 的形状）⇒ `done` + 全部 `needs_review`，不许变 `failed`。

    用**真的** `Translator`（真切片、真 `_check`、真收敛），只让 `_chat` 抛 ——
    这条钉住的是「记账」不能顺手把一次诚实降级升格成整篇报废。
    """
    from papershelf.pipeline.model import Block as PBlock
    from papershelf.pipeline.translator import Translator

    _ready(settings)

    def _chat_boom(self, *a, **k):
        raise RuntimeError("402 点数余额不足")

    monkeypatch.setattr(Translator, "_chat", _chat_boom)
    _stub_pipeline(monkeypatch, meta_tokens=SPEND_META, blocks=[
        PBlock(id="b-0001", type="p",
               en="A long enough English paragraph so that the no-CJK rule applies here."),
        PBlock(id="b-0002", type="p",
               en="Another long English paragraph that no provider will ever translate."),
    ])

    pid = _seed(make_user, client)
    assert client.post(f"/api/papers/{pid}/convert").status_code == 202

    st = _state(pid)
    got = _blocks(pid)
    assert st["conv_state"] == "done", "部分失败是降级，不是失败"
    assert got, "产物必须还在"
    assert all(b["needs_review"] for b in got), f"未译块必须挂「待校对」：{got}"
    assert st["tokens_used"] == SPEND_META, (
        f"②b 的 {SPEND_META} 要如实记账（翻译没调通，不该有额外消耗）："
        f"got {st['tokens_used']}")


# ── 4. 同族第三处：①c 抛了，它已经烧掉的也要进账 ────────────────────────
def test_a_failed_proofread_still_records_what_it_spent(
        client, settings, make_user, monkeypatch, no_egress):
    """①c 跑到一半抛出：整篇仍 `done`（校对失败不毁整篇），但它花的钱要记账。

    `Proofreader` 实例自己累计 `stats.tokens`，而汇总那行在抛出点之后 —— 不补差额就是
    同族第三处漏账。这里替身先"花掉 1300"再抛。
    """
    from papershelf.pipeline.model import Block as PBlock
    from papershelf.pipeline import proofread as pr_mod

    _ready(settings, proofread=True)

    class _FakePf:
        last = None

        def __init__(self, cfg, **kw):
            from papershelf.pipeline.proofread import ProofreadStats

            self.stats = ProofreadStats()
            _FakePf.last = self

        def proofread_doc(self, doc, pdf, skip_pages=None):
            self.stats.tokens = 1300          # 真的付过费，然后上游炸了
            raise RuntimeError("①c 上游 504")

    monkeypatch.setattr(pr_mod, "Proofreader", _FakePf)
    _stub_pipeline(monkeypatch, meta_tokens=SPEND_META,
                   translator=_fake_translator({}, 0), blocks=[
                       PBlock(id="b-0001", type="p", payload={"page": 1},
                              en="A long enough English paragraph to require translation."),
                   ])

    pid = _seed(make_user, client)
    assert client.post(f"/api/papers/{pid}/convert").status_code == 202

    assert _FakePf.last is not None, "①c 必须真的被调用（夹具前提：有页且有 PDF）"
    st = _state(pid)
    assert st["conv_state"] == "done", "校对失败不该毁掉整篇转换"
    assert st["tokens_used"] == SPEND_META + 1300, (
        f"①c 花掉 1300 却只有 ②b 进账：got {st['tokens_used']}")
