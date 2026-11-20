# optviewer 命令行绘图插件

`optviewer.exe` 已更新到 **1.2.0**，内置 Python 和 matplotlib，直接运行即可。在原有 ORCA/Gaussian 几何优化、过渡态优化、ORCA IRC 和内存使用记录绘图基础上，**新增实时追踪窗口**，并把默认输出分辨率调低以加快生成速度。

## 用法

命令统一按"计算输出文件在前、PNG 在后"书写：

```powershell
# 几何优化、OptTS 或 IRC：自动识别，生成 PNG（默认 dpi=110，更快、文件更小）
optviewer "1.out" "1.png"

# 只给输入文件时进入"实时追踪窗口"模式（见下）
optviewer "1.out"              # 打开实时窗口，追踪 1.out 的增长
optviewer "1.out" -O "图.png"  # 显式指定优化/IRC 图（等价于位置参数写法）
optviewer "1.out" -M "内存.png"   # 单独画内存使用记录

# 实时窗口模式参数
optviewer -w "1.out" "快照.png"  # 显式 -w 进入实时模式，并按指定路径保存快照
optviewer "1.out" --interval 2.5 # 实时轮询间隔（默认 1.5 秒）
```

PNG 路径必须明确给出。每张 PNG 会自动生成同名 CSV；可用 `--csv` 指定其他位置。`-O` 与 `-M` 分别生成独立的图，不能在同一条命令中混用。`--dpi` 仍可手动覆盖默认 110，`--summary` 输出解析摘要。

## 实时追踪窗口（1.2.0 新增）

只给输入文件（或加 `-w`）时，会弹出一个**原生 Windows 窗口**（纯 Win32 实现，不依赖 tkinter 等额外组件），持续追踪该 `.out` 文件的增长：

- 日志每新增一条能量记录（ORCA/Gaussian 的 `FINAL SINGLE POINT ENERGY`、ORCA `Total Energy`、Gaussian 逐步 `SCF Done`），图上就**实时加一个数据点**，窗口标题同步显示最新能量。
- 按键：**S** 随时保存当前图为 PNG（同时写同名 CSV）；**Esc 或 Q** 退出；点窗口关闭按钮亦可。
- 关闭窗口时自动保存最后一张快照（`<日志名>_live.png` 和 `<日志名>_live.csv`）。
- 若运行环境没有可用桌面（如通过 SSH/远程服务调用），会自动退化为控制台追踪：逐条打印新点，结束时同样保存快照。
- 隐藏环境变量 `OPTVIEWER_AUTOCLOSE_MS`（毫秒）可让窗口自动关闭，供脚本/测试使用。

## 既有功能说明

IRC 图显示每步绝对能量、相对 TS 能量、最大梯度及 RMS 梯度；CSV 记录所有路径点的能量（Eh）、相对能量（kcal/mol）、方向、方向内迭代编号及原日志行号。优先使用 `IRC PATH SUMMARY`，避免正向、反向与汇总表重复计数；尚未输出汇总表时可读取正反向迭代表。迭代次数到上限会明确标注，不会作为已收敛处理。原有几何优化模式继续使用六面板收敛图。

内存图按文件中的出现顺序记录实际使用量与模块报告的峰值，标注最高点的 **MB 数值、模块和原日志行号**。CSV 还保留原始记录。支持常见的 `MB in use`、`memory used`、`memory usage`、`peak memory`、RSS 等带单位记录；KB/MB/GB/TB 和 KiB/MiB/GiB/TiB 会统一换算为 MB。`MaxCore`、内存上限、剩余量和预计需要量不计入使用量。无使用记录时会说明原因，不生成虚假曲线。

## 真实数据示例

`examples/` 里有两组实际计算结果：

- `examples/decane/`：**正癸烷 C10H22（32 原子）**，用 ORCA 6.1.1 的 `B3LYP/def2-SVP RIJCOSX Opt Freq` 真实跑完（3 分钟，优化 6 步收敛、0 虚频）。含输入文件、完整日志、能量图（`decane_optfreq.png`）、内存图（43 条记录、峰值 349 MB）和实时追踪窗口保存的快照（`decane_optfreq_live.png`）。可直接当作回归样例：
  ```powershell
  optviewer examples/decane/decane_optfreq.out examples/decane/out.png
  optviewer examples/decane/decane_optfreq.out -M examples/decane/mem.png
  optviewer examples/decane/decane_optfreq.out   # 实时窗口
  ```
- `examples/inputs/`：ORCA 与 Gaussian 输入文件示例（`.inp` / `.gjf` 各一）。

## 性能说明

`optviewer.exe` 每次调用有约 1 秒的固定启动开销（内置 Python 运行时初始化），绘图本身约 1 秒。1.2.0 把默认 dpi 从 180 降到 110，生成的文件明显变小（清晰度对屏幕查看足够）；如果追求更快的"看图"，请使用**实时追踪窗口**——打开后每个新数据点直接在进程内刷新，无需重复启动。

## 构建与测试

维护说明：新增逻辑位于 `optviewer.py`。原有优化绘图器以原包内的 Python 3.14 字节码 `legacy_optviewer.marshal` 保留。原版程序备份在 `backups/optviewer-1.0.0.exe`。运行 `python build_optviewer.py` 可使用原包的 Python、matplotlib 与 Windows 自带 .NET 编译器重新打包，无需下载依赖；`optviewer.exe` 可独立运行。回归检查使用 `python -m pytest tests -q -p no:anyio`（若本机 Anaconda 的 anyio 插件导致 pytest 启动即挂起，用 `-p no:anyio` 禁用即可；也可设环境变量 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`）。
