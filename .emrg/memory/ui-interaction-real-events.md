---
id: b4e07a12
event_at: 2026-09-13T13:50:00
created_at: 2026-09-13T13:50:00
updated_at: 2026-09-13T13:50:00
type: reference
scope: project
status: active
---

# 验收 UI 交互：必须用真实输入事件（合成事件会系统性遮蔽顺序缺陷）

**来源**：㉛ 修订（`350f33f`）—— 拖选文字后浮条「一闪即没」，上一轮"真浏览器实测全绿"却没发现。

## 一句话

**"处理函数没写错"≠"真实序列下状态还在"。**
合成事件（`dispatchEvent(new MouseEvent(...))`、`el.click()`）**只调用处理函数**，
它**跳过**了浏览器真实的事件序列 —— 而缺陷常常长在**两个事件之间的衔接**上。

## 具体病例（㉛）

- 代码里有三个舞台级处理器：`mousedown → hideBar`（原型）／`mouseup → 选中就点亮浮条`／`click → 点别处收浮条`。
- 真人拖选的真实序列是 **`mousedown → (拖) → mouseup → click`**：拖选结束时浏览器**还会补一个 `click`**
  （target = 落点所在块）。那一下被当成"点了别处" → `mouseup` 刚设好的浮条被当场收掉。
- 合成事件测法只会发出 `mouseup`（或只发 `click`）→ **永远看不到"紧接着的 click"** → 一路全绿。
- 判据修法：`window.getSelection()` **非塌缩 ⇒ 这是拖选的尾巴** ⇒ 只更新 `selBlock`，**不动 bar/target**。

## 怎么做（browser-harness 实操）

1. **真实鼠标事件**：`cdp("Input.dispatchMouseEvent", type="mouseMoved"/"mousePressed"/"mouseReleased", x=…, y=…, button="left", clickCount=1)`。
   ⚠️ 给 `_response_timeout=30` —— 默认 5s 常超时（IPC 抖动，重试即可）。
2. ⚠️ **必须先 `activate_tab(current_tab())`** —— 后台标签页**收不到** CDP 输入事件
   （表现为 `document.addEventListener(..., true)` 的日志**全空**）。这与"事件没挂上/元素没命中"
   长得**一模一样**，本轮为此误判过一次（还差点当成 app 的 bug）。
3. **"一闪即没"用 `MutationObserver` 拍**（`childList+subtree`）：
   能直接区分「**加过又被删**」与「**压根没加**」—— 轮询快照区分不了。
4. 起止坐标要按**真实句子形状**取：`mark` 可能跨行，`getBoundingClientRect()` 的中点在两行之间（落在行间空隙，
   `elementFromPoint` 会返回外层 `<p>`），要用 `getClientRects()[0]` 取第一行的点。
5. 本地 8012 与生产**都要跑**：本轮本地就能复现 → 说明不是环境差异，别自我安慰成"生产才有"。

## 同族教训（本项目反复出现：**本地替身会系统性遮蔽缺陷**）

| 缺陷 | 替身遮蔽了哪一层 |
|---|---|
| 发信 37.5% 失败（`mailer-gmail-ip-rotation`） | 假 SMTP 服务器不轮转 IP ＋ 夹具把 `mailer._send` 整个换掉 |
| 按钮变体被基类盖成透明 | 只看截图（"像不像原型"）→ 没量 computed style |
| 浮条一闪即没（本轮） | 只发合成事件 → 跳过了 `mouseup → click` 的真实衔接 |

**通则**：**验收要跑在"最接近真实的那一层"，而不是最方便的那一层**；
凡是"看起来没反应/一闪即没/偶发"，先怀疑**事件顺序**与**目标标签页/可见性**，
再怀疑业务逻辑。
