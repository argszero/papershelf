"""㊻ 翻译的跨块承接（2026-09-19，宿主选 C）。

宿主原话：「翻译时需要参考前后的block，尤其是跨页时。前面的block的半句翻译应该和后面 block
的半句翻译很好的承接起来」（截图：某页页首孤零零一个「工艺。」）。

**病灶不是"没给上下文"**（上下文早就给了），是三处叠加：
① 上下文取的是**裸相邻**块 —— 跨页处那个"上文"恰好是**页眉**（`payload["band"]`，㊶）；
② 窗口只有各 1 块；
③ 提示词里**没有一条**说"这两块是同一句话"—— 那例两半本来就在同一个切片里，
   模型看得见两半，照样把前半句译完整、后半句译成孤零零一个词。

这些断言都对应真实踩过的坑（每条都在生产库副本上复现过），不是形式化覆盖。
"""

from __future__ import annotations

from papershelf.pipeline.model import Block
from papershelf.pipeline.translator import (
    CTX_NEIGHBORS,
    SYSTEM_PROMPT,
    Translator,
)

# ── 造块小工具 ────────────────────────────────────────────────────────────


def para(bid: str, text: str, **payload) -> Block:
    return Block(id=bid, type="p", en=text, payload=payload)


def header(bid: str, text: str = "Journal of Intelligent Manufacturing 35:1409") -> Block:
    """页眉：**不是独立块类型**，而是带 `payload["band"]` 的 `p` 块（㊶ 定的）。

    ⚠️ 这正是宿主那例的成因 —— 只看 `type` 会把页眉当正文。
    """
    return Block(id=bid, type="p", en=text, payload={"band": "top", "page": 3})


def tr() -> Translator:
    t = Translator.__new__(Translator)
    t.glossary = []
    return t


# ── 接缝检测 ──────────────────────────────────────────────────────────────


def test_seam_detected_when_half_sentence_is_split():
    """前块没结句 + 后块小写起 ⇒ 同一句话被切开。"""
    a = para("b-0001", "This review focuses on defect detection based on")
    b = para("b-0002", "the monitoring data collected in the process.")
    assert [(x.id, y.id) for x, y in Translator.seam_pairs([a, b])] == [("b-0001", "b-0002")]


def test_seam_survives_the_page_header_between_the_two_halves():
    """**跨页那一例**：两半之间夹着页眉，仍须认出来（跳过页面家具后再看相邻）。

    裸相邻取上下文/相邻对都会在这里断掉 —— 那正是宿主截图里 p3 页首那半句的处境。
    """
    a = para("b-0026", "However, ML has also been criticized when combined with traditional")
    h = header("b-0027")
    b = para("b-0028", "manufacturing processes. Therefore, it has found wide use.")
    assert [(x.id, y.id) for x, y in Translator.seam_pairs([a, h, b])] == [("b-0026", "b-0028")]


def test_labels_and_page_numbers_are_not_half_sentences():
    """**不是话**的块不算半句：页码/标注/表头/作者行（实测 4 篇里共 8 处这种假阳性）。

    解析端的判据挂在几何上（必须在同一页的左右栏接缝上），孤零零一个 `123` 落不到那条缝；
    翻译端只有文本与顺序 ⇒ 必须另有一条判据。判据是**词数**（这些都是 1–4 个词）：
    实测的假阳性全是标注 `MICRESS` / `Unsupervised`、表头 `voltage`、页码 `123`、
    作者行 `B T. Herzog` / `s3826323@student.rmit.edu.au B A. Molotnikov`、
    刊名行 `Journal of Manufacturing Processes`。
    """
    tail = "advanced. In particular, Deep Learning has seen enormous progress"
    for junk in ("123", "MICRESS", "Unsupervised", "voltage"):
        assert Translator.seam_pairs([para("b-0087", junk), para("b-0095", tail)]) == [], junk
    # 反向（标签块在后）本来就被"后块小写起"挡一部分，但作者行是小写起的 —— 也要挡住
    for junk in ("123", "voltage", "B T. Herzog", "s3826323@student.rmit.edu.au B A. Molotnikov",
                 "Journal of Manufacturing Processes"):
        assert Translator.seam_pairs([para("b-0001", "text that never ends"), para("b-0002", junk)]) == [], junk


def test_a_genuinely_short_second_half_still_counts():
    """阈值不能定在"字母数/长度"上：真接缝的后半可能只有四个词。

    实测 paper 3 `b-0033 → b-0036`：`…emphasizing their` ／ `transformative impact on WAAM.`
    —— 后半 26 个字母、4 个词，但它是货真价实的半句。
    """
    a = para("b-0033", "This review systematically addresses these challenges by critically "
                       "examining the state of the art, emphasizing their")
    b = para("b-0036", "transformative impact on WAAM.")
    assert [(x.id, y.id) for x, y in Translator.seam_pairs([a, b])] == [("b-0033", "b-0036")]


def test_structural_blocks_are_not_half_sentences():
    """标题/图注/表格/文献条目与前后文不构成半句话（对"结构单元"谈承接是另一种错）。"""
    a = para("b-0001", "The approach builds on recent work such as")
    for t in ("h2", "figure", "table", "refs", "ref"):
        b = Block(id="b-0002", type=t, en="(2018)). evolving machine learning pipelines")
        assert Translator.seam_pairs([a, b]) == [], t


# ── 切片不得切在接缝上 ────────────────────────────────────────────────────


def test_without_keep_together_the_pair_is_split_across_chunks():
    """对照实验：不传 `keep_together` 时，`max_blocks=1` 必然把这一对切开。"""
    a = para("b-0001", "built on")
    b = para("b-0002", "the same idea.")
    assert [[x.id for x in c] for c in Translator.chunk([a, b], max_blocks=1)] == \
        [["b-0001"], ["b-0002"]]


def test_keep_together_holds_the_pair_in_one_chunk():
    a = para("b-0001", "built on")
    b = para("b-0002", "the same idea.")
    kept = Translator.chunk([a, b], max_blocks=1, keep_together={("b-0001", "b-0002")})
    assert [[x.id for x in c] for c in kept] == [["b-0001", "b-0002"]]


def test_keep_together_holds_across_the_header_block_between_them():
    """跨页那一对中间隔着页眉：切点落在**页眉与后半句之间**同样是切开接缝。

    只看"当前切片最后一块"，`cur[-1]` 是页眉，判据会漏。
    """
    a = para("b-0001", "built on")
    h = header("b-0002")
    b = para("b-0003", "the same idea.")
    kept = Translator.chunk([a, h, b], max_blocks=1, keep_together={("b-0001", "b-0003")})
    assert [[x.id for x in c] for c in kept] == [["b-0001", "b-0002", "b-0003"]]


def test_chained_seams_all_stay_together():
    """连号接缝（`b-0001→b-0002→b-0003`，实测有）：后半**同时**是下一对的前半。

    只在"凑齐了"的分支里清空 `open_pair`、不再查 `firsts`，会把第二对整对漏掉。
    """
    a, b, c = (para("b-0001", "one"), para("b-0002", "two"), para("b-0003", "three."))
    kept = Translator.chunk([a, b, c], max_blocks=1,
                            keep_together={("b-0001", "b-0002"), ("b-0002", "b-0003")})
    assert [[x.id for x in c_] for c_ in kept] == [["b-0001", "b-0002", "b-0003"]]


def test_keep_together_does_not_glue_unrelated_blocks():
    """接缝之外照旧按尺寸切（`keep_together` 只买"这一对不许分家"，不是取消切片）。"""
    blocks = [para(f"b-{i:04d}", f"block {i}") for i in range(1, 6)]
    kept = Translator.chunk(blocks, max_blocks=2, keep_together={("b-0001", "b-0002")})
    assert [[x.id for x in c] for c in kept] == [
        ["b-0001", "b-0002"], ["b-0003", "b-0004"], ["b-0005"],
    ]


# ── 上下文：取最近的**正文**邻块 ──────────────────────────────────────────


def test_context_skips_page_furniture_and_widens_the_window():
    """上下文窗口 = 前后各 `CTX_NEIGHBORS` 个正文块，且**跳过页眉**。

    原先取 `ordered[idx-1]`：跨页处那个"上文"就是页眉（全库实测 24 次），
    模型拿到一段期刊页眉，对"上一句说到哪"仍然一无所知。
    """
    blocks = [
        para("b-0001", "first paragraph"),
        para("b-0002", "second paragraph"),
        para("b-0003", "third paragraph"),
        header("b-0004"),
    ]
    got = Translator._ctx_before(blocks, 3)          # 切片从页眉后的块开始
    assert [b.id for b in got] == ["b-0002", "b-0003"]   # 由远到近，页眉不在其中
    assert CTX_NEIGHBORS == 2


def test_context_after_skips_furniture_and_respects_count():
    blocks = [
        header("b-0001"),
        para("b-0002", "first"),
        para("b-0003", "second"),
        para("b-0004", "third"),
    ]
    assert [b.id for b in Translator._ctx_after(blocks, 0)] == ["b-0002", "b-0003"]


def test_context_is_empty_at_the_document_edges():
    blocks = [header("b-0001"), para("b-0002", "only paragraph")]
    assert Translator._ctx_before(blocks, 1) == []
    assert Translator._ctx_after(blocks, 1) == []


# ── 提示词 ────────────────────────────────────────────────────────────────


def test_user_prompt_lists_a_seam_wholly_inside_the_range():
    a = para("b-0001", "…based on the monitoring data collected")
    b = para("b-0002", "from the process and its environment.")
    p = tr()._user_prompt([a, b], None, None, [(a, b)])
    assert "【跨块接缝" in p
    assert "b-0001 → b-0002" in p
    assert "同一句话" in p
    # **必须给出这句的完整原文**：一句"这两块是一句话"的空话实测不够，模型照样译断
    assert "monitoring data collected from the process and its environment." in p
    assert "断点在" in p


def test_user_prompt_handles_a_half_outside_the_range_both_directions():
    """只有一半在本次范围内时说法不同：后半要"接着写"、前半要"不许补成完整句"。"""
    a = para("b-0001", "…based on the monitoring data collected")
    b = para("b-0002", "from the process and its environment.")
    only_b = tr()._user_prompt([b], None, None, [(a, b)])
    assert "b-0001 → b-0002" in only_b and "不要另起一句" in only_b
    only_a = tr()._user_prompt([a], None, None, [(a, b)])
    assert "b-0001 → b-0002" in only_a and "没有说完" in only_a
    assert "不要合并" in only_a


def test_user_prompt_says_nothing_about_unrelated_seams():
    a = para("b-0001", "…based on")
    b = para("b-0002", "the process.")
    far = para("b-0009", "unrelated paragraph.")
    p = tr()._user_prompt([far], None, None, [(a, b)])
    assert "【跨块接缝" not in p


def test_user_prompt_renders_every_context_block():
    ctx = [para("b-0001", "first context paragraph"),
           para("b-0002", "second context paragraph")]
    p = tr()._user_prompt([para("b-0003", "target")], ctx, ctx)
    assert p.count("first context paragraph") == 2      # 上文 / 下文各一次
    assert p.count("second context paragraph") == 2


def test_system_prompt_carries_the_seam_rule():
    """规则 7 是"承接"的另一半 —— 只给上下文、不给这条要求，实测模型照样译断。"""
    assert "【跨块接缝" in SYSTEM_PROMPT
    assert "不要合并成一块" in SYSTEM_PROMPT          # 块数/`data-b` 是硬约束（决策④）
    assert "前半块只放断点之前的译文" in SYSTEM_PROMPT
    assert "后半块从断点之后开始" in SYSTEM_PROMPT
    assert "先把这句完整译成一句通顺的中文" in SYSTEM_PROMPT   # 实测的差别就在这条动作指令


# ── 端到端（打桩 LLM，零 token）───────────────────────────────────────────


def _stub_translator(record: list[str]) -> Translator:
    t = Translator.__new__(Translator)
    t.glossary, t.system = [], SYSTEM_PROMPT
    t.tokens_used, t.needs_review = 0, []
    t._table_retry = t._ref_retry = 0

    def chat(user: str, system: str | None = None) -> str:
        record.append(user)
        # 回一份"块数、`data-b`、顺序都不变"的中文（决策④ 的硬约束）
        ids = [seg.split('"')[1] for seg in user.split("data-b=")[1:]]
        return "\n".join(f'<p data-b="{i}">中文译文（{i}）。</p>' for i in dict.fromkeys(ids))

    t._chat = chat                                    # type: ignore[method-assign]
    return t


def test_translate_blocks_keeps_a_seam_pair_in_one_request():
    """真路径：`translate_blocks` 必须把接缝两半交给**同一次**调用。

    收窄 `max_blocks` 逼出切片边界 —— 不接上 `keep_together` 时，两半分处两个请求，
    谁都看不见另一半，"承接"根本无从谈起（实测 4 篇各 1–6 对落在边界上）。
    """
    a = para("b-0026", "However, ML has also been criticized when combined with traditional")
    h = header("b-0027")
    b = para("b-0028", "manufacturing processes. Therefore, it has found wide use in AM.")
    sent: list[str] = []
    t = _stub_translator(sent)
    t.translate_blocks([a, h, b], max_blocks=1, log=lambda *_: None)
    both = [p for p in sent if 'data-b="b-0026"' in p and 'data-b="b-0028"' in p]
    assert len(both) == 1                        # 两半（含中间的页眉）只在同一次请求里出现
    assert both[0] == sent[0]                    # 而且就是第一轮那次
    assert 'data-b="b-0027"' in sent[0]          # 页眉夹在中间，仍在同一片里
    assert "【跨块接缝" in sent[0]


def test_translate_blocks_context_excludes_the_page_header():
    """端到端确认：跨页那一段的"上文"不能是页眉（宿主那例的直接死因）。"""
    a = para("b-0100", "The first paragraph ends here.")
    h = header("b-0101")
    b = para("b-0102", "The paragraph after the page break begins.")
    t = Translator.__new__(Translator)
    t.glossary, t.system = [], SYSTEM_PROMPT
    t.tokens_used, t.needs_review = 0, []
    t._table_retry = t._ref_retry = 0
    sent: list[str] = []

    def chat(user: str, system: str | None = None) -> str:
        sent.append(user)
        return '<p data-b="b-0102">中文。</p>'

    t._chat = chat                                     # type: ignore[method-assign]
    t.translate_blocks([a, h, b], only={"b-0102"}, log=lambda *_: None)
    assert len(sent) == 1
    assert "The first paragraph ends here." in sent[0]   # 上文 = 最近的正文本体
    assert "Journal of Intelligent Manufacturing" not in sent[0]
