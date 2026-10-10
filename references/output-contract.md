# JSON 与脚本交付约定

根据用户目标输出真实数组，不能因为目标是通用抓包就强迫所有请求变成商品。agent 负责提取、生成文件并运行验证，用户无需写字段映射或代码。默认按 [脚本生成与验收](script-generation.md) 交付经过真实小样本实测的运行脚本，由脚本负责后续有界批量保存；只有用户要求本次批量才需继续运行到授权范围。

## 脚本生成任务的交付

交付位置使用用户选定文件夹，任务适配、证据和认证材料保留在 Skill/仓库外。商品任务至少交付以下实际文件：

| 文件 | 验收内容 |
|---|---|
| 运行包内的 `collect_data.py`、`target_adapter.py`、`collector_config.json`、`proxypin_mcp.py`、`export_capture.py` | 模块齐全；真实目标适配和配置来自本次观察；独立新进程能启动并采集 |
| 不可变样本目录中的 `products.json` | 非空真实关系数组；名称/真实 ID/URL 对照完整源响应 |
| 运行输出目录中的 `products.json` | 由脚本新采集产生；两页实测有第二页新增 ID，不复制样本冒充独立运行 |
| 运行输出目录中的 `report.json`、`errors.json`、`checkpoint.json` | 状态/停止原因/页数/计数和实际结果一致；部分失败保留数据与恢复位置 |
| 与商品 JSON 同目录的 `download_images.py` | 默认预览通过；本任务未要求下载时 `images_downloaded` 为 `0`，不创建图片目录 |

当前工厂运行包输出商品关系数组；请求分析可用下面的已捕获流量导出器，其他数据格式由 agent 按真实目标编写并验证相应脚本，不能宣称工厂自动支持任意数据。

工厂的 `agent_verified` 和报告中的 `source_evidence` 只记录 agent 对真实样本的断言及报告 hash。运行报告的 `collection_verification:adapter_assertions_not_independently_verified` 明确提醒适配器判断仍需来源核验；工厂成功或运行 `status:probe_complete/completed` 本身不能替代两页/新增 ID/独立运行验收，`completed` 也不代表未知后端全量。真实验证未通过时报告当前阻塞及已有部分，不交示例 JSON 充当成果。

必须实际验证续跑和失败行为：登录界面、目标业务 HTTP 401 或已确认业务登录失效码停止采集并提示正常登录；HTTP 403 停止并报告权限/签名待核实，先检查原 App/网站的正常会话，不能自动断言登录失效。HTTP 200 的 HTML/非 JSON 默认是解析失败，只有实际证据确认登录页才提示重新登录。失败保留本次运行部分结果和检查点，不得空数组成功；明确 `--overwrite` 会替换以前运行结果，不备份旧文件，默认使用新空目录。配置累计页数上限跨续跑生效。直接 API 需要两页新鲜重放及必要的动态签名生成器实测；签名未证明采用 App/浏览器产生新请求的 MCP 路线，交付中注明仍需运行的设备/App/MCP。参数和命令见 [商品运行包接口](script-generation.md#商品运行包的确切接口)。

## 网络分析

`requests.json` 至少包含 `name`（方法/路径标签）、`url`、`method`、`status`、`request_id` 和 `response_summary`。请求摘要 URL 应脱敏账户/认证查询，正文总结描述结构和异常，不复制无关个人数据。未知业务结构时先交付请求分析，不猜商品字段。

已有捕获可用通用导出器，下面是 agent 命令模板：

```text
python scripts/export_capture.py --endpoint <当前MCP地址> --flow-id <真实flowID> --output-dir <本次输出目录>
python scripts/export_capture.py --endpoint <当前MCP地址> --host <已观察到的主机> --since-ms <操作前时间> --limit <范围> --output-dir <本次输出目录>
```

脚本仅读取已有流量，不触发业务重放或改变抓包配置。默认 `limit=20`、`max-pages=10` 是有界样本；报告停止原因，不代表全 App。历史会话或 HAR 可用实际 MCP/agent 解析，不将不支持的参数硬塞给脚本。

## 商品与图片

默认 `products.json` 为 flat JSON 数组，每个商品/图片关系一行，至少有**真实** `name` 和 API 返回的 `url`。可包含 `id`、`source`、`request_id`、分类、图片角色等实际字段。相同 URL 对应不同商品保留多行；下载器合并下载而保留关系。商品 ID、分类 ID、容器 ID 分开。缺少名称/URL 的记录单独输出 `unresolved-images.json`，不要用数组序号或文件名补造商品名。

agent 从完整真实响应生成任务适配，而非让用户手工填写。当前商品运行包接受 `name/url/source`、可选 `id/request_id`，确认的资源签名可增加 `expiry_status`；其他分类/角色字段由任务适配另行维护。下面的通用导出器 mapping 用于已捕获样本，支持规则的精确 `match.host/path`、`records` 列表位置，以及每条记录的 `fields.name/url/id`。以下**虚构结构**只演示语法，不是任何 App 的默认配置：

```json
{
  "version": 1,
  "rules": [{
    "match": {"host": "example.example.test", "path": "/catalog/items"},
    "decode": {"type": "json"},
    "records": "data.products.*",
    "fields": {"name": "title", "url": "images.*.url", "id": "sku"}
  }]
}
```

`.` 访问对象键，`*` 展开数组，`$` 表示当前根。名称/ID 必须是单值，URL 可展开多张图。复杂的跨层关联、特殊键名、额外图片角色由 agent 编写适配提取器，不能强行使用不适用的 mapping。

确认包装字段确为 Base64 UTF-8 JSON 时，可设 `decode={"type":"base64_json","path":"实际包装字段","confirmed":true}`。普通JSON及Base64包装都可用 `decode.success={"path":"实际状态字段","equals":实际成功值}`，支持字符串/整数/布尔/null的严格类型相等；不统一假定业务码。Base64的success在包装解码前验证。mapping不含秘密值、请求头、固定商品文本或可执行代码。

```text
python scripts/export_capture.py --endpoint <当前MCP地址> --flow-id <真实flowID> --mapping <agent生成的profile.json> --output-dir <本次输出目录>
```

常见预览截断或 Unicode 字节边界问题由读取器校验，不将不完整响应记为成功。输出 `report.json` 和 `errors.json` 供 agent 判断计数、覆盖及失败；不能隐藏错误后交付“全量”。

报告会区分`mapping.matched_flows/matched_records/empty_arrays`和`product_result_ready`。没有选定flow、规则完全未命中、缺字段或业务失败不能视为成功；观察到的合法空数组只表示该业务页为空，不能通过非空商品样本验收。导出器只处理已捕获flow，不替agent遍历业务API。

签名资源 URL 可能过期，与登录凭据要区别判断。签名图片链接在用户指定抓取范围内可导出并注明有效期未验证；认证/会话凭据不能混入可分享结果。确认是目标图片资源的签名查询时，商品运行包配置或导出器对应 mapping rule 显式设置 `allow_signed_image_urls:true`，脚本保留受支持的资源签名 URL 并标记 `expiry_status:not_checked`；该选项仍不放行账号凭据。导出器对无法确认的受保护链接保守放入待处理项，agent 按实际字段和权限判断处理，不默认要求用户重新批准整个采集任务，也不将脱敏坏链接冒充可下载链接。

## 批量下载脚本

将 `scripts/download_images.py` 复制到 `products.json` 同目录，提供 ready-to-run 文件。默认预览只读 JSON，不联网、不写图片目录：

```text
python download_images.py
```

用户明确选择下载时，由 agent 执行或提供对应电脑上可直接运行的命令：

```text
python download_images.py --download --output ./images
```

标准库脚本支持并发 1–4、有限重试、超时、图片类型验证，以及已下载文件的 SHA256 校验与续跑；中断的单个文件重新下载。`download-manifest.json` 保留 URL 与多商品关系，`download-errors.json` 保留失败。URL 过期、需会话或返回 HTML 时明确失败，不关闭 TLS 验证、不把登录页保存成图片。无下载请求时仅交付脚本并验证预览，不进行批量下载。

## 交付前检查

解析 JSON 确认为数组；抽查名称/ID/URL 与完整响应对应。统计关系数量和唯一 URL 数量，说明用户操作样本、已遍历目录与未覆盖区域。不得从采集上限推断总量；没有真实结果就报告具体阻塞，不能生成示例数据充当采集成果。

准备/抓包支持流程可使用 `capture_session.py validate-output`：sample_output检查不可变样本；batch/deliver分别检查实际最终文件，自动复制下载器并默认预览。脚本生成任务还必须独立运行生成脚本、验证第二页新增 ID 和检查点，不能把支持流程的结构检查当脚本通过。结构验证不代替来源抽查，批量必须再校验最终数组；最终文件改变需重新检查。已捕获文件校验命令见 [交付配方](command-recipes.md#样本与最终交付校验)。

最终给用户可点击的 JSON/完整运行包/下载器路径、实际计数、已验证范围、停止原因及必要依赖。采集脚本命令由 agent 使用本机实测解释器和全部真实绝对路径填写并实际运行后交付，用户无需改参数或写代码。下载未被要求时只交脚本和预览；需要自行下载时再给一条本机可执行的下载命令。
