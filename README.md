# STEP-Seg Annotator

用于把已经融合的 STEP 零件人工切分为多个独立、封闭的 OpenCascade Solid。工具以表面点击作为入口，推荐能够真正切开当前实体的平面，预览后由用户确认。

## 当前 v2 功能

- 导入 `.step` / `.stp`，原文件中的每个 Solid 初始化为一个实体；
- 点击实体表面，搜索并排序有效平面切分候选；
- 彩色预览切分结果，确认后生成多个闭合 Solid；
- 实体独立选择、隐藏、命名、着色和可选分类；
- 撤销上一次切分、重置原始模型、自动保存并重放切分历史；
- 导出组合 STEP、每个实体的独立 STEP 和 `manifest.json`；
- 支持多 Solid 输入，校验几何有效性和切分前后体积守恒。

首版只处理具有可观察平面连接证据的加法结构，例如底座、方块、圆柱和凸台。孔、槽、凹腔、自由曲面连接以及没有可见接缝的建模历史恢复不在当前范围内。

## 安装与运行

```bash
uv sync --locked --dev --no-editable
uv run --no-sync stepseg-annotator
```

直接打开零件：

```bash
uv run --no-sync stepseg-annotator /path/to/part.step
```

## 使用流程

1. 打开 STEP，左侧显示当前所有闭合实体。
2. 点击需要分离结构的一个外表面，右侧出现有效切平面候选。
3. 切换候选检查彩色预览和各子实体体积，确认后提交切分。
4. 对新实体重复点击和切分；需要时使用“撤销切分”或“重置”。
5. 为实体填写名称和可选类别。同类结构仍保留不同的 `entity_XXXX` 实例 ID。
6. 保存后生成与源文件同目录的 `.stepseg.json`；导出时选择一个空目录。

导出目录结构：

```text
export/
├── combined.step
├── manifest.json
└── entities/
    ├── entity_0002.step
    └── entity_0003.step
```

## 命令行

```bash
uv run --no-sync stepseg inspect /path/to/part.step
uv run --no-sync stepseg validate /path/to/part.stepseg.json
uv run --no-sync stepseg export-solids /path/to/part.stepseg.json --output exports
```

开发检查：

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

真实 CAD、`.stepseg.json` 和导出实体默认不进入 Git。
