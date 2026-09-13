"""⑲ 元数据抽取 + 写回 + 「占位标题锁死」回归（离线，不调 LLM、不联网）。

起因（2026-09-12 宿主报告）：生产文献库 12 篇**全叫 `original`**、副标题恒
「未提取到作者」、发表/年份/标签全空。查下来是三个真缺陷叠加：

1. **上传把文件名写成标题，且这个值再也改不掉**：上传时 `papers.title = Path(name).stem`，
   转换完成的写回是 `COALESCE(NULLIF(title,''), ?)` —— 占位值非空 ⇒ `COALESCE` 取旧值
   ⇒ **LLM 抽到的真标题永远写不进去**（本文件 ① ② 两条）。
2. **⑲「LLM 自动抽取元数据/标签」一行都没实现**：`pipeline/` 里只有 `title_en` 等赋值，
   `converter.py` 里那几行 `meta.get("authors")` **有消费端、无生产端**（③ ④）。
3. **`parse._finalize` 取第一个 h1 当标题后把其余 h1 全删** —— 封面两个 h1
   （期刊名在前、真标题在后）时真标题被**静默销毁**，连补抽都无从补起；
   实测 12 篇里 1/3 篇如此（⑤）。
"""

from __future__ import annotations

import json

import pytest

from papershelf.pipeline.metadata import (_as_tags, _as_year, _first_object, _parse_json,
                                          MetadataExtractor)
from papershelf.pipeline.model import Block, Doc
from papershelf.pipeline.translator import LLMConfig

CFG = LLMConfig(base_url="http://x", api_key="k", model="m")


# ── 夹具：papers 行（`plan_id` 有外键，必须真插一行 plan）────────────────

def _seed_paper(settings, **cols) -> int:
    """插一行 papers，返回 id。

    默认值就是**生产里的病灶形态**：标题 = PDF 文件名 `original`，
    且 `pdf_path` 带落盘时间戳前缀（正是让"靠文件名推导占位符"失效的那个形状）。
    `title_is_placeholder` 默认 1（导入时的占位值）；用户手改过标题的行应传 0。
    """
    from papershelf.server.db import connect

    row = {"title": "original", "title_is_placeholder": 1,
           "pdf_path": "/data/papers/2026-09-11T143352+0000_original.pdf"}
    row.update(cols)
    email = row.pop("email", "u@x.edu.cn")
    conn = connect(settings)
    try:
        conn.execute("INSERT OR IGNORE INTO users (email,password_hash,status,created_at) "
                     "VALUES (?,'h','active',datetime('now'))", (email,))
        uid = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        conn.execute("INSERT INTO plans (user_id,name,created_at) VALUES (?,?,datetime('now'))",
                     (uid, "计划"))
        plan_id = conn.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()["id"]
        cur = conn.execute(
            f"INSERT INTO papers (plan_id, source, conv_state, status, created_at, updated_at, "
            f"{', '.join(row)}) "
            f"VALUES (?, 'upload', 'doing', 'unread', datetime('now'), datetime('now'), "
            f"{', '.join('?' * len(row))})",
            (plan_id, *row.values()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _row(settings, pid: int):
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()


# ── ① 占位标题必须能被抽到的真标题覆盖（生产「12 篇全叫 original」的修复点）──

def test_placeholder_title_is_overwritten_by_extracted_title(settings):
    from papershelf.server.converter import _writeback_meta

    pid = _seed_paper(settings)
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)),
                        {"title_en": "Toward closed-loop quality assurance",
                         "title_zh": "面向闭环质量保证的增材制造质量保障"}, tokens=10)
    finally:
        conn.close()

    row = _row(settings, pid)
    assert row["title"] == "面向闭环质量保证的增材制造质量保障"   # 中文标题优先


def test_title_falls_back_to_english_when_no_chinese(settings):
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)),
                        {"title_en": "Sensor-integrated data acquisition"}, tokens=3)
    finally:
        conn.close()
    assert _row(settings, pid)["title"] == "Sensor-integrated data acquisition"


# ── ② 用户手填的标题绝不被覆盖（⑲「可手动改」）──────────────────────────

def test_user_edited_title_is_never_overwritten(settings):
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings, title="我自己起的中文标题", title_is_placeholder=0)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)),
                        {"title_en": "Machine-extracted", "title_zh": "机器抽的"}, tokens=10)
    finally:
        conn.close()
    assert _row(settings, pid)["title"] == "我自己起的中文标题"


def test_placeholder_flag_beats_pdf_path_guess(settings):
    """**这条钉住的是那个更深的坑**：`pdf_path` 的名字被改过，只有 `source_ref` 还认得出。

    生产 12 篇的 `pdf_path` 是 `2026-09-11T143352+0000_original.pdf`，
    `Path(...).stem` = `2026-09-11T143352+0000_original` ≠ `title='original'` ——
    只比 `pdf_path` 的判据会把它们全判成"用户手填"，真标题永远写不进去。
    `source_ref` 保存的是**原始文件名**，所以兜底必须先比它。

    但兜底终究只是兜底：真判据是 `title_is_placeholder` 列 ——
    `source_ref` 也可能为空（老数据/别的导入路径），那时兜底就抓瞎了。
    """
    from papershelf.server.converter import _is_placeholder_title

    prod_like = {"title": "original", "title_is_placeholder": 1,
                 "pdf_path": "/data/papers/2026-09-11T143352+0000_original.pdf",
                 "source_ref": "original.pdf"}
    assert _is_placeholder_title(prod_like) is True

    # 缺列时兜底能靠 source_ref 认出来
    no_col = {k: v for k, v in prod_like.items() if k != "title_is_placeholder"}
    assert _is_placeholder_title(no_col) is True
    # 但 source_ref 也没了（`pdf_path` 名字被改过）→ 兜底抓瞎，保守不动
    assert _is_placeholder_title({k: v for k, v in no_col.items() if k != "source_ref"}) is False


def test_placeholder_detection_edge_cases():
    """兜底判据本身（只在**缺列**时走到）：空标题可填；与文件名 stem 一致算占位。

    ⚠️ 兜底判据已知不可靠，这里只钉住它"别炸、别乱覆盖"：
    `Path("2603.19455").stem` 是 `"2603"`（`.` 被当后缀分隔符），
    arXiv 导入的 `title='arXiv:2603.19455'` 与它也不相等 —— 所以 arXiv 行**只能靠列**。
    """
    from papershelf.server.converter import _is_placeholder_title

    assert _is_placeholder_title({"title": ""}) is True
    assert _is_placeholder_title({"title": "a", "pdf_path": "/x/a.pdf"}) is True
    assert _is_placeholder_title({"title": "真标题", "pdf_path": "/x/a.pdf"}) is False
    # 判不出来时**保守**：不覆盖（宁可留占位符，不能吃掉用户手填）
    assert _is_placeholder_title({"title": "arXiv:2603.19455", "source_ref": "2603.19455"}) is False
    assert _is_placeholder_title({"title": "原样标题", "pdf_path": ""}) is False


def test_migration_backfills_placeholder_flag_conservatively(settings):
    """迁移只把"标题确实等于文件名 stem"的存量行标回 1，其余一律 0（不覆盖）。

    生产那 12 篇（`pdf_path` 带时间戳前缀）就会落在 0 一侧 —— 这是**有意为之**：
    宁可让用户手动点一次，也不能擅自覆盖他可能手填过的标题。
    """
    from papershelf.server.db import _migrate, connect

    same = _seed_paper(settings, title="paper", pdf_path="/x/paper.pdf")
    different = _seed_paper(settings, title="用户起的", pdf_path="/x/paper.pdf",
                            email="u2@x.edu.cn")
    conn = connect(settings)
    try:
        # 模拟**真正的旧库**（列还不存在）——必须真删列，否则 `_migrate` 的
        # `if 列不存在` 守卫会让整段跳过（它是有意的幂等，不是缺陷）。
        conn.execute("ALTER TABLE papers DROP COLUMN title_is_placeholder")
        conn.commit()
        assert "title_is_placeholder" not in {
            r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
        _migrate(conn)
        got = {r["id"]: r["title_is_placeholder"]
               for r in conn.execute("SELECT id,title_is_placeholder FROM papers")}
    finally:
        conn.close()
    assert got[same] == 1
    assert got[different] == 0


def test_migration_flags_production_shaped_rows(settings):
    """**生产那 12 篇的形状**在迁移后必须被标回 1 —— 否则真标题永远写不进去。

    形状：`title='original'`、`source_ref='original.pdf'`、
    `pdf_path='..._2026-09-11T143352+0000_original.pdf'`（带时间戳前缀）。
    判据必须靠 `source_ref` 才认得出来。
    """
    from papershelf.server.db import _migrate, connect

    pid = _seed_paper(settings, title_is_placeholder=0,   # 先按"非占位"存
                      title="original", source_ref="original.pdf",
                      pdf_path="/data/papers/2026-09-11T143352+0000_original.pdf")
    conn = connect(settings)
    try:
        conn.execute("ALTER TABLE papers DROP COLUMN title_is_placeholder")
        conn.commit()
        _migrate(conn)
        got = conn.execute("SELECT title_is_placeholder FROM papers WHERE id=?",
                           (pid,)).fetchone()["title_is_placeholder"]
    finally:
        conn.close()
    assert got == 1


# ── ③ 作者/期刊/年份/标签：只在为空时写入，用户填的优先 ─────────────────

def test_meta_writeback_fills_only_empty_fields(settings):
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings, authors="用户填的作者", venue="", year=2020,
                      tags='["用户标签"]')
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)),
                        {"authors": "抽取的作者", "venue": "Precision Engineering",
                         "year": 2025, "tags": ["抽取标签", "增材制造"]}, tokens=7)
    finally:
        conn.close()

    row = _row(settings, pid)
    assert row["authors"] == "用户填的作者"            # 已填 → 不覆盖
    assert row["venue"] == "Precision Engineering"     # 空位 → 填上
    assert row["year"] == 2020                          # 已填 → 不覆盖
    assert json.loads(row["tags"]) == ["用户标签"]      # 已填 → 不覆盖


def test_meta_writeback_fills_authors_venue_year_tags(settings):
    """全部为空时四个字段都要落库（生产 12 篇正是这种形态）。"""
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)),
                        {"authors": "张三 · 李四", "venue": "Precision Engineering",
                         "year": 2025, "tags": ["增材制造", "缺陷检测"]}, tokens=9)
    finally:
        conn.close()

    row = _row(settings, pid)
    assert (row["authors"], row["venue"], row["year"]) == ("张三 · 李四", "Precision Engineering", 2025)
    assert json.loads(row["tags"]) == ["增材制造", "缺陷检测"]


def test_meta_writeback_sets_done_and_counts_tokens(settings):
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)), {"title_en": "T"}, tokens=123)
    finally:
        conn.close()
    row = _row(settings, pid)
    assert row["conv_state"] == "done"
    assert row["conv_error"] is None
    assert row["tokens_used"] == 123


def test_meta_writeback_marks_title_confirmed(settings):
    """写进抽到的真标题后，**占位标记必须一起撤销**（宿主 2026-09-13 口径：回填成功 = 已确认）。

    原先只改 `title` 不改标记 —— 列在说谎，代价是生产实测得出来的：
    `backfill-meta --only-missing` 的判据是「占位=1 且没作者」，所以已回填的 12 篇
    每跑一次都会被当成"仍缺元数据"再抽一遍（≈1.6k tokens/篇，白烧）。
    """
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)),
                        {"title_zh": "面向闭环质量保证的粉末熔融增材制造", "authors": "张三"},
                        tokens=7)
        row = conn.execute("SELECT title, title_is_placeholder FROM papers WHERE id=?",
                           (pid,)).fetchone()
        assert row["title"] == "面向闭环质量保证的粉末熔融增材制造"
        assert row["title_is_placeholder"] == 0, "写了真标题却没撤占位标记（列在说谎）"

        # 确认之后重跑转换**不得**再覆盖：保护范围从"用户手填"扩到"已确认"（刻意）
        _writeback_meta(conn, pid,
                        dict(conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()),
                        {"title_en": "Another Title"}, tokens=7)
        assert conn.execute("SELECT title FROM papers WHERE id=?",
                            (pid,)).fetchone()["title"] == "面向闭环质量保证的粉末熔融增材制造"
    finally:
        conn.close()


def test_meta_writeback_with_empty_meta_only_marks_done(settings):
    """抽取失败（`{}`）时**不得**把标题清空或写入空串，也**不得**撤掉占位标记。"""
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    pid = _seed_paper(settings)
    conn = connect(settings)
    try:
        _writeback_meta(conn, pid, dict(_row(settings, pid)), {}, tokens=0)
    finally:
        conn.close()
    row = _row(settings, pid)
    assert row["title"] == "original"                  # 原样，没被清空
    assert row["authors"] is None and row["year"] is None
    # 标记留在 1 才有"下次再补"的机会 —— 没抽到就撤掉的话这篇永远补不上
    assert row["title_is_placeholder"] == 1


# ── ④ 端到端：上传占位 → 抽取覆盖 → 手改后不再被覆盖 ────────────────────

def test_placeholder_flag_lifecycle_through_api(client, settings, make_user):
    """走真实 HTTP：上传的文件名被标占位 → 抽取能覆盖 → 用户一改就锁死。"""
    from papershelf.server.converter import _writeback_meta
    from papershelf.server.db import connect

    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]

    import io
    up = client.post(f"/api/plans/{plan_id}/papers/upload",
                     files={"files": ("original.pdf", io.BytesIO(b"%PDF-1.4\n%fake"), "application/pdf")},
                     data={"background_convert": "false"})
    assert up.status_code == 201, up.text
    pid = up.json()[0]["id"]

    conn = connect(settings)
    try:
        assert conn.execute("SELECT title_is_placeholder FROM papers WHERE id=?",
                            (pid,)).fetchone()["title_is_placeholder"] == 1
        _writeback_meta(conn, pid, dict(conn.execute("SELECT * FROM papers WHERE id=?",
                                                     (pid,)).fetchone()),
                        {"title_en": "Toward closed-loop", "title_zh": "面向闭环"}, tokens=5)
        assert conn.execute("SELECT title FROM papers WHERE id=?", (pid,)).fetchone()["title"] \
            == "面向闭环"
    finally:
        conn.close()

    # 用户手动改名 → 标记撤销 → 再跑一次转换也覆盖不了
    r = client.patch(f"/api/papers/{pid}", json={"title": "我自己起的名字"})
    assert r.status_code == 200 and r.json()["title"] == "我自己起的名字"
    conn = connect(settings)
    try:
        assert conn.execute("SELECT title_is_placeholder FROM papers WHERE id=?",
                            (pid,)).fetchone()["title_is_placeholder"] == 0
        _writeback_meta(conn, pid, dict(conn.execute("SELECT * FROM papers WHERE id=?",
                                                     (pid,)).fetchone()),
                        {"title_en": "Machine title", "title_zh": "机器抽的"}, tokens=5)
    finally:
        conn.close()
    assert client.get(f"/api/plans/{plan_id}/papers").json()[0]["title"] == "我自己起的名字"


# ── ⑤ 解析阶段必须留下全部标题候选（真标题被销毁的直接回归）──────────────

def test_finalize_keeps_all_title_candidates():
    """`_finalize` 取第一个 h1 当标题，但**其余候选必须留在 meta 里**。

    ⚠️ 布局取自生产实测（第 1 篇 / 第 3 篇）：期刊名是 h1、真标题是**另一个 h1**，
    两者之间隔着作者行等块。原先「取第一个 + 删所有 h1」把真标题销毁。
    """
    from papershelf.pipeline.parse import _finalize

    doc = Doc(meta={})
    doc.add("h1", "Journal Manufacturing Processes")
    doc.add("p", "张三, 李四")
    doc.add("h1", "Toward closed-loop quality assurance in powder bed fusion")
    doc.add("p", "正文")
    out = _finalize(doc)

    assert out.meta["title_candidates"] == [
        "Journal Manufacturing Processes",
        "Toward closed-loop quality assurance in powder bed fusion",
    ]
    # 既有的保守默认不变（仍取第一个）——保证零回归；谁对由 LLM 在候选里挑
    assert out.meta["title_en"] == "Journal Manufacturing Processes"
    # 块照旧删除：它们已进 meta，留在正文会让阅读器重复显示标题
    assert not [b for b in out.blocks if b.type == "h1"]


def test_finalize_merges_wrapped_title_lines():
    """被 PDF 拆成多行的同一个标题仍要合并（原有行为，别被候选收集破坏）。"""
    from papershelf.pipeline.parse import _finalize

    doc = Doc(meta={})
    doc.add("h1", "An Overview on Safety-Critical Control Under")
    doc.add("h1", "Disturbances: A Control Barrier")
    doc.add("h1", "Function Approach")
    out = _finalize(doc)

    assert out.meta["title_candidates"] == [
        "An Overview on Safety-Critical Control Under Disturbances: A Control Barrier Function Approach"
    ]


def test_finalize_tolerates_no_title():
    from papershelf.pipeline.parse import _finalize

    doc = Doc(meta={})
    doc.add("p", "没有标题的一页")
    out = _finalize(doc)
    assert "title_candidates" not in out.meta
    assert "title_en" not in out.meta


# ── ⑥ LLM 输出的容错解析（真模型会加围栏、加解说、给错类型）────────────

@pytest.mark.parametrize("raw,expect", [
    ('{"title_en":"T"}', {"title_en": "T"}),
    ('```json\n{"title_en":"T"}\n```', {"title_en": "T"}),            # markdown 围栏
    ('好的，结果如下：\n{"title_en":"T"}\n以上。', {"title_en": "T"}),  # 夹带解说
    ('not json at all', None),
    ('[1,2,3]', None),                                                # 不是对象
    ('', None),
])
def test_parse_json_is_tolerant(raw, expect):
    assert _parse_json(raw) == expect


def test_first_object_handles_nested_braces():
    assert _first_object('前言 {"a":{"b":1}} 后记') == '{"a":{"b":1}}'
    assert _first_object('没有花括号') == ""


def test_year_must_be_int_like():
    """`papers.year` 是 INTEGER 列 —— 喂字符串会静默变 0。"""
    assert _as_year(2024) == 2024
    assert _as_year("2024年") == 2024            # 从串里抠
    assert _as_year("Precision Engineering 95 (2025)") == 2025
    assert _as_year("n.d.") is None
    assert _as_year(1234) is None                 # 超出合理区间 → 丢弃而不是照存
    assert _as_year(None) is None
    assert _as_year(True) is None                 # bool 是 int 子类，必须先挡


def test_tags_normalization():
    assert _as_tags(["增材制造", " 缺陷检测 "]) == ["增材制造", "缺陷检测"]
    assert _as_tags("a，b、c") == ["a", "b", "c"]        # 中文分隔符
    assert _as_tags(["#CBF", "CBF"]) == ["CBF"]          # 去重 + 去井号
    assert _as_tags(None) == []
    assert _as_tags(["x", 1, None]) == ["x"]             # 非字符串项丢弃
    assert len(_as_tags([f"t{i}" for i in range(20)])) == 5   # 上限 5


# ── ⑦ 抽取器：失败不得炸整篇 ────────────────────────────────────────────

class _Boom(MetadataExtractor):
    def _chat(self, user: str) -> str:      # type: ignore[override]
        raise RuntimeError("LLM 挂了")


def test_extract_swallows_llm_failure():
    """元数据是锦上添花；抽取失败必须返回 `{}` 而不是把整篇转换打成 failed。"""
    doc = Doc(meta={})
    doc.add("p", "Some English body text here.")
    assert _Boom(CFG).extract(doc.blocks, log=lambda *a: None) == {}


def test_extract_swallows_garbage_json():
    class _Junk(_Boom):
        def _chat(self, user: str) -> str:  # type: ignore[override]
            return "抱歉，我无法从这些内容判断。"

    doc = Doc(meta={})
    doc.add("p", "Some English body text here.")
    assert _Junk(CFG).extract(doc.blocks, log=lambda *a: None) == {}


def test_extract_skips_empty_input():
    assert MetadataExtractor(CFG).extract([], log=lambda *a: None) == {}
    # 但给了候选标题时仍要问一次（正文块全是图/公式的情形）
    assert MetadataExtractor(CFG).head_blocks([]) == []


def test_extract_validates_and_normalizes():
    class _Fake(MetadataExtractor):
        def _chat(self, user: str) -> str:  # type: ignore[override]
            return json.dumps({
                "title_en": "  A Real Title  ", "title_zh": "真标题",
                "authors": "张三", "venue": "", "year": "2025年",
                "tags": "增材制造, 缺陷检测, 增材制造",
                "evil": "被丢弃",
            })

    doc = Doc(meta={})
    doc.add("p", "Precision Engineering 95 (2025) 163-187")
    got = _Fake(CFG).extract(doc.blocks, log=lambda *a: None)
    assert got == {"title_en": "A Real Title", "title_zh": "真标题", "authors": "张三",
                   "year": 2025, "tags": ["增材制造", "缺陷检测"]}   # venue 空串不写入


def test_extract_drops_bogus_year_but_keeps_rest():
    class _Fake(MetadataExtractor):
        def _chat(self, user: str) -> str:  # type: ignore[override]
            return '{"title_en":"T","year":"n.d."}'

    doc = Doc(meta={})
    doc.add("p", "body")
    got = _Fake(CFG).extract(doc.blocks, log=lambda *a: None)
    assert got == {"title_en": "T"}       # year 报错也不许污染 title


def test_head_blocks_skips_figures_equations_and_references():
    blocks = [
        Block(id="b-0001", type="p", en="Journal of Something 2024"),
        Block(id="b-0002", type="figure", en="", payload={"caption": "Fig 1"}),
        Block(id="b-0003", type="eq", en="E = mc^2"),
        Block(id="b-0004", type="h2", en="Authors Here"),
        Block(id="b-0005", type="refs", en="[1] Someone 2020"),
    ]
    assert [b.id for b in MetadataExtractor.head_blocks(blocks)] == ["b-0001", "b-0004"]


def test_head_blocks_respects_char_and_count_caps():
    blocks = [Block(id=f"b-{i:04d}", type="p", en="x" * 1000) for i in range(1, 30)]
    got = MetadataExtractor.head_blocks(blocks)
    assert len(got) <= 14
    assert sum(len(b.en) for b in got) <= 4000 + 1000


def test_prompt_includes_title_candidates():
    """候选标题必须进 prompt —— 否则模型只能从残块里**编**。

    实测真发生过：第 3 篇真标题 `Sensor-integrated data acquisition ...`
    被 `_finalize` 删掉后，模型凭摘要编出了含 "A Review" 的标题（真标题里没有）。"""
    doc = Doc(meta={})
    doc.add("p", "Precision Engineering 95 (2025) 163-187")
    p = MetadataExtractor(CFG)._prompt(
        MetadataExtractor.head_blocks(doc.blocks),
        ["Precision Engineering", "Sensor-integrated data acquisition"])
    assert "候选标题" in p
    assert "Sensor-integrated data acquisition" in p
    assert "第一个往往是期刊名" in p            # 把「期刊名在最前」的坑写进提示
