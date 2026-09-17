"""配置护栏：**读进来的环境变量必须有人用**（钉住 2026-09-17 删掉的死配置）。

## 这条护栏防的是什么

`PAPERSHELF_TOKEN_BUDGET`（`docs/install.md` 原先写着「单篇 token 预算」，默认 40 万）
在 `config.py` 里有一个 `Settings.token_budget_per_paper` 字段，**但全仓库没有任何地方用它** ——
真实转换实测烧了 **1,072,896 tokens**，它一次都没拦过。宿主 2026-09-17 定：「不需要限制」，直接删掉。

比"没有护栏"更糟的是"**文档说有护栏**"：出事故时人会去看配置项、确认自己设对了，
然后**排错方向整个错掉**（同 `metadata-gap-19` 的教训：「有写回代码」≠「有数据来源」，
这里反过来：「有配置项」≠「有功能」）。

## 判据

不检查"某个变量名有没有被删"（那是一次性的），而是检查**类不变式**：
`Settings` 里每一个字段（= 每一个配置项）必须在 `src/` 里**被 `settings.<字段名>` 消费过**，
或者被显式登记在 `ALLOWED_UNCONSUMED` 并写明理由。新加的配置项若只是读了存着，这里立刻转红。

> 走过的弯路（值得记下）：最初写的是"环境变量**名字**必须在别处出现"—— **判据错了**，
> 它把 `PAPERSHELF_PROOFREAD_DPI` 这类**真的在用**的项全报成死配置（消费端写的是
> `settings.proofread_dpi`，不是环境变量名）。**"被消费"要按代码里的字段名量，不是按变量名量。**
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "src" / "papershelf" / "server" / "config.py"

# 允许"读了但没有消费端"的例外（每条都要写清理由，否则别加）。
ALLOWED_UNCONSUMED: set[str] = set()


_KEYWORDS = {"try", "except", "finally", "else", "elif", "if", "for", "while",
             "with", "return", "raise", "pass", "yield", "assert"}


def _field_names(source: str) -> set[str]:
    """从 config.py 抽出 `Settings` 的字段名（形如 `    xxx_yyy: 类型 = field(...)`）。

    ⚠️ 两个必须同时成立的限制，缺一个就会抓到假字段（**实测踩过**）：
    ① 类型标注后要紧跟非空白字符（`[ \\t]*` 而非 `\\s*` —— `\\s` 会吃掉换行，
       于是 `try:` 加上下一行的第一个字母会被算成一个名叫 `try` 的字段）；
    ② 关键字黑名单兜底（缩进 4 的 `try:` 在 config.py 里真的存在）。
    """
    names = set()
    for m in re.finditer(r"^    ([a-z][a-z0-9_]*):[ \t]*[A-Za-z\[(\"']", source, flags=re.M):
        if m.group(1) not in _KEYWORDS:
            names.add(m.group(1))
    return names


def _src_sources() -> list[Path]:
    """仓库里会被打进镜像的源码（排除 `.emrg` 运行时与构建产物）。"""
    out: list[Path] = []
    for pat in ("src/**/*.py", "web/src/**/*.ts", "web/src/**/*.tsx"):
        out.extend(ROOT.glob(pat))
    return [p for p in out if p.is_file() and ".emrg" not in p.parts]


def test_every_settings_field_is_consumed_by_the_code():
    """`Settings` 的每个字段都必须**被代码消费**（某处写 `settings.<字段>`，或同文件里的派生属性）——
    只是被读进来存着、没有任何消费端，就是死配置。"""
    source = CONFIG.read_text(encoding="utf-8")
    fields = _field_names(source)
    assert len(fields) > 8, f"只解析出 {len(fields)} 个字段，config.py 的结构变了，先看这条断言"

    # ⚠️ **config.py 自己也算消费端**：`email_allowlist` 就是被同文件里的
    # `allowed_domains` 属性消费的（字段 → 派生属性 → 路由）。判据只管"有没有人用"，
    # 不管在哪用；只是**定义它自己那一行不算**（那行写的是 `email_allowlist: str = field(...)`，
    # 匹配不了 `[.]email_allowlist`）。
    joined = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in _src_sources())

    dead = [f for f in sorted(fields)
            if f not in ALLOWED_UNCONSUMED and not re.search(rf"[.]{f}\b", joined)]
    assert not dead, (
        f"这些 `Settings` 字段全仓库只有 config.py 自己定义、**没有任何消费端**（死配置）：{dead}。\n"
        "两种修法：① 接上消费端；② 删掉这个配置项（连同 .env.example / docs/install.md 的说明）。\n"
        "别留着 —— 文档里多一个不生效的配置项，排障时就会把人往错方向带。"
        "（本仓库已有先例：`PAPERSHELF_TOKEN_BUDGET` 写过「单篇 token 预算」，实际一次都没生效。）"
    )


def test_removed_per_paper_token_budget_does_not_come_back():
    """`PAPERSHELF_TOKEN_BUDGET` 已按宿主指示删除（2026-09-17），不得复活。

    ⚠️ 注意区分**活的那一条**：`PAPERSHELF_PROOFREAD_TOKEN_BUDGET`（①c agent 的累计预算）
    是 `proofread.Proofreader(token_budget=…)` 真正吃的东西，**必须保留**。
    两条名字只差一个词，所以断言一律精确匹配整名 + 边界，不用裸 `in`。

    ⚠️ 也不禁止"提到它"：config.py / design.md 里留着**说明它为什么被删**的注释是**有意为之**
    （下次有人想加成本护栏时，先看到这段就不用重新踩一遍）。要禁的是**真的把它读进来**。
    """
    name = "PAPERSHELF_TOKEN_BUDGET"
    # ① config.py 里不得再读它，也不得再有对应字段
    cfg = CONFIG.read_text(encoding="utf-8")
    for pattern in (rf'os\.environ\.get\(\s*["\']{name}["\']', rf'_int\(\s*["\']{name}["\']',
                    rf'os\.environ\[\s*["\']{name}["\']\s*\]', rf'^\s*token_budget_per_paper:',
                    r'^\s*\w*token_budget_per_paper\w*:'):
        assert not re.search(pattern, cfg, flags=re.M), (
            f"config.py 里又出现了 `{name}` 的读取/字段（{pattern}）。"
            "宿主 2026-09-17：「不需要 PAPERSHELF_TOKEN_BUDGET 限制」。"
            "要加成本护栏请用 `PAPERSHELF_PROOFREAD_TOKEN_BUDGET`（已有活实现）"
            "或 `MAX_CONCURRENCY` / `MAX_ATTEMPTS`。"
        )
    # ② `.env.example` 不得再提供它（示例文件是"有哪些旋钮"的权威清单，留着=又回到假护栏）
    example = ROOT / ".env.example"
    if example.exists():
        text = example.read_text(encoding="utf-8")
        offending = [ln for ln in text.splitlines()
                     if re.match(rf"\s*{name}\s*=", ln)]
        assert not offending, f".env.example 里还在推荐 `{name}`：{offending}"


def test_proofread_token_budget_is_actually_wired():
    """活的那条（①c agent 预算）**必须仍然是活的** —— 别为了删死配置把它一起删了。

    它是"防模型兜圈子"的最后一道闸：轮数没超但模型开始空转时挡在这里。
    """
    cfg = CONFIG.read_text(encoding="utf-8")
    assert "PAPERSHELF_PROOFREAD_TOKEN_BUDGET" in cfg
    converter = (ROOT / "src/papershelf/server/converter.py").read_text(encoding="utf-8")
    assert "proofread_token_budget" in converter, (
        "converter 不再把 proofread_token_budget 传给 ①c —— 这条护栏就废了（配置项还在，但没人用）"
    )
    pf = (ROOT / "src/papershelf/pipeline/proofread.py").read_text(encoding="utf-8")
    assert "self.token_budget" in pf, "①c agent 内部不再比对预算"

