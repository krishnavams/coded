"""Tools that reach the user or the model's eyes: ask_user, view_image."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from coded.choice import MAX_OPTIONS, MAX_QUESTIONS, Question
from coded.tools.base import Tool, ToolContext, ToolResult

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # base64 adds a third; most APIs refuse more


class AskUserTool(Tool):
    name = "ask_user"
    brief = "Ask the user 1-4 questions when you are blocked on their decision."
    description = """Ask the user one question, or up to 4 related questions at once, and wait \
for the answers. Use it only when you are blocked on a decision that is genuinely theirs (a \
preference, a trade-off, missing information you cannot find in the code or config) — not for \
things you can check yourself, and not to ask permission (risky tools already ask). Give each \
question 2-6 short `options` when the choices are clear (a label, optionally a description); \
the user can always type their own answer instead. Set `multi_select` when several options \
can apply. With several questions the user reviews all answers before submitting. Keep each \
question self-contained. For one question you may pass `question` + `options` directly."""
    _option = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Short choice (1-5 words)."},
            "description": {"type": "string", "description": "What choosing it means."},
        },
        "required": ["label"],
    }
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS,
                "description": "The questions to ask (1-4), answered together.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string",
                                     "description": "The question, ending with '?'."},
                        "header": {"type": "string",
                                   "description": "Very short tab label (max 12 chars)."},
                        "options": {"type": "array", "items": _option, "maxItems": MAX_OPTIONS,
                                    "description": "2-6 answer choices."},
                        "multi_select": {"type": "boolean",
                                         "description": "Allow several choices."},
                    },
                    "required": ["question"],
                },
            },
            "question": {"type": "string", "description": "A single question, ending with '?'."},
            "options": {
                "type": "array", "items": {"type": "string"}, "maxItems": MAX_OPTIONS,
                "description": "Short answer choices for the single `question`.",
            },
        },
    }

    def _parse(self, args: Dict[str, Any]):
        raw = args.get("questions")
        if raw is None:
            raw = [{"question": args.get("question"), "options": args.get("options")}]
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            return None, "Provide `questions` (a list of 1-4) or a single `question`."
        out: List[Question] = []
        for item in raw[:MAX_QUESTIONS]:
            if isinstance(item, str):
                item = {"question": item}
            if not isinstance(item, dict):
                return None, "Each entry of `questions` must be an object with a `question`."
            text = str(item.get("question") or "").strip()
            if not text:
                return None, "Provide the `question` to ask."
            options = item.get("options") or []
            if not isinstance(options, list):
                return None, "`options` must be a list of strings or {label, description}."
            out.append(Question.make(text, options, header=str(item.get("header") or "")[:16],
                                     multi_select=bool(item.get("multi_select"))))
        return out, ""

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        questions, err = self._parse(args)
        if questions is None:
            return ToolResult.error(err)
        answers = ctx.permissions.questions(questions)
        if answers is None:
            return ToolResult.ok(
                "Nobody can answer right now (non-interactive session). Make the most "
                "reasonable assumption, say which one you made, and continue.")
        if not answers:
            return ToolResult.ok("The user declined to answer. Do not ask again; use your "
                                 "best judgment, say what you assumed, and continue.")
        answers = [(a or "").strip() for a in answers]
        if len(questions) == 1:
            if not answers[0]:
                return ToolResult.ok("The user skipped the question. Use your best judgment, "
                                     "say what you assumed, and continue.")
            return ToolResult.ok(f"The user answered: {answers[0]}")
        lines = ["The user answered:"]
        for q, a in zip(questions, answers):
            lines.append(f"- {q.text} → {a or '(skipped: use your best judgment)'}")
        return ToolResult.ok("\n".join(lines))


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
                                    f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MB are refused. "
                                    "Resize or crop it first (e.g. with bash).")
        attach(str(path))
        try:
            shown = str(path.relative_to(ctx.cwd))
        except ValueError:
            shown = str(path)
        return ToolResult.ok(f"Attached {shown} ({size // 1024} KB); it follows as an image.")
