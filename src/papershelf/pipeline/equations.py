"""公式 LaTeX 化 —— 宿主决策 B（2026-09-10，决策㉒ 后实测暴露）。

fitz 抽出的数学是 **Unicode 文本且间距被拆散**（`L f V ( x ) := ∂V ( x )`、`˙ x = f ( x ) + g ( x ) u (1)`），
与宿主满意的参照产物（39 个 display + 343 个 inline，带 `\\tag{N}`）差距明显。
本模块用一个**只排版、不改写**的 LLM 通道把数学转成标准 LaTeX 并包裹定界符，
交前端渲染（与遗留待定 8 的渲染方案联动）。

三条工程约束：

1. **顺序**：LaTeX 化只作用于**英文块**，且必须在**翻译之前** ——
   译文由构造继承同一份 LaTeX（翻译 prompt 规则 3），中英公式天然一致。
2. **只增不改**：非数学字句逐字保留。`validate.latex_problems()` 逐块验证
   （原文实词一个不少、公式编号一致、块标记不变），不合格**重试一次**，
   仍不合格则**回落原文本**并标 `needs_review`（决策⑯），绝不把简体 HTML 写坏。
3. **降级不阻塞**：LaTeX 化是**增强**而非必需品，失败不得让管线失败（决策② 的无人值守要求）。
"""

from __future__ import annotations

import logging
import re

from .markup import render_block
from .model import Block
from .translator import Translator
from .validate import _WORD_RE, equation_numbers, extract_blocks, latex_problems

log = logging.getLogger("papershelf.pipeline.equations")

# 数学符号（Unicode 数学区、希腊字母、常见运算符）
_MATH_CHARS = (
    # 方括号字形（⎡⎢⎣⎤⎥⎦）也要算数学信号：矩阵被 PDF 逐行抽块时全靠它们识别
    "∂≤≥∈∀∃∇∑∫√∞→↦⊂⊆⊃∪∩¬∧∨⇒⇔≈≠≡∝⊥⊤⋅×÷±∓∥⟨⟩⌈⌉⌊⌋⎡⎢⎣⎤⎥⎦ℝℕℤℂ"
    # PDF 提取的变音符与排版符号：˜\tilde ˙\dot ¯\bar ˆ\hat −（U+2212 减号）· 中点 ′ 撇
    "˜˙¯ˆˇ−·′″†‡∘"
    "αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ"
)
_MATH_RE = re.compile(f"[{re.escape(_MATH_CHARS)}]")
_EQNUM_RE = re.compile(r"\(\s*\d{1,2}\s*\)\s*$")

LATEX_SYSTEM = r"""你是学术论文的 LaTeX 排版助手。任务：把 PDF 提取出的**数学内容**转写为标准 LaTeX。

铁律（违反即视为失败）：
1. **只排版、不改写**：不得改写、翻译、增删任何字句。非数学的文字必须逐字保留原样。
2. 行内数学用 \( ... \) 包裹；独立成行的展示公式用 \[ ... \] 包裹；
   原编号（如 (12)）用 \tag{12} 保留在 \[ ... \] 内。
3. 输入的每个元素带 data-b="b-XXXX"，**必须原样保留**；块数、顺序不变；
   只输出翻译后的 HTML 片段，不要 markdown 代码围栏，不要任何解释文字。
4. 若某个块**不含数学**，则把它原样返回。
5. 用标准命令：\frac \sqrt \partial \nabla \leq \geq \in \forall \exists \sum \int \alpha \mathbb{R}；
   导数点号用 \dot{x}；矩阵向量用 \mathbf / \boldsymbol；上标 ^ 下标 _ 带花括号。
6. 同一个符号的写法全文保持一致。
"""


def looks_math(text: str, *, block_type: str = "") -> bool:
    """该块是否含数学内容（决定是否送 LaTeX 化，省 token）。

    判据宽进：只要出现数学符号就送；此外覆盖两类「无特殊符号」的公式
    （`x(t) = Ax(t) + Bu(t)` 与带编号的展示公式）。**含行内公式的段落也在此列。**
    """
    if block_type == "eq":
        return True
    t = (text or "").strip()
    if not t:
        return False
    if _MATH_RE.search(t):
        return True
    if _EQNUM_RE.search(t) and len(_WORD_RE.findall(t)) < 8:
        return True          # 形如 "… = … (12)" 的编号展示公式
    if t.count("=") >= 1 and len(_WORD_RE.findall(t)) < 4 and len(t) < 160:
        return True          # 纯符号行（无希腊字母/特殊符号）
    return False


class Latexizer(Translator):
    """复用 Translator 的 LLM 通道（OpenAI 兼容 + token 计量），换一套 system prompt。"""

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.system = LATEX_SYSTEM
        self.failed: list[str] = []

    def _prompt(self, blocks: list[Block]) -> str:
        # ⚠️ typeset=False：prompt 里必须是 **LaTeX 源码**。渲染成 MathML 再让模型"LaTeX 化"
        #    既烧钱又必然错（模型会去猜 MathML 对应的 LaTeX）。
        body = "\n".join(render_block(b, lang="en", typeset=False) for b in blocks)
        return (
            "【需要 LaTeX 化的片段】\n" + body + "\n\n"
            "请输出上述片段（保持所有 data-b 属性不变）：数学内容转成 LaTeX 并用定界符包裹，"
            "其余文字逐字保留原样。"
        )

    def latexize(
        self,
        blocks: list[Block],
        *,
        only: set[str] | None = None,
        max_retry: int = 1,
        max_blocks: int = 6,
        max_chars: int = 4000,
        log=print,
    ) -> dict[str, int]:
        """就地改写 `block.en`（数学 → LaTeX）。返回统计。"""
        targets = [
            b for b in blocks
            if looks_math(b.en, block_type=b.type) and (only is None or b.id in only)
        ]
        if not targets:
            log("  （没有含数学的块）")
            return {"blocks": 0, "chunks": 0, "retried": 0, "failed": 0}

        done = 0
        retried = 0
        chunks = self.chunk(targets, max_blocks, max_chars)
        log(f"  · 公式 LaTeX 化开始：{len(targets)} 块含数学，共 {len(chunks)} 个切片")
        for ci, chunk in enumerate(chunks, start=1):
            log(f"  · 公式切片 {ci}/{len(chunks)}：{len(chunk)} 块（{sum(len(b.en) for b in chunk)} 字符）")
            pending = self._attempt(chunk, log=log)
            if pending:
                retried += 1
                log(f"    ↻ 保真校验未过，重试 {len(pending)} 块：{', '.join(pending)}")
                pending = self._attempt([b for b in chunk if b.id in set(pending)], log=log)
            for bid in pending:
                self.failed.append(bid)
            done += len(chunk) - len(pending)

        if self.failed:
            log(f"  ⚠️ {len(self.failed)} 块 LaTeX 化后保真校验仍不过 → 回落原文本并标「待校对」")
        return {"blocks": len(targets), "chunks": len(chunks), "retried": retried,
                "failed": len(self.failed), "done": done}

    def _attempt(self, chunk: list[Block], *, log=print) -> list[str]:
        """送一次模型并逐块校验，返回**未通过**的块 ID（原文本尚未被改写）。"""
        try:
            out = self._chat(self._prompt(chunk))
        except Exception as exc:
            log(f"    ! 调用失败：{exc}")
            return [b.id for b in chunk]

        _, got = extract_blocks(out)
        bad: list[str] = []
        # 编号在**切片级**校验（见 latex_problems 说明）：逐块比会把跨块的 	ag 判成丢失
        before_nums = equation_numbers(" ".join(b.en for b in chunk))
        after_nums = equation_numbers(" ".join(got.get(b.id, "") for b in chunk))
        numbers_ok = before_nums == after_nums
        if not numbers_ok:
            log(f"    ! 切片公式编号变化 {before_nums} → {after_nums}（警告，不回落）")
        for b in chunk:
            new = (got.get(b.id) or "").strip()
            if not new:
                bad.append(b.id)
                continue
            problems = latex_problems(b.en, new, check_numbers=False)
            if problems:
                log(f"    ! {b.id} 保真存疑：{problems[0]}")
                b.payload.setdefault("latex_problems", problems)
                bad.append(b.id)
                continue
            b.en = new                      # 通过 → 采用（这是唯一的改写点）
            b.payload["latexized"] = True
            b.payload.pop("latex_problems", None)
            b.payload.pop("needs_review", None)   # 重试成功后必须撤掉「待校对」
        # 未通过的块保持原文；若重试后仍不合格，调用方会把它计入 failed
        for bid in bad:
            b = next((x for x in chunk if x.id == bid), None)
            if b is not None:
                b.payload["needs_review"] = True
        return bad
