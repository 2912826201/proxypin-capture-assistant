# ProxyPin 抓包助手 Skill

面向不懂代码的用户，由具备工具的智能体准备环境、操作设备，通过 ProxyPin MCP 抓取真实请求并交付 JSON。适用于不同 App/网站，不包含特定应用的接口、账号或商品数据。

## 标准执行入口

加载 [SKILL.md](SKILL.md)，随后从 [全链路执行规程](references/runbook.md) 开始。规程为每阶段明确输入、执行、通过条件与失败分支；[命令配方](references/command-recipes.md) 给出工具命令和每个参数的实际来源。

固定阶段为：目标 → 宿主工具 → 设备路线 → App 基线 → MCP → 抓包路由 → 真实请求 → 完整正文 → 字段映射 → 样本 → 批量 → 交付 → 恢复。请求分析、非批量和已有捕获会跳过确实不适用的阶段并记录理由。

`capture_session.py` 检查执行顺序、证据文件和输出，返回当前阶段与下一步。它不替智能体判断证据真假或业务成功。工具启动、MCP 连通、HTTP 200、空数组合法均不能代替业务验收。失败时仍能单独登记恢复，保留原待处理阶段。

## 新电脑与其他智能体使用

需要完整仓库目录，包括 `scripts/` 和 `references/`；只复制 SKILL.md 的文本不能得到附带工具。这是私有仓库，读取它的电脑需要具有访问权限的 GitHub 账号。新环境不迁移旧电脑的令牌、serial 或代理配置。

Codex 可以使用 `$skill-installer` 从本仓库根目录安装：

```text
请从 https://github.com/2912826201/proxypin-capture-assistant 安装
proxypin-capture-assistant Skill，Skill 位于仓库根目录。
```

其他宿主按其支持的技能加载方式读取完整目录。智能体至少需要实际可用的本机命令/文件工具、原生 ProxyPin MCP 或可读取的真实抓包文件。**普通聊天窗口不会因为加载 Skill 自动获得电脑和手机控制权。** 云沙箱的 `127.0.0.1` 也不是用户电脑的 ProxyPin；入口规程先验证能力与执行宿主，缺能力时说明具体入口，不虚构安装或结果。

使用时说明目标即可，例如：

```text
请用 proxypin-capture-assistant 从零配置工具，抓取我指定 App 的
商品名称和图片 URL，输出 JSON 数组与批量下载脚本。
尽量由你操作设备，只在确实无法代办时主动提醒我；这次不下载图片。
```

## 自动准备与设备操作

- Windows 用 PowerShell 初始化助手探测/准备缺少的 Python、ADB、官方 ProxyPin 和已核实的目标 APK；不依赖预先有 Python。工具和采集数据保存在任务目录，不能写进 Skill。
- 新建 Android 自动化环境先验证任务专用模拟器的兼容性。准备助手按 `check → discover → 选择实际镜像/profile → prepare → start → verify` 运行；未知配置先为 null，管理工具准备后再发现候选。记录归属，只控制本任务 AVD。
- 用户指定真机、需要其现有会话、硬件或设备完整性时使用真机。Android 操作需要已授权 ADB 或其他实际控制工具。iPhone 按当前可用工具判断，不承诺 MCP 能点击手机。
- 由智能体代办安装、配置、操作与验证；只有 BIOS/重启、工具不能处理的授权或本人登录等确实不能代办的动作，才主动提示最小步骤并在之后验证。
- 移动 MCP 仍需 Bearer，ADB forward 不取消认证。移动鉴权助手从当前 UI dump 本地交接令牌到子进程环境；不能让用户把令牌发送聊天。
- 系统 CA 只在必要的隔离 AVD 分支处理，不保证解决登录、证书固定或完整性校验。每次重启重新验证 CA、VPN、MCP 与业务请求。

各 Python 助手使用 Python 3.10+ 标准库。相关细节见 [初始化](references/bootstrap.md)、[模拟器](references/emulator.md)、[设备接管](references/device-control.md) 和 [手动动作提示](references/manual-steps.md)。

## 交付结果

| 任务 | 默认交付 |
|---|---|
| 网络请求分析 | `requests.json` 数组 |
| 商品与图片 | `products.json` 数组、`download_images.py` |
| 其他结构化数据 | `data.json` 数组 |

商品记录包含真实名称、图片 URL、响应中已有的 ID 与来源等字段，具体见 [输出约定](references/output-contract.md)。映射从实际完整响应建立；样本必须非空且逐条抽查来源。批量保留样本，最终文件单独重新验证，并报告覆盖、停止原因和缺口，不把已见目录说成未知后端全量。

默认只收集 URL。下载器默认预览，不联网、不创建图片目录；显式 `--download` 才下载。智能体准备解释器后给出已替换为本机真实绝对路径的执行命令，用户无需修改脚本或猜 `python` 是否在 PATH。签名图片 URL 的有效期如未测试必须说明。

## 配套工具

| 工具 | 作用 |
|---|---|
| `bootstrap_windows.ps1` | 探测与按需初始化宿主工具、安装移动 APK |
| `prepare_emulator.py` | 发现工具/镜像、准备和验证任务专用 AVD、精确停止 |
| `capture_session.py` | 阶段顺序、证据变化、样本/最终输出校验、失败恢复记录 |
| `mobile_mcp.py` | 当前移动 UI 令牌的本地安全交接 |
| `proxypin_mcp.py` | MCP schema 发现、参数文件调用、完整正文分页保存 |
| `export_capture.py` | 从实际 flow 提取请求及已映射商品，报告错误和命中情况 |
| `download_images.py` | JSON 图片 URL 预览和显式批量下载 |

`export_capture.py` 处理 MCP flow，不支持 `--har`；真实 HAR 走规程的文件分析分支。采集脚本不会自动遍历未知业务 API，也不会用下单/支付等操作探测。

## 验证范围

`tests/` 包含状态、模拟器、移动鉴权、MCP 和导出管线的离线测试，覆盖空白工具发现、配置归属、正文分页、业务失败、字段未命中和最终文件变化等行为。独立智能体已从空白环境、错误业务响应、远程宿主和交付变更场景审查操作规程，发现的顺序依赖已修正。

这些测试不安装软件、不启动设备、不抓取真实流量。本次更新没有重新验证豆包宿主或真实空白电脑/手机的完整链路。历史上一个 Android App 在特定模拟器、系统 CA 与 ProxyPin VPN 组合下完成过 HTTPS 抓包，不能据此保证其他 App 或环境也成功。每次使用仍需按真实证据逐阶段验收。

本 Skill 与 [ProxyPin 项目](https://github.com/wanghongenpin/proxypin) 独立；仓库仅包含工作流、配套代码与离线测试。
