"""papershelf 命令行 —— 管线（M1 里程碑）的驱动入口。"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .pipeline import (
    Doc,
    Latexizer,
    LLMConfig,
    Translator,
    document_html,
    en_html,
    expects_chinese,
    extract_blocks,
    looks_math,
    parse_pdf,
    synth,
    validate,
)

app = typer.Typer(add_completion=False, help="papershelf —— PDF/arXiv → 双语对照精读")


def _load_doc(path: str | Path) -> Doc:
    return Doc.from_json(Path(path).read_text(encoding="utf-8"))


def _fill_zh(doc: Doc, zh_fragment: str) -> None:
    """把译文片段按块 ID 回填到 Doc。"""
    _, texts = extract_blocks(zh_fragment)
    for b in doc.blocks:
        if b.id in texts:
            b.zh = texts[b.id]
            b.zh_source = "mt"


@app.command()
def parse(
    pdf: Path = typer.Argument(..., exists=True, help="输入 PDF"),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="输出目录"),
) -> None:
    """解析 PDF → 块级 JSON + 带标记的英文 HTML。"""
    out.mkdir(parents=True, exist_ok=True)
    doc = parse_pdf(pdf, assets_dir=out / "assets")
    (out / "doc.json").write_text(doc.to_json(), encoding="utf-8")
    (out / "en.html").write_text(en_html(doc), encoding="utf-8")
    kinds: dict[str, int] = {}
    for b in doc.blocks:
        kinds[b.type] = kinds.get(b.type, 0) + 1
    typer.echo(f"✅ 解析完成：{len(doc.blocks)} 块，{len(doc.assets)} 图")
    typer.echo(f"   块类型：{json.dumps(kinds, ensure_ascii=False)}")
    typer.echo(f"   产物：{out/'doc.json'}  {out/'en.html'}")


@app.command()
def latex(
    doc_path: Path = typer.Argument(..., help="doc.json（解析产物）"),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="输出目录"),
    ids: str = typer.Option("", "--ids", help="只处理这些块（逗号分隔）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只统计与预估，不调模型"),
    max_blocks: int = typer.Option(6, help="每切片最大块数"),
    max_chars: int = typer.Option(4000, help="每切片最大字符数"),
) -> None:
    """公式 LaTeX 化（决策 B）—— **必须在 translate 之前跑**，译文才能继承同一份 LaTeX。"""
    doc = _load_doc(doc_path)
    only = {x.strip() for x in ids.split(",") if x.strip()} or None
    targets = [b for b in doc.blocks
               if looks_math(b.en, block_type=b.type) and (only is None or b.id in only)]
    chars = sum(len(b.en) for b in targets)
    typer.echo(f"含数学的块：{len(targets)} / {len(doc.blocks)}，共 {chars} 字符"
               f"（粗估 ≈{chars * 3.5 / 1000:.0f}k tokens，含输出）")
    if dry_run:
        typer.echo("   --dry-run：未调用模型，退出。首批：" + ", ".join(b.id for b in targets[:10]))
        raise typer.Exit(0)
    if not targets:
        raise typer.Exit(0)

    lz = Latexizer(LLMConfig.from_env())
    before_en = {b.id: b.en for b in doc.blocks}          # ⟨用来判断「谁的 en 真变了」⟩
    stats = lz.latexize(doc.blocks, only=only, max_blocks=max_blocks, max_chars=max_chars)
    typer.echo(f"✅ LaTeX 化完成：{stats['blocks']} 块 / {stats['chunks']} 片，"
               f"重试 {stats['retried']} 片，失败回落 {stats['failed']} 块，tokens≈{lz.tokens_used}")

    # en 被改写 → 只有**这些**块的译文作废（人工修订保留）。
    # ⚠️ 不能用 payload['latexized'] 判断：它标记的是「历史上被 LaTeX 化过」（含本次未碰的块），
    #    那样每跑一次就清掉全篇译文、白烧一遍 LLM（实测踩过）。
    stale = [b for b in doc.blocks if before_en.get(b.id, b.en) != b.en]
    kept = [b for b in stale if b.zh_source == "human"]
    for b in stale:
        if b.zh_source != "human":
            b.zh, b.zh_source = "", "none"
    if kept:
        typer.echo(f"   ⚠️ {len(kept)} 块为人工修订（保留不动）：{', '.join(b.id for b in kept[:8])}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.json").write_text(doc.to_json(), encoding="utf-8")
    # en.html 必须同步重写：校验器拿它与 zh.html 比对，若停留在 LaTeX 化之前就是苹果对橘子
    (out / "en.html").write_text(en_html(doc), encoding="utf-8")
    if stale:
        typer.echo(f"   ↳ {len(stale)} 块英文已变，译文已清空 → 接着跑 "
                   f"`translate {out/'doc.json'} --resume` 即可精确补翻")
    typer.echo(f"   产物：{out/'doc.json'}")


@app.command()
def translate(
    doc_path: Path = typer.Argument(..., help="doc.json"),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="输出目录"),
    glossary: Path | None = typer.Option(None, "--glossary", help="术语表 JSON"),
    max_blocks: int = typer.Option(12, help="每切片最大块数"),
    max_chars: int = typer.Option(6000, help="每切片最大字符数"),
    limit: int = typer.Option(0, help="只翻前 N 块（0 = 全部，试翻/控成本用）"),
    resume: bool = typer.Option(False, "--resume", help="跳过已有译文的块（断点续跑，无人值守任务必需）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印将翻译什么、预估消耗，不调模型（防误烧钱）"),
    ids: str = typer.Option("", "--ids", help="只翻这些块（逗号分隔），用于定点重译（决策⑯）"),
) -> None:
    """分块翻译（标记穿透）→ 中文 HTML 并回填 doc.json。"""
    doc = _load_doc(doc_path)
    cfg = LLMConfig.from_env()
    gl = json.loads(glossary.read_text(encoding="utf-8")) if glossary else []
    tr = Translator(cfg, gl)

    ordered = Translator.ordered(doc.blocks)
    todo = [b for b in ordered
            if b.zh_source != "human" and expects_chinese(b.en, block_type=b.type)]
    # 待翻集合：--resume 跳过已有译文（断点续跑）；--limit 试翻只送前 N 块
    only: set[str] | None = None
    if ids:
        only = {x.strip() for x in ids.split(",") if x.strip()}
        typer.echo(f"⌖ 定点重译 {len(only)} 块（不管是否已有译文）")
    elif resume:
        only = {b.id for b in todo if not (b.zh or "").strip()}
        typer.echo(f"↻ 续跑：{len(todo) - len(only)} 块已有译文，跳过")
    if limit and not ids:
        only = set(b.id for b in todo[:limit]) if only is None else only & set(b.id for b in todo[:limit])
    run_blocks = doc.blocks if only is None else [b for b in doc.blocks if b.id in only]
    plan = [b for b in ordered if only is None or b.id in only]
    chars = sum(len(b.en) + len(b.payload.get("caption") or "") for b in plan)
    typer.echo(f"需翻译 {len(plan)} 块 / 共 {len(ordered)} 块（免中文块不送模型）；模型 {cfg.model}")
    typer.echo(f"   正文 {chars} 字符，粗估 ≈{chars * 2.6 / 1000:.0f}k tokens（含 prompt 开销）")
    if dry_run:
        typer.echo("   --dry-run：未调用模型，退出。首批块：" + ", ".join(b.id for b in plan[:10]))
        raise typer.Exit(0)
    if not (only if only is not None else todo):
        typer.echo("✅ 没有待翻译的块")
    texts = tr.translate_blocks(run_blocks, only=only, max_blocks=max_blocks, max_chars=max_chars)
    for b, tx in zip(Translator.ordered(run_blocks), texts):
        if tx and b.zh_source != "human":
            b.zh, b.zh_source = tx, "mt"
    if tr.needs_review:
        typer.echo(f"⚠️ 待校对 {len(tr.needs_review)} 块：{', '.join(tr.needs_review[:10])}")

    out.mkdir(parents=True, exist_ok=True)
    tag = ".trial" if limit else ""
    (out / f"doc{tag}.json").write_text(doc.to_json(), encoding="utf-8")
    (out / "zh.html").write_text(
        document_html(doc.blocks, lang="zh", title=doc.meta.get("title_zh", ""),
                      subtitle=doc.meta.get("title_en", ""), lang_attr="zh-CN"),
        encoding="utf-8",
    )
    (out / f"zh{tag}.html").write_text(
        document_html(doc.blocks, lang="zh", title=doc.meta.get("title_zh") or doc.meta.get("title_en", ""),
                      meta_line=doc.meta.get("authors", ""), lang_attr="zh-CN", marker=True),
        encoding="utf-8",
    )
    typer.echo(f"✅ 翻译完成，tokens≈{tr.tokens_used}")
    typer.echo(f"   产物：{out/f'doc{tag}.json'}  {out/f'zh{tag}.html'}")


@app.command()
def check(en: Path = typer.Argument(..., help="英文 HTML"), zh: Path = typer.Argument(..., help="中文 HTML")) -> None:
    """标记保真校验（纯程序）。"""
    report = validate(en.read_text(encoding="utf-8"), zh.read_text(encoding="utf-8"))
    typer.echo(report.summary())
    if report.blocks_to_retry():
        typer.echo(f"   需重译块：{', '.join(report.blocks_to_retry()[:12])}")
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def export(
    doc_path: Path = typer.Argument(..., help="doc.json"),
    out: Path = typer.Option(Path("out/full.html"), "--out", "-o"),
    mode: str = typer.Option("dual", help="dual | zh | en"),
) -> None:
    """由块级 JSON 合成单文件 HTML（导出/分享形态，决策⑳）。"""
    doc = _load_doc(doc_path)
    html = synth(doc.blocks, mode, title=doc.meta.get("title_zh") or doc.meta.get("title_en", ""),
                 meta_line=doc.meta.get("authors", ""))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    typer.echo(f"✅ 已合成（{mode}）：{out}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址（自托管对外部署用 0.0.0.0）"),
    port: int = typer.Option(8000, help="端口"),
    reload: bool = typer.Option(False, "--reload", help="开发热重载"),
) -> None:
    """启动服务端（决策⑨ 单体：API + 前端静态产物同进程）。"""
    import uvicorn

    uvicorn.run("papershelf.server.app:app", host=host, port=port, reload=reload)


@app.command("create-admin")
def create_admin(
    email: str = typer.Argument(..., help="管理员邮箱"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True,
                                 help="管理员密码（≥8 位）"),
) -> None:
    """创建/提升管理员（§7：`PAPERSHELF_ADMIN_EMAIL` 之外的第二条引导路径）。

    适用于容器已起、但当初没配 ADMIN_EMAIL 的场景——不用改环境变量重启。
    """
    from .server.config import get_settings
    from .server.db import connect, init_db
    from .server.security import ensure_admin

    settings = get_settings(refresh=True)
    init_db(settings)
    conn = connect(settings)
    try:
        user_id = ensure_admin(conn, email, password)
    finally:
        conn.close()
    typer.echo(f"✅ 管理员就绪：{email}（id={user_id}）")


@app.command("backfill-meta")
def backfill_meta(
    only_missing: bool = typer.Option(
        True, "--only-missing/--all",
        help="只补空字段（默认）／连已填的也重抽一遍"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印将要写什么，不落库"),
    limit: int = typer.Option(0, "--limit", help="最多处理几篇（0 = 全部）"),
    probe_timeout: float = typer.Option(15.0, "--probe-timeout", help="启动前的端连通性预检超时（秒）"),
) -> None:
    """回填存量文献的元数据（决策㉘ / ⑲）—— **不重跑翻译**。

    为什么要专门做这个：⑲ 修复前入库的文献，`title` 是上传时的文件名占位、
    `authors`/`venue`/`year`/`tags` 全空。重跑转换能把它们补上，但那是把
    **整篇译文重新烧一遍 token**（实测单篇 5.8 万–24.9 万）；
    而元数据只需要首屏十几块 —— 实测 **≈1.5k tokens/篇，便宜约 100 倍**。

    实现上完全**复用解析缓存**：指纹命中 `doc_cache` 时不必再解析 PDF，
    直接拿块跑 `MetadataExtractor`。所以本命令**不需要 LLM 以外的任何重活**。
    """
    from .pipeline import LLMConfig
    from .pipeline.metadata import MetadataExtractor
    from .server.config import get_settings
    from .server.converter import _writeback_meta
    from .server.db import connect, init_db
    from .server.repo import load_doc, save_doc

    settings = get_settings(refresh=True)
    init_db(settings)
    cfg = LLMConfig.from_env()
    if not cfg.api_key:
        typer.echo("❌ 未配置 LLM（PAPERSHELF_LLM_API_KEY）—— 元数据抽取需要它", err=True)
        raise typer.Exit(2)

    conn = connect(settings)
    try:
        # ⚠️ 先做一次**预检**：端点不通时必须在十几秒内失败退出，而不是每篇等 300s。
        # 踩过：把 `log` 写成 `lambda *a: None` 后，LLM 连接超时（TLS 握手）被静默吞掉，
        # 输出只剩一行似是而非的 `→ (无) | — | — | — | []`，看上去像"模型没抽到东西"，
        # 实则**一次都没连上**；12 篇会白等一小时（每篇都等到 300s 超时）。
        # 这里只查**连接层**（不校验状态码），用短超时把"端点死了"与"模型没抽到"分开。
        import httpx

        try:
            httpx.get(cfg.base_url, timeout=probe_timeout)
        except httpx.HTTPError as exc:
            typer.echo(f"❌ LLM 端点不可达（{type(exc).__name__}: {exc}）"
                       f"—— 已中止，未做任何修改（{probe_timeout:.0f}s 预检超时）", err=True)
            typer.echo(f"   端点：{cfg.base_url}", err=True)
            typer.echo("   确认网络与该地址可用后重试；也可用 --probe-timeout 放宽。", err=True)
            raise typer.Exit(2) from exc

        sql = "SELECT * FROM papers WHERE conv_state='done' ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
        todo = [p for p in rows
                if not (only_missing and not p.get("title_is_placeholder") and p.get("authors"))]
        typer.echo(f"共 {len(rows)} 篇已完成；本次处理 {len(todo)} 篇"
                   f"（{'dry-run，不落库' if dry_run else '写入'}）")

        def _log(*a: object) -> None:
            typer.echo("      ! " + " ".join(str(x) for x in a))

        def _candidates_for(paper: dict, doc) -> list[str] | None:
            """候选标题：优先用存量 `meta`，没有就**重解析 PDF 现场取**。

            ⚠️ 这一步是必需的，不是优化。⑲ 修复前入库的文献是被**旧版** `_finalize`
            处理的 —— 它取第一个 h1 当标题后把**其余 h1 全删**，而那些 h1 **没有存进 meta**
            （`title_candidates` 是随本次修复才有的）。于是存量块里**根本没有标题文本**，
            模型只能如实返回空串（它被明确要求不许编造）。
            实测：生产 3 篇的 `title_zh/title_en` 全是空，而作者/期刊/年份都抽到了。

            重解析只吃 CPU、**不花 token**（走同一个 `parse_pdf`，与转换路径同源）。
            只对"存量没有候选且 pdf 还在"的行做，正常路径零影响。
            """
            known = doc.meta.get("title_candidates")
            if known:
                return list(known)
            pdf_path = paper.get("pdf_path")
            if not pdf_path or not Path(pdf_path).exists():
                return None
            try:
                fresh = parse_pdf(pdf_path, assets_dir=settings.papers_dir / f"p{paper['id']}")
            except Exception as exc:                          # noqa: BLE001
                typer.echo(f"      ! 重解析失败，跳过候选恢复：{exc}")
                return None
            return fresh.meta.get("title_candidates") or None

        tokens = changed = failed = 0
        for p in todo:
            doc = load_doc(conn, p["id"])
            if doc is None:
                typer.echo(f"  · #{p['id']} 无解析产物（doc 缺失）→ 跳过")
                continue
            cands = _candidates_for(p, doc)
            mx = MetadataExtractor(cfg)
            got = mx.extract(doc.blocks, candidates=cands, log=_log)
            tokens += mx.tokens_used
            if not got:
                failed += 1
                typer.echo(f"  · #{p['id']} {p['title']!r} → ✗ 抽取失败（原因见上）")
                continue
            new_title = got.get("title_zh") or got.get("title_en") or "(无)"
            typer.echo(f"  · #{p['id']} {p['title']!r} → {new_title!r}"
                       f" | {got.get('authors') or '—'} | {got.get('venue') or '—'}"
                       f" | {got.get('year') or '—'} | {got.get('tags') or []}")
            if dry_run:
                continue
            # 块本身不动（译文已定稿），只更新 meta 与 papers 行：
            # `title_zh`/`title_en` 进 meta 供阅读器页眉与导出用，
            # 其余字段由 `_writeback_meta` 按「用户手填 > 自动抽取」写回。
            doc.meta.update(got)
            save_doc(conn, p["id"], doc)
            _writeback_meta(conn, p["id"], p, got, 0)
            changed += 1
        tail = f"，{failed} 篇抽取失败" if failed else ""
        typer.echo(f"\n✅ 完成：{changed} 篇更新{tail}，合计 {tokens} tokens"
                   f"（≈{tokens // max(changed + failed, 1)}/篇）")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
