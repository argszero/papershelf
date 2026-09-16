"""前端样式护栏（离线，不联网、不起浏览器）。

起因（2026-09-11 真实缺陷，**M5 引入、且已在生产上挂了将近一天**）：

`web/src/styles.css` 里按钮基类被 M5 写成了 `.btn, button.btn`，特异性 **(0,1,1)**；
而所有变体（`.btn-primary` / `.btn-secondary` / `.btn-ghost`）只有 **(0,1,0)** ——
**变体比基类还弱**。基类里的 `background: transparent; border: 1px solid transparent`
于是反过来盖掉变体，实心主按钮全部变成"透明底 + 透明边"。

为什么特别难发现：

1. **`<a class="btn btn-primary">`（react-router `Link`）不受影响** —— 元素选择器
   `button.btn` 不匹配 `a`。所以同一屏里 Link 按钮正常、`<button>` 按钮透明，
   看起来像"某个页面样式没写"而不是"级联坏了"。
2. **`:hover` 规则带 3 个伪类 (0,3,0)，能压过基类** —— 于是表现为
   「按钮平时看不见，鼠标移上去才出现」。宿主看到的正是这个现象。
3. `.btn:disabled { opacity: .5 }` 让禁用态看起来"像设计如此"，掩盖了底色丢失。

修法：**基类与变体一律只用类选择器**，把特异性铺平，让变体靠源码顺序取胜
（原型就是这么写的，原型里没有任何元素限定）。本文件把这条规则钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "web" / "src" / "styles.css"

# 按钮族里所有"变体"类：它们必须能盖过基类 `.btn`
VARIANT_CLASSES = ("btn-primary", "btn-secondary", "btn-ghost", "btn-danger")


def _rule_selectors(css: str) -> list[str]:
    """粗略提取所有选择器（足够用于本护栏：只看逗号分隔的选择器片段）。

    先剥掉注释，再按 `{...}` 取每条规则的选择器部分。
    """
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    selectors: list[str] = []
    for match in re.finditer(r"([^{}]+)\{", without_comments):
        chunk = match.group(1).strip()
        if chunk.startswith("@"):  # at-rule（如 @media）本身不是选择器
            chunk = ""
        for part in chunk.split(","):
            part = part.strip()
            if part:
                selectors.append(part)
    return selectors


@pytest.fixture(scope="module")
def css() -> str:
    assert CSS.is_file(), f"样式表不存在：{CSS}"
    return CSS.read_text(encoding="utf-8")


def test_base_btn_class_has_no_element_qualifier(css: str) -> None:
    """`.btn` 基类不得写成 `button.btn` —— 这会把基类特异性抬到 (0,1,1)。"""
    offenders = [
        s for s in _rule_selectors(css)
        if re.fullmatch(r"(button|a|input)\s*\.btn", s)
    ]
    assert not offenders, (
        "按钮基类被写成了元素+类限定，特异性会高于所有变体，变体底色会被盖掉："
        f"{offenders}"
    )


@pytest.mark.parametrize("variant", VARIANT_CLASSES)
def test_variants_are_not_element_qualified(css: str, variant: str) -> None:
    """变体同理：`button.primary` 这种写法会让变体特异性随元素标签漂移。"""
    offenders = [
        s for s in _rule_selectors(css)
        if re.fullmatch(rf"(button|a|input)\s*\.{re.escape(variant)}", s)
    ]
    assert not offenders, f".{variant} 变体被元素限定，请只用类选择器：{offenders}"


def test_variant_specificity_covers_base(css: str) -> None:
    """核心断言：变体选择器的特异性必须 >= 基类，且源码顺序在基类之后。

    这正是当初坏掉的地方 —— 变体 (0,1,0) < 基类 (0,1,1)。
    """
    selectors = _rule_selectors(css)

    def specificity(sel: str) -> tuple[int, int, int]:
        ids = len(re.findall(r"#[\w-]+", sel))
        classes = len(re.findall(r"\.[\w-]+", sel))
        classes += len(re.findall(r"\[[^\]]+\]", sel))          # 属性选择器算 (0,1,0)
        classes += len(re.findall(r"(?<!:):(?!:)[\w-]+", sel))  # 伪类（非伪元素）算 (0,1,0)
        elements = len(re.findall(r"(?<![\w.#-])([a-z]+)(?![\w-])", sel))
        return (ids, classes, elements)

    base_idx = [
        i for i, s in enumerate(selectors)
        if s == ".btn" or re.fullmatch(r"\.btn:[\w-]+(\([^)]*\))?", s)
    ]
    assert base_idx, "找不到 `.btn` 基类规则"

    for variant in VARIANT_CLASSES:
        v_idx = [
            i for i, s in enumerate(selectors)
            if re.search(rf"\.{re.escape(variant)}\b", s)
        ]
        assert v_idx, f"找不到 .{variant} 的规则"
        first_variant = v_idx[0]
        # 变体必须出现在基类之后（同特异性下靠顺序取胜）
        assert first_variant > min(base_idx), (
            f".{variant} 出现在 .btn 基类之前；同特异性时会被基类盖掉。"
        )
        # 且变体特异性不得低于它想覆盖的基类规则
        base_spec = max(specificity(selectors[i]) for i in base_idx)
        variant_spec = max(specificity(selectors[i]) for i in v_idx)
        assert variant_spec >= base_spec, (
            f".{variant} 特异性 {variant_spec} 低于 .btn 基类 {base_spec}，"
            "变体会被基类的 background/border 覆盖。"
        )


def test_no_element_qualified_button_selectors_at_all(css: str) -> None:
    """回归原型：原型里**不存在**任何元素限定的按钮选择器。

    这条兜住上面几条正则没想到的写法（如 `a.btn-danger`、`input.btn-sm`），
    防止以后有人"只修某一个按钮"再把元素选择器加回来。
    """
    offenders = [
        s for s in _rule_selectors(css)
        if re.match(r"(button|a|input)\s*\.", s)
        and re.search(r"\.btn(?!-)|\.primary|\.ghost|\.danger", s)
    ]
    assert not offenders, f"按钮族不得使用元素+类限定选择器：{offenders}"


def test_images_keep_aspect_ratio(css: str) -> None:
    """图片不得被拉伸：`max-width:100%` 必须配 `height:auto`。

    起因（2026-09-11 宿主报「阅读器里图片都被拉伸了，效果非常差」）：
    阅读器给 `<img>` 写了 `width`/`height` 属性（防懒加载抖动，见 Reader.tsx），
    而 `img, svg { max-width:100% }` 只压宽度 —— 高度仍是属性里的固定值，
    于是栏宽小于原图宽时图片被**纵向压扁**。

    真机实测（生产 share 页，15 张图）：14 张被压 24%~49%，
    唯一正常的是原宽 1019px、本来就没超过栏宽的那张 —— 正好印证成因。

    这条守的是「宽度被夹住时高度必须等比回落」。
    """
    # 先剥注释：CSS 注释里也会出现 `img` / `max-width` 字样，
    # 不剥的话注释会被当成选择器混进来（踩过：护栏因此永远绿）。
    naked = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", naked)
    img_rules = [
        body for sel, body in blocks
        if any(re.search(r"(^|\s|,)(figure\s+)?img(\s|$|,|\.)", s.strip())
               for s in sel.split(","))
    ]
    assert img_rules, "找不到任何 img 规则（选择器写法变了？）"

    clamped = [b for b in img_rules if re.search(r"max-width\s*:\s*100%", b)]
    assert clamped, "找不到 `max-width:100%` 的 img 规则"
    for body in clamped:
        assert re.search(r"height\s*:\s*auto", body), (
            "有 `max-width:100%` 的 img 规则却没写 `height:auto`："
            "图片带 width/height 属性时会被纵向压扁（生产实测 24%~49%）。"
        )


# ── 只读分享必须**复用整站**（⑩）────────────────────────────────────────
WEB = ROOT / "web" / "src"


def test_share_reuses_the_whole_app_shell_not_a_stripped_page():
    """分享页必须是**整站只读化**，不许再出现"分享专用精简页面"。

    起因（2026-09-11 宿主：「分享实现的不对，分享后的页面应该和分享者看到的一模一样，
    只是只读」）：此前 `pages/Share.tsx` 另写了一套"brand + 导出 + 文献下拉 + 文档舞台"
    的窄壳，与原型（以及决策⑩ 的原文"和分享者看到的是相同的页面"）直接冲突。

    这条护栏钉住三件事，任何一件被改回窄壳都会红：
      1. `/share/:token/*` 挂在**同一张 `AppRoutes`** 上（不是另写路由）；
      2. 分享外壳仍渲染 `Sidebar`（侧栏是"整站"最显眼的标志）；
      3. 旧的独立分享页 `pages/Share.tsx` 不存在了。
    """
    app = (WEB / "App.tsx").read_text(encoding="utf-8")
    assert "AppRoutes" in app, "分享作用域没有复用整站路由表 AppRoutes"
    assert "/share/:token/*" in app, "分享路由不是 `/share/:token/*`（前缀式，子路径要能兜住）"

    shell = (WEB / "shell" / "index.tsx").read_text(encoding="utf-8")
    assert "export function ShareShell" in shell, "分享外壳不见了"
    share_shell = shell.split("export function ShareShell")[1]
    assert "<Sidebar />" in share_shell, "分享外壳没有渲染侧栏 —— 又退回精简分享页了"

    assert not (WEB / "pages" / "Share.tsx").exists(), (
        "pages/Share.tsx 又回来了：分享必须复用整站页面（⑩），不要另写窄壳"
    )


def test_share_context_is_the_only_place_that_prefixes_paths():
    """**分享作用域内**的站内链接只能出自 `shareContext.tsx` 的 `path()`。

    页面若自己拼 `/share/${token}/library` 这类**作用域内**路径，路由结构一变就会有
    页面漏改，表现为"在分享页点某处跳回了登录页"。

    例外（不是"某几个文件放行"，而是**另一种语义**）：指向某条**具体分享链接自身**
    的顶层 URL —— `Shares`（分享管理页）里"预览这条链接"用的 `/share/${rec.token}`，
    以及 `ShareModal` 新建完给的那条。它们的 token 来自**数据**而非当前所在作用域，
    本来就该是顶层绝对路径；拿 `useLink()` 改写反而错（会变成 `/share/<当前>/share/<那条>`）。

    所以判据是：写了 `/share/${...token}/` **后面还接着路径段**（= 试图构造当前
    作用域内的站内链接）才算违规。"预览某一条链接"写成 `/share/${share.token}`
    以反引号收尾，是合法的顶层绝对路径。
    """
    import re

    # `token}` 之后必须还有一个 `/`（作用域内的子路径），否则视为"指向链接自身"
    scope_prefix = re.compile(r"`/share/\$\{[^}]*token\}/")
    offenders = []
    for f in (WEB / "pages").glob("*.tsx"):
        src = f.read_text(encoding="utf-8")
        if scope_prefix.search(src):
            offenders.append(f.name)
    assert not offenders, (
        "页面里出现了手写的 `/share/${token}/...`（当前分享作用域的路径）；"
        f"请用 `useLink()` / `share.path()`：{offenders}"
    )


def test_readme_image_paths_exist():
    """README 里引用的截图/文档路径必须真实存在。

    首屏大图挂了是**最贵的一种失效**：README 是绝大多数人的第一眼，
    而图片路径写错不会被任何东西拦住（Markdown 不会报错，CI 也不会）。

    同类坑：改写 README 时删掉了配图，但正文里的 `<img>` 还指着它 ——
    本护栏顺带覆盖 `docs/*.md`。
    """
    import re

    def strip_code(text: str) -> str:
        """先剔掉围栏代码块 —— 里面有大量**示例** HTML（`<img src="assets/fig3.png">`），
        它们是讲解用的假路径，不是引用。

        ⚠️ 围栏可以带缩进（列表里的代码块是 `  ```），所以 `^` 后面要允许空白，
        否则这段示例 HTML 会被当成真引用（本护栏第一次跑就是这么误报的）。
        """
        return re.sub(r"^[ \t]*```.*?^[ \t]*```", "", text, flags=re.S | re.M)

    md_files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    missing: list[str] = []
    for md in md_files:
        text = strip_code(md.read_text(encoding="utf-8"))
        # Markdown 图片 `![alt](path)` 与 HTML `<img src="path">`
        refs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
        refs += re.findall(r'<img\s[^>]*src="([^"]+)"', text)
        for ref in refs:
            if ref.startswith(("http://", "https://", "data:")):
                continue
            if not (md.parent / ref).resolve().exists():
                missing.append(f"{md.relative_to(ROOT)} → {ref}")

    assert not missing, "文档里引用了不存在的文件（图片挂掉 / 死链）：\n" + "\n".join(missing)


# ── 转换队列：进度刷新必须"有活儿才刷"（2026-09-12）──────────────────────
PLAN_CTX = ROOT / "web" / "src" / "planContext.tsx"


def test_conversion_polling_is_gated_on_inflight_work():
    """前端只在**真有转换在跑**时才轮询刷新，安静时不许打服务器。

    背景（2026-09-12 事故）：转换改由服务端队列在后台跑（`server/queue.py`）后，
    前端除了"导入完那一刻"再没有刷新时机 —— 传 12 篇时界面会永远停在
    「转换中 12」，只能手动刷新才看到一篇篇变「已生成」。

    修法是在 `planContext` 里加一个**条件**轮询。这里钉住"条件"这个一半：
    一旦有人把 `if (busyCount === 0) return` 删掉（比如为了"确保能刷新"），
    六个页面就会各自每秒打一次接口，而绝大多数时候什么都不会变。
    """
    src = PLAN_CTX.read_text(encoding="utf-8")
    assert "busyCount" in src, "转换进度轮询的前置条件（busyCount）不见了"
    assert re.search(r"if\s*\(\s*busyCount\s*===\s*0\s*\)\s*return", src), (
        "转换进度轮询必须**只在有 queued/doing 时**开启："
        "缺少 `if (busyCount === 0) return` 退化成无条件轮询"
    )
    assert re.search(r"conv_state\s*===\s*'queued'", src) and \
        re.search(r"conv_state\s*===\s*'doing'", src), (
        "busyCount 的判据必须是 `queued || doing` 两个状态 —— "
        "只算 queued 的话，进入 doing 后轮询会立刻停掉（而这正是最需要刷新的时刻）"
    )


def test_queue_claim_happens_inside_the_concurrency_slot():
    """服务端：认领（`queued → doing`）必须发生在**拿到并发槽位之后**。

    本地实测踩到：旧写法（先认领、再抢槽位）在并发=2、队列=8 篇时，
    **8 篇立刻全变 `doing`**，界面炸出 6 篇假"转换中"；此刻重启，那 6 篇
    又成僵尸。与被修的事故是同一个失败模式（谎报进度 + 重启即丢）。
    ⚠️ 必须按**行首锚定**匹配代码行，不能用裸 `str.index("with concurrency_semaphore()")`：
    `convert_paper` 的 docstring 里正引用着这句话，裸 index 会命中 docstring（位置远早于
    真正的代码），于是这个护栏变成**永远通过的空测试** —— 实测注入"先认领再抢槽位"
    它照样绿。本文件第一次就是这么写错的，双向验证才发现。
    """
    src = (ROOT / "src" / "papershelf" / "server" / "converter.py").read_text(encoding="utf-8")
    body = src[src.index("def convert_paper("):]
    body = body[:body.index("\ndef ", 1)]

    def first_line(pattern: str) -> int:
        m = re.search(pattern, body, flags=re.MULTILINE)
        assert m is not None, f"在 convert_paper 里找不到代码行：{pattern}"
        return m.start()

    i_slot = first_line(r"^\s*with concurrency_semaphore\(\):")
    i_claim = first_line(r"^\s*(if not )?claim_paper\(conn, paper_id\)")
    assert i_slot < i_claim, (
        "`with concurrency_semaphore()` 必须在 `claim_paper()` 之前 —— "
        "反过来的话，等槽位的文献会被提前报成 doing（界面说谎 + 重启即僵尸）"
    )


def test_queue_polls_and_recovers_on_startup():
    """服务端：必须有常驻轮询 + 启动恢复，不能只在请求线程里跑。

    这是 2026-09-12 事故的根因面：`BackgroundTasks` 只在单次请求期间存在，
    请求一结束就没有任何东西再看 `queued`；容器重启更是把一切带走。
    这条护栏保证那两件事**至少各有一处实现**。
    """
    q = ROOT / "src" / "papershelf" / "server" / "queue.py"
    assert q.exists(), "转换队列模块被删了？它承载启动恢复 + 常驻轮询（见其 docstring）"
    src = q.read_text(encoding="utf-8")
    assert "def recover_stuck(" in src, "启动恢复（doing → queued）不见了"
    assert "threading.Thread(" in src, "常驻轮询线程不见了"
    app = (ROOT / "src" / "papershelf" / "server" / "app.py").read_text(encoding="utf-8")
    assert "ConversionQueue" in app and "lifespan" in app, (
        "app 必须通过 lifespan 起停转换队列，否则队列根本没有生命周期宿主"
    )


# ── 划痕（决策㉛，2026-09-13）────────────────────────────────────────────
def test_mark_styles_exist(css: str) -> None:
    """划痕/闪动/色板这几个类名必须存在 —— **它们是 JS 与实际渲染之间的唯一契约**。

    成因（这类缺陷为什么难发现）：`<mark class="hl hl-<色>">` 由**服务端**渲染
    （`dangerouslySetInnerHTML`），React 完全不认识它们；前端只按 `data-h` 找。
    所以一旦样式表里改了名（或删了规则），**JS 不会报错、测试也不会失败**，
    只是"点了没反应"——正好落在这个仓库最痛的那一类"本地全绿、只有人看得见"的缺陷上。
    """
    naked = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selector in (".hl", ".hl.is-flash", ".blk-p.is-flash",
                     ".hl-amber", ".hl-green", ".hl-blue", ".hl-pink",
                     ".pen", ".pen.is-on", ".pen-amber", ".pen-green", ".pen-blue", ".pen-pink",
                     ".mark-bar", ".mark-bar .mb-btn", ".mark-bar .mb-del"):
        assert selector in naked, f"缺少划痕样式 {selector}（划痕/色板全靠它）"
    assert "@keyframes hlFlash" in naked, "闪烁动画 hlFlash 不见了（点击笔记跳转就没有落点提示）"
    # 跨行的一道划痕：底色/圆角必须逐行闭合，否则会画成缺角的长方框
    assert "box-decoration-break: clone" in naked, ".hl 缺少 box-decoration-break: clone"
    # 四支笔**等价**：不许出现"某支笔有额外语义样式"（宿主：颜色不带含义）
    for color in ("amber", "green", "blue", "pink"):
        assert f".pen-{color}" in naked, f"色板少了 {color} 这支笔"


def test_highlight_and_note_affordance_styles_exist(css: str) -> None:
    """高亮圆点、笔记引文 —— 与原型 `.nc-hl` / `.nc-q2` / `.nf-quote` 同名。"""
    naked = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selector in (".nc-hl", ".nc-q2", ".nf-quote", ".note-card.is-on"):
        assert selector in naked, f"缺少 {selector}（与原型同名，改了名就等于悄悄丢功能）"


def test_sidebar_has_no_standalone_reader_entry() -> None:
    """侧栏**不得**再有独立的「阅读器」项（宿主 2026-09-13）。

    阅读器是"从文献库点进去"的二级页面；侧栏多一个入口会让两个地方都像"一级视图"，
    而且要解释"为什么不选文献也能进阅读器"。原型侧栏只有 总览/文献库/进度看板/阅读计划/分享管理。
    """
    shell = (ROOT / "web" / "src" / "shell" / "index.tsx").read_text(encoding="utf-8")
    assert "to: '/reader'" not in shell, "侧栏又出现了独立的阅读器入口"
    # 但进阅读器时侧栏要停在「文献库」上（原型 L1665 同款行为）
    assert "'library'" in shell, "阅读器态应把侧栏高亮落到文献库"


def test_zh_only_mode_always_renders_the_chinese_column() -> None:
    """「仅中文」模式必须**一律渲染** `.t-zh` —— 与 CSS 藏掉的 `.t-en` 配套，缺一即开天窗。

    2026-09-15 真缺陷（宿主：「"仅中文"时，显示不对」）：两侧条件互相打架 ——

      - `styles.css`：`.lang-zh .t-en { display: none !important }`（藏英文栏）
      - `Reader.tsx`：渲染中文栏的条件写成了 `mode === 'dual' && !wide`

    叠加结果：**仅中文模式下正文段落全部空白**（实测 34 个段落，可见文字 0 个，
    `.blk-p` 高度 14px 只剩内边距）。标题之所以看着正常，是因为它走 `.b-zh`
    内联 span 那条分支，恰好不受影响 —— 所以"看到标题还在"会是完美的伪装。

    这种错**一屏截图看不出来**（页面上有东西），必须逐段量 `innerText` 才现形；
    所以钉一条静态护栏，把 CSS 与 JSX 的耦合固定住。
    """
    css = CSS.read_text(encoding="utf-8")
    tsx = (WEB / "pages" / "Reader.tsx").read_text(encoding="utf-8")

    naked = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert ".lang-zh .t-en" in naked, "CSS 不再隐藏 .lang-zh 下的 .t-en（前提变了，请复核本护栏）"

    assert "const dual = mode === 'dual' && !wide" in tsx, (
        "双栏排版条件变了；请确认「仅中文」分支仍会渲染中文栏")
    assert "const showZh = mode === 'zh' || dual" in tsx, (
        "渲染中文栏的条件不是「仅中文一律渲染」—— `.lang-zh` 下 English 栏被 CSS 藏了，"
        "只按 dual 渲染会让整段正文空白（2026-09-15 真缺陷）")
    assert "{showZh && <div className=\"t-zh\">" in tsx, "中文栏没有挂在 showZh 上"

    # 免中文块（references / 纯公式 / 版权页脚）在仅中文下要回落原文而不是留白，
    # 标题同理 —— 两个回落条件都必须保留 `mode === 'zh'` 这条出口。
    assert "!wide || mode === 'zh'" in tsx, "标题在仅中文 + 无译文时会渲染成空白"
    assert "meta.title_zh || mode === 'zh'" in tsx, "文档标题在仅中文 + 无译文时会渲染成空白"


def test_headings_keep_the_mark_coordinate_container() -> None:
    """标题必须**自己**带 `.b-inline[data-lang]`，否则选中标题不弹浮条（2026-09-16 宿主实测）。

    病灶是**两半**，缺任何一半都表现为"选中标题什么都不会发生"：

    - 服务端（`pipeline/markup.py::render_block`）：标题原走 `_esc(text)`，
      既不吐零宽锚点也没有 `<mark>` → 前端量不出字符坐标；
    - 前端（`Reader.tsx` 的 `b.type.startsWith('h')` 分支）：原先直接吐
      `<span className="b-en">{b.en}</span>` 纯文本 —— `selectionSegments()` 是
      从选区文本节点**往上找** `.b-inline[data-lang]` 才拿到坐标系与块 id 的，
      找不到就整条 `segs` 为空，浮条不出现。

    这条护栏只钉前端那一半（服务端那一半由 `tests/test_markup.py` 钉）；
    两处必须同时成立，所以这里也顺带断言标题用的是 `prose()` 而不是 `_esc()`。
    """
    tsx = (WEB / "pages" / "Reader.tsx").read_text(encoding="utf-8")
    assert 'b.type.startsWith(\'h\')' in tsx, "标题分支的判据变了，请复核本护栏"
    head_branch = tsx[tsx.index("b.type.startsWith('h')"):]
    head_branch = head_branch[:head_branch.index("b.type === 'figure'")]
    assert 'as="span"' in head_branch, (
        "标题又退回渲染纯文本了 —— 它必须包一层 `.b-inline[data-lang]`，"
        "否则 marks.ts 找不到坐标系，选中标题不弹 mark-bar")
    assert 'className="b-en"' in head_branch and 'className="b-zh"' in head_branch, (
        "标题的英文/中文两栏结构不能少（仅中文模式靠 b-zh 兜底）")
    # `<h2>` 里只允许短语内容：绝不能塞 `<div>`（浏览器会把 div 甩到标题外面）
    assert '<div' not in head_branch, "标题分支里出现了 div —— `<h2><div>` 会被浏览器拆开"

    markup = (ROOT / "src" / "papershelf" / "pipeline" / "markup.py").read_text(encoding="utf-8")
    assert '"h1", "h2", "h3", "h4"' in markup and "prose(text)" in markup, (
        "服务端标题不再走 prose() —— 没有锚点/没有 <mark>，只改前端是修不好的")
