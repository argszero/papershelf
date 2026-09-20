"""块级修订 —— 决策⑯ 的兜底通道（重译此块 / 编辑译文 / **手工插入·删除块**）。

决策⑯ 的边界很明确：**不做自动语义自检**，语义问题一律由用户按需修订。

宿主 2026-09-20 把"手工修订"补全成三个原语：

> 所以，实际上不是拆分，是可以在任意 block 的前面或者后面添加新的 block。同样，合并也不是
> 单独的操作。只要支持新 block 的插入、老 block 的编辑、老 block 的删除。就相当于实现了
> block 的手动拆分和合并。并且可以确保只影响当前 block 的笔记，其前后未动的 block 的笔记可以不影响。

所以这里有四个动作：`PATCH` 编辑、`POST` 插入、`DELETE` 删除、`POST /retranslate` 重译。
插入/编辑/删除的批注语义全在 `server/blockops.py`（**只碰被改那一块的批注**）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...pipeline import LLMConfig, Translator
from ...pipeline.validate import expects_chinese
from .. import blockops
from ..config import Settings, get_settings
from ..db import load_json
from ..repo import get_block, list_highlights, public_block, update_block
from ..security import current_user, get_conn, require_paper

router = APIRouter(prefix="/api/docs/{paper_id}", tags=["blocks"])


class BlockPatch(BaseModel):
    """改一个块。三个字段都可选（`None` = 不动），但至少要给一个。

    `en`（原文）与 `zh`（译文）**各按自己的坐标系重新锚定批注** —— 中英没有字级对应，
    按比例换算是猜（㉛ 的同一条理由，见 `blockops.edit_block`）。
    """

    zh: str | None = None
    en: str | None = None
    type: str | None = None
    # 前端「确认无误」后置 true → 撤掉「待校对」徽标（§5.7）
    reconciled: bool = False


class BlockNew(BaseModel):
    """在某一块的**前/后**插入一个新块（拆分 = 插入 + 编辑原块）。"""

    after: str | None = None
    before: str | None = None
    en: str = ""
    zh: str = ""
    type: str | None = None
    level: int | None = None


def _glossary(conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
    row = conn.execute("SELECT glossary FROM plan_settings WHERE plan_id=?", (plan_id,)).fetchone()
    return load_json(row["glossary"], []) if row else []


@router.get("/blocks/{block_id}")
def get_one_block(paper_id: int, block_id: str,
                  conn: sqlite3.Connection = Depends(get_conn),
                  user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """取**单块**（含按当前划痕渲染好的 HTML，㉛）。

    为什么需要它：划痕是服务端渲染进 HTML 的，前端划一道之后要让屏幕上出现
    `<mark>`，只有三条路 ——（a）自己拿 Range 去包 DOM（前端就得懂切句/公式/转义，
    与"服务端渲染"这条不变量冲突）；（b）重拉整篇（900 块的文档要几百 KB，
    划一道就重下一次，纯浪费）；（c）**重拉这一块**。选 (c)。
    """
    require_paper(conn, paper_id, user)
    fresh = get_block(conn, paper_id, block_id)
    if fresh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "块不存在")
    marks = [h for h in list_highlights(conn, paper_id) if h["block_id"] == block_id]
    return public_block(fresh, marks=marks)


# `blockops` 的失败原因 → HTTP 状态码。**别用字符串匹配**（reason 是给人看的中文，
# 改一个字就把 404 变成 400 —— 那种"错误码随文案漂移"的 bug 测试很难盯住）。
_STATUS = {"not_found": status.HTTP_404_NOT_FOUND, "no_doc": status.HTTP_409_CONFLICT}


def _http_status(res: dict[str, Any]) -> int:
    return _STATUS.get(str(res.get("code") or ""), status.HTTP_400_BAD_REQUEST)


def _marks(conn: sqlite3.Connection, paper_id: int, block_id: str) -> list[dict[str, Any]]:
    return [h for h in list_highlights(conn, paper_id) if h["block_id"] == block_id]


@router.patch("/blocks/{block_id}")
def edit_block(paper_id: int, block_id: str, body: BlockPatch,
               conn: sqlite3.Connection = Depends(get_conn),
               user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """改这一块的原文/译文/类型（⑯ + 宿主 2026-09-20 的"老 block 的编辑"）。

    ⚠️ 回的是**完整块**（含重新渲染的 `zh_html`）：前端只能整块替换 —— 只回 `{id, zh}`
    的话它只能就地改纯文本，而 `zh_html` 还是旧的，界面看起来"保存成功但没变化"。
    ⚠️ 必须带上**本块的划痕**：服务端渲染的 HTML 是划痕的唯一来源，不传等于"编辑一次
    译文顺手抹掉这一块的划痕"。
    """
    require_paper(conn, paper_id, user)
    res = blockops.edit_block(conn, paper_id, block_id, en=body.en, zh=body.zh,
                              type=body.type, reconciled=body.reconciled)
    if not res["ok"]:
        raise HTTPException(_http_status(res), res["reason"])
    fresh = get_block(conn, paper_id, block_id)
    out = public_block(fresh, marks=_marks(conn, paper_id, block_id)) if fresh else {"id": block_id}
    # 账本一起回给前端：改动让该块自己的 N 条批注随之失效时，用户必须**当场**看见
    # （「移动了 3 条 / 有 2 条随被删文字一起移除」）—— 静默丢批注是这里最坏的失败模式。
    out["anchors_moved"] = res.get("anchors_moved", 0)
    out["anchors_dropped"] = res.get("anchors_dropped", [])
    out["zh_stale"] = bool(res.get("zh_stale"))
    return out


@router.post("/blocks", status_code=201)
def insert_block(paper_id: int, body: BlockNew,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """在 `after` / `before` 指定的块旁边插入一个新块。

    **已有块一个都不动**（id 与文字都不变）⇒ 钉在它们上面的笔记/划痕天然不受影响；
    新块取「现有最大号 + 1」，所以永远不会与旧块 id 撞上。
    """
    require_paper(conn, paper_id, user)
    res = blockops.insert_block(conn, paper_id, after=body.after, before=body.before,
                               en=body.en, zh=body.zh, type=body.type, level=body.level)
    if not res["ok"]:
        raise HTTPException(_http_status(res), res["reason"])
    fresh = get_block(conn, paper_id, res["id"])
    return public_block(fresh) if fresh else {"id": res["id"]}


@router.delete("/blocks/{block_id}")
def delete_block(paper_id: int, block_id: str,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """删掉一个块（合并 = 编辑 + 删除）。

    **该块的笔记一条都不删**：内容留着，只把锚点清空成「文献级笔记」（`content` 是用户
    自己写的字，比它的锚点值钱）；划痕随块一起收走。返回一份账给前端弹 toast。
    """
    require_paper(conn, paper_id, user)
    res = blockops.delete_block(conn, paper_id, block_id)
    if not res["ok"]:
        raise HTTPException(_http_status(res), res["reason"])
    return res


@router.post("/blocks/{block_id}/retranslate")
def retranslate_block(paper_id: int, block_id: str,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: dict[str, Any] = Depends(current_user),
                      settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """只重译这一块（决策⑯ 的「重译此块」）。上下文取前后各一块。

    能重译的是"**可能有中文**"的块（`blockops.RETRANSLATE_TYPES`）：正文/标题/摘要、
    **图注**（宿主 2026-09-20）、表格、文献条目。表格与文献条目各走自己的通道（㊴/㊹），
    译文不只在 `zh` 里 —— 所以下面要把 `payload` 里那几项一起回写。
    """
    paper = require_paper(conn, paper_id, user)
    rows = conn.execute("SELECT * FROM blocks WHERE paper_id=? ORDER BY ord",
                        (paper_id,)).fetchall()
    target = next((r for r in rows if r["id"] == block_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "块不存在")

    from ...pipeline.model import Block

    def to_block(r) -> Block:
        return Block(id=r["id"], type=r["type"], en=r["en"] or "", zh=r["zh"] or "",
                     zh_source=r["zh_source"] or "none", section=r["section"] or "",
                     level=r["level"] or 0, payload=load_json(r["payload"], {}))

    ordered = [to_block(r) for r in rows]
    idx = [i for i, b in enumerate(ordered) if b.id == block_id][0]
    target_block = ordered[idx]

    # ⚠️ **这是本轮真机验收抓到的真缺陷**（不是新写的代码，是原来就有的）：
    # `translate_blocks` 会跳过 `zh_source == "human"` 的块（⑯：**重跑**不得覆盖人工修订），
    # 而"重译此块"恰恰是**用户明说"用机器译文覆盖这一块"**。两者撞在一起时：
    # todo 为空 → 返回的是那一块的**旧中文** → 端点以为"译好了" → 把旧 `zh` 原样写回，
    # 还顺手把 `zh_source` 从 `human` 降级成 `mt` —— 表现是
    # **「点了重译没变化，『已人工修订』标记却消失了」**（既没重译，又丢了来源）。
    # 所以：给**这一份副本**摘掉来源戳，让翻译器真的去翻它。
    target_block.zh_source = "none"

    # ⚠️ 顺序要紧：**"这一块本来就没有可译的文字"是一个与 LLM 无关的事实**，
    #    放在"服务端未配置 LLM"之前答 —— 否则运维没配 Key 时，用户点一个
    #    永远不可能成功的按钮会收到"未配置 LLM"，去查配置查半天（真因是按钮不该有）。
    if not expects_chinese(target_block.en, block_type=target_block.type):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "这一块没有需要翻译的文字（公式/装饰图/参考文献碎片保持原文）")
    if not settings.llm_base_url or not settings.llm_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务端未配置 LLM")

    cfg = LLMConfig(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                    model=settings.llm_model)
    tr = Translator(cfg, _glossary(conn, paper["plan_id"]))
    texts = tr.translate_blocks(
        [target_block], only={block_id}, max_retry=1,
        log=lambda *a: None,
    )
    new_zh = (texts[0] if texts else "").strip()
    if not new_zh:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "重译失败，请稍后重试")

    # ⚠️ 表格的译文**不只在 `zh` 里**：网格（`payload.rows_zh` / `caption_zh`）才是
    # 阅读器真正渲染的东西（决策㊴）。翻译器把它写在了**内存里的那个 Block 副本**上，
    # 而这里是"取库里的行 → 拼 Block → 翻译 → 单列回写"，副本一丢网格就没了 ——
    # 表现是「重译此块」后中文照旧是英文（重译了、但渲染用的网格没变）。
    # 参考文献条目（`ref`）同理：`title_zh` 是"这条译了什么"的记录，一起回写。
    patch: dict[str, Any] = {"reconciled": True}
    if target_block.type == "table":
        for k in ("rows_zh", "caption_zh"):
            if k in target_block.payload:
                patch[k] = target_block.payload[k]
    elif target_block.type == "ref":
        for k in ("title_en", "title_zh"):
            if k in target_block.payload:
                patch[k] = target_block.payload[k]
    update_block(conn, paper_id, block_id, zh=new_zh, zh_source="mt", payload_patch=patch)
    fresh = get_block(conn, paper_id, block_id)
    marks = [h for h in list_highlights(conn, paper_id) if h["block_id"] == block_id]
    out = public_block(fresh, marks=marks) if fresh else {"id": block_id, "zh": new_zh}
    out["tokens"] = tr.tokens_used
    return out
