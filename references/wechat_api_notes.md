# WeChat MP API Notes

Use the current service-account docs first:

- Draft add: https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_add
- Upload article content image: https://developers.weixin.qq.com/doc/service/api/material/permanent/api_uploadimage
- Upload permanent material: https://developers.weixin.qq.com/doc/service/api/material/permanent/api_addmaterial
- Access token: https://developers.weixin.qq.com/doc/service/api/base/api_getaccesstoken.html

Important constraints:

- `draft/add` creates a backend draft and returns `media_id`; it does not mass-send.
- `content` should use HTML with inline CSS because WeChat editors strip or rewrite many external styles.
- Body images should use `/cgi-bin/media/uploadimg`. Those images do not count against the permanent material limit, but must be JPG or PNG and less than 1 MB.
- Cover images should be uploaded as permanent material with `/cgi-bin/material/add_material`. `type=image` is the practical default for article covers; `type=thumb` requires JPG and less than 64 KB.
- WeChat filters external images from article content. Upload local images whenever possible.
- Access tokens expire. Cache tokens briefly, but never commit tokens or secrets.
- The public-account backend usually requires the caller IP to be in the official-account IP whitelist. Error `40164` often means the current machine/server IP is not whitelisted.
- Common image errors: `40005` invalid file type, `40009` invalid image size, `40007` invalid media_id.
- Title and digest limits are enforced by WeChat; keep title within 64 characters and digest within 120 characters.
