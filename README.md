# STEP-Seg Annotator

用于 STEP B-Rep 零件的人工特征实例分割工具。它以面为最小标注单元：导入 STEP，选中一个面，预览规则候选，确认后将面组归入一个带类别的实例。

## 当前 MVP

- 导入 `.step` / `.stp`，支持一个文件中的多个 solid；
- OpenCascade 原生视图：旋转、平移、缩放、标准视角、线框；
- 面级点击、候选区域预览、手动增删面、实例创建/修改、底面标记；
- 原生 `.stepanno.json` 保存、自动保存、校验与 AAGNet 标签导出；
- 标签体系可在界面中维护，`background` 为保留类别。

## 安装与运行

```bash
uv sync --locked --dev --no-editable
uv run --no-sync stepseg-annotator
```

也可以直接打开文件：

```bash
uv run --no-sync stepseg-annotator /path/to/part.step
```

命令行校验与导出：

```bash
uv run --no-sync stepseg validate /path/to/part.stepanno.json
uv run --no-sync stepseg export-aagnet /path/to/part.stepanno.json --output exports
```

开发检查：

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

## 标注流程

1. 打开 STEP；每个 solid 的所有面会先归到各自的 `background` 实例。
2. 单击一个面，选择规则候选（种子 / 同类曲面 / 相切 / 连通）；按住 Shift 可补面，按住 Ctrl 可删面。
3. 选择类别后新建或更新实例；可选地为实例标记底面。
4. 填写标注者、复核者并保存。所有面已唯一归属时，可将状态设为 `completed` 或 `reviewed`。
5. 导出会生成 AAGNet 所需的 `seg`、`inst`、`bottom` 数据和 `source_map.json`。只有在标签体系维护了 AAGNet ID 时，才可使用严格 25 类导出。

真实 CAD 与标注结果默认不进入 Git；代码仓库只保存程序、格式规范和小型合成测试。
