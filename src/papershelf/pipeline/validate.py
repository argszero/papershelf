"""标记保真校验 —— 决策④ 的核心保险。

因为决策② 取消了「逐篇人工验收」，正确性必须**由程序判定**。
本模块只做**可判定的结构检查**（零成本、不引入第二个模型）：

| 检查 | 抓什么 |
|---|---|
| 标记缺失 / 多余 | 译文漏块、凭空多块 |
| 标记重复 | 块被复制 |
| 顺序错乱 | 模型重排了内容 |
| 漏译 | 该块中文仍是英文（无 CJK 字符）——**仅对「本应有中文」的块** |
| 标签配对 | 结构被破坏 |
| 公式编号 | `\\tag{N}` 与英文版不一致 |

⚠️ 边界（决策⑯ 已明确）：**结构完整 ≠ 语义正确**。
误译/幻觉抓不到，由阅读器的「块级重译 / 编辑译文」兜底。

⚠️ 收敛性要求：本模块的输出会被用来**触发自动重译**，所以每条判定都必须
「可被重试修复」。凡重试也修不好的（数字/编号改写、本质保持英文的块），
一律降级为**警告**，否则管线会陷入永不收敛的重试、白白烧钱（已实测发生）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")
# `<style>`/`<script>` 的**内容**不是 HTML 标记（`HTMLParser` 也按 CDATA 处理），
# 但正则版数标签会照数 —— 见 `_strip_non_markup`。
_NON_MARKUP_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1\s*>", re.S | re.I)


def _strip_non_markup(html: str) -> str:
    """去掉 `<style>`/`<script>` 的内容，只留真正的标记面（给正则数标签用）。"""
    return _NON_MARKUP_RE.sub(" ", html or "")
_TAG_BALANCE_EXCLUDE = {"img", "br", "hr", "meta", "link", "input", "source", "area", "base"}
_TAG_NUM_RE = re.compile(r"\\tag\{([^}]*)\}")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")

# 按 prompt 规则**本就应保持英文**的块类型：参考文献碎片、纯公式、
# 以及页边装饰块（`deco` = 出版社/期刊标识的透明 PNG，见 `parse._add_graphic` ——
# 它压根没有文字，要求它"有中文"会立刻变成一场永不收敛的重译）
NO_ZH_TYPES = {"refs", "eq", "deco"}
# **部分中文**块（v13）：整块的字面文本只有一部分是译文 —— 参考文献条目（`ref`）
# **只译标题**，作者/期刊/卷期页/DOI 按决策保留原文。于是：
# - 「疑似漏译」判定照旧适用（中文那栏必须有 CJK，否则就是标题没译出来）；
# - **数字/编号比对不适用**（中文栏里那串数字是原件照抄的，不是译文改写的），
#   不排除就会给每一条参考文献刷一条「数字不一致」的警告，把真警告淹掉。
#
# ⚠️ 这一类必须**随 HTML 走**（`markup` 给它挂 `data-pt="1"`，与 `data-nt` 同一套路）：
# `validate` 拿到的是**渲染好的 HTML**，只有元素标签与属性，**没有块类型** ——
# 这里比对过一次 `en.tags`（元素标签），而 `ref` 渲染出来就是 `<p>`，
# 于是「数字不比对」这条**永远不会生效**（实测：给每条文献刷一条 digit_mismatch）。
PARTIAL_ZH_TYPES = {"ref"}
# LaTeX 化之后，「可译散文」的判据：剥掉数学与 LaTeX 命令后**还剩至少 1 个实词**
MIN_WORDS_FOR_ZH = 1
# 数学函数名不算散文（否则 "inf u ∈ R^m …" 会被要求译，模型给不出中文 → 死循环）
_MATH_WORDS = {
    "inf", "sup", "max", "min", "lim", "sin", "cos", "tan", "log", "ln", "exp",
    "det", "diag", "rank", "arg", "mod", "vec", "def", "iff", "wrt", "resp",
    "frac", "sqrt", "cdot", "quad", "text", "mathrm", "left", "right", "tag",
    "forall", "exists", "partial", "nabla", "begin", "end", "array", "bmatrix",
    "mathbf", "boldsymbol", "mathcal", "top", "bot", "leq", "geq", "neq", "approx",
    "mathbb", "parallel", "circ", "ast", "star", "dagger", "prime", "dots", "cdots",
}
_LATEX_CMD_RE = re.compile(r"\\[A-Za-z]+\*?")


def prose_core(text: str) -> list[str]:
    """剥掉 **LaTeX 命令 + 数学区间 + HTML 标签**后剩下的散文实词（≥3 字母）。

    LaTeX 化之后这是「该块还剩多少可译文字」的唯一判据：
    `where \\( L_f V(x) \\)` → `["where"]`（有散文，须译）；
    `\\[ \\dot{x} = f(x) + g(x)u \\tag{1} \\]` → `[]`（纯公式，免译）。

    数学区间内 `\\text{...}` 的内容**不计入** —— 它多半是 `\\text{aclf}` 这类数学标签，
    而不是句子（词组的情形由 `text_cmd_words` 另行判定）。
    """
    def _text_content(m: re.Match) -> str:
        return " " + " ".join(_TEXT_CMD_RE.findall(m.group(0))) + " "

    s = _join_hyphen_breaks(text).replace("forall", " for all ")
    s = _MATH_REGION_RE.sub(" ", s)          # 数学区间整段剔除（\text{} 由 text_cmd_words 单独看）
    s = _TAG_RE.sub(" ", _LATEX_CMD_RE.sub(" ", s))
    return [w.lower() for w in re.findall(r"[A-Za-z]{3,}", s) if w.lower() not in _MATH_WORDS]


def text_cmd_words(text: str) -> list[str]:
    """数学区间内 `\\text{...}` 等命令包裹的实词（≥3 字母）。"""
    out: list[str] = []
    for m in _MATH_REGION_RE.finditer(_join_hyphen_breaks(text)):
        for chunk in _TEXT_CMD_RE.findall(m.group(0)):
            out += [w.lower() for w in re.findall(r"[A-Za-z]{3,}", chunk)
                    if w.lower() not in _MATH_WORDS]
    return out


def expects_chinese(en_text: str, *, block_type: str = "") -> bool:
    """该块是否**应该**出现中文译文 —— 决定「漏译」判定是否适用。

    豁免两类（它们正是「永不收敛重试」的根源）：
    1. 参考文献**碎片块** / 纯公式块（prompt 明确要求保持英文原样）；
    2. 剥掉数学/LaTeX 后**不剩任何散文实词**的块 —— 它们是展示公式与符号，
       如 `\\[ \\dot{x} = f(x) + g(x)u \\tag{1} \\]`，本就没有可译的句子。
       注意 `where \\( ... \\)` 这类公式引导语**要译**（剩 "where" 一个实词）。
       （图注与标题例外：它们再短也是正文内容，必须译。）

    ⚠️ `ref`（完整的参考文献条目，v13）**在这里就是"应该有中文"** —— 它只译标题，
    标题也是必须译出来的内容（见 `PARTIAL_ZH_TYPES`）。

    这里的判定**同时**被 translator 用于决定「哪些块需要送模型翻译」，
    两处必须一致，否则又会出现「不译 → 判漏译 → 重译 → 仍不译」的死循环。
    """
    if block_type in NO_ZH_TYPES:
        return False
    if block_type == "figure":
        # 图注是正文内容，短图注（如 "Fig. 2. Overall structure."）也必须译
        return bool(prose_core(en_text))
    if block_type in ("h1", "h2", "h3", "h4"):
        # 标题同理 —— "I. INTRODUCTION" 只有 1 个实词，但它必须译成「I. 引言」
        # （实测：靠「实词≥4」的通用规则会把标题整批漏掉，且校验也发现不了）
        #
        # ⚠️ 必须用**去掉数学后的**散文实词（与正文同一套 `prose_core`），不能用裸 `_WORD_RE`：
        # 解析会把展示公式的续行误判成标题（实测一句被切成两块：
        # `adj = 0.91), as well as topography …` 与 `\(\mathrm{adj} = 0.77 \text{–} 0.96\)) [ 230 ].`，
        # 两块都成了 h2）。后者剥掉数学后**一个实词都不剩**，裸 `_WORD_RE` 却能从
        # `\mathrm` / `\text` 里数出词来 → 判「本应有中文」→ 模型给不出中文 →
        # 重译 2 轮仍不合格 → 整篇转换报废（生产实测：一篇 400 块论文因这类块整体 failed）。
        return bool(prose_core(en_text))
    if block_type == "table":
        # 表格（决策㊴）：`en_text` 是**网格的裸文本串**（行一行一行、格以 ` | ` 分隔，
        # 见 `model.table_text`）。判据与正文同一套 —— 只有**纯数字/符号**的表才免译
        # （那种表把中文塞进格子里只会更糟），有实词就必须有中文。
        # ⚠️ 这里必须与 translator 的表格通路**同判据**，否则又是「不译 → 判漏译 →
        # 重译 → 仍不译」的死循环（`translator` 复用本函数，单一来源）。
        return bool(prose_core(en_text))
    outside = prose_core(en_text)
    if len(outside) >= MIN_WORDS_FOR_ZH:
        return True
    # 公式内的 \\text{...}：只有**词组**（≥2 词）才算散文，
    # 单词是数学标签（`\\text{aclf}` = adaptive CLF），不该要求译
    return len(text_cmd_words(en_text)) >= 2


class _BlockTextExtractor(HTMLParser):
    """提取带 data-b 的块 → 块 ID 顺序 与 各块纯文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.order: list[str] = []
        self.texts: dict[str, str] = {}
        self.tags: dict[str, str] = {}     # 块 ID → 元素标签（判定「图注」需要）
        self.nt: set[str] = set()          # 带 data-nt 的块 = 不要求中文
        self.partial: set[str] = set()     # 带 data-pt 的块 = **部分中文**（见 PARTIAL_ZH_TYPES）
        self._stack: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in _TAG_BALANCE_EXCLUDE:
            return
        d = dict(attrs)
        bid = d.get("data-b")
        if bid and bid not in self.texts:
            self.texts[bid] = ""
            self.order.append(bid)
            self.tags[bid] = tag
            if d.get("data-nt"):
                self.nt.add(bid)
            if d.get("data-pt"):
                self.partial.add(bid)
            self._stack.append(bid)
        else:
            self._stack.append(None)

    def handle_endtag(self, tag):
        if tag in _TAG_BALANCE_EXCLUDE:
            return
        if self._stack:
            self._stack.pop()

    def handle_data(self, data):
        # 归属到**最内层**的带标记元素，保证嵌套元素内的文本也并入其所属块
        for bid in reversed(self._stack):
            if bid is not None:
                self.texts[bid] += data
                return


def extract_blocks(html: str) -> tuple[list[str], dict[str, str]]:
    p = _BlockTextExtractor()
    p.feed(html)
    return p.order, p.texts


def tag_balance(html: str) -> dict[str, int]:
    """返回开闭标签数量差（应为 0）。

    ⚠️ 数标签是**纯正则**（`_TAG_RE`），所以必须先把 `<style>`/`<script>` 的**内容**
    剥掉（`_strip_non_markup`）：样式表里的选择器与注释文本会被当成真标签。
    实测（v10）：往 CSS 注释里写了 `<h1..h4>` 三个字，整篇转换当场报
    「标签不配对 {'h1': 1, 'span': 1, 'h2': 1}」——**改一句 CSS 文案就能炸掉转换**，
    而报错指向的是"译文标签不配对"，与真因隔了十万八千里。
    """
    diff: dict[str, int] = {}
    for m in _TAG_RE.finditer(_strip_non_markup(html)):
        closing, tag = m.group(1), m.group(2).lower()
        if tag in _TAG_BALANCE_EXCLUDE:
            continue
        diff[tag] = diff.get(tag, 0) + (-1 if closing else 1)
    return {k: v for k, v in diff.items() if v != 0}


@dataclass
class Report:
    ok: bool = True
    missing: list[str] = field(default_factory=list)        # 英文有、中文无
    extra: list[str] = field(default_factory=list)          # 中文有、英文无
    duplicated: list[str] = field(default_factory=list)
    out_of_order: list[str] = field(default_factory=list)
    untranslated: list[str] = field(default_factory=list)   # 该块中文仍为英文
    tag_imbalance: dict[str, int] = field(default_factory=dict)
    # ⚠️ 以下两项为**警告**而非失败：译文合法地改写数字/编号的情形存在，
    #    若当作失败会导致重试永不收敛、白白烧钱（实测已发生）。
    #    按决策⑯，这类语义偏差交由「块级重译 / 编辑译文」按需处理。
    tag_mismatch: list[str] = field(default_factory=list)   # \tag{N} 编号不一致
    digit_mismatch: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.tag_mismatch or self.digit_mismatch)

    def blocks_to_retry(self) -> list[str]:
        """需要**自动重译**的块：只含可判定且必然可收敛的结构性问题。"""
        seen: list[str] = []
        for group in (self.missing, self.duplicated, self.out_of_order,
                      self.untranslated):
            for b in group:
                if b not in seen:
                    seen.append(b)
        return seen

    def summary(self) -> str:
        head = "✅ 标记保真校验通过" if self.ok else "❌ 标记保真校验未通过："
        lines = [head]
        for name, val in [
            ("缺失块", self.missing), ("多余块", self.extra),
            ("重复块", self.duplicated), ("顺序错乱", self.out_of_order),
            ("疑似漏译", self.untranslated),
        ]:
            if val:
                lines.append(f"  · {name}（{len(val)}）：{', '.join(val[:8])}"
                             + (" …" if len(val) > 8 else ""))
        if self.tag_imbalance:
            lines.append(f"  · 标签不配对：{self.tag_imbalance}")
        if self.has_warnings:
            lines.append("  ⚠️ 警告（不阻塞，可在阅读器中按需修订）：")
            for name, val in [("公式编号不一致", self.tag_mismatch),
                              ("数字不一致", self.digit_mismatch)]:
                if val:
                    lines.append(f"     - {name}（{len(val)}）：{', '.join(val[:8])}"
                                 + (" …" if len(val) > 8 else ""))
        return "\n".join(lines) if lines != [head] else head


# ── 公式 LaTeX 化的保真校验（决策 B）────────────────────────────────────────
# LaTeX 化是「只排版不改写」的改写，必须证明它没顺手改字。
# 判据都选**重试修得好**的：实词不能少、编号不能变、必须真的产生了 LaTeX。
_MATH_REGION_RE = re.compile(r"\\\((?:.|\n)*?\\\)|\\\[(?:.|\n)*?\\\]")
_TEXT_CMD_RE = re.compile(r"\\(?:text|mathrm|mbox|operatorname|textbf)\s*\{([^{}]*)\}")
_TAG_RE_NUM = re.compile(r"\\tag\s*\{\s*([^}]*?)\s*\}")
_PAREN_NUM_RE = re.compile(r"\(\s*(\d{1,2})\s*\)")
MATH_DELIM_RE = re.compile(r"\\\(|\\\[")
# PDF 提取的连字断行（defi- nition）：模型修好它属于**改进**，比对前两侧都归一化
_HYPHEN_BREAK_RE = re.compile(r"([A-Za-z])-[\s\u00ad]+([a-z])")


def _join_hyphen_breaks(text: str) -> str:
    prev = None
    out = text or ""
    while prev != out:
        prev = out
        out = _HYPHEN_BREAK_RE.sub(r"\1\2", out)
    return out


def prose_words(text: str) -> list[str]:
    """取出「非数学」的实词（≥4 字母）——用于证明 LaTeX 化没有动文字。

    先剥掉 LaTeX 区间，但保留其中的 `\\text{...}` 内容（那是模型包进数学里的文字，
    本就是原文的词，不算丢失）。用 ≥4 字母是为了避开 inf/sup/sin/lim 这类函数名
    （它们在原文里属于数学，不该要求出现在散文里）。
    """
    def _drop(m: re.Match) -> str:
        return " " + " ".join(_TEXT_CMD_RE.findall(m.group(0))) + " "

    # PDF 的斜体 forall 常被抽成与前一单词粘连的 "thatforall"：归一化后再分词，
    # 否则「模型把它写成 \\forall」会被判成内容词丢失（实测踩过）。
    s = _join_hyphen_breaks(text).replace("forall", " for all ")
    prose = _MATH_REGION_RE.sub(_drop, s)
    prose = _TAG_RE.sub(" ", prose)
    return [w.lower() for w in re.findall(r"[A-Za-z]{4,}", prose)]


def equation_numbers(text: str) -> list[str]:
    """公式编号序列：`(12)` 与 `\\tag{12}` 归一化后比较。"""
    t = text or ""
    tagged = _TAG_RE_NUM.findall(t)
    return tagged if tagged else _PAREN_NUM_RE.findall(t)


def latex_problems(before: str, after: str, *, check_numbers: bool = True) -> list[str]:
    """检查一次 LaTeX 化是否「只排版、未改写」。返回问题列表（空 = 通过）。

    `check_numbers=False` 用于**逐块**检查：展示公式常被 PDF 拆成多个块
    （`m u + w` 与 `(35)` 是两块），`\\tag` 可能落到相邻块 —— 编号改由**切片级**
    多集合比对（见 `equation_numbers`），因为相邻块渲染顺序不变、视觉位置仍正确。
    """
    problems: list[str] = []
    if not MATH_DELIM_RE.search(after or ""):
        problems.append("未产生 LaTeX 定界符")
    if check_numbers and equation_numbers(before) != equation_numbers(after):
        problems.append(f"公式编号变化 {equation_numbers(before)} → {equation_numbers(after)}")
    was, now = prose_words(before), prose_words(after)
    from collections import Counter
    cw, cn = Counter(was), Counter(now)
    lost = [w for w, c in cw.items() if cn[w] < c]
    # 只把「整词被改写」当问题：功能词被折进数学是**合法排版**
    # （`for all` → `\\forall`、`x in C` → `x \\in C`、`that` 之类的连接词不会真丢信息），
    # 否则会把好转换反复判死。判据：丢了一个**内容词**（≥7 字母），或丢失面超过四分之一。
    # PDF 常把缺空格的词抽成粘连体（`thecontrol`、`theloadtorq`）；模型拆开后
    # 它在词表里就"消失"了。用**拼接串**兜底：能在候选文本的无空格拼接里找到
    # 该词的前 6 个字母 → 判为「被拆开」而非「丢失」。
    # ⚠️ 必须用「整段文本的字母拼接」而不是 prose_words 的拼接 ——
    #    prose_words 只留 ≥4 字母的词，粘连词里的 "the" 早被丢掉了，永远匹配不上。
    joined_now = re.sub(r"[^a-z]", "", _join_hyphen_breaks(after).lower())
    def _genuinely_lost(w: str) -> bool:
        return len(w) >= 7 and w[:6] not in joined_now

    content_lost = [w for w in lost if _genuinely_lost(w)]
    if content_lost or (was and len(lost) > 0.35 * len(was)):
        problems.append(f"文字被改写/丢失：{', '.join(sorted(content_lost or lost)[:6])}")
    if len(after) < 0.7 * len(before):
        problems.append(f"长度异常收缩 {len(before)} → {len(after)}")
    return problems


def scan(html: str) -> _BlockTextExtractor:
    """解析出块顺序 / 块文本 / 免中文块集合。"""
    p = _BlockTextExtractor()
    p.feed(html)
    return p


def validate(en_html: str, zh_html: str, *, check_digits: bool = True) -> Report:
    """比对英文版与中文版的块标记与结构。"""
    r = Report()
    en, zh = scan(en_html), scan(zh_html)
    en_order, en_text, en_nt = en.order, en.texts, en.nt
    zh_order, zh_text = zh.order, zh.texts

    en_set, zh_set = set(en_order), set(zh_order)
    r.missing = [b for b in en_order if b not in zh_set]
    r.extra = [b for b in zh_order if b not in en_set]

    seen: set[str] = set()
    dups: list[str] = []
    for b in zh_order:
        if b in seen and b not in dups:
            dups.append(b)
        seen.add(b)
    r.duplicated = dups

    en_seq = [b for b in en_order if b in zh_set]
    zh_seq = [b for b in zh_order if b in en_set and b not in r.duplicated]
    if en_seq != zh_seq:
        # 标出第一个错位的块
        for a, b in zip(en_seq, zh_seq):
            if a != b:
                r.out_of_order.append(b)
                break

    for b in en_seq:
        zh_txt = zh_text.get(b, "")
        en_txt = en_text.get(b, "")
        # 免中文块（参考文献/公式/无字母块）不参与漏译与数字判定：
        # 它们本就该保持原样，判它们「漏译」会触发无意义的重译循环
        # 元素标签参与判定：<figure> 的文本是图注，短图注也要求中文
        if (b in en_nt or b in zh.nt
                or not expects_chinese(en_txt, block_type=en.tags.get(b, ""))):
            continue
        if not zh_txt.strip():
            r.untranslated.append(b)
            continue
        # 漏译：英文块有实际内容，而中文块毫无 CJK 字符
        if len(en_txt.strip()) > 40 and not _CJK_RE.search(zh_txt):
            r.untranslated.append(b)
        if check_digits and b not in en.partial:
            # `PARTIAL_ZH_TYPES`（参考文献条目）里那串数字是**原件照抄**的（作者/卷期页/DOI
            # 本来就保留原文），与"译文改写了数字"是两回事 —— 不排除就会给每一条文献
            # 刷一条警告，把真警告淹掉。判据取自 HTML 上的 `data-pt`（见 `PARTIAL_ZH_TYPES`）。
            if _TAG_NUM_RE.findall(en_txt) != _TAG_NUM_RE.findall(zh_txt):
                r.tag_mismatch.append(b)
            elif _DIGIT_RE.findall(en_txt) != _DIGIT_RE.findall(zh_txt):
                r.digit_mismatch.append(b)

    r.tag_imbalance = tag_balance(zh_html)
    r.ok = not (
        r.missing or r.extra or r.duplicated or r.out_of_order
        or r.untranslated or r.tag_imbalance
    )
    return r
