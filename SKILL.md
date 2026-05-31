---
name: wechat-mp-publisher
description: Convert local Markdown articles into styled WeChat Official Account draft articles, upload local images to WeChat, upload or reuse cover media, and create draft-box entries through the WeChat MP API. Use when Codex needs to publish AI-written Markdown, tables, code blocks, or local image-rich articles to a WeChat Official Account / 微信公众号草稿箱, or when a user asks to fix Markdown formatting for WeChat articles.
---

# WeChat MP Publisher

## Workflow

Use `scripts/wechat_publisher.py` as the deterministic implementation. Default to creating a draft in the WeChat backend, not mass-sending to subscribers.

1. Read `references/wechat_api_notes.md` when credentials, API limits, image rules, or WeChat errors matter.
2. Ask for or infer the local Markdown path, title, cover image, author, digest, and source URL.
3. Run a preview first unless the user explicitly wants to create a draft immediately:

```bash
python3 /Users/huruo/.codex/skills/wechat-mp-publisher/scripts/wechat_publisher.py preview ARTICLE.md --output-html ARTICLE.preview.html
```

4. Create the WeChat draft when credentials are available:

```bash
export WECHAT_MP_APPID="..."
export WECHAT_MP_APPSECRET="..."
python3 /Users/huruo/.codex/skills/wechat-mp-publisher/scripts/wechat_publisher.py draft ARTICLE.md --cover COVER.jpg --output-html ARTICLE.preview.html
```

Use `--thumb-media-id MEDIA_ID` instead of `--cover COVER.jpg` when the cover media is already uploaded. Use `--title`, `--author`, `--digest`, and `--source-url` to override Markdown frontmatter or detected defaults.

## Markdown Input

Support optional frontmatter:

```markdown
---
title: Article title
author: Author name
digest: Short summary
cover: ./cover.jpg
source_url: https://example.com/original
---
```

Local Markdown images such as `![alt](images/a.png)` are resolved relative to the Markdown file and uploaded to WeChat during `draft`. Remote non-WeChat image URLs are left unchanged and may be filtered by WeChat; use local files when reliability matters.

## Safety

Never place `APPSECRET`, access tokens, or private media IDs in the skill files or Git history. Prefer environment variables. If the user asks for actual mass publish or send-all behavior, explain that this skill creates drafts only and ask for explicit confirmation before adding irreversible publishing code.
