# 空白电脑与手机初始化

当必需工具、ProxyPin 或用户指定 App 未安装时使用本流程。agent 执行下载、解压、安装、普通设置与验证，不把安装清单交给用户自行研究。用户已要求准备抓包环境时，在该范围内直接完成必要的常规安装，不逐个组件重复请求确认。已经满足的项目直接复用；安装范围由用户抓取目标和实际缺项决定。

## 最小启动条件与发现

当前助手须已有可执行命令/操作设备的工具以及所需文件权限。Skill 能指导和运行准备工作，但不能凭空赋予宿主未提供的手机控制权限。先检测系统、CPU 架构、当前工具和设备，不假定空白电脑装了 Python、ADB、Android Studio、Git 或 Flutter。

按 [全链路规程](runbook.md) 先验宿主能力，再执行host_tools/route_device。Android 在安装前选真机或模拟器。采用模拟器时用 `scripts/prepare_emulator.py` 和 [命令配方](command-recipes.md#模拟器准备) 准备SDK管理工具、Java、模拟器、镜像及任务专用AVD；不发真机接线/USB授权提示。现有Windows bootstrap helper不包含这些组件；两个helper均须分阶段验收，不能把准备成功当作业务抓包成功。

记录 `setup-state.json`：每项组件的来源、版本、路径、状态、安装验证、待处理动作及恢复项目。状态区分 `missing / prepared / installed / verified / pending_user / blocked`；下载到文件不等于安装成功，安装成功不等于手机可控制或 MCP 可抓流量。不在报告中存密码、token、账号或设备内容。

1. 查宿主自带运行时；有依赖发现工具时先用它。验证 Python 3.10+ 能导入 `ssl/json/urllib/concurrent.futures`，并用该实际解释器运行本次所需脚本 `--help`；便携/隔离Python也须通过。可复用时不重复安装或改全局PATH。
2. 需 Android 接管但没有设备控制 MCP / ADB 时，准备官方 SDK Platform-Tools。无需为了 ADB 安装完整 Android Studio、Java、Gradle 或 Flutter。先用官方工具目录的绝对路径运行，避免改用户全局环境。
3. 查已连接设备和用户目标 App。缺少设备/目标名称且无法推断时只补问缺项，不要求用户选技术方案。

ADB 未识别手机时，agent 检查连接、USB 模式和设备驱动；Windows 按实际厂商从其官方来源准备适配驱动，不给所有手机套用 Google 驱动。仍需用户更换数据线、解锁或确认设备授权时才提示。[Android 真机连接](https://developer.android.com/studio/run/device)、[OEM 驱动](https://developer.android.com/studio/run/oem-usb)。

## Windows

可使用 `scripts/bootstrap_windows.ps1`，它自身不依赖 Python。先本地探测，再由 agent 根据实际缺项以 `-Install` 执行所需组件。参数由 agent 填写，不让用户编辑代码。

每次显式传 `-ToolsRoot <任务工作目录中的tools>`，不使用落在Skill内的默认工具目录。读取JSON的`status/probed/installed/pending`；helper最终可退出0却仍有pending，不能以shell退出码判完成。只有选定设备后才执行移动安装并传`-DeviceSerial`。完整命令顺序见 [命令配方](command-recipes.md#解释器与初始化)。

- `-AndroidTools`：下载 Google 官方 Windows Platform-Tools，解压到受控工具目录并验证 ADB。
- `-ProxyPinDesktop`：从 `wanghongenpin/proxypin` 当前正式发布元数据选择匹配 Windows/架构的发布资产，安装或解压。
- `-ProxyPinAndroid`：准备官方 Android APK，设备授权后安装并验证。
- Python 不可复用时，agent 从 [Python 官方 Windows 发布页](https://www.python.org/downloads/windows/) 核实当前稳定、兼容架构的 embeddable ZIP，用 `-PythonArchiveUrl` 准备便携运行时；有核实的上游 SHA256 时同时给 `-PythonArchiveSha256`，可用本地路径用 `-PythonPath`。不要把这里的某个版本号当成未来默认值。
- `-TargetApk` / `-TargetPackage`：适用于已核实来源的用户指定目标 APK；设备选择用 `-DeviceSerial`。商店、拆分安装包等按实际安装路径处理，不将所有 App 假定为单 APK。

示例仅供 agent 按缺项调用：

```text
powershell -NoProfile -File scripts/bootstrap_windows.ps1
powershell -NoProfile -File scripts/bootstrap_windows.ps1 -Install -AndroidTools -ProxyPinAndroid
```

遵守宿主执行限制。若脚本执行被策略阻止，说明具体限制并用现有获准工具完成可做的部分；不擅自降低系统执行策略。助手需要显示 ProxyPin 界面以便操作时正常打开；后台安装/下载助手保持隐藏。

## macOS / Linux

agent 以相同“发现 → 官方下载 → 安装 → 验证”流程执行，不调用 Windows helper。

- ADB：使用 [Google 官方 Platform-Tools](https://developer.android.com/tools/releases/platform-tools) 对应系统 ZIP，解压到任务工具目录，验证 `adb version`。macOS/ Linux 选实际架构支持的构件；Linux USB 访问失败时按当前发行版及设备厂商处理权限/udev，不能把空设备列表立即解释为未接线。
- Python：先复用宿主/已有 Python；否则核实官方发布或发行版维护的软件包。agent 能安装就执行，缺管理员密码时才交给用户本人输入。验证版本和必需标准库后再运行采集脚本。
- ProxyPin：从 [官方发布页](https://github.com/wanghongenpin/proxypin/releases) 选择当前系统资产。macOS 可安装到有权限的 Applications 位置；Linux 根据实际发行版选择兼容的包/运行方式。遇代码签名、系统授权或依赖问题按实际提示处理，不关闭 Gatekeeper 或整体软件安全检查。

任意平台首次启动失败时检查实际错误和发布包依赖，缺什么补什么并重试验证。Windows 缺运行库则核实并安装微软官方兼容架构构件；Linux 使用实际包元数据和发行版包管理器处理依赖。helper 完成下载/解压后，agent 仍须验证程序启动、界面与 MCP，不能把便携包落盘作为全环境完成。

## 安装 ProxyPin 与目标手机 App

默认安装官方可用发布版，不先 clone 整个 Flutter 项目。只有用户要修改/构建 ProxyPin 源码时，才另行准备仓库和当前 README 要求的开发工具链。普通抓包任务按路线安装手机、桌面或两端；手机本机 VPN + MCP 路线可能不需要电脑也运行 ProxyPin。

Android 流程：

1. agent 从官方发布页准备兼容的 ProxyPin APK；能确定手机 ABI 时选匹配构件，不确定时优先官方通用包。核对来源、上游可用的校验/签名，记录本地 SHA256；不能将自己算出的 hash 称为发布者签名验证。
2. 真机首次连接没有手机控制能力时，主动指导最小动作：数据线连接、解锁、开启开发者选项/USB 调试并允许本电脑。agent 在等待时可完成下载和电脑工具准备；授权后立即接管。未授权 ADB 无法自己确认其 RSA 信任弹窗。专用模拟器按其路线自动启动并验证实例身份，不需要用户在真实手机授权。
3. 用已授权目标设备安装 APK；成功后检查 `pm path` / `dumpsys package` 的包名和版本，再启动并读屏。安装受 OEM 提示限制时先检查实际屏幕，能代办的确认由 agent 做；设备密码或不能操作的提示才请用户处理。
4. 目标 App 未安装时，agent 查用户指定的官方站点/官方应用商店，核实应用身份、开发者、包名和兼容版本，再下载/安装。若来源或同名 App 身份无法确认，只问一次必要的来源信息，不猜 APK、不换成别的 App。商店可通过已授权 UI 操作时由 agent 完成；账号密码、二次验证或付费步骤按本人操作/授权处理。
5. 拆分 APK 使用已确认的一整套官方构件和对应安装命令。签名冲突或降级失败时不得靠卸载 App、清空数据、关闭安全检查来“修复”。已有正常 App 直接复用，不自动覆盖用户版本。

iPhone 流程：通过实际可用的设备控制能力与官方 App Store 安装 ProxyPin 和用户指定 App；没有可用 iPhone 控制工具时说明当前限制，必要商店操作由用户完成。不能拿 ADB 代替 iOS 控制，不能声称空白 iPhone 可在所有电脑上全程无人操作。Apple ID、本人密码、生物识别等仍由用户完成。

## 安装后的闭环

agent 打开 ProxyPin，配置所选抓包路线、MCP 和必要证书，再按 [设备接管](device-control.md) 操作目标 App。连通分层验证：工具可运行 → 设备可控制 → App 可打开 → MCP `initialize/tools/list` 成功 → 本次目标动作的新请求可见 → 所需正文可读 → 输出 JSON /脚本。缺哪一层就报告那一层，不能只写“环境安装完毕”。

Android 已授权 ADB 时可尝试 `adb -s <serial> forward tcp:0 tcp:<手机面板实际MCP端口>`，用返回本机端口连接手机 MCP；**仍要手机 Bearer token**，不能因地址变成 loopback 就按免 token 桌面实例处理。附带 MCP 客户端和导出器使用 `--require-token` 强制安全输入/环境凭据。这是基于官方 ADB forward 和手机 MCP 监听方式的可验证连接方案：必须实测初始化和工具调用；成功时不必为了 MCP 另要求同 Wi-Fi。失败再采用局域网等实际可用路径。

完成后先导出结果，恢复本次代理/VPN/过滤设置，移除本次端口转发；保留用户以后需要的已安装工具和应用。模拟器按 [专用路线的恢复与清理](emulator.md#恢复与清理) 处理。临时 APK、截图、抓包正文与安装缓存按用户清理要求处理，不卸载正式工具或删除账号数据。

官方来源：[ProxyPin 发布](https://github.com/wanghongenpin/proxypin/releases)、[MCP](https://github.com/wanghongenpin/proxypin/wiki/MCP%E6%9C%8D%E5%8A%A1)、[Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools)、[ADB](https://developer.android.com/tools/adb)、[Python Windows](https://docs.python.org/3/using/windows.html)。发布资产与版本每次重新核实。
