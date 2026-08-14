# Grok Studio

这是一个无第三方依赖的图片/视频工作台。它把文档中的 OpenAI 风格接口转发到可配置的上游网关，同时把 ImgBB 上传结果和生成媒体 URL 记录到本地 SQLite，页面可直接在电脑或手机浏览器使用。

## 启动

1. 编辑 `config.json`，至少填写 `upstream_base_url`、`upstream_api_key` 和 `imgbb_api_key`。如果要保护本服务，再填写 `api_key`；留空表示本地调用无需鉴权。
2. 运行 `python app.py`，打开 `http://127.0.0.1:8000/`。

`config.example.json` 提供了配置模板，也可以使用环境变量 `GROK2API_UPSTREAM_URL`、`GROK2API_UPSTREAM_KEY`、`IMGBB_API_KEY` 覆盖配置文件中的值。历史记录默认写入 `data/media.db`。

## 接口

- `POST /v1/images/generations`：生图，成功结果写入图片历史。
- `POST /v1/images/edits`：编辑图片，`image.url` 必须是公共 URL。
- 页面“瀑布流生图”会固定并发 6 个独立的 `n=1` 生图请求，单张失败不会影响其他结果。
- `POST /v1/videos/generations`：创建视频任务，返回 `request_id` 并记录 pending 状态。
- `GET /v1/videos/{request_id}`：查询视频；完成后自动更新 URL。
- `GET /api/videos/{request_id}`：浏览器同源查询别名，服务端自动带配置中的上游 `Authorization`，并保存 pending/progress 状态。
- `POST /v1/media/uploads`（或 `/api/upload`）：上传文件到 ImgBB 并记录 URL。
- `GET /v1/media/history`（或 `/api/history`）：查询本地素材历史，支持 `kind=image|video|upload` 和 `limit`。
- `DELETE /api/history/{id}`：删除一条本地记录；`POST /api/history/delete` 搭配 `{ "ids": ["..."] }` 批量删除。
- `DELETE /api/history?kind=image|video|upload`：按素材类型清空历史；不传 `kind` 清空全部历史。
- `GET /api/prompt-favorites?kind=image|video`：按生图或视频分类读取提示词收藏。
- `POST /api/prompt-favorites`：新增或修改提示词收藏，参数包含 `id`（修改时）、`kind`、`name` 和 `prompt`。
- `DELETE /api/prompt-favorites/{id}`：删除一条提示词收藏。

当上游或 ImgBB 未配置时，接口会返回带有 `configuration_error` 类型的 JSON 错误，而不是伪造生成结果。

服务端会在标准输出记录每次请求的 URL、脱敏参数、状态码和返回结果摘要；API Key、Token 和图片二进制内容不会原样写入日志。
