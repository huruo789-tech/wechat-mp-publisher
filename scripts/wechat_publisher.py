#!/usr/bin/env python3
"""Create WeChat Official Account drafts from local Markdown."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


WECHAT_API_BASE = "https://api.weixin.qq.com"
CONTENT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
PERMANENT_IMAGE_EXTENSIONS = {".bmp", ".png", ".jpeg", ".jpg", ".gif"}


class WeChatPublisherError(RuntimeError):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text

    metadata: dict[str, str] = {}
    for line in lines[1:end_index]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        metadata[key] = value

    body = "\n".join(lines[end_index + 1 :]).lstrip("\n")
    return metadata, body


def first_heading(markdown_text: str) -> str | None:
    for line in markdown_text.splitlines():
        match = re.match(r"^\s{0,3}#\s+(.+?)\s*$", line)
        if match:
            return strip_inline_markdown(match.group(1)).strip()
    return None


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_~>#`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def digest_from_markdown(markdown_text: str, limit: int = 120) -> str:
    lines = []
    in_code = False
    for line in markdown_text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _is_table_separator(line):
            continue
        clean = strip_inline_markdown(line)
        if clean:
            lines.append(clean)
        if len(" ".join(lines)) >= limit:
            break
    return trim_text(" ".join(lines), limit)


def trim_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def validate_article_fields(title: str, digest: str | None) -> None:
    if not title:
        raise WeChatPublisherError("Article title is required.")
    if len(title) > 64:
        raise WeChatPublisherError(
            f"Title is {len(title)} characters; WeChat usually requires 64 or fewer."
        )
    if digest and len(digest) > 120:
        raise WeChatPublisherError(
            f"Digest is {len(digest)} characters; WeChat usually requires 120 or fewer."
        )


def is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def resolve_local_path(raw_path: str, base_dir: Path) -> Path:
    parsed = urllib.parse.urlparse(raw_path)
    if parsed.scheme == "file":
        return Path(urllib.parse.unquote(parsed.path)).expanduser().resolve()
    path = Path(urllib.parse.unquote(raw_path.split("#", 1)[0].split("?", 1)[0]))
    if not path.is_absolute():
        path = base_dir / path
    return path.expanduser().resolve()


def validate_content_image(path: Path) -> None:
    if not path.exists():
        raise WeChatPublisherError(f"Image not found: {path}")
    if path.suffix.lower() not in CONTENT_IMAGE_EXTENSIONS:
        raise WeChatPublisherError(
            f"WeChat article images must be JPG or PNG: {path}"
        )
    if path.stat().st_size >= 1024 * 1024:
        raise WeChatPublisherError(
            f"WeChat article image must be less than 1 MB: {path}"
        )


def validate_cover_image(path: Path, material_type: str) -> None:
    if not path.exists():
        raise WeChatPublisherError(f"Cover image not found: {path}")
    suffix = path.suffix.lower()
    if material_type == "thumb":
        if suffix not in {".jpg", ".jpeg"}:
            raise WeChatPublisherError("WeChat thumb material must be JPG.")
        if path.stat().st_size >= 64 * 1024:
            raise WeChatPublisherError("WeChat thumb material must be less than 64 KB.")
    elif material_type == "image":
        if suffix not in PERMANENT_IMAGE_EXTENSIONS:
            raise WeChatPublisherError(
                "Permanent cover image must be bmp/png/jpeg/jpg/gif."
            )
        if path.stat().st_size >= 10 * 1024 * 1024:
            raise WeChatPublisherError("Permanent cover image must be less than 10 MB.")


class WeChatAPI:
    def __init__(
        self,
        appid: str | None = None,
        appsecret: str | None = None,
        access_token: str | None = None,
        use_cache: bool = True,
        cache_path: Path | None = None,
    ) -> None:
        self.appid = appid
        self.appsecret = appsecret
        self._access_token = access_token
        self.use_cache = use_cache
        self.cache_path = cache_path or Path.home() / ".cache" / "wechat-mp-publisher" / "token.json"

    def access_token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self.appid or not self.appsecret:
            raise WeChatPublisherError(
                "Set WECHAT_MP_APPID and WECHAT_MP_APPSECRET, or pass --access-token."
            )
        cached = self._read_cached_token()
        if cached:
            self._access_token = cached
            return cached

        query = urllib.parse.urlencode(
            {
                "grant_type": "client_credential",
                "appid": self.appid,
                "secret": self.appsecret,
            }
        )
        data = self._request_json("GET", f"{WECHAT_API_BASE}/cgi-bin/token?{query}")
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 7200))
        if not token:
            raise WeChatPublisherError(f"WeChat did not return access_token: {data}")
        self._access_token = token
        self._write_cached_token(token, expires_in)
        return token

    def _cache_key(self) -> str:
        assert self.appid is not None
        return hashlib.sha256(self.appid.encode("utf-8")).hexdigest()

    def _read_cached_token(self) -> str | None:
        if not self.use_cache or not self.appid or not self.cache_path.exists():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        item = payload.get(self._cache_key())
        if not item:
            return None
        if time.time() + 180 >= float(item.get("expires_at", 0)):
            return None
        return item.get("access_token")

    def _write_cached_token(self, token: str, expires_in: int) -> None:
        if not self.use_cache or not self.appid:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {}
            if self.cache_path.exists():
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            payload[self._cache_key()] = {
                "access_token": token,
                "expires_at": time.time() + max(0, expires_in - 300),
            }
            self.cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            return

    def upload_content_image(self, image_path: Path) -> str:
        validate_content_image(image_path)
        token = self.access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/media/uploadimg?access_token={urllib.parse.quote(token)}"
        data = self._request_json(
            "POST",
            url,
            files={"media": image_path},
        )
        image_url = data.get("url")
        if not image_url:
            raise WeChatPublisherError(f"WeChat did not return image URL: {data}")
        return image_url

    def upload_permanent_material(self, media_path: Path, material_type: str = "image") -> dict:
        validate_cover_image(media_path, material_type)
        token = self.access_token()
        query = urllib.parse.urlencode({"access_token": token, "type": material_type})
        url = f"{WECHAT_API_BASE}/cgi-bin/material/add_material?{query}"
        return self._request_json("POST", url, files={"media": media_path})

    def add_draft(self, article: dict) -> dict:
        token = self.access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/draft/add?access_token={urllib.parse.quote(token)}"
        return self._request_json("POST", url, json_body={"articles": [article]})

    def _request_json(
        self,
        method: str,
        url: str,
        json_body: dict | None = None,
        fields: dict[str, str] | None = None,
        files: dict[str, Path] | None = None,
    ) -> dict:
        body: bytes | None = None
        headers: dict[str, str] = {}
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif files:
            body, content_type = encode_multipart(fields or {}, files)
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise WeChatPublisherError(
                f"WeChat HTTP {exc.code}: {raw.decode('utf-8', errors='replace')}"
            ) from exc
        except urllib.error.URLError as exc:
            raise WeChatPublisherError(f"WeChat request failed: {exc}") from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeChatPublisherError(
                f"WeChat returned non-JSON response: {raw[:300]!r}"
            ) from exc

        errcode = data.get("errcode")
        if errcode not in (None, 0):
            raise WeChatPublisherError(
                f"WeChat API error {errcode}: {data.get('errmsg', data)}"
            )
        return data


def encode_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"----wechat-mp-publisher-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, path in files.items():
        filename = path.name
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


@dataclass
class UploadedImage:
    source: str
    target: str


@dataclass
class ImageResolver:
    base_dir: Path
    api: WeChatAPI | None = None
    upload_local: bool = False
    fail_on_remote: bool = False
    uploaded: list[UploadedImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _cache: dict[Path, str] = field(default_factory=dict)

    def resolve(self, source: str) -> str:
        source = source.strip()
        if source.startswith("data:"):
            raise WeChatPublisherError(
                "Data URL images are not supported. Save the image as a local JPG/PNG."
            )
        if is_http_url(source):
            if "mmbiz.qpic.cn" not in source and "mmbiz.qlogo.cn" not in source:
                message = (
                    f"Remote image left unchanged and may be filtered by WeChat: {source}"
                )
                if self.fail_on_remote:
                    raise WeChatPublisherError(message)
                self.warnings.append(message)
            return source

        path = resolve_local_path(source, self.base_dir)
        if not self.upload_local:
            return source
        if not self.api:
            raise WeChatPublisherError("Image upload requested without WeChat API client.")
        if path not in self._cache:
            target = self.api.upload_content_image(path)
            self._cache[path] = target
            self.uploaded.append(UploadedImage(str(path), target))
        return self._cache[path]


class MarkdownRenderer:
    def __init__(self, image_resolver: ImageResolver, style_preset: str = "clean") -> None:
        self.image_resolver = image_resolver
        self.style_preset = style_preset

    def render(self, markdown_text: str) -> str:
        lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        html_blocks: list[str] = []
        paragraph: list[str] = []
        index = 0

        def flush_paragraph() -> None:
            if paragraph:
                text = " ".join(line.strip() for line in paragraph).strip()
                if text:
                    html_blocks.append(f'<p style="{P_STYLES}">{self.inline(text)}</p>')
                paragraph.clear()

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()

            if not stripped:
                flush_paragraph()
                index += 1
                continue

            fence_match = re.match(r"^\s*```(\w+)?\s*$", line)
            if fence_match:
                flush_paragraph()
                language = fence_match.group(1) or ""
                code_lines = []
                index += 1
                while index < len(lines) and not re.match(r"^\s*```\s*$", lines[index]):
                    code_lines.append(lines[index])
                    index += 1
                if index < len(lines):
                    index += 1
                label = f'<span style="{CODE_LABEL_STYLE}">{html.escape(language)}</span>' if language else ""
                code = html.escape("\n".join(code_lines))
                html_blocks.append(
                    f'<pre style="{PRE_STYLE}">{label}<code>{code}</code></pre>'
                )
                continue

            if _is_heading(line):
                flush_paragraph()
                level, text = _heading_parts(line)
                style = HEADING_STYLES[min(level, 4)]
                html_blocks.append(
                    f'<h{level} style="{style}">{self.inline(text)}</h{level}>'
                )
                index += 1
                continue

            if _is_horizontal_rule(line):
                flush_paragraph()
                html_blocks.append(f'<hr style="{HR_STYLE}" />')
                index += 1
                continue

            if index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
                flush_paragraph()
                table_html, index = self._render_table(lines, index)
                html_blocks.append(table_html)
                continue

            quote_match = re.match(r"^\s{0,3}>\s?(.*)$", line)
            if quote_match:
                flush_paragraph()
                quote_lines = []
                while index < len(lines):
                    match = re.match(r"^\s{0,3}>\s?(.*)$", lines[index])
                    if not match:
                        break
                    quote_lines.append(match.group(1).strip())
                    index += 1
                quote = " ".join(quote_lines).strip()
                html_blocks.append(
                    f'<blockquote style="{BLOCKQUOTE_STYLE}">{self.inline(quote)}</blockquote>'
                )
                continue

            list_match = _list_marker(line)
            if list_match:
                flush_paragraph()
                list_html, index = self._render_list(lines, index, list_match[0])
                html_blocks.append(list_html)
                continue

            paragraph.append(line)
            index += 1

        flush_paragraph()
        return f'<section style="{SECTION_STYLE}">\n' + "\n".join(html_blocks) + "\n</section>"

    def _render_table(self, lines: list[str], start: int) -> tuple[str, int]:
        headers = _split_table_row(lines[start])
        alignments = _table_alignments(lines[start + 1])
        rows: list[list[str]] = []
        index = start + 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            rows.append(_split_table_row(lines[index]))
            index += 1

        html_rows = [f'<table style="{TABLE_STYLE}"><thead><tr>']
        for cell_index, cell in enumerate(headers):
            align = alignments[cell_index] if cell_index < len(alignments) else "left"
            html_rows.append(
                f'<th style="{TH_STYLE} text-align:{align};">{self.inline(cell)}</th>'
            )
        html_rows.append("</tr></thead><tbody>")
        for row in rows:
            html_rows.append("<tr>")
            for cell_index in range(len(headers)):
                cell = row[cell_index] if cell_index < len(row) else ""
                align = alignments[cell_index] if cell_index < len(alignments) else "left"
                html_rows.append(
                    f'<td style="{TD_STYLE} text-align:{align};">{self.inline(cell)}</td>'
                )
            html_rows.append("</tr>")
        html_rows.append("</tbody></table>")
        return "".join(html_rows), index

    def _render_list(self, lines: list[str], start: int, ordered: bool) -> tuple[str, int]:
        tag = "ol" if ordered else "ul"
        style = OL_STYLE if ordered else UL_STYLE
        html_items = [f'<{tag} style="{style}">']
        index = start
        while index < len(lines):
            match = _list_marker(lines[index])
            if not match or match[0] != ordered:
                break
            html_items.append(f'<li style="{LI_STYLE}">{self.inline(match[1])}</li>')
            index += 1
        html_items.append(f"</{tag}>")
        return "".join(html_items), index

    def inline(self, text: str) -> str:
        placeholders: list[str] = []

        def hold(value: str) -> str:
            placeholders.append(value)
            return f"\x00P{len(placeholders) - 1}\x00"

        def code_repl(match: re.Match[str]) -> str:
            value = html.escape(match.group(1), quote=False)
            return hold(f'<code style="{CODE_STYLE}">{value}</code>')

        def image_repl(match: re.Match[str]) -> str:
            alt = match.group(1).strip()
            source = match.group(2).strip()
            resolved = self.image_resolver.resolve(source)
            return hold(
                '<img '
                f'src="{html.escape(resolved, quote=True)}" '
                f'alt="{html.escape(alt, quote=True)}" '
                f'style="{IMG_STYLE}" />'
            )

        def link_repl(match: re.Match[str]) -> str:
            label = html.escape(match.group(1), quote=False)
            href = html.escape(match.group(2).strip(), quote=True)
            return hold(f'<a href="{href}" style="{A_STYLE}">{label}</a>')

        text = re.sub(r"`([^`]+)`", code_repl, text)
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_repl, text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, text)
        rendered = html.escape(text, quote=False)
        rendered = re.sub(r"\*\*(.+?)\*\*", rf'<strong style="{STRONG_STYLE}">\1</strong>', rendered)
        rendered = re.sub(r"__(.+?)__", rf'<strong style="{STRONG_STYLE}">\1</strong>', rendered)
        rendered = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", rf'<em style="{EM_STYLE}">\1</em>', rendered)
        rendered = re.sub(r"(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)", rf'<em style="{EM_STYLE}">\1</em>', rendered)
        for idx, value in enumerate(placeholders):
            rendered = rendered.replace(f"\x00P{idx}\x00", value)
        return rendered


SECTION_STYLE = "max-width:100%;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#1f2329;line-height:1.75;font-size:16px;letter-spacing:0;"
P_STYLES = "margin:0 0 16px;color:#1f2329;line-height:1.75;font-size:16px;"
HEADING_STYLES = {
    1: "margin:0 0 22px;font-size:24px;line-height:1.35;font-weight:700;color:#111827;",
    2: "margin:28px 0 14px;padding-left:10px;border-left:4px solid #07c160;font-size:20px;line-height:1.4;font-weight:700;color:#111827;",
    3: "margin:24px 0 12px;font-size:18px;line-height:1.45;font-weight:700;color:#111827;",
    4: "margin:20px 0 10px;font-size:16px;line-height:1.5;font-weight:700;color:#111827;",
}
BLOCKQUOTE_STYLE = "margin:16px 0;padding:10px 14px;border-left:4px solid #d0d7de;background:#f6f8fa;color:#57606a;line-height:1.7;"
PRE_STYLE = "margin:18px 0;padding:14px 16px;background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.6;color:#24292f;overflow-x:auto;"
CODE_STYLE = "padding:2px 5px;background:#f6f8fa;border-radius:4px;font-family:SFMono-Regular,Consolas,'Liberation Mono',monospace;font-size:90%;color:#24292f;"
CODE_LABEL_STYLE = "display:block;margin-bottom:8px;color:#6b7280;font-size:12px;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;"
TABLE_STYLE = "border-collapse:collapse;width:100%;margin:18px 0;font-size:14px;line-height:1.55;color:#1f2329;"
TH_STYLE = "border:1px solid #d0d7de;padding:9px 10px;background:#f6f8fa;font-weight:700;"
TD_STYLE = "border:1px solid #d0d7de;padding:9px 10px;vertical-align:top;"
UL_STYLE = "margin:0 0 16px 1.2em;padding:0;color:#1f2329;line-height:1.75;"
OL_STYLE = UL_STYLE
LI_STYLE = "margin:0 0 6px;padding:0;"
IMG_STYLE = "max-width:100%;height:auto;display:block;margin:18px auto;border-radius:6px;"
A_STYLE = "color:#0b57d0;text-decoration:none;border-bottom:1px solid rgba(11,87,208,.35);"
STRONG_STYLE = "font-weight:700;color:#111827;"
EM_STYLE = "font-style:italic;"
HR_STYLE = "border:0;border-top:1px solid #d0d7de;margin:24px 0;"


def _is_heading(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}#{1,6}\s+.+$", line))


def _heading_parts(line: str) -> tuple[int, str]:
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
    assert match
    return len(match.group(1)), match.group(2)


def _is_horizontal_rule(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}(-\s*){3,}$", line.strip()))


def _list_marker(line: str) -> tuple[bool, str] | None:
    ordered = re.match(r"^\s{0,3}\d+[.)]\s+(.+)$", line)
    if ordered:
        return True, ordered.group(1).strip()
    unordered = re.match(r"^\s{0,3}[-*+]\s+(.+)$", line)
    if unordered:
        return False, unordered.group(1).strip()
    return None


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(re.match(r"^:?-{3,}:?$", cell.strip()) for cell in cells)


def _table_alignments(separator: str) -> list[str]:
    alignments = []
    for cell in _split_table_row(separator):
        cell = cell.strip()
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append("center")
        elif cell.endswith(":"):
            alignments.append("right")
        else:
            alignments.append("left")
    return alignments


def make_preview_document(content_html: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
</head>
<body style="margin:0;background:#f5f5f5;">
  <main style="max-width:720px;margin:0 auto;padding:32px 18px;background:#fff;min-height:100vh;">
{content_html}
  </main>
</body>
</html>
"""


def write_output_html(path: Path | None, content_html: str, title: str) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(make_preview_document(content_html, title), encoding="utf-8")


def build_article(
    title: str,
    author: str | None,
    digest: str | None,
    content_html: str,
    thumb_media_id: str,
    source_url: str | None,
    need_open_comment: int,
    only_fans_can_comment: int,
) -> dict:
    article = {
        "article_type": "news",
        "title": title,
        "content": content_html,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": need_open_comment,
        "only_fans_can_comment": only_fans_can_comment,
    }
    if author:
        article["author"] = author
    if digest:
        article["digest"] = digest
    if source_url:
        article["content_source_url"] = source_url
    return article


def load_article_inputs(args: argparse.Namespace) -> tuple[Path, dict[str, str], str]:
    markdown_path = Path(args.markdown_path).expanduser().resolve()
    if not markdown_path.exists():
        raise WeChatPublisherError(f"Markdown file not found: {markdown_path}")
    metadata, body = parse_frontmatter(read_text(markdown_path))
    return markdown_path, metadata, body


def metadata_value(args: argparse.Namespace, metadata: dict[str, str], name: str) -> str | None:
    value = getattr(args, name, None)
    if value:
        return value
    return metadata.get(name)


def render_markdown(
    body: str,
    markdown_path: Path,
    api: WeChatAPI | None = None,
    upload_local_images: bool = False,
    fail_on_remote_images: bool = False,
) -> tuple[str, ImageResolver]:
    resolver = ImageResolver(
        base_dir=markdown_path.parent,
        api=api,
        upload_local=upload_local_images,
        fail_on_remote=fail_on_remote_images,
    )
    renderer = MarkdownRenderer(resolver)
    return renderer.render(body), resolver


def command_preview(args: argparse.Namespace) -> dict:
    markdown_path, metadata, body = load_article_inputs(args)
    title = metadata_value(args, metadata, "title") or first_heading(body) or markdown_path.stem
    digest = metadata_value(args, metadata, "digest") or digest_from_markdown(body)
    validate_article_fields(title, digest)
    content_html, resolver = render_markdown(
        body,
        markdown_path,
        upload_local_images=False,
        fail_on_remote_images=args.fail_on_remote_images,
    )
    output_html = Path(args.output_html).expanduser().resolve() if args.output_html else None
    write_output_html(output_html, content_html, title)
    return {
        "ok": True,
        "mode": "preview",
        "title": title,
        "digest": digest,
        "html_path": str(output_html) if output_html else None,
        "warnings": resolver.warnings,
    }


def command_upload_cover(args: argparse.Namespace) -> dict:
    api = build_api(args)
    cover = Path(args.cover).expanduser().resolve()
    result = api.upload_permanent_material(cover, args.cover_material_type)
    return {
        "ok": True,
        "cover": str(cover),
        "material_type": args.cover_material_type,
        "media_id": result.get("media_id"),
        "url": result.get("url"),
        "raw": result,
    }


def command_draft(args: argparse.Namespace) -> dict:
    markdown_path, metadata, body = load_article_inputs(args)
    api = build_api(args)
    title = metadata_value(args, metadata, "title") or first_heading(body) or markdown_path.stem
    author = metadata_value(args, metadata, "author")
    digest = metadata_value(args, metadata, "digest") or digest_from_markdown(body)
    source_url = metadata_value(args, metadata, "source_url")
    validate_article_fields(title, digest)

    thumb_media_id = args.thumb_media_id
    uploaded_cover = None
    if not thumb_media_id:
        cover_arg = metadata_value(args, metadata, "cover")
        if not cover_arg:
            raise WeChatPublisherError(
                "Draft creation requires --cover, frontmatter cover, or --thumb-media-id."
            )
        cover_path = resolve_local_path(cover_arg, markdown_path.parent)
        uploaded_cover = api.upload_permanent_material(cover_path, args.cover_material_type)
        thumb_media_id = uploaded_cover.get("media_id")
        if not thumb_media_id:
            raise WeChatPublisherError(f"WeChat did not return cover media_id: {uploaded_cover}")

    content_html, resolver = render_markdown(
        body,
        markdown_path,
        api=api,
        upload_local_images=True,
        fail_on_remote_images=args.fail_on_remote_images,
    )
    output_html = Path(args.output_html).expanduser().resolve() if args.output_html else None
    write_output_html(output_html, content_html, title)

    article = build_article(
        title=title,
        author=author,
        digest=digest,
        content_html=content_html,
        thumb_media_id=thumb_media_id,
        source_url=source_url,
        need_open_comment=args.need_open_comment,
        only_fans_can_comment=args.only_fans_can_comment,
    )
    draft_result = api.add_draft(article)
    return {
        "ok": True,
        "mode": "draft",
        "draft_media_id": draft_result.get("media_id"),
        "title": title,
        "digest": digest,
        "thumb_media_id": thumb_media_id,
        "uploaded_cover": uploaded_cover,
        "uploaded_images": [image.__dict__ for image in resolver.uploaded],
        "html_path": str(output_html) if output_html else None,
        "warnings": resolver.warnings,
        "raw": draft_result,
    }


def build_api(args: argparse.Namespace) -> WeChatAPI:
    appid = args.appid or os.environ.get("WECHAT_MP_APPID")
    appsecret = args.appsecret or os.environ.get("WECHAT_MP_APPSECRET")
    access_token = args.access_token or os.environ.get("WECHAT_MP_ACCESS_TOKEN")
    cache_env = os.environ.get("WECHAT_MP_TOKEN_CACHE")
    cache_path = Path(cache_env).expanduser() if cache_env else None
    return WeChatAPI(
        appid=appid,
        appsecret=appsecret,
        access_token=access_token,
        use_cache=not args.no_token_cache,
        cache_path=cache_path,
    )


def add_common_article_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("markdown_path", help="Path to the local Markdown article.")
    parser.add_argument("--title", help="Article title override.")
    parser.add_argument("--author", help="Article author.")
    parser.add_argument("--digest", help="Article digest, max 120 characters.")
    parser.add_argument("--source-url", help="Original/source URL for the article.")
    parser.add_argument("--output-html", help="Write a local HTML preview file.")
    parser.add_argument(
        "--fail-on-remote-images",
        action="store_true",
        help="Fail if Markdown contains non-WeChat remote image URLs.",
    )


def add_api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--appid", help="WeChat Official Account AppID. Prefer env WECHAT_MP_APPID.")
    parser.add_argument(
        "--appsecret",
        help="WeChat Official Account AppSecret. Prefer env WECHAT_MP_APPSECRET.",
    )
    parser.add_argument(
        "--access-token",
        help="Use an existing access token instead of fetching one.",
    )
    parser.add_argument("--no-token-cache", action="store_true", help="Disable token caching.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Markdown to styled WeChat HTML and create WeChat MP drafts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview", help="Render Markdown to local preview HTML.")
    add_common_article_args(preview)
    preview.set_defaults(func=command_preview)

    draft = subparsers.add_parser("draft", help="Upload images and create a WeChat draft.")
    add_common_article_args(draft)
    add_api_args(draft)
    draft.add_argument("--cover", help="Local cover image path. Can also be in frontmatter.")
    draft.add_argument(
        "--thumb-media-id",
        help="Existing permanent media_id to use as the article cover.",
    )
    draft.add_argument(
        "--cover-material-type",
        default="image",
        choices=["image", "thumb"],
        help="Permanent material type for --cover. Default: image.",
    )
    draft.add_argument(
        "--need-open-comment",
        type=int,
        choices=[0, 1],
        default=0,
        help="Whether to enable comments.",
    )
    draft.add_argument(
        "--only-fans-can-comment",
        type=int,
        choices=[0, 1],
        default=0,
        help="Whether only followers can comment.",
    )
    draft.set_defaults(func=command_draft)

    cover = subparsers.add_parser("upload-cover", help="Upload a permanent cover material.")
    add_api_args(cover)
    cover.add_argument("cover", help="Local cover image path.")
    cover.add_argument(
        "--cover-material-type",
        default="image",
        choices=["image", "thumb"],
        help="Permanent material type. Default: image.",
    )
    cover.set_defaults(func=command_upload_cover)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except WeChatPublisherError as exc:
        eprint(f"ERROR: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
