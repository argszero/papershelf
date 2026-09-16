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
from .validate import expects_chinese, extract_blocks, validate

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
5. 参考文献条目（作者名、标题、期刊、年份）**保持英文原文不译**。
6. 专有名词（模型名、方法名、数据集名）按术语表处理；术语表未覆盖且学界惯用英文的可保留英文。

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


class Translator:
    def __init__(self, cfg: LLMConfig, glossary: list[dict] | None = None,
                 *, table_retry: int = 1) -> None:
        self.cfg = cfg
        self.glossary = glossary or []
        self.system = SYSTEM_PROMPT          # 子类（Latexizer）可换一套 system prompt
        self._table_retry = max(0, int(table_retry))
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

    def _user_prompt(self, blocks: list[Block], ctx_before: Block | None, ctx_after: Block | None) -> str:
        # ⚠️ typeset=False：prompt 与输出都必须是 **LaTeX 源码**，译文才能继承同一份公式
        #    （渲染成 MathML 后模型输出的"标记保真"就无从校验，见 validate.latex_problems）。
        parts = [self._glossary_text()]
        if ctx_before is not None:
            parts.append(
                "【上文（仅供理解，**不要翻译**）】\n"
                + render_block(ctx_before, lang="en", marker=False, typeset=False)
                + "\n\n"
            )
        parts.append("【需要翻译的片段】\n"
                     + "\n".join(render_block(b, lang="en", typeset=False) for b in blocks) + "\n")
        if ctx_after is not None:
            parts.append(
                "\n【下文（仅供理解，**不要翻译**）】\n"
                + render_block(ctx_after, lang="en", marker=False, typeset=False)
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
    def chunk(blocks: list[Block], max_blocks: int = 12, max_chars: int = 6000) -> list[list[Block]]:
        """按块边界切片：块不跨切片（决策④ 的必要条件）。"""
        chunks: list[list[Block]] = []
        cur: list[Block] = []
        size = 0
        for b in blocks:
            n = len(b.en) + (len(b.payload.get("caption") or "") if b.type == "figure" else 0)
            if cur and (len(cur) >= max_blocks or size + n > max_chars):
                chunks.append(cur)
                cur, size = [], 0
            cur.append(b)
            size += n
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

        两道「不烧冤枉钱」的过滤：
        - **免中文块**（参考文献/公式，决策③ 的 prompt 规则 5）根本不送模型 —— 这篇论文里
          参考文献占正文 26%，送过去只会被判「漏译」再重译，纯烧钱；
        - `zh_source == "human"` 的块**不覆盖**（决策⑯：重跑不得冲掉人工修订）。
        """
        all_texts: dict[str, str] = {}
        ordered = self.ordered(blocks)
        todo = [b for b in ordered
                if b.zh_source != "human" and expects_chinese(b.en, block_type=b.type)
                and (only is None or b.id in only)]
        # 注意：上下文仍取自**完整** ordered，只有待翻集合被收窄（断点续跑不影响上下文质量）

        # 表格与正文走**两条通道**（见模块里 `TABLE_SYSTEM` 的说明）：表格送网格、要网格，
        # 正文送带标记的 HTML。混在一起送会让模型把 `<td>` 的边界当成排版噪声。
        tables = [b for b in todo if b.type == "table"]
        todo = [b for b in todo if b.type != "table"]
        for b in tables:
            zh = self._translate_table(b, log=log)
            if zh:
                all_texts[b.id] = zh

        index = {b.id: i for i, b in enumerate(ordered)}
        chunks = self.chunk(todo, max_blocks, max_chars)
        # 「进度」必须能回答"还剩多少"：只打「切片 3」看不出是 3/45 还是 3/4
        # （生产汇报「一直显示转换中」时，日志里连总数都没有，无从判断是否在进行）。
        log(
            f"  · 翻译开始：{len(todo) + len(tables)}/{len(ordered)} 块需翻译"
            f"（含表格 {len(tables)} 张），共 {len(chunks)} 个切片"
            f"（{sum(len(b.en) for b in todo)} 字符）"
        )
        for ci, chunk in enumerate(chunks, start=1):
            first, last = index[chunk[0].id], index[chunk[-1].id]
            before = ordered[first - 1] if first > 0 else None
            after = ordered[last + 1] if last + 1 < len(ordered) else None
            log(f"  · 切片 {ci}/{len(chunks)}：{len(chunk)} 块（{sum(len(b.en) for b in chunk)} 字符）")
            try:
                out = self._chat(self._user_prompt(chunk, before, after))
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
                    out = self._chat(
                        self._user_prompt([b], ordered[i - 1] if i else None,
                                          ordered[i + 1] if i + 1 < len(ordered) else None)
                    )
                    _, txt = extract_blocks(out)
                    if b.id in txt:
                        all_texts[b.id] = txt[b.id]
                except Exception as exc:
                    log(f"    ! 重译 {bid} 失败：{exc}")

        # ── 收敛保证：重试额度用尽后不再纠缠，留痕交给阅读器的块级修订（决策⑯）──
        # 表格的"合格"判据是**形状 + 有没有中文**（`_table_bad`），与正文的文本判据不同，
        # 所以并进来一起收尾 —— 两处各留一份 needs_review 会导致界面上"待校对"数目对不上。
        self.needs_review = self._check(todo, all_texts) + [b.id for b in tables if self._table_bad(b)]
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

    # ── 分块 / 判定 ───────────────────────────────────────────────────────

    @staticmethod
    def ordered(blocks: list[Block]) -> list[Block]:
        """参与翻译的块序列（与 `translate_blocks` 的返回顺序**一致**）。

        调用方必须用它来对齐返回值，否则会错位 —— 这是 CLI 里踩过的坑。
        """
        return [b for b in blocks if b.type != "figure" or b.en or b.payload.get("caption")]

    @staticmethod
    def _check(ordered: list[Block], texts: dict[str, str]) -> list[str]:
        """用与全量校验同一套规则判定单块是否合格。"""
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
            # 说明：数字 / 公式编号的偏差不计入重译触发条件——译文合法改写数字的情形存在，
            # 当作失败会导致重试永不收敛（实测烧掉近 2 万 token 仍未通过）。
            # 此类语义偏差交由阅读器的块级修订按需处理（决策⑯）。
        return bad


__all__ = ["LLMConfig", "Translator", "validate"]
