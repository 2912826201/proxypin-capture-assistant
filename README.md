# ProxyPin 抓包助手 Skill

面向不懂代码的用户，通过 ProxyPin MCP 完成网站或 App 的网络请求抓取与分析。适用于不同应用，不内置特定 App 的接口、商品目录或账号数据。

## 能做什么

- 检查电脑与手机的现有环境，准备实际缺少的工具，安装官方 ProxyPin 和用户指定 App。
- 新建 Android 自动化环境时优先验证任务专用模拟器；App 兼容后由助手准备、操作并抓包。需要真机现有会话、硬件或设备完整性时保留真机。
- 尽量由助手操作手机、触发请求、配置抓包并分析；只有无法代办的操作才主动给出具体提示。
- 按真实响应导出 JSON 数组。商品图片任务交付商品名称、图片 URL、返回的 ID 和来源等信息，以及可批量运行的 Python 下载脚本。
- 默认收集 URL，不批量下载图片；下载脚本默认预览，显式启用后才下载。

## 在新电脑安装与使用

先在目标电脑安装并登录 Codex，然后发送：

```text
请使用 $skill-installer 从 https://github.com/2912826201/proxypin-capture-assistant 安装 Skill。
Skill 位于仓库根目录，名称为 proxypin-capture-assistant。
请检查这台电脑当前支持的 Skill 安装目录并完成安装。
```

这是私有仓库，目标电脑需登录拥有该仓库访问权限的 GitHub 账号。不会将旧电脑的令牌、设备序列号或代理配置迁移过去。安装后若未显示，请重新打开 Codex。

使用时只需说明 App/网站、设备和目标，例如：

```text
请用 $proxypin-capture-assistant 从零配置这台电脑和我的 Android 手机，
抓取我指定 App 的商品名称和图片 URL，输出 products.json 数组和下载脚本。
尽量由你接管手机，只有实在无法操作时提醒我；这次不下载图片。
```

安装与 Skill 加载方式参考 [OpenAI 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。

## 输出

| 任务 | 默认文件 |
| --- | --- |
| 网络请求分析 | `requests.json` |
| 商品与图片采集 | `products.json`、`download_images.py` |
| 其他结构化数据 | `data.json` |

具体字段、证据关联与缺失字段处理见 [输出约定](references/output-contract.md)。图片下载默认只预览；获得商品数据后可执行：

```shell
python download_images.py products.json --download
```

助手应提前准备 Python 并替用户执行所需分析步骤，不要求用户修改脚本。

## 环境与接管条件

- Windows 附带 `scripts/bootstrap_windows.ps1`，其初始化过程不依赖预先安装 Python；默认仅检测，选择安装模式后才准备工具。模拟器、Java、SDK 管理工具和 AVD 由助手按 [隔离模拟器路线](references/emulator.md) 另行准备，helper 本身不安装这些组件。
- MCP、导出和图片下载脚本使用 Python 3.10+ 标准库，无需安装第三方 Python 包。
- Android 真机接管需要已授权 ADB 或可用的手机控制工具。专用模拟器由助手建立并通过 ADB 控制，不要求连接真实手机；实际需要的虚拟化设置、主机重启、解锁或本人登录如无法代办，会主动提示用户。
- iPhone 是否可接管取决于当前可用工具与设备限制，不承诺仅凭 ProxyPin MCP 即可点击手机界面。
- ProxyPin MCP 连通、VPN/代理生效和 HTTPS 正文可解密分别验证。不同 App 的证书信任机制会影响抓取，遇到失败按实际证据处理。

详细步骤见 [空白环境初始化](references/bootstrap.md)、[隔离模拟器路线](references/emulator.md)、[设备接管](references/device-control.md) 和 [手动操作提示](references/manual-steps.md)。系统 CA 用于解决必要的证书信任，不保证绕过登录、证书固定或设备完整性校验；每次重启分别复核 CA、VPN 和 MCP。

## 已验证范围

MCP 客户端、分页导出、下载预览和 Windows 初始化逻辑已做离线测试与空白环境模拟。历史实测中，一个 Android App 在特定模拟器、系统 CA 与 ProxyPin VPN 组合下完成了 HTTPS 抓包；这不代表其他 App 或镜像已验证。新增模拟器准备与恢复流程依据官方资料编写，并进行独立场景检查。**真实空白电脑和手机的完整安装、接管与抓包流程尚未完成验证。** 本仓库作为从零测试的 Skill 基线，结果覆盖范围按每次实际抓取报告。

## 文件结构

```text
SKILL.md
agents/openai.yaml
references/
  bootstrap.md
  device-control.md
  emulator.md
  manual-steps.md
  output-contract.md
  troubleshooting.md
  workflow.md
scripts/
  bootstrap_windows.ps1
  proxypin_mcp.py
  export_capture.py
  download_images.py
```

本 Skill 与 [ProxyPin 项目](https://github.com/wanghongenpin/proxypin) 独立；仓库中只包含助手工作流与配套脚本。
