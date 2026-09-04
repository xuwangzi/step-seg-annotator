# STEP-Seg Annotator

用于对 STEP CAD 模型进行面级分割标注。程序先融合输入中的 Solid，并使用
`unionseam` 的局部几何判据恢复布尔并集抹掉的解析边界，再由标注员把生成的细面归并为面组。

## 当前功能

- 导入 `.step` / `.stp`，优先融合多个 Solid；
- 根据同一直线、圆或椭圆上的边界缺口检测 seam，并在面上压印；
- 以细面为最小单元进行点击、追加、移除和框选；
- 创建、更新和删除面组，并维护名称、类别、颜色与备注；
- 对选择和面组操作执行撤销、重置及自动保存；
- 保存 schema `3.0` 面组 JSON 和派生 STEP 快照；
- 导出 `partition.step` 与包含细面到面组映射的 `manifest.json`；
- 继续读取和导出 schema `2.0` 的旧 Solid 分割标注。

B 样条不会参与 seam 推断。无法安全融合的多 Solid 输入会保留为 Compound，所有面仍进入统一面池。

## 安装与运行

需要 Python 3.12 和 `uv`：

```bash
uv sync --locked --dev --no-editable
uv run --no-sync stepseg-annotator
uv run --no-sync stepseg-annotator /path/to/part.step
```

## 标注流程

1. 打开 STEP，等待融合、seam 检测和面压印完成。
2. 新建或选择左侧面组。
3. 单击细面；普通单击和 `Shift` 追加未选面，`Ctrl` 移除。开启“面框选”后拖动可见的选择框，投影与框相交的可见面都会加入选择集。
4. 点击“合并到当前组”，然后填写名称、类别和备注。
5. 所有细面恰好归入一个面组后，状态才可设为 `completed` 或 `reviewed`。
6. 标注保存到源文件旁的 `.stepseg.json`，派生几何位于 `.stepseg-cache/`。

导出目录：

```text
export/
├── manifest.json
└── partition.step
```

## 命令行

```bash
uv run --no-sync stepseg inspect /path/to/part.step
uv run --no-sync stepseg validate /path/to/part.stepseg.json
uv run --no-sync stepseg export-faces /path/to/part.stepseg.json --output exports
```

旧 schema `2.0` 文件仍使用：

```bash
uv run --no-sync stepseg export-solids /path/to/legacy.stepseg.json --output exports
```

开发检查：

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

真实 CAD、标注 JSON、`.stepseg-cache` 和导出结果默认不进入 Git。
