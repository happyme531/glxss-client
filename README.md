# glxss-client

LLVision GLXSS Me / G25 智能眼镜的开源 Linux 客户端，提供 PyQt6 控制台和命令行工具。

## 功能

- 摄像头：640×480、1280×720、1920×1080，支持设备最高帧率和完整分辨率拍照。
- 传感器：光照、温度、加速度、陀螺仪、姿态和磁罗盘，支持罗盘校准及航向归零。俯仰角为抬头正、低头负。
- 机身按键：识别 `0x71` 的按下、松开、短按和长按，可绑定拍照、录音等操作。
- 音频：44.1 kHz / 16 bit 双通道录音、WAV 播放、测试音、硬件音量与增益。**左声道 L 为后麦克风，右声道 R 为前麦克风。**
- 显示屏：文字、图片、亮度、开关和摄像头画面回传。面板为 640×400 非透明显示屏，黑色显示为黑色。

采集和视频回传没有固定的软件 FPS 上限。预览通过本机共享内存传送最新帧，按窗口尺寸转换；拍照仍保存原始采集分辨率。系统存在 FFmpeg `libswscale` 时自动加速，否则使用 Pillow，无需安装 FFmpeg 命令行程序。

## Linux 安装与启动

需要 Python 3.10+、[uv](https://docs.astral.sh/uv/)、系统 `libusb-1.0` 和可用的桌面图形会话。面向常见 Linux 发行版，不依赖 WSL；目前实机验证环境为 ARM64 Ubuntu / WSLg，其他发行版和架构尚未逐一实测。

安装系统运行库，例如：

```sh
# Debian / Ubuntu
sudo apt install libusb-1.0-0 libxcb-cursor0 fonts-noto-cjk
```

其他发行版安装对应的 libusb、Qt / XCB 运行库及中文字体包即可。

在项目目录安装 Python 依赖。`.venv-linux` 可避免与同一目录下的 Windows 虚拟环境混用：

```sh
UV_PROJECT_ENVIRONMENT=.venv-linux uv sync --extra dashboard --python 3.12
```

若 PyQt6 的 PyPI wheel 不支持当前架构或 glibc 版本，可使用发行版提供的 PyQt6 6.2+。例如在 Debian / Ubuntu 上，改用以下方式创建全新环境，沿用系统 Python 和 Qt，不安装 `dashboard` extra：

```sh
sudo apt install python3-pyqt6
uv venv --python /usr/bin/python3 --system-site-packages .venv-linux
UV_PROJECT_ENVIRONMENT=.venv-linux uv sync
```

配置 USB 权限：将当前用户加入 `plugdev` 组，安装项目的 udev 规则。重新登录使组成员关系生效，然后重新插入眼镜：

```sh
sudo groupadd -f plugdev
sudo usermod -aG plugdev "$USER"
sudo install -m 644 tools/99-glxss.rules /etc/udev/rules.d/99-glxss.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb
```

眼镜每次上电先进入引导状态 `03e7:2150`，需要下载固件到 RAM。加载后会重新枚举为 `2e09:0030`：

```sh
.venv-linux/bin/python glxss.py boot
bash tools/run-dashboard.sh
```

固件加载只写易失性内存，不刷写闪存。拔电后需要重新加载。

### WSL 环境

Windows 端的 USB 共享与挂接由 [usbipd-win](https://github.com/dorssel/usbipd-win) 管理，应用内部不调用 `wsl.exe` 或 `usbipd`。窗口通过 WSLg 显示。

1. 在 Windows 使用 `usbipd list` 查看设备；首次共享用管理员终端执行 `usbipd bind --busid <BUSID>`。
2. 可在 Windows 项目目录执行 `uv run python glxss.py boot` 加载引导状态的固件。
3. 设备重新枚举后，再用 `usbipd list` 查运行态 BUSID，执行 `usbipd attach --wsl --busid <BUSID>`。
4. 在 WSL 按上面的 Linux 步骤启动。若环境没有运行 udev，可临时执行 `sudo .venv-linux/bin/python tools/grant-usb-access.py` 授权当前眼镜节点；重新枚举后需要再次执行。

## 使用说明与限制

- **音频爆音 workaround**：音频页默认勾选“播放音频时暂停摄像头”。应用等待摄像头采集和视频回传停止后再播放，结束后恢复原分辨率、帧率及预览状态。可取消勾选以同时传输；当前设备在 WSL / USB/IP 下同时传输仍可能爆音，根因尚未确认。
- “暂停预览”保留后台采集；播放保护会真正停止摄像头 USB 流。麦克风采集和录音不触发播放保护。
- 录音保存在 `captures/audio/`，照片保存在 `captures/`。文件播放接受 44.1 kHz、16 bit PCM WAV，支持单/双通道。
- 摄像头声明的最高帧率不等于实际输出帧率，界面分别显示实际采集与显示 FPS。回传统计是 USB 发帧速率，尚未测量面板物理刷新率。
- 磁罗盘会受附近磁性物体影响；校准后显示磁航向，未加入磁偏角修正。
- 当前实现针对 G25 固件；G26 / G26PRO 的协议不同，未适配。**尚未实现在眼镜上运行自定义 AI 模型。**

## 命令行

```sh
.venv-linux/bin/python glxss.py info
.venv-linux/bin/python glxss.py sensors --seconds 10
.venv-linux/bin/python glxss.py capture captures/photo.png --mode 1920x1080
.venv-linux/bin/python glxss.py display --text '你好，GLXSS'
.venv-linux/bin/python glxss.py display --image example.png
```

控制台与命令行会占用相同的 USB 接口，请关闭控制台后再运行命令行采集或显示命令。

## 代码结构与验证

- `glxss.py`：G25 协议、RAM 引导、传感器及 CLI。
- `usb_stream.py` / `lcd_stream.py`：独立的异步摄像头采集与显示屏发送。
- `video.py` / `frame_transport.py`：可选原生转色加速与最新帧共享内存。
- `audio.py` / `audio_worker.py`：USB Audio Class 1 等时传输，不依赖 ALSA 驱动或桌面音频服务器。
- `interaction.py` / `playback.py`：按键、罗盘校准及音频播放保护。
- `bridge.py` / `dashboard.py`：本机进程通信和 PyQt6 界面。
- `tools/`：Linux 启动与设备权限配置；`tests/`：协议、状态切换和视频传送回归测试。

```sh
.venv-linux/bin/python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
```

实验脚本、采集样本和反编译资料留在本地忽略目录，不属于运行依赖或提交内容。
