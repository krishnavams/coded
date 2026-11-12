"""Tools that reach the user or the model's eyes: ask_user, view_image."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from coded.tools.base import Tool, ToolContext, ToolResult

_MAX_OPTIONS = 6
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


class AskUserTool(Tool):
    name = "ask_user"
    brief = "Ask the user a question when you are blocked on their decision."
    description = """Ask the user one question and wait for the answer. Use it only when you \
are blocked on a decision that is genuinely theirs (a preference, a trade-off, missing \
information you cannot find in the code or config) — not for things you can check yourself, \
and not to ask permission (risky tools already ask). Offer 2-6 short `options` when the \
choices are clear; the user may still type their own answer. Keep the question self-contained."""
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, ending with '?'."},
            "options": {
                "type": "array", "items": {"type": "string"}, "maxItems": _MAX_OPTIONS,
                "description": "Optional short answer choices (the user may answer freely).",
            },
        },
        "required": ["question"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = str(args.get("question") or "").strip()
        if not question:
            return ToolResult.error("Provide the `question` to ask.")
        options = args.get("options") or []
        if not isinstance(options, list):
            return ToolResult.error("`options` must be a list of strings.")
        options: List[str] = [str(o).strip() for o in options if str(o).strip()][:_MAX_OPTIONS]
        answer = ctx.permissions.question(question, options)
        if answer is None:
            return ToolResult.ok(
                "Nobody can answer right now (non-interactive session). Make the most "
                "reasonable assumption, say which one you made, and continue.")
        answer = answer.strip()
        if not answer:
            return ToolResult.ok("The user skipped the question. Use your best judgment, "
                                 "say what you assumed, and continue.")
        return ToolResult.ok(f"The user answered: {answer}")


class ViewImageTool(Tool):
    name = "view_image"
    parallel_safe = True
    brief = "Look at a local image file (screenshot, diagram, mockup)."
    description = """Look at a local image file (png, jpg, gif, webp, bmp): a screenshot, a \
diagram, a UI mockup or a rendered chart. The image is attached to the conversation right \
after this call, so you can see it in your next step. Needs a vision-capable model."""
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the image file."}},
        "required": ["path"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ToolResult.error("Provide the image `path`.")
        model = ctx.extra.get("model")
        if model is not None and not getattr(model, "supports_vision", False):
            return ToolResult.error(
                f"The current model ({model.name}) cannot see images (supports_vision is off). "
                "Describe what you need from the image to the user instead, or ask them to "
                "switch to a vision model.")
        attach = ctx.extra.get("attach_image")
        if attach is None:
            return ToolResult.error("Images cannot be attached in this context.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        if not path.is_file():
            return ToolResult.error(f"No such file: {raw}. Use glob to find the image.")
        if path.suffix.lower() not in IMAGE_EXTS:
            return ToolResult.error(f"{raw} is not an image ({', '.join(IMAGE_EXTS)}). "
                                    "Use read for text and documents.")
        size = path.stat().st_size
        if size > _MAX_IMAGE_BYTES:
            return ToolResult.error(f"{raw} is {size // (1024 * 1024)} MB; images over "
                                    f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MB are refused.")
        attach(str(path))
        try:
            shown = str(path.relative_to(ctx.cwd))
        except ValueError:
            shown = str(path)
        return ToolResult.ok(f"Attached {shown} ({size // 1024} KB); it follows as an image.")
