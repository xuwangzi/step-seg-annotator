# STEP-Seg Annotator

用于对 STEP CAD 模型进行面级分割标注。程序先融合输入中的 Solid，并使用
`unionseam` 的局部几何判据恢复布尔并集抹掉的解析边界，再由标注员把生成的细面归并为面组。

## 当前功能

- 导入 `.step` / `.stp`，优先融合多个 Solid；
- 根据同一直线、圆或椭圆上的边界缺口检测 seam，并在面上压印；
- 以细面为最小单元进行点击、追加、移除和面连选；
- 创建、更新和删除面组，并维护名称、类别、颜色与备注；
- 对选择和面组操作执行撤销、重置及自动保存；
- 保存 schema `3.0` 面组 JSON 和派生 STEP 快照；
- 导出 `partition.step` 与包含细面到面组映射的 `manifest.json`；
- 继续读取和导出 schema `2.0` 的旧 Solid 分割标注。

B 样条不会参与 seam 推断。无法安全融合的多 Solid 输入会保留为 Compound，所有面仍进入统一面池。

## 安装与运行

需要 Python 3.12 和 `uv`。首次安装或依赖发生变化时执行：

```bash
uv sync --locked --dev --no-editable
uv pip install --python .venv/Scripts/python.exe --no-deps --editable .
```

代码更新后不需要重新安装。关闭正在运行的旧程序，在项目目录执行：

```powershell
cd D:\code\step-seg-annotator
.\.venv\Scripts\stepseg-annotator.exe
```

也可以在启动时指定 STEP 文件：

```powershell
.\.venv\Scripts\stepseg-annotator.exe D:\path\to\part.step
```

项目使用 editable 安装后，程序会直接加载 `src` 下的当前源码。若切换了 Git 分支，先确认分支和工作区状态：

```powershell
git branch --show-current
git status
```

如果不使用 editable 安装，也可以始终通过源码启动：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stepseg.launcher
```

## 标注流程

1. 使用“打开 STEP”打开单个文件，或使用“打开文件夹”载入当前目录下的全部 `.step/.stp` 文件。
2. 在左侧上方 STEP 列表中切换文件；左侧下方显示当前文件的面组。
3. 等待融合、seam 检测和面压印完成，在左下角新建面组，或在面组列表中选择已有面组。
4. 默认使用“面点选”点击细面；按住空格后操作提示会变为“面连选”，移动鼠标经过的可见细面会连续加入当前面组。已经属于其他面组的面不能重复选择；点选时 `Ctrl` 可移除当前组中的面。
5. 在右侧“面组信息”中填写名称、颜色、类别和备注，点击“更新面组信息”。
6. 所有细面恰好归入一个面组后，状态才可设为 `completed` 或 `reviewed`。
7. 标注保存到每个源文件旁的 `.stepseg.json`，派生几何位于各自的 `.stepseg-cache/`。

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
