# WeChat MP Publisher Skill

把本地 Markdown 文章转换成微信公众号更稳定识别的内联 CSS HTML，自动上传正文图片和封面图，并创建微信公众号草稿箱文章。

这个仓库是一个 Codex / Agent Skill。它的核心目标是解决 AI 写好的 Markdown 文章复制到微信公众号后台后出现的排版错乱、表格失效、图片需要手动逐个插入的问题。

## 能做什么

- 将 Markdown 段落、标题、引用、列表、表格、代码块转换为适合微信编辑器的 HTML。
- 为表格、代码块、图片等元素注入内联 CSS，减少复制或 API 写入后的样式丢失。
- 将本地 Markdown 图片上传到微信 `media/uploadimg`，并替换为微信可用图片 URL。
- 上传封面图为微信永久素材，或复用已有 `thumb_media_id`。
- 调用微信 `draft/add` 创建草稿箱文章。
- 先生成本地 HTML 预览，便于发布前检查样式。

它不会直接群发文章。默认只创建草稿，方便你在微信公众号后台最终确认。

## 安装

作为 Codex Skill 使用时，把仓库放到本地 skills 目录：

```bash
cd ~/.codex/skills
git clone https://github.com/huruo789-tech/wechat-mp-publisher.git
```

也可以直接运行脚本：

```bash
python3 scripts/wechat_publisher.py --help
```

脚本只使用 Python 标准库，不需要额外安装 Python 依赖。

## 凭证配置

在微信公众号后台获取 `APPID` 和 `APPSECRET`，然后用环境变量配置：

```bash
export WECHAT_MP_APPID="your_appid"
export WECHAT_MP_APPSECRET="your_appsecret"
```

也支持直接传入已有 access token：

```bash
export WECHAT_MP_ACCESS_TOKEN="your_access_token"
```

不要把 `APPSECRET`、access token 或私有素材 ID 写进仓库。

微信接口通常还要求当前机器或服务器 IP 已加入公众号后台的 IP 白名单。如果遇到 `40164`，优先检查 IP 白名单。

## Markdown 示例

文章支持可选 frontmatter：

```markdown
---
title: AI 自动发布微信公众号文章
author: Your Name
digest: 这是一篇由 AI 辅助生成并自动排版到公众号草稿箱的文章。
cover: ./cover.jpg
source_url: https://example.com/original
---

# AI 自动发布微信公众号文章

这是一段 **加粗文本**。

| 模块 | 作用 |
| --- | --- |
| Markdown | 写作源文件 |
| WeChat API | 上传图片并创建草稿 |

![配图](./images/demo.png)
```

## 本地预览

先把 Markdown 转为 HTML 预览：

```bash
python3 scripts/wechat_publisher.py preview article.md \
  --output-html article.preview.html
```

预览不会访问微信 API，也不会上传图片。

## 创建微信公众号草稿

使用本地封面图：

```bash
python3 scripts/wechat_publisher.py draft article.md \
  --cover cover.jpg \
  --output-html article.preview.html
```

如果封面已经上传过，可以复用 `thumb_media_id`：

```bash
python3 scripts/wechat_publisher.py draft article.md \
  --thumb-media-id MEDIA_ID \
  --output-html article.preview.html
```

常用覆盖参数：

```bash
python3 scripts/wechat_publisher.py draft article.md \
  --title "文章标题" \
  --author "作者名" \
  --digest "摘要" \
  --source-url "https://example.com/original" \
  --cover cover.jpg
```

## 单独上传封面

```bash
python3 scripts/wechat_publisher.py upload-cover cover.jpg
```

返回结果里会包含 `media_id`，后续可以作为 `--thumb-media-id` 使用。

## 微信接口注意事项

- 正文图片使用 `/cgi-bin/media/uploadimg`，只支持 JPG/PNG，且必须小于 1 MB。
- 封面图使用 `/cgi-bin/material/add_material`，默认按 `type=image` 上传。
- 微信会过滤文章正文里的外部图片链接，可靠做法是使用本地图片并让脚本上传。
- `draft/add` 只创建草稿，不会群发。
- 标题建议不超过 64 个字符，摘要建议不超过 120 个字符。

## Skill 文件结构

```text
wechat-mp-publisher/
├── SKILL.md
├── agents/openai.yaml
├── references/wechat_api_notes.md
└── scripts/wechat_publisher.py
```

`SKILL.md` 是给 Agent 读取的工作流说明；`README.md` 是给 GitHub 访客阅读的项目说明。
