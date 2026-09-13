---
id: "d9f3a7b2"
event_at: "2026-09-10T17:40:57+08:00"
created_at: "2026-09-10T17:43:00+08:00"
updated_at: "2026-09-10T17:43:00+08:00"
type: decision
scope: project
status: merged
---

# 决策⑨：技术栈 = Python 单体（FastAPI + SQLite）+ 前端 SPA

宿主 2026-09-10 回答（问题 9）：**A**。

## 确定
- **后端**：Python + FastAPI，单体服务
- **数据库**：SQLite（自托管多用户规模足够，省掉独立 DB 容器）
- **转换 worker**：Python 进程，与 API 同栈 —— **fitz/PyMuPDF 直接可用**，已验证管线代码可原样搬入
- **前端**：独立 SPA（**具体框架待定**：React / Svelte / Vue）
- **部署**：一个容器 + SQLite 文件（数据目录可挂载、可备份）

## 为什么选 A（理由存档）
- 硬约束：解析依赖 **Python 的 fitz/PyMuPDF**（已验证管线），Python 单体与它**零阻抗**
- 前端独立 SPA 才扛得住「并排对照 + 滚动联动 + 看板拖拽」这类交互
- SQLite 对几十~几百用户的自托管场景完全够用

## 已排除
- B 服务端渲染模板（Jinja2）—— 交互复杂度高，原生 JS 写会累
- C TS 全栈 + Python 边车 —— 两套运行时/两个容器/跨进程协议，复杂度不划算
- D Go/Rust + Python 边车 —— 开发速度慢，且仍绕不开 Python 边车

## 待定子项
- 前端框架具体选型
- 任务队列实现（进程内 asyncio / RQ / Celery / 自研 DB 轮询）—— 自托管偏好轻量
- 认证实现（JWT / session cookie）

---

> **已并入 `design-decisions.md`（设计决策总表）** —— 本文件为历史碎片，保留以备溯源，内容以总表为准。
