"""「标题是不是占位值」的**唯一判据**（⑲ / 2026-09-12）。

放在独立模块而不是 `converter.py` 里，是因为 **`db._migrate` 也要用它**做存量回溯 ——
而 `converter` 已经 import 了 `db`，反向 import 会成环。

## 为什么必须落列而不是现场推导

最初的判据是「`title == Path(pdf_path).stem`」，看着很干净：用户一改标题就不相等，
自动获得保护，连迁移都不用。**但生产 12 篇全部判成"非占位"**：

    title      = 'original'
    pdf_path   = '/data/papers/2026-09-11T143352+0000_original.pdf'   ← 落盘时加了时间戳前缀
    Path(...).stem = '2026-09-11T143352+0000_original'                ← 永不等于 title

于是"修好"之后真标题**仍然写不进去**，只是从"必错"变成"看起来对"。

教训：**占位符是一种意图，不是一种值**。凡是需要"当时那个文件名"的信息，
事后从被改过的路径里推不出来 —— 只能落列（`papers.title_is_placeholder`）。
下面的路径判据**只作兜底**，服务缺列的老库/老数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def title_stems(source_ref: Any, pdf_path: Any) -> set[str]:
    """能从路径里推出的「可能的原始文件名」（去后缀）。

    ⚠️ `source_ref` 才是**原始文件名**（`original.pdf`）；`pdf_path` 落盘时被加了时间戳
    前缀，排在后面只作补充。`Path("2603.19455").stem` 是 `"2603"`（`.` 被当后缀分隔符）——
    所以 arXiv 那样的点号编号**推不出来**，只能靠列。
    """
    out: set[str] = set()
    for v in (source_ref, pdf_path):
        if not v:
            continue
        stem = Path(str(v)).stem.strip()
        if stem:
            out.add(stem)
    return out


def is_placeholder_title(title: Any, *, flag: Any = ..., source_ref: Any = None,
                         pdf_path: Any = None) -> bool:
    """`title` 是否仍是「导入时随手写下、用户从未改过」的占位值。

    `flag` 传 `papers.title_is_placeholder` 的列值（`...` 表示缺列 → 走兜底判据）。
    语义：True = 可被自动抽取的真标题覆盖；False = 用户已明确命名，谁都不许覆盖。
    """
    if flag is not ... and flag is not None:
        return bool(flag)
    text = (title or "").strip()
    if not text:
        return True                       # 空标题当然可以填
    return text in title_stems(source_ref, pdf_path)
