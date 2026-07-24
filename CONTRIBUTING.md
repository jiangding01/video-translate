# 贡献指南

## 本地开发

```bash
uv sync --extra dev --extra asr-mlx
uv run --no-sync xvt doctor
uv run --no-sync pytest -q
```

MLX Whisper 仅适用于 macOS Apple Silicon。其他环境可使用 `--engine faster`，或只运行不依赖 ASR 的测试。

## 修改原则

- 时间轴由 ASR 或源字幕拥有。翻译代码不得改动 cue ID、起止时间或顺序。
- 外部命令使用参数数组执行，不拼接 shell 字符串。
- 新功能应写入 `job.json` 的 stage 和 artifact，便于 `verify`、`resume` 与 JSONL 消费者追踪。
- API Key、Cookie、代理凭据和本地媒体文件不得提交到仓库。
- 保持 CLI 的普通输出可读，JSONL 模式只输出结构化事件。

## 提交前检查

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
```

涉及 FFmpeg、字体、yt-dlp 或翻译提供方时，请至少补充一个边界条件测试。真实 API 或 YouTube 验收应使用自己的凭据和公开测试素材，避免把凭据或下载媒体加入提交。

## 文档同步

当 CLI 参数、产物、默认行为或安全边界变化时，同步检查：

- `README.md`
- `docs/ARCHITECTURE.md`
- `CHANGELOG.md`
- `ROADMAP.md`，仅在已完成或规划状态发生变化时更新

## 首次发布建议

确认 `main` 已包含可运行代码和配套文档后，按 semver 打标签（当前开发版本见 `pyproject.toml` / `CHANGELOG.md`）。发布前再次运行 `doctor`、静态检查和完整测试。
