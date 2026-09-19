"""SQLite 访问层 —— schema 严格对齐 docs/design.md §3.1。

只做三件事：连接（含 PRAGMA）、建表、事务辅助。业务读写分散在各 `*_repo` 函数里，
但**表结构只有一个来源**（本文件的 DDL），避免实现与文档漂移。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import SHARE_MAX_HOURS, Settings
from .titlemeta import is_placeholder_title

log = logging.getLogger("papershelf.db")

# `b-0010:c0` —— ㉚ 的句锚点（只用于存量迁移，见 `_resolve_sid_span`）。
_SID_RE = re.compile(r"^(.+):([ce])(\d+)$")

# 划痕（㉛）建表 DDL **单独放一份**：迁移要 `DROP` 再按新形状重建，
# 若把 DDL 抄成两份，"重建出来的表"与"新建库的表"迟早会长得不一样。
# 用法：`SCHEMA` 里留一个 `__HIGHLIGHTS_DDL__` 占位符，末尾统一替换（见 `SCHEMA` 定义处）。
# ⚠️ 这个常量必须**是 DDL 本身**（迁移直接 `executescript(HIGHLIGHTS_DDL)`）——
# 曾把它写成只含占位符的字符串，于是迁移一到真数据就 `near "__HIGHLIGHTS_DDL__": syntax error`。
HIGHLIGHTS_DDL = """
CREATE TABLE IF NOT EXISTS highlights (
  id         INTEGER PRIMARY KEY,
  paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  block_id   TEXT NOT NULL,
  lang       TEXT NOT NULL,
  start      INTEGER NOT NULL,
  end        INTEGER NOT NULL,
  color      TEXT NOT NULL DEFAULT 'amber',
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hl_paper ON highlights(paper_id);
"""

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending | active | disabled
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  activated_at  TEXT,
  -- 注册协议（2026-09-11 宿主指示：「所有责任在用户」）：
  -- 版本号 = 用户勾选时生效的条款版本，条款改版后据此判断是否需要重新确认。
  consent_version TEXT,
  consent_at      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

-- 邮箱验证码（⑭）：注册与忘记密码都靠它。**只存哈希**，明文只在发信时存在一次。
-- `purpose` 区分用途（register | reset），两者互不通用。
CREATE TABLE IF NOT EXISTS verification_codes (
  email      TEXT NOT NULL,
  purpose    TEXT NOT NULL,                      -- register | reset
  code_hash  TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,         -- 校验失败次数，超限即作废
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (email, purpose)
);

CREATE TABLE IF NOT EXISTS plans (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  goal        INTEGER,
  description TEXT,
  created_at  TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_user ON plans(user_id);

CREATE TABLE IF NOT EXISTS plan_settings (
  plan_id  INTEGER PRIMARY KEY REFERENCES plans(id) ON DELETE CASCADE,
  glossary TEXT                                    -- JSON: [{en, zh, note}]（⑫）
);

CREATE TABLE IF NOT EXISTS papers (
  id            INTEGER PRIMARY KEY,
  plan_id       INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  title         TEXT,
  authors       TEXT,
  venue         TEXT,
  year          INTEGER,
  tags          TEXT,                              -- JSON 数组（⑲ 自由标签）
  source        TEXT NOT NULL DEFAULT 'upload',    -- upload | arxiv（⑱）
  source_ref    TEXT,
  pdf_path      TEXT,
  arxiv_html    INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'unread',    -- unread|reading|read|reviewed（⑰）
  status_at     TEXT,
  progress      INTEGER NOT NULL DEFAULT 0,        -- 0-100（⑰）
  progress_mode TEXT NOT NULL DEFAULT 'auto',      -- auto | manual（⑰ 已读锁定）
  -- 「最近阅读」（⑰ 补充，2026-09-15）：**只有阅读器滚动上报进度时才写**。
  -- 为什么不复用 `updated_at`：任何 PATCH 都会刷新它（改标题、改标签、拖看板…），
  -- 那记的是"最近一次改动"而不是"最近一次阅读"，两个字段会被混成一句话。
  last_read_at  TEXT,
  conv_state    TEXT NOT NULL DEFAULT 'none',      -- none|queued|doing|done|failed（②）
  conv_error    TEXT,
  -- 本次排队**要跑什么**（㊹ 修订的存量出口，2026-09-19）：NULL/`convert` = 完整转换，
  -- `refs` = 只重建文末参考文献（`server/refsfix.py`）。它与 `conv_state` 是两件事：
  -- 后者回答"轮到谁了"，前者回答"轮到它时做什么"。跑完即清空（见 `converter._set_state`），
  -- 所以它**不会**残留成"下次转换被误当成修文献"。
  pending_job   TEXT,
  conv_attempts INTEGER NOT NULL DEFAULT 0,
  tokens_used   INTEGER NOT NULL DEFAULT 0,        -- 用量可见性（待定项 6）
  -- 标题是不是「导入时随手写下、用户从未改过」的占位值（⑲ + 2026-09-12 修复）。
  -- 单独存列而不是现场推导：`pdf_path` 被落盘时加了时间戳前缀
  -- （`2026-09-11T143352+0000_original.pdf`），文件名 stem 与 `title` **再也不相等**，
  -- 任何"拿 pdf_path 猜"的判据都会把占位符当成用户手填值（生产 12 篇全部中招）。
  -- 语义：1 = 可被自动抽取的真标题覆盖；0 = 用户已明确命名，谁都不许覆盖。
  title_is_placeholder INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_plan ON papers(plan_id);
CREATE INDEX IF NOT EXISTS idx_papers_state ON papers(conv_state);

CREATE TABLE IF NOT EXISTS docs (
  paper_id    INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  meta        TEXT,
  assets      TEXT,
  block_count INTEGER,
  version     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT,
  updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS blocks (
  id        TEXT NOT NULL,
  paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  ord       INTEGER NOT NULL,
  type      TEXT NOT NULL,
  level     INTEGER,
  section   TEXT,
  en        TEXT,
  zh        TEXT,
  zh_source TEXT NOT NULL DEFAULT 'none',          -- mt | human | none（⑯）
  payload   TEXT,
  PRIMARY KEY (paper_id, id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_paper ON blocks(paper_id, ord);

CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY,
  paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  block_id   TEXT,
  content    TEXT NOT NULL,
  created_at TEXT,
  sid        TEXT,                                  -- 遗留：㉚ 的句锚点，㉛ 起改用下面的区间
  quote      TEXT,                                  -- 选区原文摘录（列表里回显）
  lang       TEXT,                                  -- ㉛ 锚定语言：en | zh
  start      INTEGER,                               -- ㉛ 选区起点（**裸文本**字符偏移）
  end        INTEGER,                               -- ㉛ 选区终点（不含）
  hl_id      INTEGER                                -- ㉛ 若是"给某条划痕写笔记"，指向它
);
CREATE INDEX IF NOT EXISTS idx_notes_paper ON notes(paper_id);

-- 划痕（决策㉛）：高亮的单位是**任意字符区间**，不再是句子。
-- 坐标是块**裸文本**（`blocks.en` / `blocks.zh`）的字符偏移 `[start, end)`，
-- 与 `markup.prose_html` 渲染时用的坐标同一套 —— 所以划痕在重跑管线后依然对得上
-- （译文若被改过，偏移会漂；这是区间锚点的固有代价，与块级笔记同风险，不做映射）。
-- `lang` 必填：中英两侧各划各的（原文与译文没有字级对应，不该连着亮）。
__HIGHLIGHTS_DDL__

-- 只读分享（⑩⑪）。有效期**上限 24 小时**（宿主 2026-09-11 指示）：
-- `expires_at` 一律非空，创建时由 `expires_in(hours)` 写入；永不过期的链接不再可能产生。
-- `label` / `hours` 为「分享管理」（2026-09-12）新增：前者是给链接起的备注（"给导师看"），
-- 后者是**这次续期实际用了多少小时** —— 从时间戳反推会在续期后失真
-- （`created_at` 不动、`expires_at` 往后推，"有效时长"会算成一个越来越大的鬼数字）。
CREATE TABLE IF NOT EXISTS shares (
  token      TEXT PRIMARY KEY,
  plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  label      TEXT,
  hours      INTEGER
);

-- 缓存去重（待定项 7）：同一份 PDF 解析结果复用，只重放翻译
CREATE TABLE IF NOT EXISTS doc_cache (
  fingerprint TEXT PRIMARY KEY,      -- PDF 内容 hash
  doc_json    TEXT NOT NULL,
  created_at  TEXT
);
""".replace("__HIGHLIGHTS_DDL__", HIGHLIGHTS_DDL)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_highlights_to_ranges(conn: sqlite3.Connection) -> None:
    """㉚ 的**句锚点** → ㉛ 的**字符区间**（2026-09-13）。

    为什么必须换算而不是丢弃：生产上已经有真数据（12 号文献 4 条）。而
    「句序号 → 字符区间」是**确定性**的（块 ID 已经落在 sid 里、语言也落在里面、
    切句规则没变），换算得出来就不该让用户重划一遍。

    换算出区间的前提是**块还在**（`blocks` 表里有这个块 ID）。解析不出来的行
    （块被重解析换掉了 / sid 格式不是本仓库那一套）只能**丢弃并如实记数** ——
    留着一行没有坐标的高亮，前端只会渲不出来，还挡住"这张表到底还有多少条"的判断。
    """
    from ..pipeline.markup import DEFAULT_HL_COLOR   # 存量划痕给默认笔色（不带含义）

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(highlights)")}
    if not cols or "lang" in cols:
        return                            # 新库（建表就是新形状）或已迁移过
    if "sid" not in cols:
        return
    old = conn.execute("SELECT paper_id, sid, created_at FROM highlights").fetchall()
    conn.execute("DROP TABLE highlights")
    conn.executescript(HIGHLIGHTS_DDL)
    kept = dropped = 0
    for r in old:
        span = _resolve_sid_span(conn, r["paper_id"], r["sid"])
        if span is None:
            dropped += 1
            continue
        block_id, lang, start, end = span
        conn.execute(
            "INSERT INTO highlights (paper_id, block_id, lang, start, end, color, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (r["paper_id"], block_id, lang, start, end, DEFAULT_HL_COLOR, r["created_at"]),
        )
        kept += 1
    if old:
        log.info("highlights 迁移：%s 条句锚点 → 字符区间（%s 条无法解析已丢弃）",
                 kept + dropped, dropped)
    # 笔记也带上句锚点（㉚ 的路径写过 `sid`），同理换算；0 行的库这里静默跳过。
    for r in conn.execute("SELECT id, paper_id, sid FROM notes WHERE sid IS NOT NULL"
                          " AND (start IS NULL OR end IS NULL)").fetchall():
        span = _resolve_sid_span(conn, r["paper_id"], r["sid"])
        if span is None:
            continue
        block_id, lang, start, end = span
        conn.execute("UPDATE notes SET block_id=?, lang=?, start=?, end=? WHERE id=?",
                     (block_id, lang, start, end, r["id"]))


def _resolve_sid_span(conn: sqlite3.Connection, paper_id: int,
                      sid: str) -> tuple[str, str, int, int] | None:
    """`b-0010:c0` → `(block_id, lang, start, end)`；解析不出来返回 None。"""
    from ..pipeline.markup import sentence_offsets

    m = _SID_RE.match(sid or "")
    if not m:
        return None
    block_id, lang = m.group(1), ("zh" if m.group(2) == "c" else "en")
    idx = int(m.group(3))
    row = conn.execute("SELECT en, zh FROM blocks WHERE paper_id=? AND id=?",
                       (paper_id, block_id)).fetchone()
    if row is None:
        return None
    text = (row["zh"] if lang == "zh" else row["en"]) or ""
    spans = sentence_offsets(text, lang)
    if idx >= len(spans):
        return None
    start, end = spans[idx]
    # 切句把**分隔空白**算进了后一句（`... hard.` / ` It is also hard.`）。
    # 存成划痕前把两端空白去掉：否则高亮会从行首那个空格开始，
    # 颜色看着没错，但"这道划痕的起点到底在哪"会让人算不明白。
    chunk = text[start:end]
    start, end = start + (len(chunk) - len(chunk.lstrip())), end - (len(chunk) - len(chunk.rstrip()))
    if end <= start:                       # 整句都是空白 → 没有可划的东西
        return None
    return block_id, lang, start, end


def _migrate(conn: sqlite3.Connection) -> None:
    """增量迁移 —— `CREATE TABLE IF NOT EXISTS` 不会给**已存在**的旧表补列。

    本函数必须幂等：每次启动都跑。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for name in ("consent_version", "consent_at"):
        if name not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
    # 分享管理（2026-09-12）新增两列。存量链接的 `hours` 留空：
    # 它是"这次续期用了多少小时"，旧数据的原始意图已不可考，
    # 宁可在管理页显示成「未记录」也不编一个数字出来。
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(shares)")}
    for name, ddl in (("label", "TEXT"), ("hours", "INTEGER")):
        if name not in scols:
            conn.execute(f"ALTER TABLE shares ADD COLUMN {name} {ddl}")
    # 存量永久分享链接（`expires_at IS NULL`）按新规收敛：从**现在**起 24 小时。
    # 给宽限而不是立即作废 —— 已经发出去的链接立刻全废会让访客撞 410，体验是"链接坏了"。
    conn.execute(
        "UPDATE shares SET expires_at=? WHERE expires_at IS NULL",
        ((datetime.now(timezone.utc) + timedelta(hours=SHARE_MAX_HOURS)).isoformat(timespec="seconds"),),
    )
    # 句子级高亮/笔记（决策㉚，2026-09-13）：老库的 notes 表没有 `sid` / `quote`。
    ncols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
    for name, ddl in (("sid", "TEXT"), ("quote", "TEXT"),
                      # ㉛（2026-09-13）：锚点从「句」换成「字符区间」
                      ("lang", "TEXT"), ("start", "INTEGER"), ("end", "INTEGER"),
                      ("hl_id", "INTEGER")):
        if name not in ncols:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {name} {ddl}")
    _migrate_highlights_to_ranges(conn)
    # 「标题是不是占位值」标记（⑲ 修复，2026-09-12）。
    # 存量行靠**同一个判据**回溯（`titlemeta.is_placeholder_title`，与写回路径共用一份，
    # 避免"迁移标 A、运行时按 B 判"的漂移）。判据会先看 `source_ref`（原始文件名）
    # 再看 `pdf_path` —— 后者落盘时被加了时间戳前缀，单看它认不出占位符。
    # 仍认不出的行一律保持 `0`（不覆盖）：宁可让用户手动点一次编辑，
    # 也不能擅自覆盖他可能手填过的标题。
    # 「最近阅读」列（⑰ 补充，2026-09-15）。存量行的回填**只做有证据的那些**：
    # `progress > 0` 只可能来自阅读器滚动上报，而滚动上报恰好会刷新 `updated_at`，
    # 所以对这些行 `updated_at` 就是「最后一次与这篇交互」的近似值
    # （若此后又编辑过元数据会偏晚一点 —— 这是近似，不是精确值，宁可偏晚也不留空，
    #  否则会出现"进度 13% 但最近阅读：未读"的自相矛盾）。
    # `progress = 0` 的行一律留 NULL（= 从没读过），不编时间戳。
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    # 「这次排队要跑什么」（㊹ 修订的存量出口，2026-09-19）。存量行留 NULL = 完整转换，
    # 正是它们本来的语义（加这一列**不改任何既有行的行为**）。
    if "pending_job" not in pcols:
        conn.execute("ALTER TABLE papers ADD COLUMN pending_job TEXT")
    if "last_read_at" not in pcols:
        conn.execute("ALTER TABLE papers ADD COLUMN last_read_at TEXT")
        conn.execute("UPDATE papers SET last_read_at=updated_at WHERE progress > 0")
    if "title_is_placeholder" not in pcols:
        conn.execute("ALTER TABLE papers ADD COLUMN title_is_placeholder INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE papers SET title_is_placeholder = 0")
        for r in conn.execute("SELECT id, title, pdf_path, source_ref FROM papers").fetchall():
            if is_placeholder_title(r["title"], flag=..., source_ref=r["source_ref"],
                                    pdf_path=r["pdf_path"]):
                conn.execute("UPDATE papers SET title_is_placeholder = 1 WHERE id=?", (r["id"],))
    conn.commit()


def init_db(settings: Settings) -> None:
    settings.ensure_dirs()
    # ⚠️ SQLite 的 `with conn:` 只提交事务、**不关闭连接**（Python 3.13 会报 ResourceWarning）。
    #    建表路径在每次 create_app() 都会跑到，漏了就一直在漏。
    conn = connect(settings)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务：异常回滚（`with connect(...)` 本身不提交，容易漏掉，统一走这里）。"""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── 通用小工具 ────────────────────────────────────────────────────────────
def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_list(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def expires_in(hours: int) -> str | None:
    """分享有效期（小时）—— 上限 `SHARE_MAX_HOURS`。

    `hours <= 0` 不再表示"永久"，而是**取上限 24 小时**（宿主 2026-09-11：
    「分享链接有效期最大设置 24 小时」→ 永久链接这条路整体取消）。
    """
    hours = max(1, min(int(hours), SHARE_MAX_HOURS))
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
