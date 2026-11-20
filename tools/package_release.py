"""Package the portable Windows release without source, caches or user files."""
import configparser
import hashlib
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'optviewer-1.2.0-windows-x64'

GUIDE = '''optviewer 1.2.0 — Windows 便携版

一、直接运行

适用于 Windows x64。已内置 Python 和 matplotlib，无需另行安装。
请先完整解压压缩包，再使用；不要在压缩包内部直接运行。
这是命令行程序，请使用 PowerShell 或命令提示符。

在解压后的目录打开 PowerShell，输入：
  optviewer --version
  optviewer "1.out" "1.png"          # 几何优化/过渡态/IRC 自动识别，出能量图（推荐写法）
  optviewer "1.out" -M "内存.png"     # 单独画内存使用记录
  optviewer "1.out"                  # 只给输入文件 = 打开实时追踪窗口（见"二"）
  optviewer -w "1.out" "快照.png"     # 显式 -w 实时模式，按指定路径保存快照
  optviewer "1.out" --interval 2.5   # 实时轮询间隔（默认 1.5 秒）

-O 自动识别几何优化、过渡态优化与 ORCA IRC，生成能量图。
-M 读取日志中报告的实际内存使用记录，生成内存图并标注最高 MB。
每次绘图同时生成同名 CSV，保存逐步数值。
默认 dpi=110（更快、文件更小）；需要更高清可加 --dpi 220。
使用含空格或中文的路径时，请将路径放在双引号中。

二、实时追踪窗口（1.2.0 新增）

只给输入文件（或加 -w）时，弹出一个原生 Windows 窗口，持续追踪该 .out 的增长：
  - 日志每新增一条能量记录（FINAL SINGLE POINT ENERGY / Total Energy / SCF Done），
    图上实时加一个数据点，窗口标题同步显示最新能量。
  - 按键：S 保存当前图为 PNG（同时写 CSV）；Esc 或 Q 退出；点窗口关闭按钮亦可。
  - 关闭窗口时自动保存最后一张快照：<日志名>_live.png 与 <日志名>_live.csv。
  - 没有可用桌面时自动退化为控制台追踪，逐条打印新点，结束同样保存快照。

三、加入当前用户的 PATH（无需管理员权限）

1. 把解压后的整个文件夹放到一个固定位置，例如：
     C:\\Tools\\optviewer
   确认下面这个文件存在：
     C:\\Tools\\optviewer\\optviewer.exe
2. 按 Win + R，输入 rundll32.exe sysdm.cpl,EditEnvironmentVariables 并回车。
3. 在上方"用户变量"中选择 Path → 编辑 → 新建。
4. 添加 exe 所在的文件夹：C:\\Tools\\optviewer
   不要填写 optviewer.exe 本身，不要替换或删除原有 Path 条目。
5. 逐个点击"确定"，关闭并重新打开终端。
6. 在任意计算文件所在目录验证并运行：
     optviewer --version
     optviewer "1.out" "1.png"
     optviewer "1.out"

四、文件说明

optviewer.exe：独立命令行程序，是运行所需的主体。
optviewer.ini：可选绘图设置，例如 dpi、尺寸；没有它也可使用默认设置。
使用说明.txt：本说明文件。

无需复制 Python、源码、.build、缓存、测试文件、原始计算文件或旧版备份。
该程序读取已有计算输出并绘图，不会运行新的量化计算。
'''


def main():
    folder = ROOT / 'dist' / NAME
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'optviewer.exe', folder / 'optviewer.exe')
    original = configparser.ConfigParser()
    original.read(ROOT / 'optviewer.ini', encoding='utf-8')
    config = configparser.ConfigParser()
    config['plot'] = dict(original['plot'])
    with (folder / 'optviewer.ini').open('w', encoding='utf-8') as file:
        config.write(file)
    (folder / '使用说明.txt').write_text(GUIDE, encoding='utf-8-sig')
    archive = ROOT / 'dist' / (NAME + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for name in ('optviewer.exe', 'optviewer.ini', '使用说明.txt'):
            output.write(folder / name, NAME + '/' + name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    print(f'{archive}\n{archive.stat().st_size / 1e6:.1f} MB\nSHA256: {checksum}')


if __name__ == '__main__':
    main()
