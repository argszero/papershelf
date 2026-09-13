"""arXiv 导入 —— 决策⑱：**优先官方 HTML**（`arxiv.org/html/<ID>v<N>`），无则回落 PDF。

为什么优先 HTML（这是宿主既有经验换来的教训）：
- `<math alttext="...">` 里的 LaTeX 是**原始、未损坏**的 —— 无需 LLM 做公式 LaTeX 化（决策㉓ 的成本直接省掉）；
- 图片是原始文件，不经 PDF 截断（PDF 截断会导致 XObject 流损坏、MuPDF/PDFium/sips 全渲染失败）；
- 参考文献是 `<li id="bib.bibNN">`，结构干净。

无 HTML 版时**回落 PDF**：下载 → 走与上传完全相同的管线。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

from ..pipeline.model import Block, Doc, make_block_id

log = logging.getLogger("papershelf.arxiv")

_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
# ⚠️ 捕获组里**不能**把版本号也算进去：`abs/2405.04434v2` 会整串进 group(1)，
#    结果是 ID 带着 "v2" 去拼 URL（`/html/2405.04434v2v2`），实测踩过。
_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(v\d+)?")
# 老式编号（math.GT/0309136 这类，2007 年前）
_OLD_ID_RE = re.compile(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")


def normalize_ref(ref: str) -> tuple[str, str]:
    """从 arXiv ID / URL / PDF 链接里取出 (ID, 版本)。

    支持：`1706.03762`、`1706.03762v5`、`abs|pdf|html/<ID>`、老式 `math.GT/0309136`。
    """
    ref = (ref or "").strip()
    for pattern in (_URL_RE, _OLD_ID_RE, _ID_RE):
        m = pattern.search(ref)
        if m:
            return m.group(1).rstrip("/"), (m.group(2) or "")
    raise ValueError(f"无法识别的 arXiv 标识：{ref}")


def import_arxiv_doc(paper: dict, settings) -> Doc:
    arxiv_id, version = normalize_ref(paper.get("source_ref") or "")
    out_dir = settings.papers_dir / f"p{paper['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)

    html = _fetch_html(arxiv_id, version)
    if html:
        return _from_html(html, arxiv_id, version, paper, settings)

    # ── 回落：下载 PDF，走与上传相同的管线 ──
    pdf_path = out_dir / f"{arxiv_id}{version}.pdf"
    if not pdf_path.exists() or pdf_path.stat().st_size < 10_000:
        _download_pdf(arxiv_id, version, pdf_path)
    from ..pipeline import parse_pdf

    doc = parse_pdf(pdf_path, assets_dir=out_dir / "assets")
    doc.meta.setdefault("arxiv_id", f"{arxiv_id}{version}")
    return doc


def _fetch_html(arxiv_id: str, version: str) -> str | None:
    url = f"https://arxiv.org/html/{arxiv_id}{version or ''}"
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "papershelf/0.1 (+self-hosted)"})
        if r.status_code == 200 and "<math" in r.text:
            return r.text
        log.info("arXiv HTML 不可用（%s → %s），回落 PDF", url, r.status_code)
    except Exception as exc:  # noqa: BLE001
        log.info("arXiv HTML 抓取失败：%s", exc)
    return None


def _download_pdf(arxiv_id: str, version: str, target: Path) -> None:
    url = f"https://arxiv.org/pdf/{arxiv_id}{version or ''}"
    with httpx.stream("GET", url, timeout=120, follow_redirects=True,
                      headers={"User-Agent": "papershelf/0.1 (+self-hosted)"}) as r:
        r.raise_for_status()
        with target.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    if not target.read_bytes()[:5].startswith(b"%PDF"):
        raise RuntimeError("下载到的不是 PDF（可能被反爬拦截）")


# ── HTML → Doc（laTeXML 输出的结构）────────────────────────────────────────


def _extract_title(html: str) -> str:
    """论文标题：`ltx_title_document`（比 h1 更可靠，laTeXML 的层级随论文而变）。"""
    m = re.search(r'<h1[^>]*class="[^"]*ltx_title_document[^"]*"[^>]*>(.*?)</h1>', html, re.S | re.I)
    if m:
        t = _plain(m.group(1))
        if t:
            return t
    m = re.search(r'<meta[^>]+name="citation_title"[^>]+content="([^"]+)"', html, re.I)
    return _plain(m.group(1)) if m else ""


def _extract_abstract(html: str) -> str:
    m = re.search(r'<div[^>]*class="[^"]*ltx_abstract[^"]*"[^>]*>(.*?)</div>\s*</section>',
                  html, re.S | re.I)
    if not m:
        m = re.search(r'<div[^>]*class="[^"]*ltx_abstract[^"]*"[^>]*>(.*?)</div>', html, re.S | re.I)
    if not m:
        return ""
    body = re.sub(r"<h[1-6][^>]*>.*?</h[1-6]>", " ", m.group(1), flags=re.S | re.I)
    typeset, _ = _inline_math(body)
    return _plain(typeset)


def _block_number(eq_id: str, fallback_index: int = 0) -> str:
    """`S3.E1` → `1`；`S3.EGx1`（无编号公式组）→ **空串**。

    不要给无编号公式硬编一个号：编号是给人对位的，编造出来的号比没有更糟
    （渲染时也不会再输出 `\tag{}`）。
    """
    m = re.search(r"E(\d+)$", eq_id or "")
    return m.group(1) if m else ""


def _iter_equation_tables(html: str) -> list[tuple[int, int, str]]:
    """定位所有 `ltx_equation` 表格，返回 [(起点, 终点, eq_id)]。

    ⚠️ 必须**按深度计数**匹配 `</table>`：多行对齐公式的编号表是**嵌套**的，
    非贪婪正则 `(.*?)</table>` 会在内层 table 处提前截断
    —— 实测后果：Attention 那篇 12 条展示公式只抽出 5 条，其余静默丢失。
    """
    out: list[tuple[int, int, str]] = []
    for m in re.finditer(r"<table([^>]*)>", html, re.I):
        attrs = m.group(1)
        if "ltx_equation" not in attrs or "ltx_eqn_table" not in attrs:
            continue
        # 找到与本 table 配对的 </table>
        depth, pos = 1, m.end()
        for t in re.finditer(r"</?table\b[^>]*>", html[m.end():], re.I):
            pos = m.end() + t.end()
            depth += -1 if t.group(0).startswith("</") else 1
            if depth == 0:
                break
        eq_id = (re.search(r'id="([^"]+)"', attrs) or [None, ""])[1]
        out.append((m.start(), pos, eq_id))
    return out


def _extract_equation_numbers(html: str) -> dict[str, str]:
    """`ltx_equation` 表格 → 编号（`ltx_tag_equation` 里的 `(12)`）。

    编号是**校验与阅读都需要的**（`validate.latex_problems` 会比对 \\tag 编号），
    而它不在 <math> 里，必须从这里取。
    """
    out: dict[str, str] = {}
    for start, end, eq_id in _iter_equation_tables(html):
        body = html[start:end]
        t = re.search(r'ltx_tag_equation[^>]*>\s*\(?\s*([\w.]+?)\s*\)?\s*</span>', body)
        out[eq_id] = t.group(1) if t else ""
    return out
_TEX_MACROS = {
    r"\textbackslash": "\\", r"\&": "&", r"\%": "%", r"\$": "$", r"\_": "_",
    r"\#": "#", r"\{": "{", r"\}": "}",
}


def _clean_tex(tex: str) -> str:
    r"""去掉源文件里对渲染无意义、对校验有害的残留。

    ``\displaystyle`` 是排版指令（MathML 里由 ``<mstyle displaystyle>`` 表达），
    ``\label{}`` 是交叉引用锚点 —— 两者留在文本里只会污染校验与阅读。
    """
    tex = re.sub(r"\\displaystyle\b", "", tex or "")
    tex = re.sub(r"\\label\{[^}]*\}", "", tex)
    return " ".join(tex.split())


def _alttext_to_latex(alttext: str, display: bool) -> str:
    """`<math alttext>` 给的是原生 LaTeX：直接包定界符，零 LLM 成本（决策㉓ 的捷径）。"""
    tex = _clean_tex(alttext)
    for k, v in _TEX_MACROS.items():
        tex = tex.replace(k, v)
    if display:
        return f"\\[ {tex} \\]"
    return f"\\( {tex} \\)"


def _download_image(url: str, target: Path) -> None:
    """下载 arXiv 页面里的图片（抽成函数便于测试打桩，避免单测真联网）。"""
    r = httpx.get(url, timeout=30, follow_redirects=True)
    if r.status_code == 200:
        target.write_bytes(r.content)


def _from_html(html: str, arxiv_id: str, version: str, paper: dict, settings) -> Doc:
    """把 arxiv.org/html 页面转成块级 Doc。

    实现取舍：不引入 HTML 解析依赖，用正则按**块级元素**顺序切开。
    laTeXML 的输出结构规整（`<section>`/`<h2>`/`<p>`/`<figure>`/`<math>`），
    够用；遇到不规整的段落就整段落文本，不会丢内容。
    """
    from html import unescape

    title = paper.get("title") or _extract_title(html) or f"arXiv:{arxiv_id}{version}"
    doc = Doc(meta={"title_en": title, "arxiv_id": f"{arxiv_id}{version}",
                    "source": "arxiv", "title_zh": ""})

    # 去掉脚本/样式/导航，避免噪声进正文
    body = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    eqnos = _extract_equation_numbers(body)
    # 展示公式整体替换成占位符——**必须最先做**：
    # 公式表内还有 <p>/<td>/<math>，留在文中会被后续正则抢先匹配，公式就丢了。
    eq_holder: dict[str, str] = {}
    spans = _iter_equation_tables(body)
    for idx, (start, end, eq_id) in enumerate(reversed(spans)):
        key = f"\x00EQ{len(spans) - idx - 1}\x00"
        eq_holder[key] = body[start:end]
        body = body[:start] + key + body[end:]
    # ⚠️ 顺序即优先级：equation 占位符排在 `<p>` 之前（见上）
    chunk_re = re.compile(
        r"<h([1-6])[^>]*>(.*?)</h\1>"
        r"|\x00EQ(\d+)\x00"
        r"|<figcaption[^>]*>(.*?)</figcaption>"
        r"|<figure[^>]*>(.*?)</figure>"
        r"|<p[^>]*>(.*?)</p>"
        r"|<li[^>]*id=\"bib[^\"]*\"[^>]*>(.*?)</li>",
        re.S | re.I,
    )
    section = ""
    fig_index = 0
    for m in chunk_re.finditer(body):
        h_lvl, h_txt, eq_idx, cap, fig, para, ref = m.groups()
        if eq_idx is not None:                                # 独立公式（原生 LaTeX，零 LLM 成本）
            table = eq_holder.get(f"\x00EQ{eq_idx}\x00", "")
            tex = _block_math(table)
            if not tex:
                continue
            spans_map = {i: eid for i, (_, _, eid) in enumerate(spans)}
            eq_id = spans_map.get(int(eq_idx), "")
            num = eqnos.get(eq_id) or _block_number(eq_id)
            doc.blocks.append(Block(
                id=make_block_id(len(doc.blocks) + 1), type="eq", en=f"\\[ {tex} \\]",
                section=section, payload={"latex": tex, "number": num, "latexized": True},
            ))
            continue
        if ref is not None:                                   # 参考文献条目（保持英文）
            text = _plain(ref)
            if text:
                doc.blocks.append(Block(id=make_block_id(len(doc.blocks) + 1), type="refs",
                                        en=text, section="REFERENCES"))
            continue
        if fig is not None:                                   # 图：取 img + 图注，公式图跳过
            src = re.search(r'<img[^>]+src="([^"]+)"', fig)
            if cap:
                typeset_cap, _ = _inline_math(cap)
                caption = _plain(typeset_cap)
            else:
                caption = ""
            if src:
                fig_index += 1
                name = src.group(1).split("/")[-1]
                assets = settings.papers_dir / f"p{paper['id']}/assets"
                assets.mkdir(parents=True, exist_ok=True)
                local = assets / name
                if not local.exists():
                    url = (src.group(1) if src.group(1).startswith("http")
                           else f"https://arxiv.org/html/{arxiv_id}{version}/{src.group(1)}")
                    try:
                        _download_image(url, local)
                    except Exception as exc:  # noqa: BLE001
                        log.info("图片下载失败 %s：%s", name, exc)
                doc.assets.append({"name": name, "path": str(local), "page": 0})
                doc.blocks.append(Block(
                    id=make_block_id(len(doc.blocks) + 1), type="figure", en=caption,
                    section=section,
                    payload={"src": local.name, "caption": caption, "wait": False},
                ))
            continue
        if cap is not None:
            continue                                          # 图注已在 figure 里处理
        if h_lvl is not None:
            text = _plain(h_txt)
            if not text:
                continue
            level = min(4, max(2, int(h_lvl)))
            section = text
            doc.blocks.append(Block(id=make_block_id(len(doc.blocks) + 1),
                                    type=f"h{level}", en=text, section=section, level=level))
            continue
        if para is not None:
            typeset, has_display = _inline_math(para)
            text = _plain(typeset)
            if not text:
                continue
            btype = "eq" if has_display and len(text) < 120 else "p"
            payload = {"latex": text} if btype == "eq" else {}
            doc.blocks.append(Block(id=make_block_id(len(doc.blocks) + 1), type=btype,
                                    en=text, section=section, payload=payload))
    abstract = _extract_abstract(html)
    if abstract:                                              # 摘要单列成块（与 PDF 管线一致）
        doc.blocks.insert(0, Block(id="", type="abstract", en=abstract, section="ABSTRACT"))
    if not doc.blocks:
        raise RuntimeError("arXiv HTML 解析后没有内容（页面结构可能已变化）")
    for i, b in enumerate(doc.blocks, start=1):               # 插入摘要后统一重编号，ID 保持连续
        b.id = make_block_id(i)
    doc.meta["block_count"] = len(doc.blocks)
    return doc


def _block_math(fragment: str) -> str:
    """从 equation table 里取出 LaTeX：多行公式用 `\\` 连接，保留 `aligned` 语义。"""
    texes: list[str] = []
    for m in re.finditer(r"<math([^>]*)>(.*?)</math>", fragment, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        alt = re.search(r'alttext="([^"]*)"', attrs)
        if alt:
            texes.append(_clean_tex(alt.group(1)))
        else:
            ann = re.search(r'<annotation[^>]*encoding="application/x-tex"[^>]*>(.*?)</annotation>',
                            inner, re.S | re.I)
            if ann:
                texes.append(_plain(ann.group(1)))
    texes = [t for t in texes if t]
    if not texes:
        return ""
    if len(texes) == 1:
        return texes[0]
    body = " \\\\ ".join(texes)
    return f"\\begin{{aligned}} {body} \\end{{aligned}}"


def _inline_math(fragment: str) -> tuple[str, bool]:
    """把 `<math alttext>` 就地替换成 LaTeX 定界符，保留其余 HTML 供 `_plain` 提取。"""
    has_display = False

    def repl(m: re.Match) -> str:
        nonlocal has_display
        attrs, inner = m.group(1), m.group(2)
        alt = re.search(r'alttext="([^"]*)"', attrs)
        display = 'display="block"' in attrs or bool(re.search(r'class="[^"]*ltx_eqn', attrs))
        if not display:
            # 独立成段的公式（前后无文字）也算展示公式
            display = _is_standalone(m)
        if display:
            has_display = True
        if alt:
            return _alttext_to_latex(alt.group(1), display)
        return f" {_plain(inner)} "                       # 没 alttext 就退化为纯文本

    return re.sub(r"<math([^>]*)>(.*?)</math>", repl, fragment, flags=re.S | re.I), has_display


def _is_standalone(m: re.Match) -> bool:
    """`<math display="block">` 之外的启发式：整段只有这个 math。"""
    return False


def _plain(fragment: str) -> str:
    from html import unescape

    text = re.sub(r"<[^>]+>", "", fragment or "")
    text = unescape(text)
    return " ".join(text.split())
