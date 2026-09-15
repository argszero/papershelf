"""①c 原文抽取校对 agent（`pipeline/proofread.py`）离线回归 —— 不调 LLM、不联网。

宿主 2026-09-14：「LLM 需要用于提取后的校对更新，要校对文字、格式，所有能校对的都要校对」，
且指定**形态**：「参考 emrg 代码……给 agent 提供输入（原始 pdf 或 pdf 转成的图片、提取的结果），
以及工具（读图片工具、读提取结果的工具、修改提取结果的工具等等），让 llm 以 agent 形式校对更新」。

本文件钉住三件事：
1. **工具层**（`ProofreadTools`）：读图/读块/尺子/改文本/切分/合并/删除/排序/收尾各自的行为边界，
   尤其是**护栏**（模型会编）与"改了之后 doc 真的变了"。
2. **循环层**（`Proofreader._run`）：`tool_calls` → 执行 → 回 `tool` 消息 → 继续；图走紧跟其后的
   user 消息（不能插在 tool 消息中间）；漏页会被**追问**；`finish` 在有页未核定时被拒绝。
3. **成本护栏**：轮数用尽 / token 预算用尽必须停手并留痕（不是静默死循环）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from papershelf.pipeline.model import Block, Doc, make_block_id
from papershelf.pipeline.proofread import (Proofreader, ProofreadStats, ProofreadTools,
                                           _artifacts, _acceptable_text_change)
from papershelf.pipeline.translator import LLMConfig

CFG = LLMConfig(base_url="http://x", api_key="k", model="m")


def _doc(*texts: str, page: int = 1) -> Doc:
    doc = Doc()
    for i, t in enumerate(texts, start=1):
        doc.blocks.append(Block(id=make_block_id(i), type="p", en=t,
                                payload={"page": page, "bbox": [0, 0, 1, 1]}))
    return doc


def _tiny_pdf(path, pages=2):
    fitz = pytest.importorskip("fitz")
    src = fitz.open()
    for i in range(pages):
        pg = src.new_page()
        pg.insert_text((72, 72), f"page {i + 1} " + "lorem ipsum dolor sit amet " * 3)
    src.save(str(path))
    src.close()


@pytest.fixture
def tools(tmp_path):
    """两页、各一块的文档 + 一个真 PDF（渲染图要用）。"""
    doc = _doc("This page has plenty of extractable text to review.", page=1)
    doc.blocks.append(Block(id="b-0002", type="p", en="Second page text, long enough to review.",
                            payload={"page": 2, "bbox": [0, 0, 1, 1]}))
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf)
    t = ProofreadTools(doc, pdf)
    yield t
    t.close()


# ── ① 工具自述 ────────────────────────────────────────────────────────────

def test_specs_are_openai_function_definitions(tools):
    specs = tools.specs()
    names = [s["function"]["name"] for s in specs]
    # 宿主点名的三类工具都要在：读图 / 读抽取结果 / 改抽取结果
    assert {"read_page", "read_blocks", "read_block", "edit_block"} <= set(names)
    assert {"merge_block", "delete_block", "reorder_page", "mark_page_done", "finish"} <= set(names)
    for s in specs:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"


def test_unknown_tool_returns_error_not_raises(tools):
    out = tools.call("nope", {})
    assert out.error and "未知工具" in out.text


# ── ② 读工具 ──────────────────────────────────────────────────────────────

def test_read_page_returns_image_and_zoom_region(tools):
    out = tools.call("read_page", {"page": 1})
    assert not out.error and len(out.images) == 1 and len(out.images[0]) > 1000
    # 放大：给 region 也必须出图（校对看不清的上下标就靠它）
    zoom = tools.call("read_page", {"page": 1, "region": [0.0, 0.0, 0.4, 0.2]})
    assert not zoom.error and zoom.images and zoom.images[0] != out.images[0]


def test_read_page_rejects_bad_page(tools):
    assert tools.call("read_page", {"page": 99}).error


def test_read_blocks_paginates_and_truncates(tools):
    doc = tools.doc
    doc.blocks[0].en = "x" * 900
    out = json.loads(tools.call("read_blocks", {"page": 1, "limit": 1}).text)
    assert out["total"] == 1 and len(out["blocks"][0]["text"]) < 400 and out["blocks"][0]["chars"] == 900
    # 全文走 read_block
    full = json.loads(tools.call("read_block", {"id": "b-0001"}).text)
    assert len(full["text"]) == 900


def test_check_artifacts_lists_facts_for_agent(tools):
    tools.doc.blocks[0].en = "multiple vari- ables are used [ 12 ] here."
    out = json.loads(tools.call("check_artifacts", {"page": 1}).text)
    got = " ".join(out["suspicious"]["b-0001"])
    assert "行末断词" in got and "引用编号" in got
    assert "不一定是错" in out["note"]           # 必须讲清"这是事实不是判决"


# ── ③ 写工具（含护栏）─────────────────────────────────────────────────────

def test_edit_block_applies_and_cleans_ligatures(tools):
    tools.doc.blocks[0].en = "A signi\ufb01cant vari- ous method."
    out = tools.call("edit_block", {"id": "b-0001", "text": "A significant various method.",
                                    "reason": "断词合并"})
    assert not out.error
    assert tools.doc.blocks[0].en == "A significant various method."
    assert tools.stats.text_fixed == 1


def test_edit_block_guard_rejects_fabrication(tools):
    before = tools.doc.blocks[0].en
    out = tools.call("edit_block", {"id": "b-0001", "text": "这是一段模型自己编的完全无关的中文内容"})
    assert out.error and "护栏拒绝" in out.text
    assert tools.doc.blocks[0].en == before and tools.stats.rejected == 1


def test_split_block_creates_blocks_in_place(tools):
    text = "First paragraph of this block. " * 3 + "Second paragraph of this block. " * 3
    tools.doc.blocks[0].en = text
    out = tools.call("split_block", {"id": "b-0001",
                                     "parts": ["First paragraph of this block. " * 3,
                                               "Second paragraph of this block. " * 3]})
    assert not out.error and tools.stats.split == 1
    ids = [b.id for b in tools.doc.blocks]
    assert ids[0] == "b-0001" and len(ids) == 3          # 原块 + 新块（b-0002 还在后面）
    assert tools.doc.blocks[1].en.startswith("Second paragraph")


def test_split_block_needs_two_parts(tools):
    out = tools.call("split_block", {"id": "b-0001", "parts": ["only one"]})
    assert out.error and tools.stats.rejected == 1


def test_merge_block_joins_previous_on_same_page(tools):
    tools.doc.blocks[0].en = "This sentence is continued"
    tools.doc.blocks.insert(1, Block(id="b-0009", type="p", en="and the tail of it.",
                                     payload={"page": 1, "bbox": [0, 0, 1, 1]}))
    out = tools.call("merge_block", {"id": "b-0009"})
    assert not out.error and tools.stats.merged == 1
    assert tools.doc.blocks[0].en == "This sentence is continued and the tail of it."
    assert "b-0009" not in [b.id for b in tools.doc.blocks]


def test_merge_block_rejects_first_block_of_page(tools):
    out = tools.call("merge_block", {"id": "b-0001"})
    assert out.error and tools.stats.rejected == 1


def test_delete_block_requires_containment(tools):
    text = "This exact sentence appears twice in the page extraction."
    tools.doc.blocks[0].en = text
    tools.doc.blocks.insert(1, Block(id="b-0008", type="p", en=text,
                                     payload={"page": 1, "bbox": [0, 0, 1, 1]}))
    assert not tools.call("delete_block", {"id": "b-0008"}).error
    assert tools.stats.dropped == 1
    # 不包含 → 拒绝（否则会把正文删掉）
    out = tools.call("delete_block", {"id": "b-0002"})
    assert out.error and tools.stats.rejected == 1


def test_reorder_page_requires_permutation(tools):
    tools.doc.blocks.insert(1, Block(id="b-0007", type="p", en="another block",
                                     payload={"page": 1, "bbox": [0, 0, 1, 1]}))
    ok = tools.call("reorder_page", {"page": 1, "ids": ["b-0007", "b-0001"]})
    assert not ok.error and tools.stats.reordered == 1
    assert [b.id for b in tools.doc.blocks][:2] == ["b-0007", "b-0001"]
    bad = tools.call("reorder_page", {"page": 1, "ids": ["b-0007"]})       # 漏了 b-0001
    assert bad.error and "排列" in bad.text


# ── ④ 收尾与"所有页都要校对"的强制 ────────────────────────────────────────

def test_finish_refused_while_pages_pending(tools):
    out = tools.call("finish", {"summary": "done"})
    assert out.error and "还有 2 页" in out.text          # 一页都没核对，不能被放行
    assert not tools.finished


def test_finish_allowed_after_all_pages_marked(tools):
    tools.call("mark_page_done", {"page": 1, "note": "无问题"})
    left = tools.call("mark_page_done", {"page": 2})
    assert "还剩 0 页" in left.text or "可以 finish" in left.text
    assert not tools.call("finish", {"summary": "整篇核对完毕"}).error
    assert tools.finished and any("整篇核对完毕" in n for n in tools.stats.notes)
    assert tools.stats.notes[0] == "第 1 页：无问题"


# ── ⑤ agent 循环（用替身 LLM 驱动真实的工具层）────────────────────────────

class _ScriptedLLM:
    """按脚本回 tool_calls 的替身：每一步可以是「调工具」或「停下说句话」。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def __call__(self, messages, tools):
        self.seen.append([dict(m) if isinstance(m, dict) else m for m in messages])
        step = self.script.pop(0) if self.script else {"calls": [], "text": "结束了"}
        return {"finish_reason": "tool_calls" if step.get("calls") else "stop",
                "message": {"role": "assistant", "content": step.get("text", ""),
                            "tool_calls": [{"id": f"call_{i}", "type": "function",
                                            "function": {"name": n, "arguments": json.dumps(a)}}
                                           for i, (n, a) in enumerate(step.get("calls") or [])]}}


def _run(tmp_path, script, doc=None, **kw):
    doc = doc or _doc("A block of text with vari- ous issues to fix.", page=1)
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=1)
    pf = Proofreader(CFG, **kw)
    pf._chat = _ScriptedLLM(script)                        # 只换 LLM，工具层是真的
    st = pf.proofread_doc(doc, pdf)
    return doc, st, pf._chat


def test_loop_executes_tools_and_applies_edits(tmp_path):
    doc, st, llm = _run(tmp_path, [
        {"calls": [("read_page", {"page": 1})]},
        {"calls": [("edit_block", {"id": "b-0001", "text": "A block of text with various issues to fix.",
                                   "reason": "vari- ous 是断词"})]},
        {"calls": [("mark_page_done", {"page": 1})]},
        {"calls": [("finish", {"summary": "done"})]},
    ])
    assert doc.blocks[0].en == "A block of text with various issues to fix."
    assert st.text_fixed == 1 and st.pages == 1 and st.ok_pages == [1]
    assert not st.stopped and not st.unreviewed


def test_image_is_delivered_as_a_user_message_after_tool_results(tmp_path):
    """⚠️ 图不能插在 tool 消息中间 —— 会破坏 tool_calls ↔ tool 的配对（provider 直接报错）。"""
    _doc_, st, llm = _run(tmp_path, [
        {"calls": [("read_page", {"page": 1})]},
        {"calls": [("mark_page_done", {"page": 1})]},
        {"calls": [("finish", {})]},
    ])
    msgs = llm.seen[1]                                     # 第 2 次调用时消息里应已有图
    roles = [m["role"] for m in msgs]
    assert roles[-1] == "user" and isinstance(msgs[-1]["content"], list)
    assert msgs[-1]["content"][1]["type"] == "image_url"
    # 图的前一条必须是 tool 结果
    assert roles[-2] == "tool"
    # assistant(tool_calls) → tool 的配对完整
    assert any(m.get("tool_calls") for m in msgs if m["role"] == "assistant")


def test_loop_nudges_when_pages_left_unreviewed(tmp_path):
    """模型想直接停手 → 追问，不是就这么算了（宿主："所有能校对的都要校对"）。"""
    doc, st, llm = _run(tmp_path, [
        {"text": "我看完了。", "calls": []},               # 没调任何工具就想结束
        {"calls": [("mark_page_done", {"page": 1})]},
        {"calls": [("finish", {})]},
    ])
    assert any("mark_page_done" in json.dumps(m, ensure_ascii=False) for m in llm.seen[1])
    assert st.pages == 1 and not st.unreviewed


def test_loop_stops_when_agent_keeps_stalling(tmp_path):
    doc, st, _ = _run(tmp_path, [{"text": "跳过", "calls": []} for _ in range(6)])
    assert st.unreviewed == [1]
    assert "未核对" in st.stopped


def test_round_budget_is_enforced(tmp_path):
    script = [{"calls": [("read_blocks", {"page": 1})]} for _ in range(10)]
    _doc_, st, _ = _run(tmp_path, script, max_rounds=3)
    assert st.rounds <= 3 and "轮数用尽" in st.stopped


def test_token_budget_is_enforced(tmp_path, monkeypatch):
    doc = _doc("Another block of text to proofread here.", page=1)
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=1)
    pf = Proofreader(CFG, token_budget=1000)
    inner = _ScriptedLLM([{"calls": [("read_blocks", {"page": 1})]} for _ in range(10)])

    def counting(messages, tools):
        pf.stats.tokens += 5000                            # 每次调用都超预算
        return inner(messages, tools)

    pf._chat = counting
    st = pf.proofread_doc(doc, pdf)
    assert "token 预算用尽" in st.stopped and st.rounds <= 2


def test_resume_pages_are_pre_marked_and_not_pending(tmp_path):
    doc = _doc("Page one text that is long enough.", page=1)
    doc.blocks.append(Block(id="b-0002", type="p", en="Page two text that is long enough.",
                            payload={"page": 2, "bbox": [0, 0, 1, 1]}))
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=2)
    pf = Proofreader(CFG)
    pf._chat = _ScriptedLLM([
        {"calls": [("mark_page_done", {"page": 2})]},
        {"calls": [("finish", {})]},
    ])
    st = pf.proofread_doc(doc, pdf, skip_pages=[1])
    assert st.ok_pages == [1, 2] and st.pages == 1         # 只做了第 2 页，但账上两页都核对过
    assert st.skipped == 1 and not st.unreviewed


def test_seed_tells_agent_what_to_do_and_what_is_skipped(tmp_path):
    doc = _doc("Page one text long enough to review.", page=1)
    doc.meta["title"] = "A review of machine learning"
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=1)
    pf = Proofreader(CFG)
    llm = _ScriptedLLM([{"calls": [("mark_page_done", {"page": 1})]}, {"calls": [("finish", {})]}])
    pf._chat = llm
    pf.proofread_doc(doc, pdf, skip_pages=[1])
    seed = json.dumps(llm.seen[0], ensure_ascii=False)
    assert "已经校对过" in seed and "逐页校对" in seed


# ── ⑥ 尺子与护栏（纯函数）────────────────────────────────────────────────

def test_artifacts_only_reports_string_level_facts():
    got = _artifacts("multiple vari- ables are used [ 12 ] here. Table 1 .")
    assert any("行末断词" in g for g in got)
    assert any("引用编号" in g for g in got)
    assert any("句点前" in g for g in got)
    assert _artifacts("Everything is perfectly clean here.") == []


@pytest.mark.parametrize("old,new,ok", [
    ("vari- ous methods", "various methods", True),
    ("short", "", False),
    ("same text here", "same text here", False),
    ("a b c", "完全不同的另一段内容而且很长很长很长很长", False),
])
def test_text_change_guard(old, new, ok):
    assert _acceptable_text_change(old, new)[0] is ok


def test_stats_summary_mentions_agent_activity():
    s = ProofreadStats(pages=3, rounds=42, tool_calls=60, unreviewed=[4], stopped="轮数用尽（400 轮）")
    out = s.summary()
    assert "42 轮" in out and "未核对 1 页" in out and "轮数用尽" in out


# ── ⑦ 图片与请求体大小（实测 413 的回归）─────────────────────────────────

def test_stale_images_are_pruned_from_context():
    """⚠️ 回归：图不剪 → 每轮重发，十几轮后请求体几 MB → 服务端 413 整篇校对崩掉。"""
    from papershelf.pipeline.proofread import MAX_IMAGES_PER_ROUND, _prune_images

    def img(tag):
        return {"type": "image_url", "image_url": {"url": f"data:x,{tag}"}}

    tags = [f"P{i}" for i in range(MAX_IMAGES_PER_ROUND + 2)]
    msgs = [{"role": "user", "content": [{"type": "text", "text": "看这一页"}, img(t)]} for t in tags]
    _prune_images(msgs)
    left = [p["image_url"]["url"].split(",")[1]
            for m in msgs if isinstance(m["content"], list)
            for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url"]
    assert left == tags[-MAX_IMAGES_PER_ROUND:]              # 只留最近几张
    assert isinstance(msgs[0]["content"], str) and "已从上下文移除" in msgs[0]["content"]
    assert "read_page" in msgs[0]["content"]                 # 告诉它可以再看（命中渲染缓存）


def test_fit_images_drops_oldest_until_under_budget():
    """按**体积**剪，不按张数：服务端上限实测 1MB，不同页的图大小差很多。"""
    from papershelf.pipeline.proofread import _body_size, _fit_images

    def img(pad):
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * pad}}

    msgs = [{"role": "user", "content": [{"type": "text", "text": f"page{i}"}, img(200_000)]}
            for i in range(3)]
    assert _body_size(msgs) > 600_000
    dropped = _fit_images(msgs, budget=300_000)
    assert dropped >= 2 and _body_size(msgs) <= 300_000
    # 丢图不是丢信息：留下的那条说明告诉模型可以重新 read_page（命中渲染缓存）
    assert any("read_page" in m["content"] for m in msgs if isinstance(m["content"], str))
    # 没有图可丢时不死循环
    assert _fit_images([{"role": "user", "content": "纯文本"}], budget=1) == 0


def test_compact_keeps_system_task_summary_and_recent_rounds(tmp_path):
    """⚠️ 回归：不压上下文 → 每轮重发全部历史，3 页烧 41 万 tokens。"""
    from papershelf.pipeline.model import Block, Doc
    from papershelf.pipeline.proofread import KEEP_TAIL_MESSAGES, Proofreader, ProofreadTools

    doc = Doc()
    doc.blocks.append(Block(id="b-0001", type="p", en="text", payload={"page": 1}))
    pdf = tmp_path / "x.pdf"
    _tiny_pdf(pdf, pages=2)
    tools = ProofreadTools(doc, pdf)
    tools.done_pages.add(1)
    tools.stats.text_fixed = 4
    pf = Proofreader(CFG)
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]
    for i in range(12):                                   # 12 轮往返（含 tool 对）
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "结果"})
    before = len(messages)
    pf._compact(messages, tools)
    assert len(messages) < before
    assert messages[0]["content"] == "S" and messages[1]["content"] == "T"   # system/任务不动
    assert "进度摘要" in messages[2]["content"] and "文字修订 4 块" in messages[2]["content"]
    assert messages[3].get("role") != "tool"              # 不留孤儿 tool 消息
    assert len(messages) - 3 <= KEEP_TAIL_MESSAGES + 2
    # 已完成的页写进摘要、未完成的页明确列出
    assert "已核对完成的页：[1]" in messages[2]["content"]


def test_split_allocates_unique_ids_for_every_new_block(tools):
    """⚠️ 回归：多段切分时新块**先全部构造再插入**，只看 doc.blocks 会撞 ID。

    实测一篇论文切出 3 个已存在同名的 `b-0044` —— 重复 ID 会让划痕/笔记锚到错误的块。
    """
    text = "Paragraph one of this block. " * 3 + "Paragraph two of this block. " * 3 + \
           "Paragraph three of this block. " * 3
    tools.doc.blocks[0].en = text
    out = tools.call("split_block", {"id": "b-0001",
                                     "parts": ["Paragraph one of this block. " * 3,
                                               "Paragraph two of this block. " * 3,
                                               "Paragraph three of this block. " * 3]})
    assert not out.error
    ids = [b.id for b in tools.doc.blocks]
    assert len(ids) == len(set(ids))                    # 绝不出现重复 ID
    assert len(ids) == 4                                # 原块 + 2 个新块 + 原有的 b-0002


# ── ⑧ 成本（宿主 2026-09-14：「跑完一篇 37 页不能失控」）──────────────────

def test_thinking_budget_is_mapped_to_provider_params():
    """⚠️ 成本的真正大头是**思考**：同一个请求 auto 2001 tokens → `off` 80 tokens。

    参数名不认就白设（上游会忽略，悄悄退回最贵的形态），所以映射关系必须钉死。
    """
    from papershelf.pipeline.proofread import _apply_thinking

    p = {"model": "m"}
    _apply_thinking(p, "off")
    assert p["thinking"] == {"type": "disabled"} and "reasoning_effort" not in p
    p = {}
    _apply_thinking(p, "minimal")
    assert p["reasoning_effort"] == "minimal" and "thinking" not in p
    for empty in ("", "auto", "on"):
        p = {}
        _apply_thinking(p, empty)
        assert p == {}                                  # 空/auto = 不发，用上游默认


def test_seed_carries_only_page_level_suspect_counts(tmp_path):
    """⚠️ 成本回归：seed 是**每轮重发**的，塞全篇明细 → 实测 2.7k tokens/轮 × 78 轮 ≈ 21 万白烧。

    所以 seed 里只留**页级计数**；"哪一块、可疑在哪"下移到当页的 `read_blocks`（`flags`）。
    """
    doc = _doc("A block with vari- ous issues [ 12 ] here.", page=1)
    doc.blocks.append(Block(id="b-0002", type="p", en="Clean text with nothing to flag.",
                            payload={"page": 2, "bbox": [0, 0, 1, 1]}))
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=2)
    pf = Proofreader(CFG)
    llm = _ScriptedLLM([{"calls": [("mark_page_done", {"page": 1})]},
                        {"calls": [("mark_page_done", {"page": 2})]},
                        {"calls": [("finish", {})]}])
    pf._chat = llm
    pf.proofread_doc(doc, pdf)
    seed = llm.seen[0][1]["content"]                          # 任务说明本体
    assert '"suspect_pages": {"1": 1}' in seed                # 第 1 页有 1 个可疑块
    assert "b-0001" not in seed                               # 但**不列块号**（明细在工具结果里）
    assert len(seed) < 2500                                   # seed 本身必须很小（每轮重发）


def test_read_blocks_flags_suspicious_blocks_inline(tools):
    """可疑点跟着**当页的工具结果**走（只活在最近几轮上下文里），不再靠 seed 背全篇。"""
    tools.doc.blocks[0].en = "Text with vari- ous issues and [ 12 ] citations."
    raw = tools.call("read_blocks", {"page": 1}).text
    first = json.loads(raw)["blocks"][0]
    assert "断词" in first["flags"] and "引用空格" in first["flags"]
    # 干净块不带 flags（省字符）
    tools.doc.blocks[0].en = "Text with nothing to flag at all."
    assert "flags" not in json.loads(tools.call("read_blocks", {"page": 1}).text)["blocks"][0]


def test_stuck_page_gets_hurried(tmp_path):
    """提示词写了「每页两轮」，但**提示词不是护栏**：连着磨 3 轮没 mark_page_done 就催一次。"""
    from papershelf.pipeline.proofread import MAX_ROUNDS_PER_PAGE

    script = [{"calls": [("read_blocks", {"page": 1})]} for _ in range(MAX_ROUNDS_PER_PAGE + 1)]
    script += [{"calls": [("mark_page_done", {"page": 1})]}, {"calls": [("finish", {})]}]
    _doc_, st, llm = _run(tmp_path, script)
    hurried = [m for m in llm.seen if "轮数就是成本" in json.dumps(m, ensure_ascii=False)]
    assert hurried, "磨了 4 轮都没催"
    assert st.pages == 1 and not st.stopped


# ── ⑨ 瞬时故障（⚠️ 2026-09-14：跑真论文时被 504 咬掉整篇）──────────────────

class _FlakyPost:
    """按脚本返回状态码的假 httpx 客户端：真发不了网，但把重试路径走一遍。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, payload, headers):
        self.calls += 1
        item = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(item, Exception):
            raise item
        if item != 200:
            return _Resp(item)
        return _Resp(200, {"choices": [{"message": {"content": "ok"}}],
                           "usage": {"total_tokens": 7}})


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.request = _REQ

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=self.request,
                                        response=self)


_REQ = httpx.Request("POST", "http://x/chat/completions")


def _patched_client(monkeypatch, handler):
    """把 httpx.Client 换成假的（`_post` 内部 `with httpx.Client(...) as c: c.post(...)`）。"""

    class _C:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):     # noqa: A002 — 跟 httpx 一致
            return handler(json, headers)

    monkeypatch.setattr(httpx, "Client", _C)


def test_transient_errors_are_retried_with_backoff(monkeypatch):
    """⚠️ 真事故：37 页跑到第 11 分钟，上游网关一次 **504** → 整篇校对归零（几十万 token 白烧）。

    `429`/`5xx`/超时/断连是**瞬时**的，必须退避重试；`retries` 曾经是个从未被使用的摆设。
    """
    sleeps: list[float] = []
    monkeypatch.setattr("papershelf.pipeline.proofread.time.sleep", sleeps.append)
    handler = _FlakyPost([504, 429, 200])
    _patched_client(monkeypatch, handler)
    pf = Proofreader(CFG, retries=3)
    out = pf._chat([], None)                               # 走完整入口（含记账）
    assert out["message"]["content"] == "ok"
    assert handler.calls == 3 and len(sleeps) == 2        # 退避了两次
    assert sleeps[0] < sleeps[1]                           # 是**退避**，不是等长重试
    assert pf.stats.tokens == 7                            # 成功那次的用量要记账（失败的不记）


def test_retries_are_finite_and_hard_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr("papershelf.pipeline.proofread.time.sleep", lambda s: None)
    handler = _FlakyPost([500, 500, 500, 500])
    _patched_client(monkeypatch, handler)
    pf = Proofreader(CFG, retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        pf._chat([], None)
    assert handler.calls == 3                              # 首次 + 2 次重试，不无限重试

    handler = _FlakyPost([400, 200])                       # 400 = 请求本身的问题
    _patched_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        Proofreader(CFG, retries=3)._chat([], None)
    assert handler.calls == 1                              # 不重试（重试只会白花钱）


def test_interruption_keeps_what_was_already_done(tmp_path):
    """中断也必须交账：已改的块 + 已核对的页要留下来（否则重跑从第 1 页重买）。"""
    from papershelf.pipeline.proofread import ProofreadTools

    doc = _doc("Page one text that is long enough to review.", page=1)
    doc.blocks.append(Block(id="b-0002", type="p", en="Page two text, long enough to review.",
                            payload={"page": 2, "bbox": [0, 0, 1, 1]}))
    pdf = tmp_path / "t.pdf"
    _tiny_pdf(pdf, pages=2)
    pf = Proofreader(CFG)
    inner = _ScriptedLLM([
        {"calls": [("edit_block", {"id": "b-0001", "text": "Page one text that is long enough "
                                                           "to review.", "reason": "x"})]},
        {"calls": [("mark_page_done", {"page": 1})]},
    ])

    def boom(messages, tools):
        if len(inner.seen) >= 2:
            raise httpx.ConnectError("网络断了")
        return inner(messages, tools)

    pf._chat = boom
    st = pf.proofread_doc(doc, pdf)
    assert st.ok_pages == [1] and st.unreviewed == [2]     # 第 1 页的成果保住了
    assert "中断" in st.stopped and st.failed == 1
    assert st.tokens >= 0
    assert ProofreadTools(doc, pdf).done_pages == set()    # 只是确认工具层可再建（无副作用）

