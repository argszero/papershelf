"""发布流水线的**破坏性操作**护栏（离线，不联网）。

起因（2026-09-11 真实事故）：`.github/workflows/docker.yml` 里原本有一个
`cleanup` job，用 `actions/delete-package-versions@v5` "删掉按 digest 推送的中间标签"。
它的参数组合是：

    delete-only-untagged-versions: false     # 连有 tag 的版本也删
    ignore-versions: '^(latest|v?[0-9].*)$'  # 保护名单不含 sha-<commit> / sha256-<hex>
    min-versions-to-keep: 0                  # 一个都不留

于是它把**自己刚发布的镜像**删空了。同一 run 内的时序证据：
`merge` 推上 `latest` 并 inspect 成功 → `smoke` 拉 `latest` 起容器成功
→ `cleanup` 打印「Total versions deleted till now: 8」→ 此后 GHCR 上
`latest` / `sha-828114e` / manifest digest **全部 404**、包页面「No tagged versions found」、
部署机 `docker pull` 只能 `not found`。

这个缺陷的失败模式特别恶劣：**静默删掉交付物**，而 CI 仍然全绿。
所以这里用回归测试把它钉住 —— 发布 workflow 里不允许再出现"按版本删包"的步骤。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker.yml"

# 发布流水线里**绝不允许**出现的 action（它们的失败模式是删掉刚发布的产物）
FORBIDDEN_ACTIONS = ("actions/delete-package-versions",)

# 允许出现的"包/版本"权限只有只读的 read；write 只应出现在真正 push 镜像的 job 里
PUBLISHING_JOBS = {"build", "merge"}


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW.is_file(), f"发布 workflow 不存在：{WORKFLOW}"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return list(job.get("steps") or [])


def test_workflow_parses_and_keeps_expected_jobs(workflow):
    jobs = workflow["jobs"]
    # 发布链：测试 → 双架构构建 → 合成 manifest → 冒烟 → 发布物校验
    for name in ("test", "build", "merge", "smoke"):
        assert name in jobs, f"发布 workflow 缺少 {name} job"


def test_no_destructive_package_deletion_in_pipeline(workflow):
    """回归：任何 job 都不得调用"删除包版本"类 action。

    这条是本次事故的直接护栏。若将来确实需要清理中间产物，请先读模块 docstring，
    并按「只删 sha256-<64hex>、min-versions-to-keep≥2、删完断言 latest 仍可解析」来做，
    同时把这条测试改成精确允许而不是直接删掉。
    """
    offenders: list[str] = []
    for job_name, job in workflow["jobs"].items():
        for step in _steps(job):
            uses = str(step.get("uses") or "")
            if any(bad in uses for bad in FORBIDDEN_ACTIONS):
                offenders.append(f"{job_name} → {uses}")
    assert not offenders, (
        "发布流水线里出现了会删除包版本的 action（会把刚发布的镜像删掉）：\n  "
        + "\n  ".join(offenders)
    )


def test_latest_is_pushed_on_default_branch(workflow):
    """`latest` 必须在默认分支上被推 —— 部署机拉的就是它。"""
    merge = workflow["jobs"]["merge"]
    meta = [s for s in _steps(merge) if "metadata-action" in str(s.get("uses") or "")]
    assert meta, "merge job 里找不到 docker/metadata-action"
    tags = str(meta[0]["with"]["tags"])
    assert re.search(r"value=latest", tags), f"merge 的 tag 规则里没有 latest：{tags!r}"
    assert "enable={{is_default_branch}}" in tags, (
        "latest 应当只在默认分支启用（避免分支构建覆盖发布物）"
    )


def test_only_publishing_jobs_may_write_packages(workflow):
    """`packages: write` 只允许给真正 push 镜像的 job（最小权限）。"""
    for job_name, job in workflow["jobs"].items():
        perm = (job.get("permissions") or {}).get("packages")
        if perm == "write":
            assert job_name in PUBLISHING_JOBS, (
                f"{job_name} 拿到了 packages: write，但它不负责推送镜像 —— 最小权限被放宽了"
            )


def test_smoke_waits_for_merge_and_pulls_latest(workflow):
    """冒烟必须跑在 merge 之后、且拉的是 latest（否则它验的不是发布物）。

    ⚠️ smoke job 把镜像名放在 job 级 `env.IMAGE`（`:latest`）里再在步骤里用 `"$IMAGE"` 引用，
    所以这里要**两处都看**，否则会误判成"没拉 latest"。
    """
    smoke = workflow["jobs"]["smoke"]
    assert "merge" in (smoke.get("needs") or []), "smoke 必须 needs merge"
    image_env = str((smoke.get("env") or {}).get("IMAGE") or "")
    run_text = "\n".join(str(s.get("run") or "") for s in _steps(smoke))
    assert ":latest" in image_env or ":latest" in run_text, (
        "smoke 没有拉 :latest —— 它验的不是部署机真正会拉的东西；"
        f"IMAGE={image_env!r}"
    )
