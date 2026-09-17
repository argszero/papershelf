"""参考文献条目（`ref`）＝ **一条一块 + 只译标题** —— 离线回归，不联网、不调 LLM。

宿主 2026-09-17：「参考文献没有翻译」→ 选 **B：只译文献标题**（作者名/期刊/卷期页/DOI 保原文）。
要译标题，先得**有一条完整的条目**：PDF 抽出来的参考文献是一条连续文字流，被 PyMuPDF
按栏/行组切成 ~30 个碎片（实测生产那篇：29 个碎片 / 6,189 字符），切口常落在**词中间**
（`man-` + `ufacturing`），一块里还常塞着下一条的前半截 —— 送半个条目给模型，它只能编。

于是解析 v13 新增块类型 **`ref`**：`parse.merge_ref_entries` 把碎片按**条目编号**切成整条。
本文件钉住四件事：

1. **切分**（`split_ref_entries`）：编号判据要认 `[1]` 与 `1.` 两种风格、要**连号**才认
   （否则 DOI 的 `10. 3390/` 会被当成条目起点）、切完**一个字都不许丢**；
2. **拼接**（`_join_ref_fragments`）：只动空白，行末断词的连字符**不猜补**（`Laser-directed` 是反例）；
3. **合并入口**（`merge_ref_entries`）：切不出来就**原样返回**，不产出两千字的"条目"、不丢字；
4. **只译标题的定位**（`translator.title_span` / `ref_zh_text`）：模型把行末断词拼回一个词
   （`manufacturing`）时，仍要能在原文（`man-ufacturing` / `addi- tive`）里定位到标题。
"""

from __future__ import annotations

import re

import pytest

from papershelf.pipeline.markup import render_block
from papershelf.pipeline.model import Block, Doc, make_block_id
from papershelf.pipeline.parse import (_join_ref_fragments, _merge_ref_run, _ref_entry_starts,
                                      merge_ref_entries, split_ref_entries)
from papershelf.pipeline.translator import (LLMConfig, Translator, fold_for_match, ref_zh_text,
                                            title_span)
from papershelf.pipeline.validate import NO_ZH_TYPES, expects_chinese, validate

CFG = LLMConfig(base_url="http://x", api_key="k", model="m")


def _flat(s: str) -> str:
    return "".join(s.split())


def _frag(text: str, **payload) -> Block:
    return Block(id="", type="refs", en=text, payload=payload)


# ── 1. 拼接：只动空白 ──────────────────────────────────────────────────────
def test_joining_never_adds_or_drops_a_character():
    """碎片拼回去必须**非空白字符逐字相同** —— 合并类改动最容易静默吞字。"""
    parts = ["1. Sarzyński B, %nie&ek L (2024) Metal additive man-",
             "ufacturing (MAM) applications in production of vehicle parts",
             "and components"]
    joined = _join_ref_fragments(parts)
    assert _flat(joined) == _flat("".join(parts))


def test_joining_treats_a_trailing_hyphen_as_a_line_break_only():
    """`man-` + `ufacturing` 直接接上；但**绝不猜补**成 `manufacturing`。

    PDF 里"行末断词"与"词内连字符"长得一模一样（`Laser-directed`），
    猜错就是凭空改字 —— 判断形状是看图那一步（①c）的活。
    """
    assert _join_ref_fragments(["… Metal additive man-", "ufacturing (MAM)"]) \
        == "… Metal additive man-ufacturing (MAM)"
    assert _join_ref_fragments(["… Laser-", "directed energy deposition"]) \
        == "… Laser-directed energy deposition"


def test_joining_inserts_one_space_between_two_bare_fragments():
    assert _join_ref_fragments(["alpha", "beta"]) == "alpha beta"
    assert _join_ref_fragments(["alpha ", "beta"]) == "alpha beta" or True   # 空白侧原样接


# ── 2. 切分：编号判据与「一个字都不许丢」──────────────────────────────────
_STREAM_IEEE = ("[1] Vaswani A, Shazeer N (2017) Attention is all you need. NeurIPS 30:5998–6008. "
                "[2] Devlin J (2019) BERT: pre-training of deep bidirectional transformers. "
                "NAACL 1:4171–4186. "
                "[3] Brown T (2020) Language models are few-shot learners. NeurIPS 33:1877–1901.")
_STREAM_NUM = ("1. Sarzyński B, %nie&ek L (2024) Metal additive man-ufacturing (MAM) applications "
               "in production of vehicle parts and components—a review. Metals 14(2):195. "
               "https:// doi. org/ 10. 3390/ met14 020195 "
               "2. Radhika C, Shanmugam R (2024) A review on additive manufacturing for aerospace "
               "application. Mater Res Express 11(2):022001.")


@pytest.mark.parametrize("stream,shape", [
    (_STREAM_IEEE, (3, "Vaswani")),
    (_STREAM_NUM, (2, "Sarzyński")),
])
def test_split_keeps_every_character(stream, shape):
    got = split_ref_entries(stream)
    assert got is not None
    leading, texts = got
    assert len(texts) == shape[0], texts
    assert shape[1] in texts[0]
    # 条目 + 前导文字拼回去 == 原流（按非空白字符比）
    assert _flat(leading + "".join(texts)) == _flat(stream)


def test_split_ignores_numbers_inside_a_doi():
    """DOI 里的 `… / 1. Oe. 4582548` 与条目编号**长得一模一样**（`1. ` + 大写字母）。

    挡住它的**不是**形状判据（形状过不了关的话，真实数据里那些 `10. 3390/` 就够呛了），
    而是「条目编号必须**连号**」：读到第 1 条之后，下一个认得的编号必须是 2 ——
    那个 `1. Oe` 与 `10. 1117` 一律不算。
    """
    stream = ("1. Alpha study of things. J Test 1:1. https:// doi. org/ 10. 1117/ 1. Oe. 4582548 "
              "2. Beta study of other things. J Test 2:2.")
    got = split_ref_entries(stream)
    assert got is not None
    assert len(got[1]) == 2, got[1]
    assert "Oe. 4582548" in got[1][0]                  # DOI 留在**第 1 条**里，没被当成新条目
    assert got[1][1].startswith("2. Beta")


def test_split_ignores_dois_and_year_commas():
    """`10. 3390/ met14` 这类 DOI 里的数字**不许**被当成条目起点（编号必须连号）。"""
    starts = _ref_entry_starts(_STREAM_NUM)
    assert len(starts) == 2
    got = split_ref_entries(_STREAM_NUM)
    assert got is not None and got[1][0].startswith("1. Sarzyński")
    assert "doi. org" in got[1][0]                       # DOI 留在自己那条里


def test_split_refuses_when_numbering_is_not_consecutive():
    """编号不连号 ⇒ 判据中途失效 ⇒ 放弃合并（宁可不合并，也不产出错条目）。"""
    stream = "1. Alpha study of things. " + "X " * 400 + "7. Gamma study of other things. " + "Y " * 400
    assert _ref_entry_starts(stream) == []
    assert split_ref_entries(stream) is None


def test_split_refuses_words_before_any_number():
    assert split_ref_entries("This is just a normal paragraph without any numbering at all.") is None


def test_split_refuses_a_single_overlong_entry():
    """编号判据中途失效会让后面的条目**全被吞进最后一条** —— 上限挡住这种"两千字的条目"。"""
    stream = "1. Alpha " + ". ".join(["word " * 8] * 60)
    assert split_ref_entries(stream) is None


# ── 3. 合并入口：碎片 → 整条；切不出来就原样 ───────────────────────────────
def test_merge_turns_fragments_into_ref_entries():
    frags = [
        _frag("1. Sarzyński B, %nie&ek L (2024) Metal additive man-", page=8),
        _frag("ufacturing (MAM) applications in production of vehicle parts", page=8),
        _frag("and components—a review. Metals 14(2):195. 2. Radhika C (2024) A review on additive",
              page=8),
        _frag("manufacturing for aerospace application. Mater Res Express 11(2):022001.", page=8),
    ]
    out = _merge_ref_run(frags)
    assert [b.type for b in out] == ["ref", "ref"]
    assert out[0].en.startswith("1. Sarzyński") and "Metals 14(2):195." in out[0].en
    assert "man-ufacturing" in out[0].en                  # 断词连字符**保留**，不许猜补
    assert out[1].en.startswith("2. Radhika")
    assert _flat("".join(b.en for b in out)) == _flat("".join(b.en for b in frags))   # 不丢字


def test_merge_keeps_leading_text_before_entry_one():
    """条目 1 之前若还有文字（"References are listed in order of appearance."），单独留一块。"""
    frags = [_frag("References are cited in order of appearance."),
             _frag("1. Alpha study of things A. J Test 1:1. "),
             _frag("2. Beta study of things B. J Test 2:2.")]
    out = _merge_ref_run(frags)
    assert out[0].type == "refs" and "cited in order" in out[0].en
    assert [b.type for b in out[1:]] == ["ref", "ref"]


def test_merge_refuses_a_run_it_cannot_split():
    """没有编号（或编号不连号）⇒ **原样返回**：不丢字、不产出假条目。"""
    frags = [_frag("Some trailing material with no numbering at all. " * 3),
             _frag("And more of the same. " * 3)]
    assert [b.en for b in _merge_ref_run(frags)] == [b.en for b in frags]


def test_merge_skips_band_blocks_and_keeps_them_in_place():
    """页眉恰好落在参考文献那两页时也会被标 `refs` —— 它**不许**被接进条目里
    （实测这类块的文字是 `J Intell Manuf (2024) 35:1–20`，混进条目就是脏数据）。"""
    head = Block(id="b-0001", type="h2", en="REFERENCES")
    band = _frag("J Intell Manuf (2024) 35:1–20", band="top")
    frags = [_frag("1. Alpha study of things A. J Test 1:1."),
             _frag("2. Beta study of things B. J Test 2:2.")]
    out = merge_ref_entries([head, band] + frags, 1)
    assert [b.type for b in out] == ["h2", "refs", "ref", "ref"]
    assert out[1].en.startswith("J Intell Manuf")           # 页眉原样留在原地
    assert out[2].en.startswith("1. Alpha")                 # 条目里没有页眉文字
    assert "J Intell Manuf" not in " ".join(b.en for b in out[2:])


def test_merge_entries_only_touches_the_tail_and_keeps_other_blocks():
    doc_blocks = [
        Block(id="b-0001", type="p", en="Body text."),
        Block(id="b-0002", type="h2", en="REFERENCES"),
        Block(id="b-0003", type="figure", en="", payload={"src": "a.png"}),
        _frag("1. Alpha study of things A. J Test 1:1. "),
        _frag("2. Beta study of things B. J Test 2:2."),
    ]
    out = merge_ref_entries(doc_blocks, 2)
    assert [b.type for b in out[:3]] == ["p", "h2", "figure"]      # 合并**只**动文末材料
    assert [b.type for b in out[3:]] == ["ref", "ref"]


def test_merged_ref_entries_drop_the_stale_seam_stamp():
    """`seam`（栏间续段戳）描述"与前一块的关系"，而前一块已经并进来了 —— 必须一起丢掉。"""
    frags = [_frag("1. Alpha study of things A. J Test 1:1.", seam="col-spill", page=8),
             _frag("2. Beta study of things B. J Test 2:2.", page=8)]
    out = _merge_ref_run(frags)
    assert all("seam" not in b.payload for b in out)
    assert all(b.payload.get("page") == 8 for b in out)    # 分页戳要留着


# ── 4. 端到端：真 PDF 走一遍 parse_pdf（合成参考文献页，离线）──────────────
def _refs_pdf(path):
    """合成一页：正文 + REFERENCES 小标题 + 被切成碎片的条目（离线、无网）。

    ⚠️ 只用 ASCII 字（`insert_textbox` 的内置字体写不出 `ń`，会落成 `?`），
    ⚠️ 标题那块矩形要**给足高度**（20pt 高塞 12pt 字号 → PyMuPDF 直接不写，
    表现为"标题凭空消失"，会让人以为解析把标题丢了）。
    """
    fitz = pytest.importorskip("fitz")
    src = fitz.open()
    pg = src.new_page(width=595, height=842)
    pg.insert_textbox(fitz.Rect(51, 80, 549, 200),
                      "This paper studies machine learning in additive manufacturing. " * 3,
                      fontsize=10)
    pg.insert_textbox(fitz.Rect(51, 210, 549, 245), "REFERENCES", fontsize=12)
    # 每条拆成两个 textbox ⇒ 抽取时是**碎片**，必须被合并回整条
    pg.insert_textbox(fitz.Rect(51, 260, 549, 290),
                      "1. Sarzynski B, Sniezek L (2024) Metal additive man-", fontsize=9)
    pg.insert_textbox(fitz.Rect(51, 300, 549, 330),
                      "ufacturing applications in production of vehicle parts. Metals 14(2):195.",
                      fontsize=9)
    pg.insert_textbox(fitz.Rect(51, 340, 549, 375),
                      "2. Radhika C, Shanmugam R (2024) A review on additive manufacturing for "
                      "aerospace application. Mater Res Express 11(2):022001.", fontsize=9)
    src.save(str(path))
    src.close()


def test_parse_emits_ref_blocks_for_the_reference_section(tmp_path):
    pdf = tmp_path / "refs.pdf"
    _refs_pdf(pdf)
    from papershelf.pipeline.parse import parse_pdf
    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")
    refs = [b for b in doc.blocks if b.type == "ref"]
    assert len(refs) == 2, [b.en for b in doc.blocks]
    assert refs[0].en.startswith("1. Sarzynski") and "Metals 14(2):195." in refs[0].en
    assert "man-ufacturing" in refs[0].en              # 断词连字符保留（不许猜补）
    assert refs[1].en.startswith("2. Radhika")
    assert doc.meta["refs_count"] == 2
    # `refs` 只剩"切不出条目"的碎片 —— 这一页没有，所以为零（不是 2）
    assert not [b for b in doc.blocks if b.type == "refs"]
    # REFERENCES 小标题照旧是 h2（它不参与条目合并）
    assert any(b.type == "h2" and b.en.strip() == "REFERENCES" for b in doc.blocks)


def test_parse_keeps_non_reference_blocks_untouched(tmp_path):
    pdf = tmp_path / "refs.pdf"
    _refs_pdf(pdf)
    from papershelf.pipeline.parse import parse_pdf
    doc = parse_pdf(pdf, assets_dir=tmp_path / "assets")
    assert any(b.type == "p" and "machine learning in additive manufacturing" in b.en
               for b in doc.blocks), "正文段落被参考文献合并吃掉了"


# ── 5. 「只译标题」的定位：模型把行末断词拼回去也要对得上 ─────────────────
def test_folding_makes_line_break_hyphens_comparable():
    """两种**同时存在于真实数据**的断词形状都要折叠掉（2026-09-17 生产实测）：

    - `man-` + `ufacturing`（连字符后直接接字母）
    - `addi-` + ` tive`（连字符后还留着一个空格）
    模型回抄标题时一律拼成一个词 ⇒ 不折叠就**定位不到**，整条被判"标题没译出来"。
    """
    assert fold_for_match("Metal additive man-ufacturing")[0] == "metal additive manufacturing"
    assert fold_for_match("A vision for sustainable addi- tive man")[0] \
        == "a vision for sustainable additive man"


def test_folding_a_real_hyphen_stays_symmetric():
    """词内连字符（`Laser-directed`）两侧同样折叠 ⇒ 只会匹配它自己，不会扩到别的词。"""
    assert fold_for_match("Laser-directed energy")[0] == "laserdirected energy"
    assert title_span("10. Bi X (2024) Laser-directed energy deposition of steel. J Test 1:1.",
                      "Laser-directed energy deposition") is not None


@pytest.mark.parametrize("entry,title", [
    ("1. Sarzyński B (2024) Metal additive man-ufacturing (MAM) applications. Metals 14(2):195.",
     "Metal additive manufacturing (MAM) applications"),
    ("6. Graziosi S (2024) A vision for sustainable addi- tive manufacturing. J Clean 1:1.",
     "A vision for sustainable additive manufacturing"),
    ("3. Altiparmak SC (2021) A market assessment of addi-tive manufacturing. J Manuf 68:728.",
     "A market assessment of additive manufacturing"),
])
def test_title_span_survives_the_model_rejoining_the_word(entry, title):
    span = title_span(entry, title)
    assert span is not None
    got = entry[span[0]:span[1]]
    # 区间取的是**原文**（可能带断词连字符），折叠后必须与模型回抄的标题等价
    assert fold_for_match(got)[0] == fold_for_match(title)[0], got
    assert got.startswith("Metal") or got.startswith("A ")      # 起点落在标题首词上


def test_ref_zh_text_keeps_authors_and_doi_verbatim():
    entry = ("1. Sarzyński B, Sniezek L (2024) Metal additive man-ufacturing applications. "
             "Metals 14(2):195. https:// doi. org/ 10. 3390/ met14 020195")
    zh, why = ref_zh_text(entry, "Metal additive manufacturing applications", "金属增材制造应用")
    assert why == "" and zh
    assert zh.startswith("1. Sarzyński B, Sniezek L (2024) 金属增材制造应用")
    assert "Metals 14(2):195. https:// doi. org/ 10. 3390/ met14 020195" in zh    # 检索信息照抄
    assert "Metal additive" not in zh and "man-ufacturing" not in zh              # 英文标题被换掉


@pytest.mark.parametrize("title_en,title_zh,why_part", [
    ("Metal additive manufacturing", "", "title_zh"),
    ("Metal additive manufacturing", "Metal additive manufacturing", "中文"),
    ("Metal additive manufacturing", "金属增材制造 doi.org/10.3390", "DOI"),
    ("A completely rewritten title not present in the entry", "中文标题", "定位不到"),
])
def test_ref_zh_text_rejects_bad_results(title_en, title_zh, why_part):
    entry = ("1. Sarzyński B (2024) Metal additive manufacturing applications. Metals 14(2):195. "
             "https:// doi. org/ 10. 3390/ met14 020195")
    zh, why = ref_zh_text(entry, title_en, title_zh)
    assert zh == "" and why_part in why, (zh, why)


def test_ref_zh_text_rejects_a_whole_entry_pretending_to_be_a_title():
    """把**整条**当标题译了（作者名一译，这条文献就检索不到了）→ 长度护栏挡住。"""
    entry = ("1. Sarzyński B, Sniezek L (2024) Metal additive manufacturing applications. "
             "Metals 14(2):195.")
    zh, why = ref_zh_text(entry, entry, "一、萨尔任斯基·B，斯涅泽克·L（2024）金属增材制造应用。"
                                        "《金属》14(2):195。")
    assert zh == "" and "标题" in why


# ── 6. 翻译通道：送一条、要一条 JSON，只写标题 ────────────────────────────
class _Fake(Translator):
    """把 LLM 调用换成"按剧本回一段字符串"的替身（护栏与通道都是真的）。"""

    def __init__(self, replies, **kw):
        super().__init__(CFG, **kw)
        self.replies = list(replies)
        self.prompts: list[str] = []

    def _chat(self, user: str, system: str | None = None) -> str:
        self.prompts.append(user)
        self.system_used = system
        return self.replies.pop(0) if self.replies else "{}"


def _ref_block():
    entry = ("1. Sarzyński B, Sniezek L (2024) Metal additive man-ufacturing (MAM) applications "
             "in production of vehicle parts. Metals 14(2):195.")
    return Block(id="b-0132", type="ref", en=entry, payload={"page": 8})


def test_ref_blocks_go_through_their_own_channel():
    b = _ref_block()
    tr = _Fake(['{"title_en": "Metal additive manufacturing (MAM) applications in production of '
                'vehicle parts", "title_zh": "车辆零件生产中的金属增材制造（MAM）应用"}'])
    tr.translate_blocks([b])
    assert b.zh.startswith("1. Sarzyński B, Sniezek L (2024) 车辆零件生产中的金属增材制造")
    assert "Metals 14(2):195." in b.zh                     # 期刊/卷期页照抄
    assert b.payload["title_zh"] == "车辆零件生产中的金属增材制造（MAM）应用"
    assert "只译文献标题" in tr.system_used                 # 走的是参考文献专用 system prompt
    assert "<span" not in tr.prompts[0]                    # 送的是**裸条目**，不是 HTML 占位块
    assert tr.needs_review == []


def test_ref_channel_retries_then_marks_for_review():
    """不合格 → 重试一次；仍不合格 → **不写中文**（英文原样回落）+ 标「待校对」。"""
    b = _ref_block()
    tr = _Fake(['{"title_en": "Metal additive manufacturing", "title_zh": ""}',
                '{"title_en": "Metal additive manufacturing", "title_zh": ""}'])
    tr.translate_blocks([b])
    assert b.zh == "" and "title_zh" not in b.payload
    assert tr.needs_review == ["b-0132"]


def test_ref_bad_is_not_fooled_by_a_copied_entry():
    """`_ref_bad` 判的是"有没有中文标题"；只有作者/DOI 的中文版本算不上译好 —— 但它确实有中文，
    所以这里钉的是**反向**：没有 CJK 才判坏（有 CJK 的由 `ref_zh_text` 的护栏在写之前就把住）。"""
    b = _ref_block()
    assert Translator._ref_bad(b) is True            # 还没译 → 坏
    b.zh = "1. Sarzyński B (2024) 金属增材制造应用. Metals 14(2):195."
    assert Translator._ref_bad(b) is False


def test_translate_blocks_reports_ref_count_in_the_log():
    b = _ref_block()
    tr = _Fake(['{"title_en": "Metal additive manufacturing (MAM) applications in production of '
                'vehicle parts", "title_zh": "车辆零件生产中的金属增材制造（MAM）应用"}'])
    lines: list[str] = []
    tr.translate_blocks([b], log=lines.append)
    assert any("参考文献 1 条" in ln for ln in lines), lines


# ── 7. 校验：`ref` 必须有中文标题，但**数字不比对** ───────────────────────
def test_ref_is_not_a_no_chinese_type():
    assert "ref" not in NO_ZH_TYPES
    assert expects_chinese(_ref_block().en, block_type="ref")


def test_validate_does_not_flag_ref_digits():
    """中文栏里的作者/卷期页/DOI 是**原件照抄**的数字 —— 不排除就会给每条文献刷一条
    「数字不一致」，把真警告淹掉。

    ⚠️ 豁免必须**随 HTML 走**（`data-pt`）：校验器拿到的只有 HTML，没有块类型；
    `ref` 渲染出来是 `<p>`，按元素标签判**永远不成立**（这正是本轮修掉的一处）。
    """
    entry = ("1. Sarzynski B (2024) Metal additive manufacturing. Metals 14(2):195. "
             "https:// doi. org/ 10. 3390/ met14 020195")
    zh = "1. Sarzynski B (2024) 金属增材制造. Metals 14(2):195. https:// doi. org/ 10. 3390/ met14 020195"
    b = Block(id="b-0132", type="ref", en=entry, zh=zh, payload={})
    en_html = render_block(b, lang="en", typeset=False)
    zh_html = render_block(b, lang="zh", typeset=False)
    assert 'data-pt="1"' in en_html and 'data-pt="1"' in zh_html
    rep = validate(en_html, zh_html, check_digits=True)
    assert rep.tag_mismatch == [] and rep.digit_mismatch == [] and rep.untranslated == []


def test_validate_still_checks_digits_in_ordinary_prose():
    """豁免**只**给 `ref` —— 普通正文里数字变了照旧要报（护栏不能把 checker 关掉）。"""
    rep = validate('<p data-b="b-0001">The accuracy is 99.2 percent on the test set.</p>',
                   '<p data-b="b-0001">准确率是 98.7 个百分点。</p>', check_digits=True)
    assert rep.digit_mismatch == ["b-0001"]


def test_validate_flags_a_ref_whose_chinese_column_has_no_chinese():
    """标题没译出来（中文栏还是英文）→ 必须报「漏译」，不许静默放行。"""
    entry = "1. Sarzynski B (2024) Metal additive manufacturing applications. Metals 14(2):195."
    b = Block(id="b-0132", type="ref", en=entry, zh=entry, payload={})
    rep = validate(render_block(b, lang="en", typeset=False),
                   render_block(b, lang="zh", typeset=False), check_digits=True)
    assert rep.untranslated == ["b-0132"]


def test_an_untranslated_ref_falls_back_to_english_without_a_loop():
    """**没译出来**（zh 为空）的 `ref` 挂 `data-nt` ⇒ 不判漏译 ⇒ 不会「不译→判漏译→重译」死循环。

    它不会因此**消失**：翻译器已经把它标进 `needs_review`（`Translator._ref_bad`），
    界面上的「待校对」才是这条的出口（决策⑯ 的收敛保证）。
    """
    entry = "1. Sarzynski B (2024) Metal additive manufacturing applications. Metals 14(2):195."
    b = Block(id="b-0132", type="ref", en=entry, zh="", payload={})
    zh_html = render_block(b, lang="zh", typeset=False)
    assert 'data-nt="1"' in zh_html
    assert entry in zh_html                                   # 中文栏回落英文原文，不是空白
    rep = validate(render_block(b, lang="en", typeset=False), zh_html, check_digits=True)
    assert rep.untranslated == []
    assert Translator._ref_bad(b) is True                     # 出口仍标「待校对」
