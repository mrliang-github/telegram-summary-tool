# GitHub 发布检查清单

## 1) 仓库卫生
- [ ] 不提交本地构建产物：`build/`, `dist/`, `src/*.egg-info`
- [ ] 不提交本地依赖目录：`.venv/`, `connectors/gramjs/node_modules/`
- [ ] 不提交本地敏感文件：`.env`, `*.session`, `.gramjs.session`

## 2) 开箱验证
- [ ] 安装后执行 `tg-doctor`，确认 `raw-export` 模式可用
- [ ] 运行示例数据命令（见 README 的快速验证）可以成功输出摘要
- [ ] 如果宣传 AI 能力，确认至少一种 CLI（`claude` 或 `codex`）可被检测到

## 3) 自动化质量
- [ ] 本地执行 `python -m pytest -q` 通过
- [ ] GitHub Actions CI 绿灯

## 4) 文档完整性
- [ ] README 包含：安装、快速验证、真实数据路径、常见问题
- [ ] 平台限制说明清晰（`local` 模式仅 macOS）
- [ ] AI 前置条件清晰（CLI 已安装并登录）

## 5) 发布动作
- [ ] 更新版本号（`pyproject.toml` 和 `__init__.py`）
- [ ] 打 tag（如 `v0.1.1`）并附发布说明
