"""pipeline 包：解析 → 带标记英文 HTML → 分块翻译 → 标记校验 → 块级 JSON → 合成 HTML。"""

from .model import Block, Doc, make_block_id
from .parse import PARSE_VERSION, parse_pdf
from .markup import document_html, en_html, render_block, render_fragment
from .validate import (
    NO_ZH_TYPES, Report, expects_chinese, extract_blocks, scan, tag_balance, validate,
)
from .synth import synth, synth_dual, synth_single
from .translator import LLMConfig, Translator
from .equations import Latexizer, looks_math
from .mathml import latex_to_mathml, mathml_css, render_math

__all__ = [
    "Block", "Doc", "make_block_id",
    "parse_pdf", "PARSE_VERSION",
    "document_html", "en_html", "render_block", "render_fragment",
    "NO_ZH_TYPES", "Report", "expects_chinese", "extract_blocks", "scan", "tag_balance", "validate",
    "synth", "synth_dual", "synth_single",
    "LLMConfig", "Translator", "Latexizer", "looks_math",
    "latex_to_mathml", "mathml_css", "render_math",
]
