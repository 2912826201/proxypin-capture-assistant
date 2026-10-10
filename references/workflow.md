# 连接与抓取流程

按当前任务选一条路径，不把所有路径的设置叠加给用户。先使用可用工具接管设备，下面表格的用户操作仅在 agent 无法代办时提出。当前工具、平台与运行中的 ProxyPin 面板是最终依据。

## 选路径

| 情况 | 优先做法 | 何时需用户操作 |
|---|---|---|
| 已有 ProxyPin 流量、历史会话或 HAR | 直接读选定会话/文件 | 只有文件尚不可访问时指导导出或选择 |
| 电脑网页/桌面软件 | 本机 ProxyPin MCP + 当前软件实际采用的代理 | 本人密码，或无法控制的确认/界面 |
| 手机已装 ProxyPin | 手机本机 VPN 抓取 + 手机 MCP | 本人密码，或工具无法完成的抓包/VPN/证书步骤 |
| 手机须经电脑抓取 | 电脑 ProxyPin + 手机到电脑的代理路由 | 同网、Wi-Fi 代理/扫码连接及必要证书步骤 |

Android 有已授权 ADB 时默认由 agent 用它操作手机；无 ADB 但有设备控制 MCP 时使用后者。不用 ADB 的路径不要求开发者模式或 USB 调试。手机本地 VPN 不要求额外填写 Wi-Fi 代理；同一 Wi-Fi 仅用于所选局域网连接路径。已有可用路径不要为了统一步骤另装模拟器。

## 探测与连接

1. 检查可用原生 MCP 工具及对应实例；同时连接电脑和手机时确认数据在哪一端。没有 MCP 工具时可通过 HTTP 客户端直连，不默认改全局助手配置。
2. 工具、ProxyPin 或目标 App 未安装时按 [空白环境初始化](bootstrap.md) 自动准备、安装并验证；本人密码或工具无法操作的弹窗才由用户处理。不要降低系统软件安全设置。官方：[仓库](https://github.com/wanghongenpin/proxypin)、[发布页](https://github.com/wanghongenpin/proxypin/releases)。
3. 必须从当前 MCP 面板读取地址：电脑为顶部 AI 图标 → MCP 服务；手机为设置 → MCP 服务。开关显示“运行中”后调用 `initialize`、`tools/list`，再读 `get_proxy_status`。2026-10 核实的官方桌面默认端口为 9127，冲突可变为随机端口；手机端口重启可变。不要固定端口。
4. 当前官方桌面 loopback MCP 不需 token；手机/LAN MCP 需 Bearer。已接管手机且可读面板时，由 agent 在本地获取当前地址/token；含 token 的 UI dump 在本地解析，不原样输出到对话，只报告地址和脱敏状态。否则才用已有安全环境变量或本地隐藏输入，不让用户发 token 到聊天。token 不出现在命令参数或交付文件。手机保持 MCP 服务可用；同网仍需实际可达验证。将手机代理指向电脑时使用电脑已验证的局域网地址，不能填写电脑的 `127.0.0.1`。

附带客户端示例由 agent 按实际环境替换并运行，用户无需编辑：

```text
python scripts/proxypin_mcp.py --endpoint <面板实际URL>
python scripts/proxypin_mcp.py --endpoint <面板实际URL> --tool get_proxy_status
```

客户端直接连桌面 loopback 无 token 可以运行；非 loopback 缺 token 会停止并要求安全输入。手机经 USB 端口转发到本机时仍需 token，使用 `--require-token`。当前默认地址只是方便本机使用，连接失败要读面板，不扫描或猜测别人的 MCP 服务。Windows 控制台遇 Unicode 输出问题时由 agent 使用 Python 的 UTF-8 模式运行。客户端完成初始化通知与协议版本协商；不支持的未来协议版本应使用可用原生连接或适配客户端，而非跳过初始化。协议依据：[MCP 生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)。

## 捕获与观察

读取并记录原状态：录制、SSL、过滤、系统代理以及本任务改动的规则。开启必要的代理/录制；手机系统 VPN 授权不能由 MCP 的录制状态替代。先观察目标操作的新流量，再缩小到相关主机/应用，避免捕获范围排除了尚未发现的真实主机。

触发目标操作前记录 `since_ms` 或 flow ID 基线。优先由 agent 操作手机/浏览器；只有控制不可用时才让用户执行具体动作。操作后读取 `list_flows` 的新记录，结合 URL、时间、参数与响应确认业务来源。若 `list_histories` 支持历史，选用户指定的会话并带 `history_id`，不能把历史记录冒充这次动作。

读取 `get_flow_detail` 时保持默认脱敏。当前部分版本请求头是 `{name,value}` 数组而非对象，按实际返回解析。提取输出不复制认证头、Cookies 或账号数据。遇脱敏字段需要重放时，优先使用服务的 `replay_flow`，不默认关闭脱敏。

截断预览用 `get_flow_body` 分页取完整响应，依据 `offset/returned/total/truncated` 验证拼接；按工具实际 schema 选择分页大小。附带客户端以常见 65536 字节上限读取，若版本不同则 agent 适配。解析前检查正文可用、是否二进制及业务状态。不能用截断的 `send_request` 预览当完整结果。

## 扩大范围

先证实列表、详情和真实字段映射，才扩大采集。分页或分类参数来自捕获请求/响应；重放前确认查询是只读，使用用户当前合法会话，不猜测 ID，不默认调用写接口。记录重放时间/flow ID，避免把 agent 的请求计为真实 App 操作。采用低并发、实际分页停止条件，遇限流、登录失效或重复错误停止并说明具体缺口。

如果当前版本无可用 MCP，可指导用户通过 ProxyPin 导出 HAR，再分析选定文件；主动解释这是备用路径。不能假称 MCP 已连接。没有真实捕获证据时只交付排错状态，不生成假的商品数组。

来源：[ProxyPin 官方 MCP 文档](https://github.com/wanghongenpin/proxypin/wiki/MCP%E6%9C%8D%E5%8A%A1)。接口会更新，以 `tools/list` 和当前 UI 为准。
