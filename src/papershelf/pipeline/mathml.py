"""公式渲染 —— 遗留待定项 8 的落地：**服务端 LaTeX → MathML**。

为什么不是 MathJax/KaTeX 的 CDN：决策③ 的自托管场景常在内网/离线环境，
CDN 一挂公式就成源码。为什么不是本地 KaTeX：要额外带 ~1MB 字体与 JS，
且「阅读器」与「导出 HTML」会各用一套渲染器，长期必然漂移。

选型（用真实 366 条公式实测，见 `docs/design.md` §8）：
- `latex2mathml`：纯 Python、无外部依赖、0.09ms/条、实测 366/366 成功；
- 输出 **MathML Core**，Chrome 109+ / Safari / Firefox 原生支持，**零 JS**，
  导出单文件 HTML 可直接打印、可被 Word/LaTeX 工具再加工。

边界：渲染是**派生态**。`Block.payload["latex"]` 里的 LaTeX 永远是事实来源，
所以将来换渲染器不必重跑 LLM。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

log = logging.getLogger("papershelf.mathml")

# 行内 \( ... \) 与展示 \[ ... \]（管线统一使用这两种定界符，见 translator 的 prompt 约定）
_INLINE_RE = re.compile(r"\\\((.+?)\\\)", re.S)
_DISPLAY_RE = re.compile(r"\\\[(.+?)\\\]", re.S)
# \tag{12} → \qquad(12)：latex2mathml 对 \tag 支持不完整，转成显式编号更稳
_TAG_RE = re.compile(r"\\tag\s*\{\s*([^}]*?)\s*\}")


def _plain_text(m: re.Match[str]) -> str:
    return m.group(1)


def latex_to_mathml(tex: str, *, display: bool = False) -> str | None:
    """把一段 LaTeX 转成 MathML 字符串；失败返回 None（调用方负责回落，绝不抛）。"""
    tex = (tex or "").strip()
    if not tex:
        return None
    return _convert(tex, display)


@lru_cache(maxsize=4096)
def _convert(tex: str, display: bool) -> str | None:
    try:
        from latex2mathml.converter import convert

        prepared = _TAG_RE.sub(lambda m: r"\qquad(%s)" % m.group(1), tex)
        out = convert(prepared, display="block" if display else "inline")
        if "<math" not in out:
            return None
        if display and 'display="inline"' in out:      # 展示公式要占整行
            out = out.replace('display="inline"', 'display="block"', 1)
        return out
    except Exception as exc:                            # noqa: BLE001  渲染失败不该影响转换
        log.warning("LaTeX → MathML 失败：%s（%s）", tex[:60], exc)
        return None


def render_math(text: str) -> str:
    """把整段文本里的 LaTeX 定界符渲染成 MathML，其余部分按需转义。

    返回的字符串**已是可安全插入 HTML 的片段**：非数学部分被转义，数学部分是受控标签。
    """
    from html import escape

    if not text:
        return ""
    # 先转义，再替换——但转义会把 \( 变成 \( 也无妨，故改为分段处理
    out: list[str] = []
    pos = 0
    pattern = re.compile(r"\\\[(.+?)\\\]|\\\((.+?)\\\)", re.S)
    for m in pattern.finditer(text):
        out.append(escape(text[pos:m.start()], quote=False))
        disp = m.group(1) is not None
        body = m.group(1) if disp else m.group(2)
        ml = latex_to_mathml(body, display=disp)
        if ml:
            out.append(f'<span class="math-inline">{ml}</span>' if not disp
                       else f'<div class="math-display">{ml}</div>')
        else:
            # 回落：保留原文，至少让人看到内容（不阻塞、不报错）
            out.append(f'<code class="math-raw">{escape(m.group(0), quote=False)}</code>')
        pos = m.end()
    out.append(escape(text[pos:], quote=False))
    return "".join(out)


def render_math_block(tex: str, number: str = "") -> str:
    r"""渲染**独立公式块**（`eq`）：payload 里的 `latex` 是**裸 LaTeX**（没有定界符）。

    ⚠️ 不能直接走 `render_math`：它按定界符切分，裸 LaTeX 会被当成散文转义，
    整条公式变成源码文本（实测踩过：arXiv 路线的公式块全部以源码显示）。
    编号优先用 `	ag{}`（题注自带），否则用块里记录的 `number` 补上。
    """
    from html import escape

    tex = (tex or "").strip()
    if not tex:
        return ""
    if _DISPLAY_RE.search(tex) or _INLINE_RE.search(tex):     # 已带定界符 → 交给通用路径
        return render_math(tex)
    body = tex if (not number or "\\tag" in tex) else f"{tex} \\tag{{{number}}}"
    ml = latex_to_mathml(body, display=True)
    if ml:
        return f'<div class="math-display">{ml}</div>'
    return f'<code class="math-raw">{escape(tex, quote=False)}</code>'


def has_math(text: str) -> bool:
    return bool(text) and ("\\(" in text or "\\[" in text)


def mathml_css() -> str:
    """MathML 的最小样式：只处理换行与居中，字号交给浏览器默认（MathML Core 有良好默认）。"""
    return """
.math-display{display:block;text-align:center;margin:14px 0;overflow-x:auto;overflow-y:hidden}
.math-inline{display:inline-block;vertical-align:middle}
math{font-size:1.02em}
.math-raw{font-size:.92em;background:var(--chip,#f4f2ec);padding:1px 4px;border-radius:3px}
/* 行内公式不许撑破版心（决策⑥ 单栏流式） */
mjx-container,math{max-width:100%}
"""


__all__ = ["has_math", "latex_to_mathml", "mathml_css", "render_math", "render_math_block"]
