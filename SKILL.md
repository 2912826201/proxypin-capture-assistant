---
name: proxypin-capture-assistant
description: 按标准阶段从空白电脑准备 ProxyPin 与目标 App，尽量接管设备完成 MCP 抓包、验证和提取；面向不懂代码的用户，主动提醒无法代办的动作，交付真实 JSON 数组与图片批量下载脚本。
---

# ProxyPin 抓包助手

通过 ProxyPin 抓取不同网站、Android/iOS App 的真实请求并交付可用结果。不预设 App、接口、字段或证书。默认由 agent 安装、配置、操作、验证与导出；用户只做工具确实不能代办的动作。

## 唯一执行入口

**先读 [全链路执行规程](references/runbook.md)，按当前阶段执行。** 不先发送一份让用户自行配置的教程，不只提出计划，不跳过成功检查。每次接续工作先读任务状态，再进入下一阶段；阶段状态助手可运行时使用 `scripts/capture_session.py`，未有 Python 时先记录目标与能力探测结果，准备 Python 后再初始化。状态助手检查顺序/文件，不代替真实观察。

只补问上下文确实缺少的 App/网站、使用设备和抓取目标。先验证宿主能否执行本机命令、读写文件、控制设备或调用当前 MCP。普通聊天窗口/远程容器不会因加载 Skill 自动获得本机或手机控制权；能力不足按规程的对应分支处理，不假称已经安装/接管。

所有路径、序列号、包名、端口、工具 schema、字段和令牌从本次环境发现。工作目录放在 Skill/仓库之外，保存本次证据和交付文件，不能把示例数据、token、原始正文或账号内容写回通用 Skill。示例中的变量未赋值、占位符未替换时不执行。

## 固定阶段与放行条件

| 阶段 ID | 必须得到的结果 |
|---|---|
| `target` | 明确目标、范围、是否批量、是否下载及设备要求 |
| `host_tools` | 宿主能力实测；实际解释器能运行助手脚本 |
| `route_device` | 唯一目标设备/实例可访问，控制能力已区分 |
| `app_baseline` | 正确 App/网站在未拦截状态正常进入目标页面 |
| `mcp` | 当前地址/鉴权的 initialize 与 tools/list 成功 |
| `capture_route` | 本次 VPN/代理实际接管；录制状态单独检查 |
| `real_flow` | 目标动作对应的新请求，来源和时间有证据 |
| `required_body` | 所需完整正文及业务状态可验证，或确不需正文 |
| `mapping` | 从真实字段建立映射；请求分析不需商品映射 |
| `sample_output` | 非空真实数组、脚本预览、来源抽查都通过 |
| `batch` | 仅批量任务执行；有界扩展、停止原因、覆盖与缺口 |
| `deliver` | 最终文件重新验证，用户能找到 JSON 与下载脚本 |
| `restore` | 只恢复本次改动，无法验证的项主动说明 |

已有抓包文件走规程的导入分支，跳过不适用的安装/连接步骤并记录依据。仅文件存在、进程启动、MCP 连通、HTTP 200、空数组合法或下载预览退出 0，均不能代替相应业务阶段验收。

## 按当前阶段读取的参考

- [可直接执行的命令配方](references/command-recipes.md)：路径与参数填写来源、状态助手、MCP 参数文件/完整正文、移动 token、本地导出与校验。
- [空白环境初始化](references/bootstrap.md)：缺工具/ProxyPin/目标 App 时。Windows 使用已有 bootstrap helper；实际版本从官方源核实。
- [隔离模拟器路线](references/emulator.md)：新建 Android 自动化环境优先验证兼容性；使用 `prepare_emulator.py` 准备和验证专用实例。已有可用路线继续用；用户指定真机、现有会话/硬件/完整性需要真机时保留真机。
- [设备接管](references/device-control.md)：手机 UI/ADB 或浏览器操作。ProxyPin MCP 读流量不代表它能点手机。
- [连接与抓取](references/workflow.md)、[排错](references/troubleshooting.md)：路由、VPN、MCP、证书、分页失败时，只修当前失败层。
- [输出约定](references/output-contract.md)：按真实目标交付 `requests.json`、`products.json` 或 `data.json` 数组。商品任务包含真实名字/URL、实际 ID/来源，并交付 `download_images.py`；默认预览，不下载图片。
- [手动操作提示](references/manual-steps.md)：只有本人验证、缺控制工具或实际无法代办时，在依赖动作前给 1–3 个具体步骤；完成后独立验证并接回操作。

系统 CA 仅解决必要的证书信任，不保证解决登录、pinning 或设备完整性。只对已确认只读查询进行有界重放；下单、支付、未知写操作不用于探测。用户未回复、时间过去或命令无报错不能当作完成。失败保留当前阶段和证据，说明具体下一步，不生成示例商品充当结果。
