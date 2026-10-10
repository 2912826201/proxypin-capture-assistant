# JSON 与脚本交付约定

根据用户目标输出真实数组，不能因为目标是通用抓包就强迫所有请求变成商品。agent 负责提取、生成文件并运行验证，用户无需写字段映射或代码。

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

agent 从完整真实响应生成 mapping，而非让用户手工填写。脚本支持规则的精确 `match.host/path`、`records` 列表位置，以及每条记录的 `fields.name/url/id`。以下**虚构结构**只演示语法，不是任何 App 的默认配置：

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

签名资源 URL 可能过期，与登录凭据要区别判断。签名图片链接在用户指定抓取范围内可导出并注明有效期未验证；认证/会话凭据不能混入可分享结果。确认是目标图片资源的签名查询时，agent 在对应 mapping rule 设置 `allow_signed_image_urls:true`，脚本保留受支持的资源签名 URL 并标记 `expiry_status:not_checked`；该选项仍不放行账号凭据。导出器对无法确认的受保护链接保守放入待处理项，agent 按实际字段和权限判断处理，不默认要求用户重新批准整个采集任务，也不将脱敏坏链接冒充可下载链接。

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

标准流程使用 `capture_session.py validate-output`：sample_output检查不可变样本；batch/deliver分别检查最终文件，自动复制下载器并默认预览。结构验证不代替来源抽查，批量必须再校验最终数组；最终文件改变需重新检查。命令见 [交付配方](command-recipes.md#样本与最终交付校验)。

最终给用户可点击的 JSON/脚本路径、实际结果及必要使用方式；内部命令和调试过程由 agent 处理。只有用户需要自行下载时才解释一条运行命令及 Python 的实际安装情况。
