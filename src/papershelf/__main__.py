"""`python -m papershelf` 入口（CLI 已由 `[project.scripts]` 暴露为 `papershelf`）。"""

from .cli import app

if __name__ == "__main__":
    app()
