"""合成导出 HTML —— 决策⑳ 的派生轨道（JSON → 单文件 HTML）。

阅读器用块级 JSON 渲染；**导出/分享**时才由本模块合成自包含 HTML。
`dual` 模式输出左右并排对照（决策⑤），用 CSS grid 让同 ID 的中英块天然对齐。
"""

from __future__ import annotations

import dataclasses

from .mathml import mathml_css
from .markup import CSS, _esc, render_block
from .model import Block, table_zh_usable
from .validate import NO_ZH_TYPES

DUAL_CSS = """
/* 一行 = 一对同 ID 的中英块（决策⑤ 左右并排）。
   标记 data-b 挂在**行**上：块 ID 在文档中只出现一次，配对无歧义（决策④）。 */
.dual .row{display:grid;grid-template-columns:1fr 1fr;gap:0 26px;align-items:start}
.dual .row .col{min-width:0}
.dual .row .col.zh{border-left:2px solid var(--rule);padding-left:16px}
@media(max-width:900px){
  .dual .row{grid-template-columns:1fr}
  .dual .row .col.zh{border-left:0;border-top:1px dashed var(--rule);padding:8px 0 0}
}
/* 无译文的块（纯公式/参考文献/回落的英文）：**横跨两栏只显一次**，
   否则同一条公式会在左右栏各出现一遍，既冗余又误导 */
.dual .row.wide{display:block}
.dual .row.wide .col.zh{display:none}
"""


def with_asset_prefix(blocks: list[Block], prefix: str) -> list[Block]:
    """把块里的相对资源路径（`assets/x.png`）改写为**可被浏览器取到的 URL**。

    解析产物统一用相对路径（导出成单文件/整目录时可直接双击打开），
    但**服务端阅读器与分享**走 HTTP，必须改成 `/papers/<id>/assets/x.png`
    或 `/share/<token>/assets/x.png`，否则图片全是 404。
    """
    if not prefix:
        return blocks
    out = []
    for b in blocks:
        src = b.payload.get("src") if isinstance(b.payload, dict) else None
        if src and not src.startswith(("http://", "https://", "/", "data:")):
            nb = dataclasses.replace(b, payload={**b.payload, "src": f"{prefix.rstrip('/')}/{src.split('/')[-1]}"})
            out.append(nb)
        else:
            out.append(b)
    return out


def _shell(title: str, lang_attr: str, body: str, extra_css: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="{lang_attr}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title or 'papershelf')}</title>
<style>{CSS}{mathml_css()}{extra_css}</style>
</head>
<body>
<div class="page">
{body}
</div>
</body>
</html>
"""


def synth_dual(blocks: list[Block], *, title: str = "", meta_line: str = "",
               typeset: bool = True, asset_prefix: str = "") -> str:
    """中英左右并排（导出/分享形态）。同一块 ID 左右相邻，天然对齐。

    **按 PDF 页码分页**（与阅读器同一套观感）：块自带 `payload.page`（解析阶段盖的戳），
    这里据此切页并加「第 N 页 / 共 M 页」页脚 —— 导出件与屏幕上是同一个版式。
    老文档（无 `page`）→ 全篇一页，页脚不显示页码，内容一块不少。
    """
    blocks = with_asset_prefix(blocks, asset_prefix)
    pages: list[tuple[int, list[Block]]] = []
    for b in blocks:
        no = _page_of(b)
        if pages and pages[-1][0] == no:
            pages[-1][1].append(b)
        else:
            pages.append((no, [b]))
    total = _max_page(blocks)
    body = "\n".join(_page_section(no, pg, total, typeset) for no, pg in pages)
    head = (
        f'<div class="masthead"><h1>{_esc(title)}</h1>'
        + (f'<div class="meta">{_esc(meta_line)}</div>' if meta_line else "")
        + "</div>"
    )
    return _shell(title, "zh-CN", head + f'<div class="dual">{body}</div>', extra_css=DUAL_CSS)


def _page_of(b: Block) -> int:
    """块的 PDF 页码；缺省/非法一律 0（= 无页码信息，归入同一页）。"""
    p = b.payload.get("page") if isinstance(b.payload, dict) else None
    return p if isinstance(p, int) and p > 0 else 0


def _max_page(blocks: list[Block]) -> int:
    """PDF 总页数（取最大页码）；没有任何块带页码时返回 0 → 页脚只显「第 N 页」。"""
    return max((_page_of(b) for b in blocks), default=0)


def _page_section(no: int, blocks: list[Block], total: int, typeset: bool) -> str:
    """一页：`.page-body` + 页脚。`no == 0`（无页码信息）时不挂页脚。"""
    attr = f' data-page="{no}"' if no else ""
    foot = ""
    if no:
        foot = (f'<div class="page-foot"><span class="pf-rule"></span>'
                f'<span class="pf-no">第 {no} 页'
                f'{f" / 共 {total} 页" if total else ""}</span></div>')
    return (f'<section class="pdf-page"{attr}>'
            f'<div class="page-body">{"".join(_dual_rows(blocks, typeset))}</div>'
            f'{foot}</section>')


def _dual_rows(blocks: list[Block], typeset: bool) -> list[str]:
    rows = []
    for b in blocks:
        en = render_block(b, lang="en", marker=False, typeset=typeset)
        # 纯公式 / 参考文献 / 尚无译文 → 单栏横跨（渲染英文一次即可）。
        # ⚠️ 表格另有一条判据：中文网格"形状不符 / 逐格照抄英文"时也别配一对
        # （否则右栏是一张与左栏一模一样的表，见 `model.table_zh_usable`）。
        wide = (b.type in NO_ZH_TYPES or not (b.zh or "").strip()
                or (b.type == "table" and not table_zh_usable(b)))
        if wide:
            rows.append(f'<div class="row wide" data-b="{b.id}"><div class="col">{en}</div></div>')
            continue
        zh = render_block(b, lang="zh", marker=False, typeset=typeset)
        # 标记只出现一次：挂在行上，前端按 data-b 对齐滚动（决策⑤）
        rows.append(
            f'<div class="row" data-b="{b.id}">'
            f'<div class="col en">{en}</div><div class="col zh">{zh}</div></div>'
        )
    return rows


def synth_single(blocks: list[Block], *, lang: str, title: str = "", meta_line: str = "",
                 typeset: bool = True, asset_prefix: str = "") -> str:
    blocks = with_asset_prefix(blocks, asset_prefix)
    head = (
        f'<div class="masthead"><h1>{_esc(title)}</h1>'
        + (f'<div class="meta">{_esc(meta_line)}</div>' if meta_line else "")
        + "</div>"
    )
    body = head + "\n".join(render_block(b, lang=lang, typeset=typeset) for b in blocks)
    return _shell(title, "en" if lang == "en" else "zh-CN", body)


def synth(blocks: list[Block], mode: str, *, title: str = "", meta_line: str = "",
          typeset: bool = True, asset_prefix: str = "") -> str:
    if mode == "dual":
        return synth_dual(blocks, title=title, meta_line=meta_line, typeset=typeset,
                          asset_prefix=asset_prefix)
    return synth_single(blocks, lang=mode, title=title, meta_line=meta_line, typeset=typeset,
                        asset_prefix=asset_prefix)
