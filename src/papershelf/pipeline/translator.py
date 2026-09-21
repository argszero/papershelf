"""分块翻译 —— 决策④ 的第 ③步（标记穿透）与决策⑫（术语表对齐）。

两条硬约束：
1. **绝不整篇一次送**：长文必被输出长度截断（宿主的既有教训是靠分块 `cat >>` 躲过）。
   → 按**块边界**切片，块不跨切片。
2. **标记必须穿透**：prompt 明令 `data-b` 原样保留、不得增删合并块。

模型接入统一走 **OpenAI 兼容协议**（决策⑧），一套代码覆盖 OpenAI/DeepSeek/Qwen/…
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

from .markup import render_block
from .model import Block, grid_shape, table_text
from .parse import _looks_like_continuation
from .validate import expects_chinese, extract_blocks, validate

# ── 跨块接缝（㊻，2026-09-19，宿主选 C）─────────────────────────────────
#
# 宿主：「翻译时需要参考前后的block，尤其是跨页时。前面的block的半句翻译应该和后面block
# 的半句翻译很好的承接起来」（附截图：p3 页首孤零零一个「工艺。」）。
#
# 病灶有三处，**不是"没给上下文"**（上下文早就给了）：
#   ① 上下文取的是**裸相邻**块 —— 跨页处 `ordered[first-1]` 正是**页眉**
#      （`Journal of Intelligent Manufacturing (2024) 35:1407–1437 1409`），全库实测 24 次；
#   ② 窗口只有各 1 块；跨页那一对中间还隔着一个页眉块，等于什么都没给；
#   ③ **模型没有理由认为这两块是一句话**：那例两半本来就在同一个切片里，模型看得见两半，
#      仍然把前半句译完整、后半句译成孤零零的「工艺。」—— 提示词里没有这一条。
# 于是三层一起改：跳过页边带/装饰取**最近的正文邻块**、窗口放宽到 `CTX_NEIGHBORS` 块、
# 把接缝**显式写进 prompt**（`【跨块接缝】`）并在系统提示词里加一条硬要求。
CTX_NEIGHBORS = 2          # 上下文窗口：前后各取几个**正文**块（宿主选 C 时说的"2~3 块"）

# 页边带/装饰/公式：**不参与**接缝判据，也不当上下文（它们是"页面家具"不是"话"）。
# ⚠️ 判据必须与解析端一致：页眉/页脚在产物里不是独立块类型，而是**带 `payload["band"]` 的
#    `p` 块**（㊶ 定的）—— 只看 `type` 会把页眉当成正文，正是宿主那例的成因。
_FURNITURE_TYPES = frozenset({"deco", "eq"})
# 只有这几类块之间才谈得上"同一句被切开"：标题/表格/图注/文献条目都是**结构单元**，
# 它们跟前后文不构成半句话（把它们也算进来 = 拿"标题大写起"当反例淹掉真信号）。
_PROSE_TYPES = frozenset({"p", "abstract", "meta"})

# 半句对的**下限**：前半至少 6 词、后半至少 3 词（实测切出来的分界，见 `seam_pairs`）。
# 解析端的判据挂在几何上（同一页的左右栏接缝 + 页边带过滤），所以那里的假阳性长什么样
# 由几何决定；翻译端只有文本与顺序 —— 于是**标注/表头/作者行/页码**全会冒充"半句"
# （实测 4 篇：`MICRESS`、`Unsupervised`、`B T. Herzog`、`Journal of Manufacturing Processes`、
# `123` …）。它们共同的特征是"**不是话**"：词数极少。
# ⚠️ 阈值不能定在"字母数"上：真接缝的后半可能就是四个词（`transformative impact on WAAM.`）。
_MIN_SEAM_WORDS = (6, 3)
_MIN_WORDS_FIRST, _MIN_WORDS_SECOND = _MIN_SEAM_WORDS


def _note_lines(b: Block) -> int:
    """小字注区（v16）的**源行数**；不是注区则返回 1。

    为什么要一个函数（而不是在两处各写一遍 `count("\\n")+1`）：它是 prompt 规则 8
    （"一行对一行"）在翻译端的**执行判据**，判据写两份迟早漂开 —— 漂开的后果与
    `expects_chinese` 那条一样：一处要求、另一处不认，于是"判不合格 → 重译 → 仍不合格"。
    """
    p = b.payload if isinstance(b.payload, dict) else {}
    if not isinstance(p.get("note"), dict):
        return 1
    return (b.en or "").count("\n") + 1


# 管线模块自己持 logger（不继承调用方）：`server/logging_setup.py` 给 root 挂 handler，
# 所以 CLI 与容器里都直接可见；线程级 handler（`paper_log`）会把它们抄进单篇日志。
log = logging.getLogger("papershelf.pipeline.translator")

SYSTEM_PROMPT = """你是学术论文翻译专家，服务于中文科研人员精读英文文献。

翻译要求：
1. 忠实、准确、专业；术语使用学界通行译法，保持全文一致。
2. 长句按中文习惯重组，但**不得增删信息**、不得添加解释或评论。
3. **公式 LaTeX 原样保留**（\\(...\\) 与 \\[...\\] 内部一律不动），公式编号 \\tag{N} 不变。
4. 数字、单位、引用编号（如 [12]）、图表编号（如 Fig. 3 / Table 2）原样保留。
4b. **章节编号原样保留**：`I.` `II.` `A.` `B.` 等拉丁编号**不得转成中文数字**
   （❌「四、扰动下的安全关键控制」 → ✅「IV. 扰动下的安全关键控制」）。
4d. **图表编号译为「图 N」「表 N」**：`Fig. 3.` → `图3.`，`Table 2.` → `表2.`，编号数字不变；
   公式编号 `(12)`、引用编号 `[13]` 原样保留。
4c. **章节标题务必译出中文**：不得整条标题保留英文原文
   （❌「I. INTRODUCTION」 → ✅「I. 引言」）。
5. 参考文献条目**只译文献标题**：作者名、期刊/会议名、卷期页、年份、DOI/URL
   一律保留原文（读者要靠它们检索）。中文栏里一条文献看起来仍是原条目，只有标题成了中文。
6. 专有名词（模型名、方法名、数据集名）按术语表处理；术语表未覆盖且学界惯用英文的可保留英文。
7. **跨块接缝必须接上**（仅当提示里给了【跨块接缝】清单时）：清单里的相邻块本是**同一句话**
   被切开（分栏或跨页的排版所致，中间可能夹着页眉/页脚块）。清单里给了这句话的**完整原文**，
   请**先把这句完整译成一句通顺的中文**，再按原文的断点把它切成两半：
   - **前半块只放断点之前的译文**，结尾**不要加句号**（这话还没说完）；
   - **后半块从断点之后开始**，不要重复前半块已有的内容、不要另起一句；
   - 两半**拼起来**必须与那句完整原文一一对应（不增不减、不断错位）。
   两块**仍然是两块**：`data-b` 与块数一律不变，**不要合并成一块**。
   ⚠️ 提示里没给接缝清单时，本条不适用（按普通分块翻译即可）。
8. **原文里的换行要照抄**（仅当片段里确实有换行时）：小字注/表注这类片段在解析时
   **按原件的行保留换行** —— 译文必须**行数与行序都相同**（一行对一行），
   不要把几行并成一段、也不要把一行拆成几行；行首的上标小标（`a` `b` `c`…）原样保留。

⚠️ 结构要求（最高优先级，违反即视为失败）：
- 输入的 HTML 片段中每个元素带 `data-b="b-XXXX"` 属性，**必须原样保留**。
- 输出必须**只含翻译后的 HTML 片段**，与输入**块数相同、顺序相同**，不得增删、合并或拆分任何块。
- `data-b` 的值一个字都不能改；不要输出 markdown 代码块围栏，不要输出任何解释文字。
"""


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    timeout: float = 300.0
    max_tokens: int = 8192

    @classmethod
    def from_env(cls) -> "LLMConfig":
        base = os.environ.get("PAPERSHELF_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        key = (
            os.environ.get("PAPERSHELF_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or ""
        )
        model = os.environ.get("PAPERSHELF_LLM_MODEL") or "deepseek-chat"
        if not base:
            base = "https://api.deepseek.com/v1" if os.environ.get("DEEPSEEK_API_KEY") else ""
        if not base:
            raise RuntimeError("未配置 LLM：请设置 PAPERSHELF_LLM_BASE_URL / _API_KEY / _MODEL")
        return cls(base_url=base.rstrip("/"), api_key=key, model=model)


_FENCE_RE = re.compile(r"^\s*```(?:html)?\s*|\s*```\s*$")


# ── 表格的翻译通路（决策㊴，2026-09-16）─────────────────────────────────────
# 表格**不能**走正文那条"渲染成 HTML → 让模型翻译 HTML → 解回块文本"的通路：
# `extract_blocks` 是按 `data-b` 取块内**全部文本**的，`<td>` 之间的边界在解回来时
# 已经没了 —— 网格结构会当场塌成一堆字（这正是这轮要修的缺陷的另一种形态）。
# 所以表格单开一条通道：**送网格、要网格**，形状由程序判定（见 `_table_ok`）。
TABLE_SYSTEM = SYSTEM_PROMPT + """
关于**表格**（本轮的输入就是一张表，不是普通段落）：
- 输入是一张表的抽取结果：`caption` 是表注（可空），`rows` 是网格（第一行是表头）。
- 输出必须是**一个 JSON 对象**，且**只有 JSON**（不要 markdown 围栏、不要解释文字）：
  {"caption_zh": "表注的中文", "rows_zh": [["第一行第一格", "第一行第二格"], ["…"]]}
- `rows_zh` 的行数与列数**必须与输入完全一致**，一个格子都不能合并、拆分或删除。
- 纯数字、单位、符号、公式（如 `O(n²·d)`、`12.4`、`%`）的格子**原样保留**；
  单元格内不要添加句号，不要补全缩写。
- 表注（caption）要译；表注里的编号如 `Table 1.` 译成 `表1.`（编号数字不变）。
"""

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def _json_object(text: str) -> dict | None:
    """容错取第一个 JSON 对象（去围栏 → 直接解析 → 抠第一个平衡的 `{...}`）。"""
    cleaned = _JSON_FENCE_RE.sub("", (text or "").strip()).strip()
    start = cleaned.find("{")
    cands = [cleaned]
    if start >= 0:                       # 抠第一个平衡花括号块（模型常夹带解释文字）
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    cands.append(cleaned[start:i + 1])
                    break
    for c in cands:
        try:
            data = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _fit_grid(got: object, want: tuple[int, int]) -> list[list[str]] | None:
    """把模型回的 `rows_zh` 修成与英文网格**同形**；修不动就返回 `None`。

    只做**一种**修正（不加宽、不造格）：行数一致、每行不超列数时，把短行补空串到
    列数 —— 实测模型常把**空尾格**整行省掉（源表右侧本来就有空格），那是排版习惯
    而不是理解错误，为此整份作废太贵。除此之外一律 `None`（形状不符 → 由调用方
    重试或放弃）。
    """
    if not isinstance(got, list) or len(got) != want[0]:
        return None
    rows, width = [], want[1]
    for row in got:
        if not isinstance(row, list) or not row or len(row) > width:
            return None
        cells = [str(c) if c is not None else "" for c in row]
        rows.append(cells + [""] * (width - len(cells)))
    return rows


# ── 参考文献条目：**只译标题**（决策㊹，2026-09-17）──────────────────────────
# 宿主：「参考文献没有翻译」→ 选 **B**：**只译文献标题**，作者名/期刊名/DOI 保留原文。
#
# 与表格同型：**送一条、要一条**（JSON），合格与否由**程序**判，不由模型自称。
# 为什么不能让正文那条 HTML 通道顺便译：它按块送整段文字、要整段译文 ——
# 送一条文献过去，回来的就是"作者名也译了"的整条（作者名一译，这条文献就检索不到了）。
REFS_SYSTEM = SYSTEM_PROMPT + """
关于**参考文献条目**（本轮的输入就是**一条完整的文献条目**，不是普通段落）：
- **只译文献标题**；其余一律**原样照抄**：作者姓名、期刊/会议名、卷期页码、年份、
  出版社、DOI/URL —— 读者要靠它们去检索，译成中文就没用了。
- 输出必须是**一个 JSON 对象**，且**只有 JSON**（不要 markdown 围栏、不要解释文字）：
  {"title_en": "<条目里的英文标题，逐字照抄>", "title_zh": "<标题的中文译文>"}
- `title_en` 必须是输入里**确实存在**的一段（PDF 抽出的行末断词连字符可留可去，
  其余一个字都不能改）：程序要拿它回原文里定位标题的位置。
- `title_zh` 只写标题本身：不带条目编号、不带书名号、不带「译：」之类前缀、不带末尾句点，
  也不要把作者名/期刊名/DOI 抄进来。
- 条目里有 PDF 抽取噪声（DOI 里多出空格、`%nie&ek` 这类错字）：**照抄原文，不许凭猜补字**。
"""

_RE_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_RE_URLISH = re.compile(r"https?://|doi\.org|\bdoi:\s*10\.|arXiv:", re.I)
_RE_WS = re.compile(r"\s+")


def fold_for_match(text: str) -> tuple[str, list[int]]:
    """归一化用于**定位**：大小写折叠 + 空白压平 + 行末断词的连字符可省。

    返回 `(归一化串, 每个字符在原串里的下标)`。折叠掉的字符（连字符）不在下标表里，
    所以下标表与原串仍是**一一对应**的 —— 定位结果是原串上的真实区间。
    ⚠️ 只用来把模型回抄的标题对回原文坐标；**产物里的文字一个字都不改**。

    为什么要"连字符可省"（2026-09-17 真数据实测）：PDF 抽出来的参考文献里行末断词
    有两种**同时存在**的形状 —— `man-` + `ufacturing`（连字符后**直接**接字母）与
    `addi-` + ` tive`（连字符后还留着一个空格）。模型回抄标题时一律顺手拼成一个词
    （`manufacturing` / `additive`），**两侧都折叠才比得上**。
    ⚠️ 折叠是对**双方**做的，所以连字符全部折叠也不会把匹配放宽到"另一个词"上：
    `Laser-directed` 两侧同样折成 `laserdirected`，仍只与它自己匹配。
    """
    out: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text or "")
    while i < n:
        c = text[i]
        if c.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append(" ")
            idx.append(i)
            i = j
            continue
        if c == "-" and i and text[i - 1].isalnum():
            # 行末断词：`-` 后面可能紧跟字母，也可能先隔一个换行留下的空白
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j].isalnum():
                i = j                                 # 连字符与它后面的空白一起折叠掉
                continue
        out.append(c.lower())
        idx.append(i)
        i += 1
    return "".join(out), idx


def title_span(entry: str, title_en: str) -> tuple[int, int] | None:
    """`title_en` 在 `entry` 里的字符区间 `[start, end)`；定位不到返回 `None`。"""
    want, _ = fold_for_match(title_en)
    want = want.strip()
    if not want:
        return None
    got, idx = fold_for_match(entry)
    k = got.find(want)
    if k < 0:
        return None
    return idx[k], idx[k + len(want) - 1] + 1


def ref_zh_text(entry: str, title_en: str, title_zh: str) -> tuple[str, str]:
    """模型回的标题 → `(中文文本, 不合格原因)`（原因非空即不合格，中文文本为空串）。

    合格 = 原条目里**把标题那一段换成中文**的那一版（作者/期刊/DOI 逐字保留）。
    护栏四道，每道对应一种**可判定**的坏结果：

    | 判据 | 挡住的坏结果 |
    |---|---|
    | `title_zh` 有中文 | 模型空手回来 / 把标题照抄成英文 |
    | `title_zh` 不含 DOI/URL | 模型把**整条**文献都译了（作者名一译就没法检索） |
    | `title_en` 能在条目里定位 | 模型改写了标题（定位是硬要求：中文栏靠它替换） |
    | 标题不占满整条、中文不过长 | 同上（把整条当成"标题"） |

    ⚠️ 最后一条是**长度**判据而不是语义判据：它只挡"整条被当成标题"这一种可测的形状，
    标题译得好不好，仍然只能由人看（阅读器的块级修订兜底，决策⑯）。
    """
    zh = _RE_WS.sub(" ", (title_zh or "").strip())
    en = (title_en or "").strip()
    if not zh:
        return "", "没有 title_zh"
    if not _RE_CJK.search(zh):
        return "", "title_zh 里没有中文"
    if _RE_URLISH.search(zh):
        return "", "title_zh 里混进了 DOI/URL（像是把整条都译了）"
    if not en:
        return "", "没有回抄 title_en（定位不到标题）"
    span = title_span(entry, en)
    if span is None:
        return "", f"title_en 在条目里定位不到：{en[:60]!r}"
    start, end = span
    flat = _RE_WS.sub(" ", (entry or "").strip())
    if end - start >= 0.9 * len(flat):
        return "", "标题占了整条条目（像是把整条都当成标题译了）"
    if len(zh) > 2.5 * (end - start) + 60:
        return "", "中文标题过长（像是把整条条目都译了）"
    # 标题若把原文的句点一起抄回来了，替换后要把它补回去 —— 否则
    # `… (2024) 中文标题 Metals 14(2):195.` 少一个分隔符（期刊名会粘上来）。
    tail = entry[end - 1] if entry[end - 1] in ".。" else ""
    return _RE_WS.sub(" ", (entry[:start] + zh + tail + entry[end:]).strip()), ""


class Translator:
    def __init__(self, cfg: LLMConfig, glossary: list[dict] | None = None,
                 *, table_retry: int = 1, ref_retry: int = 1) -> None:
        self.cfg = cfg
        self.glossary = glossary or []
        self.system = SYSTEM_PROMPT          # 子类（Latexizer）可换一套 system prompt
        self._table_retry = max(0, int(table_retry))
        self._ref_retry = max(0, int(ref_retry))
        self.tokens_used = 0
        self.needs_review: list[str] = []   # 重试后仍不合格的块 → 交阅读器按需修订（决策⑯）

    # ── prompt ────────────────────────────────────────────────────────────
    def _glossary_text(self) -> str:
        if not self.glossary:
            return ""
        lines = [
            f"- {g['en']} → {g['zh']}" + (f"（{g['note']}）" if g.get("note") else "")
            for g in self.glossary
        ]
        return "术语表（必须遵守）：\n" + "\n".join(lines) + "\n\n"

    # ── 跨块接缝（㊻）─────────────────────────────────────────────────────
    @staticmethod
    def _is_furniture(b: Block) -> bool:
        """页面家具：页眉/页脚（`payload["band"]`，㊶）/ 装饰图 / 独立公式。

        它们**能出现在两块正文之间**（跨页时必然如此），但不是"话" ——
        接缝判据与上下文选取都要跳过它们。
        """
        return bool(b.payload.get("band")) or b.type in _FURNITURE_TYPES

    @classmethod
    def seam_pairs(cls, ordered: list[Block]) -> list[tuple[Block, Block]]:
        """`ordered` 里疑似「同一句被切开」的**相邻正文对**（跳过页面家具）。

        判据是**单一来源**：`parse._looks_like_continuation`（前块没结句 + 后块小写起），
        解析阶段的栏间接缝、①c 的复量提醒、这里的翻译提示全都读它 —— 三处各写一遍，
        迟早一个说"像"、另一个说"不像"。

        ⚠️ 翻译侧必须**再补一条**解析侧没有的判据：**两块都得真是"话"**。
        解析侧的判据挂在几何条件上（必须在同一页的左右栏接缝上、且不在页边带里），
        孤零零一个 `123`/`MICRESS` 落不到那条缝上；而翻译侧只看文本与顺序 —— 实测 4 篇里
        **标注、表头、作者行、页码**都在冒充"半句"：
        `MICRESS`、`Unsupervised`、`B T. Herzog`、`Journal of Manufacturing Processes`、`123`…
        （paper 2 实测 13 处里 3 处、paper 1 实测 10 处里 5 处是这种）。用**词数**挡：
        真半句是句子（≥6 词），这些标签都是 1–4 个词。
        """
        flow = [b for b in ordered if not cls._is_furniture(b)]
        out: list[tuple[Block, Block]] = []
        for a, b in zip(flow, flow[1:]):
            if a.type not in _PROSE_TYPES or b.type not in _PROSE_TYPES:
                continue
            if len((a.en or "").split()) < _MIN_WORDS_FIRST \
                    or len((b.en or "").split()) < _MIN_WORDS_SECOND:
                continue
            if _looks_like_continuation(a.en, b.en):
                out.append((a, b))
        return out

    @classmethod
    def _ctx_before(cls, ordered: list[Block], idx: int, n: int = CTX_NEIGHBORS) -> list[Block]:
        """切片首块**之前**最近的 `n` 个正文块（由远到近返回）。

        ⚠️ 必须跳过页面家具：原先取 `ordered[idx-1]` 是**裸相邻** —— 跨页处那个"上文"
        恰好是**页眉**（宿主那例的成因，全库实测 24 次）。给模型一段期刊页眉，
        它对"上一句说到哪"仍然一无所知。
        """
        out: list[Block] = []
        i = idx - 1
        while i >= 0 and len(out) < n:
            if not cls._is_furniture(ordered[i]):
                out.append(ordered[i])
            i -= 1
        return list(reversed(out))

    @classmethod
    def _ctx_after(cls, ordered: list[Block], idx: int, n: int = CTX_NEIGHBORS) -> list[Block]:
        """切片末块**之后**最近的 `n` 个正文块（由近到远）。"""
        out: list[Block] = []
        i = idx + 1
        while i < len(ordered) and len(out) < n:
            if not cls._is_furniture(ordered[i]):
                out.append(ordered[i])
            i += 1
        return out

    @staticmethod
    def _seam_note(blocks: list[Block], seams: list[tuple[Block, Block]]) -> str:
        """把与本次翻译范围有关的接缝写成一段**显式**提示（没有则空串）。

        ⚠️ **必须给出这句的完整原文**（两块拼起来那一句）：两半本来就在同一个切片里，
        模型看得见两半，照样把前半句译完整、后半句译成孤零零一个「工艺。」
        —— 实测的差别不在"看得见看不见"，而在**有没有人告诉它这两块是一句话**。
        给完整原文 + "先整句译出、再按断点切开"这条动作指令，实测 6/6 干净承接
        （只写"不得补成完整句"的版本 3 次里仍有 1 次退回译断）。

        只有一块在范围内时说法不同：后半要"接着写"、前半要"不许补成完整句"
        （另一半可能是人工修订过、或免中文的块，不要去动它）。
        """
        ids = {b.id for b in blocks}
        rows: list[str] = []
        for a, b in seams:
            a_in, b_in = a.id in ids, b.id in ids
            if not (a_in or b_in):
                continue
            whole = f"{a.en.strip()} {b.en.strip()}"
            where = f"断点在「…{a.en.strip()[-40:]}」与「{b.en.strip()[:40]}…」之间。"
            if a_in and b_in:
                rows.append(f"- {a.id} → {b.id}：这两块本是同一句话，完整原文是：\n"
                            f"  {whole}\n  {where}")
            elif b_in:
                rows.append(f"- {a.id} → {b.id}：**{b.id} 是上文那句话的后半**，"
                            f"这句话的完整原文是：\n  {whole}\n  {where}"
                            f"——{b.id} 的中文要从断点之后开始，不要另起一句、"
                            f"不要重复上文已译的内容。")
            else:
                rows.append(f"- {a.id} → {b.id}：**{a.id} 的话没有说完**"
                            f"（下半句不在本次翻译范围内）。完整原文是：\n  {whole}\n  {where}"
                            f"——{a.id} 只放断点之前的译文，不得补成完整句。")
        if not rows:
            return ""
        return (
            "【跨块接缝（同一句话被排版切成了两块）】\n" + "\n".join(rows) + "\n"
            "⚠️ 两块**仍然是两块**（`data-b` 与块数不变、不要合并）；"
            "先把整句译好，再按上面给的断点切开。\n\n"
        )

    def _user_prompt(self, blocks: list[Block], ctx_before: list[Block] | None = None,
                     ctx_after: list[Block] | None = None,
                     seams: list[tuple[Block, Block]] | None = None) -> str:
        # ⚠️ typeset=False：prompt 与输出都必须是 **LaTeX 源码**，译文才能继承同一份公式
        #    （渲染成 MathML 后模型输出的"标记保真"就无从校验，见 validate.latex_problems）。
        parts = [self._glossary_text()]
        if ctx_before:
            parts.append(
                "【上文（仅供理解，**不要翻译**）】\n"
                + "\n".join(render_block(c, lang="en", marker=False, typeset=False)
                          for c in ctx_before)
                + "\n\n"
            )
        parts.append(self._seam_note(blocks, seams or []))
        parts.append("【需要翻译的片段】\n"
                     + "\n".join(render_block(b, lang="en", typeset=False) for b in blocks) + "\n")
        if ctx_after:
            parts.append(
                "\n【下文（仅供理解，**不要翻译**）】\n"
                + "\n".join(render_block(c, lang="en", marker=False, typeset=False)
                          for c in ctx_after)
                + "\n"
            )
        parts.append("\n请输出上述【需要翻译的片段】的中文 HTML 片段，保持所有 data-b 属性不变。")
        return "".join(parts)

    # ── 调用 ──────────────────────────────────────────────────────────────
    def _chat(self, user: str, system: str | None = None) -> str:
        payload = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system or self.system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        with httpx.Client(timeout=self.cfg.timeout) as client:
            try:
                resp = client.post(f"{self.cfg.base_url}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
            except Exception as exc:
                # 每次失败的调用都要留痕：`translate_blocks` 对调用异常是 `continue`，
                # 若这里不打，一次网络/配额故障在日志里会表现为"切片忽然变少"（生产踩过 402 吞掉）。
                log.warning("LLM 调用失败（model=%s，用时 %.1fs）：%s",
                            self.cfg.model, time.monotonic() - t0, exc)
                raise
            data = resp.json()
        usage = data.get("usage") or {}
        self.tokens_used += int(usage.get("total_tokens") or 0)
        log.debug("LLM 调用成功：model=%s，tokens=%s，用时 %.1fs",
                  self.cfg.model, usage.get("total_tokens"), time.monotonic() - t0)
        return _clean(data["choices"][0]["message"]["content"])

    # ── 分块 ──────────────────────────────────────────────────────────────
    @staticmethod
    def chunk(blocks: list[Block], max_blocks: int = 12, max_chars: int = 6000,
              keep_together: set[tuple[str, str]] | None = None) -> list[list[Block]]:
        """按块边界切片：块不跨切片（决策④ 的必要条件）。

        `keep_together` = **不得被切在中间**的块对（㊻ 的跨块接缝）：
        实测 4 篇里各有 **1–6 对**接缝恰好落在切片边界上 —— 那样两半分处两个请求，
        谁都看不见另一半，"承接"根本无从谈起。切片尺寸本来就是软上限
        （`max_blocks`/`max_chars`），为接缝多带一块完全划算。

        ⚠️ 判据要**跨过页面家具**看：跨页的接缝中间夹着页眉块（`b-0026` → 页眉 → `b-0028`），
        只看"当前切片最后一块"会漏 —— 切点落在**页眉与后半句之间**同样是切开接缝。
        所以要记住"哪一对还没凑齐"（`open_pair`）：第一半进了本切片，就一直不许在
        它的后半到来之前切片（家具块夹在中间也不许）。
        """
        # ⚠️ 用**有序二元组**（`("b-0026", "b-0028")`）而不是 `frozenset`：
        #    "谁在前"正是这里要的信息，集合会把方向丢掉。
        pairs = keep_together or set()
        firsts = {a for a, _ in pairs}
        chunks: list[list[Block]] = []
        cur: list[Block] = []
        size = 0
        open_pair: str | None = None      # 本切片里"还没等到后半"的那个块 id

        for b in blocks:
            n = len(b.en) + (len(b.payload.get("caption") or "") if b.type == "figure" else 0)
            cut = cur and (len(cur) >= max_blocks or size + n > max_chars)
            if cut and open_pair is not None:
                cut = False               # 这一对是同一句话的两半：宁可让本切片超一点
            if cut:
                chunks.append(cur)
                cur, size, open_pair = [], 0, None
            cur.append(b)
            size += n
            if not Translator._is_furniture(b):
                if open_pair is not None and (open_pair, b.id) in pairs:
                    open_pair = None                       # 后半到齐
                # 后半**同时**可能是下一对的**前半**（`b-0012→b-0013→b-0014` 这种连号接缝
                # 实测就有）：只在"凑齐了"分支里清空、不再查 `firsts`，会把 `b-0013→b-0014`
                # 整个漏掉 —— 切片又切在这句话中间，而这正是本函数唯一的活。
                if b.id in firsts:
                    open_pair = b.id
        if cur:
            chunks.append(cur)
        return chunks

    def translate_blocks(
        self,
        blocks: list[Block],
        *,
        only: set[str] | None = None,
        max_retry: int = 2,
        max_blocks: int = 12,
        max_chars: int = 6000,
        log=print,
    ) -> list[str]:
        """翻译并回填 `zh`；对校验不合格的块**只重译该块**（决策④）。

        三道「不烧冤枉钱」的过滤：
        - **免中文块**（参考文献**碎片**/公式/装饰图，决策③ 的 prompt 规则 5）根本不送模型 ——
          这篇论文里参考文献占正文 26%，送过去只会被判「漏译」再重译，纯烧钱；
        - `zh_source == "human"` 的块**不覆盖**（决策⑯：重跑不得冲掉人工修订）；
        - 表格与**完整的参考文献条目**（`ref`）各走自己的通道（见模块里 `TABLE_SYSTEM`
          与 `REFS_SYSTEM` 的说明）：表格送网格、要网格；文献条目**只译标题**。
        """
        all_texts: dict[str, str] = {}
        ordered = self.ordered(blocks)
        todo = [b for b in ordered
                if b.zh_source != "human" and expects_chinese(b.en, block_type=b.type)
                and (only is None or b.id in only)]
        # 注意：上下文仍取自**完整** ordered，只有待翻集合被收窄（断点续跑不影响上下文质量）

        # ㊻ 跨块接缝：先在**完整**序列上算一次（收窄只影响送谁去翻，不影响"谁跟谁是一句话"），
        # 再按 id 取出"两半都在待翻集合里"的那些交给 `chunk()` —— 切片不得切在接缝上。
        seams = self.seam_pairs(ordered)
        seam_ids = {b.id for b in todo}
        # 有序二元组（谁在前是要紧信息，见 `chunk`）；只保留"两半都在待翻集合里"的那些。
        keep = {(a.id, b.id) for a, b in seams if a.id in seam_ids and b.id in seam_ids}
        if seams:
            head = "、".join(f"{a.id}→{b.id}" for a, b in seams[:4])
            log(f"  · 跨块接缝：{len(seams)} 处疑似「同一句被切开」"
                f"（其中 {len(keep)} 处两半都在本次范围内）：{head}"
                + (" …" if len(seams) > 4 else ""))

        # 表格 / 参考文献条目与正文走**三条通道**（见模块里 `TABLE_SYSTEM`、`REFS_SYSTEM`
        # 的说明）：混在一起送会让模型把 `<td>` 的边界当成排版噪声、
        # 把文献条目的作者名也一并译掉。
        tables = [b for b in todo if b.type == "table"]
        refs = [b for b in todo if b.type == "ref"]
        todo = [b for b in todo if b.type not in ("table", "ref")]
        for b in tables:
            zh = self._translate_table(b, log=log)
            if zh:
                all_texts[b.id] = zh
        for b in refs:
            zh = self._translate_ref(b, log=log)
            if zh:
                all_texts[b.id] = zh

        index = {b.id: i for i, b in enumerate(ordered)}
        chunks = self.chunk(todo, max_blocks, max_chars, keep_together=keep)
        # 「进度」必须能回答"还剩多少"：只打「切片 3」看不出是 3/45 还是 3/4
        # （生产汇报「一直显示转换中」时，日志里连总数都没有，无从判断是否在进行）。
        log(
            f"  · 翻译开始：{len(todo) + len(tables) + len(refs)}/{len(ordered)} 块需翻译"
            f"（含表格 {len(tables)} 张、参考文献 {len(refs)} 条），共 {len(chunks)} 个切片"
            f"（{sum(len(b.en) for b in todo)} 字符）"
        )
        for ci, chunk in enumerate(chunks, start=1):
            first, last = index[chunk[0].id], index[chunk[-1].id]
            # ㊻ 上下文 = 前后各 `CTX_NEIGHBORS` 个**正文**块（跳过页边带/装饰/公式）；
            #    接缝 = 与本次范围有关的那几条（清单里会写清"哪两块是一句话"）。
            before = self._ctx_before(ordered, first)
            after = self._ctx_after(ordered, last)
            log(f"  · 切片 {ci}/{len(chunks)}：{len(chunk)} 块（{sum(len(b.en) for b in chunk)} 字符）")
            try:
                out = self._chat(self._user_prompt(chunk, before, after, seams))
            except Exception as exc:  # 网络/接口异常 → 记录并跳过，交由后续重译
                log(f"    ! 切片 {ci} 调用失败，已跳过（{len(chunk)} 块将在校验后定点重译）：{exc}")
                continue
            got_order, got_text = extract_blocks(out)
            for b in chunk:
                if b.id in got_text:
                    all_texts[b.id] = got_text[b.id]

        # ── 逐块校验 + 定点重译 ──
        for attempt in range(max_retry + 1):
            bad = self._check(todo, all_texts)
            if not bad:
                break
            log(f"  ! 第 {attempt + 1} 轮校验：{len(bad)} 个块需重译 → {', '.join(bad[:6])}"
                + (" …" if len(bad) > 6 else ""))
            for bid in bad:
                b = next((x for x in todo if x.id == bid), None)
                if b is None:
                    continue
                i = index[bid]
                try:
                    # ㊻ 定点重译同样带上接缝与正文上下文 —— 首轮译断的那一半，
                    #    重译时若又拿不到"上下两块是一句话"这个事实，只会把同样的错再犯一次
                    #    （而且它是**唯一**能改好那半句的机会：校验只看"有没有中文"）。
                    out = self._chat(
                        self._user_prompt([b], self._ctx_before(ordered, i),
                                          self._ctx_after(ordered, i), seams)
                    )
                    _, txt = extract_blocks(out)
                    if b.id in txt:
                        all_texts[b.id] = txt[b.id]
                except Exception as exc:
                    log(f"    ! 重译 {bid} 失败：{exc}")

        # ── 收敛保证：重试额度用尽后不再纠缠，留痕交给阅读器的块级修订（决策⑯）──
        # 表格的"合格"判据是**形状 + 有没有中文**（`_table_bad`）、参考文献条目是
        # **有没有中文标题**（`_ref_bad`），都与正文的文本判据不同，
        # 所以并进来一起收尾 —— 三处各留一份 needs_review 会导致界面上"待校对"数目对不上。
        self.needs_review = (self._check(todo, all_texts, lines_must_match=False)
                             + [b.id for b in tables if self._table_bad(b)]
                             + [b.id for b in refs if self._ref_bad(b)])
        for bid in self.needs_review:
            b = next((x for x in ordered if x.id == bid), None)
            if b is not None:
                b.payload["needs_review"] = True
        if self.needs_review:
            log(f"  ⚠️ {len(self.needs_review)} 块重试后仍不合格 → 标记「待校对」，不阻塞管线"
                f"（{', '.join(self.needs_review[:6])}{' …' if len(self.needs_review) > 6 else ''}）")

        out: list[str] = []
        for b in ordered:
            if b.zh_source == "human":
                out.append(b.zh)                      # 人工修订优先，永不被覆盖
            elif expects_chinese(b.en, block_type=b.type):
                out.append(_clean(all_texts.get(b.id, "")))
            else:
                out.append(b.en)                      # 免中文块：回落英文原文
        return out

    # ── 表格（决策㊴）：送网格、要网格 ─────────────────────────────────────
    def _table_prompt(self, b: Block) -> str:
        payload = {
            "caption": str(b.payload.get("caption") or ""),
            "rows": b.payload.get("rows") or [],
        }
        return (self._glossary_text()
                + "【需要翻译的表格】\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\n\n请按格式要求输出**一个 JSON 对象**（caption_zh + rows_zh），"
                  "行数列数必须与输入一致。")

    def _translate_table(self, b: Block, log=print) -> str:
        """翻译一张表 → 回填 `payload["rows_zh"]` / `["caption_zh"]`，返回中文裸文本。

        ## 护栏（每条都对应一种**可判定**的坏结果）
        - 形状必须**完全一致**：`rows_zh` 的行数/列数与 `rows` 不同时整份作废
          （错行的表比不译更难发现 —— 读者会当成原文就长这样）；
        - 空白格子按原文补空串（模型常把空尾格整个省掉，那算形状不符，不该作废）；
        - 追加重试 `max_retry` 次，仍不合格就**不写**（`en` 原样回落），
          由 `needs_review` 交给阅读器的块级重译 —— 与正文同一套收敛保证。
        """
        rows = b.payload.get("rows") or []
        want = grid_shape(rows)
        if not want:
            return ""
        for attempt in range(self._table_retry + 1):
            try:
                data = _json_object(self._chat(self._table_prompt(b), system=TABLE_SYSTEM))
            except Exception as exc:                       # noqa: BLE001 — 网络/接口异常不该炸整篇
                log(f"    ! 表格 {b.id} 翻译调用失败：{exc}")
                data = None
            got = (data or {}).get("rows_zh")
            got = _fit_grid(got, want)
            if got is not None:
                b.payload["rows_zh"] = got
                cap_zh = str((data or {}).get("caption_zh") or "").strip()
                if cap_zh:
                    b.payload["caption_zh"] = cap_zh
                zh = table_text(got, cap_zh or str(b.payload.get("caption") or ""))
                b.zh = zh
                log(f"    · 表格 {b.id} 已译（{want[0]}×{want[1]} 格）")
                return zh
            if attempt < self._table_retry:
                log(f"    ! 表格 {b.id} 形状不符（要 {want[0]}×{want[1]}），重试 {attempt + 1}")
        log(f"    ⚠️ 表格 {b.id} 重试后仍不合格 → 保持英文，标「待校对」")
        return ""

    @staticmethod
    def _table_bad(b: Block) -> bool:
        """表格译文是否合格（与正文 `_check` 同一套取向：能重试修好的才判不合格）。"""
        want = grid_shape(b.payload.get("rows") or [])
        got = grid_shape(b.payload.get("rows_zh") or [])
        if not want or got != want:
            return True
        if not expects_chinese(b.en, block_type="table"):
            return False                    # 纯数字/符号表：本就不要求中文
        return not re.search(r"[\u4e00-\u9fff]", b.zh or "")

    # ── 参考文献条目（决策㊹）：送一条、要一条，**只译标题** ────────────────
    def _ref_prompt(self, b: Block) -> str:
        return (self._glossary_text()
                + "【需要翻译的参考文献条目】\n" + b.en
                + "\n\n请按格式要求输出**一个 JSON 对象**（title_en + title_zh）。")

    def _translate_ref(self, b: Block, log=print) -> str:
        """一条参考文献 → 只译标题：写回 `payload["title_zh"]`/`["title_en"]`，返回中文裸文本。

        中文裸文本 = **原条目里把标题那一段换成中文**（作者/期刊/DOI 逐字保留）——
        这正是中文栏要显示的东西，也是"只译标题"的可视化：两栏的文字只差标题。
        不合格就**不写**（`en` 原样回落）并标 `needs_review`，与正文/表格同一套收敛保证。
        """
        why = "未调用"
        for attempt in range(self._ref_retry + 1):
            try:
                data = _json_object(self._chat(self._ref_prompt(b), system=REFS_SYSTEM))
            except Exception as exc:                       # noqa: BLE001 — 网络/接口异常不该炸整篇
                log(f"    ! 参考文献 {b.id} 翻译调用失败：{exc}")
                data = None
            title_en = str((data or {}).get("title_en") or "").strip()
            title_zh = str((data or {}).get("title_zh") or "").strip()
            zh, why = ref_zh_text(b.en, title_en, title_zh)
            if zh:
                b.payload["title_en"], b.payload["title_zh"] = title_en, title_zh
                b.zh = zh
                log(f"    · 参考文献 {b.id} 标题已译：{title_zh[:36]}")
                return zh
            if attempt < self._ref_retry:
                log(f"    ! 参考文献 {b.id} 不合格（{why}），重试 {attempt + 1}")
        log(f"    ⚠️ 参考文献 {b.id} 重试后仍不合格（{why}）→ 保持英文，标「待校对」")
        return ""

    @staticmethod
    def _ref_bad(b: Block) -> bool:
        """文献条目是否没译出标题（与 `_table_bad` 同一取向：收敛后仍不合格才判）。"""
        return not _RE_CJK.search(b.zh or "")

    # ── 分块 / 判定 ───────────────────────────────────────────────────────

    @staticmethod
    def ordered(blocks: list[Block]) -> list[Block]:
        """参与翻译的块序列（与 `translate_blocks` 的返回顺序**一致**）。

        调用方必须用它来对齐返回值，否则会错位 —— 这是 CLI 里踩过的坑。
        """
        return [b for b in blocks if b.type != "figure" or b.en or b.payload.get("caption")]

    @staticmethod
    def _check(ordered: list[Block], texts: dict[str, str], *,
               lines_must_match: bool = True) -> list[str]:
        """用与全量校验同一套规则判定单块是否合格。

        `lines_must_match=False` 只用于**收尾那一次**判定（决定谁挂「待校对」）：
        小字注区的"行数是否与原文一致"是**结构**要求（prompt 规则 8），
        重试一次仍不一致时它只会退化成一段普通译文（对照模式两栏形状不对称而已），
        并不是"译错了" —— 把它算进「待校对」只会让读者看到一堆无意义的黄标。
        """
        bad: list[str] = []
        for b in ordered:
            if not expects_chinese(b.en, block_type=b.type):
                continue                              # 免中文块不参与判定
            t = (texts.get(b.id) or "").strip()
            if not t:
                bad.append(b.id)
                continue
            if len(b.en.strip()) > 40 and not re.search(r"[\u4e00-\u9fff]", t):
                bad.append(b.id)          # 漏译（仍是英文）
                continue
            if lines_must_match and _note_lines(b) > 1 and t.count("\n") + 1 != _note_lines(b):
                # 小字注区（v16）：译文行数与原文不一致 → 触发**一次**定点重译
                # （prompt 规则 8 明写了"一行对一行"）。重试仍不一致就接受现状 —— 见 docstring。
                bad.append(b.id)
                continue
            # 说明：数字 / 公式编号的偏差不计入重译触发条件——译文合法改写数字的情形存在，
            # 当作失败会导致重试永不收敛（实测烧掉近 2 万 token 仍未通过）。
            # 此类语义偏差交由阅读器的块级修订按需处理（决策⑯）。
        return bad


__all__ = ["LLMConfig", "Translator", "validate"]
