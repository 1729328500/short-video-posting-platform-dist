"""Configure per-user data and optional Windows shortcuts without administrator rights."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.common import atomic_json, validate_data_path
from launcher.start import HERE, DEFAULT_SOURCES, check_data_dir, default_data_dir, load_install


def configure(root, data=None, port=None, autostart=None):
    root = Path(root).resolve()
    cfg = load_install(root)
    raw = Path(data or cfg.get("data_dir") or default_data_dir()).expanduser()
    target = validate_data_path(raw if raw.is_absolute() else root / raw, root)
    good, reason = check_data_dir(target)
    if not good:
        raise ValueError(reason)
    preferred = port if port is not None else cfg.get("port", 8947)
    if type(preferred) is not int or not 1 <= preferred <= 65530:
        raise ValueError("端口必须在 1 到 65530 之间")
    cfg.update(data_dir=str(target), port=preferred)
    cfg.setdefault("update_sources", list(DEFAULT_SOURCES))
    if autostart is not None:
        cfg["autostart"] = bool(autostart)
    atomic_json(root / "install.json", cfg)
    return cfg


def shortcuts(root, autostart=False):
    if os.name != "nt":
        raise ValueError("快捷方式只支持 Windows")
    # Paths travel as environment variables, never as PowerShell source text.
    env = dict(os.environ, VP_INSTALL_ROOT=str(Path(root).resolve()), VP_AUTOSTART="1" if autostart else "0")
    script = r'''
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$root = $env:VP_INSTALL_ROOT
$name = 'Shipin-Fabu.lnk'
function New-ToolLink($folder) {
    $link = $shell.CreateShortcut([IO.Path]::Combine($folder, $name))
    $link.TargetPath = [IO.Path]::Combine($root, 'Start.bat')
    $link.WorkingDirectory = $root
    $link.Description = 'Short video posting platform'
    $link.Save()
}
New-ToolLink ([Environment]::GetFolderPath('Desktop'))
$startup = [Environment]::GetFolderPath('Startup')
if ($env:VP_AUTOSTART -eq '1') { New-ToolLink $startup }
else {
    $path = [IO.Path]::Combine($startup, $name)
    if (Test-Path -LiteralPath $path) {
        $existing = $shell.CreateShortcut($path)
        if ($existing.TargetPath -eq [IO.Path]::Combine($root, 'Start.bat')) {
            Remove-Item -LiteralPath $path
        }
    }
}
'''
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                   env=env, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--autostart", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)
    try:
        old = load_install(HERE)
        if args.interactive:
            default = args.data_dir or old.get("data_dir") or str(default_data_dir())
            args.data_dir = input("数据目录（回车使用 %s）：" % default).strip() or default
            enabled = args.autostart if args.autostart is not None else old.get("autostart", False)
            answer = input("登录 Windows 后自动启动？[y/N，当前 %s]：" % ("开" if enabled else "关")).strip().lower()
            args.autostart = enabled if not answer else answer in ("y", "yes")
        cfg = configure(HERE, args.data_dir, args.port, args.autostart)
        if not args.no_shortcut:
            shortcuts(HERE, cfg.get("autostart", False))
        print("配置已保存。数据目录：%s；双击 Start.bat 启动。" % cfg["data_dir"])
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print("安装失败：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
