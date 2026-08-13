# OpenAI 图片生成与编辑请求格式

本项目的“通用生图”页按 OpenAI-compatible HTTP 接口调用供应商。供应商的基础地址填写到 `/v1` 之前或之后均可，服务端会规范化路径。

## 图片生成

请求：

```http
POST /v1/images/generations
Content-Type: application/json
Authorization: Bearer $OPENAI_API_KEY
```

```json
{
  "model": "gpt-image-1",
  "prompt": "一只戴红色围巾的猫，工作室摄影",
  "n": 1,
  "size": "1024x1024",
  "quality": "auto",
  "output_format": "png",
  "background": "auto"
}
```

常用字段：`model`、`prompt`、`n`、`size`、`quality`、`background`、`output_format`。不同兼容供应商可能只支持其中一部分，通用页只发送用户填写的字段。

## 图片编辑

编辑使用 multipart 表单：

```http
POST /v1/images/edits
Content-Type: multipart/form-data
Authorization: Bearer $OPENAI_API_KEY
```

表单字段：

- `model`：模型名称
- `prompt`：编辑指令
- `image[]`：一张或多张输入图片
- `mask`：可选遮罩图片
- `n`、`size`、`quality`、`output_format`：可选输出参数

本项目的通用页先支持单张图片 URL 或上传图片，服务端会下载 URL 后以 multipart 形式转发。

## 返回格式

兼容服务通常返回：

```json
{
  "created": 1713833628,
  "data": [
    {"url": "https://example.com/image.png"}
  ]
}
```

也可能返回 `b64_json`。本项目会保存 `url` 或 `b64_json` 到素材库。

官方参考：

- https://platform.openai.com/docs/api-reference/images/create
- https://platform.openai.com/docs/api-reference/images/createEdit
