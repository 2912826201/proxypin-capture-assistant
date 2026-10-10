# Agent 命令配方

按 [执行规程](runbook.md) 的当前阶段使用。脚本均Python3.10+标准库；Windows初始化脚本不需要预先有Python。每条命令由agent填入本次实测值并执行，不发给用户填写。命令退出非0先查原因；JSON中的pending/errors也要查，不能仅看退出码。

## 路径与参数字典

在工作目录记录`paths.json`，仅存非秘密值。表中缺项保持null，不能编值补齐。每次shell调用是新进程时，重新读取该文件、重新赋变量；不要假定上次的PowerShell变量或env仍存在。

| 字段 | 实际值从哪里取得 |
|---|---|
| `skill_root` | 本次确实含SKILL.md、scripts、references的目录绝对路径 |
| `work_root` | 本任务独立目录，必须在Skill/仓库之外 |
| `python` | 已运行标准库检查及脚本--help的解释器绝对路径 |
| `adb` | bootstrap报告/已有SDK工具的绝对路径；运行adb version验证 |
| `serial` | 本次devices -l；模拟器再匹配emu avd name |
| `target_package` | 已核实APK manifest/商店身份及安装包详情，不是App显示名 |
| `target_apk` | 已核实来源与架构的本地APK路径；已有App可以无此项 |
| `endpoint` | 当前面板URL，或实际forward返回本机端口构成的URL |
| `mobile_mcp_port` | 当前移动MCP面板端口，不用历史端口 |
| `forward_port` | 本次adb forward tcp:0返回值及forward --list复核 |
| `flow_id` | 本次目标动作对应的真实flow记录 |
| `since_ms` | 触发动作前的UTC Unix毫秒时间 |

不在paths.json或会话状态存token、密码、Cookie、原始头/正文。建议每次命令先把**本次已创建的paths.json绝对路径**赋给`$ctxFile`，随后读取：

```powershell
$ctx = Get-Content -LiteralPath $ctxFile -Raw -Encoding UTF8 | ConvertFrom-Json
$skillRoot = $ctx.skill_root
$workRoot = $ctx.work_root
$pythonPath = $ctx.python
$adbPath = $ctx.adb
$serial = $ctx.serial
$endpoint = $ctx.endpoint
```

先检查本阶段需要的变量非空、文件存在。上面是变量读取方式；`$ctxFile`尚未赋真实值时不能执行。macOS/Linux使用等价数组参数与绝对路径，不照搬PowerShell语法。

## 解释器与初始化

Windows先用bootstrap检测，显式传任务外部工具目录。PowerShell的`$skillRoot/$workRoot`须已由本次文件定位/目录创建取得：

```powershell
$toolsRoot = Join-Path $workRoot 'tools'
& (Join-Path $skillRoot 'scripts/bootstrap_windows.ps1') -ToolsRoot $toolsRoot
```

检测只查文件，既不运行ADB/设备命令，也不安装。查看`probed`的component/path；本机已有宿主runtime时优先复用。需要ADB才执行：

```powershell
& (Join-Path $skillRoot 'scripts/bootstrap_windows.ps1') -Install -AndroidTools -ToolsRoot $toolsRoot
```

Python已找到时用`-Install -PythonPath $pythonPath`验收；缺Python时从官方当前稳定发布页确定embeddable ZIP的URL/架构及可用hash，用`-PythonArchiveUrl`和相应校验参数准备，绝不猜一个版本号。读取返回的`installed`项中`component=python_runtime,state=runtime_and_stdlib_verified,path=...`。`status=pending`或pending非空按component处理。

得到解释器后至少对本次会用的脚本运行：

```powershell
& $pythonPath -I -X utf8 -B (Join-Path $skillRoot 'scripts/capture_session.py') --help
& $pythonPath -I -X utf8 -B (Join-Path $skillRoot 'scripts/proxypin_mcp.py') --help
& $pythonPath -I -X utf8 -B (Join-Path $skillRoot 'scripts/export_capture.py') --help
& $pythonPath -I -X utf8 -B (Join-Path $skillRoot 'scripts/download_images.py') --help
```

前三项会导入旁边的辅助模块；Skill已处理隔离/便携Python的脚本定位，不需修改用户全局PATH或runtime `_pth`。不要用WindowsApps的python别名代替可运行解释器。

设备已经验证后才安装移动版与目标APK，必须指定serial与本次目标包名：

```powershell
& (Join-Path $skillRoot 'scripts/bootstrap_windows.ps1') -Install -ProxyPinAndroid -DeviceSerial $serial -ToolsRoot $toolsRoot
& (Join-Path $skillRoot 'scripts/bootstrap_windows.ps1') -Install -TargetApk $ctx.target_apk -TargetPackage $ctx.target_package -DeviceSerial $serial -ToolsRoot $toolsRoot
```

手机VPN/MCP路线无需默认安装桌面版。已有App复用；拆分APK、来源不明、签名冲突按 [初始化](bootstrap.md) 分支处理。helper不启动GUI、不配置VPN、不保证目标App业务正常。

## 会话状态

初始化示例，`$targetLabel`为已确认的非敏感App/网站标签，`$route`取`physical/emulator/browser/existing-capture`；商品任务goal用products：

```powershell
$sessionScript = Join-Path $skillRoot 'scripts/capture_session.py'
& $pythonPath -X utf8 -B $sessionScript init --work-dir $workRoot --target $targetLabel --goal products --route $route --batch
& $pythonPath -X utf8 -B $sessionScript status --work-dir $workRoot
```

仅批量任务加`--batch`；用户指定真机或现有真机会话加`--require-physical`。已有会话用status，不能重新init覆盖。已读真实HAR时用existing-capture，并补实际`--capture-file/--capture-origin/--capture-applicability target-and-goal-reviewed`，不误用为模拟样本。

通过阶段时`$step`必须等于status.current_step，`$evidenceFile`为本次已观察结果/记录：

```powershell
& $pythonPath -X utf8 -B $sessionScript pass --work-dir $workRoot --step $step --evidence-file $evidenceFile --evidence-kind agent-observed
```

此命令不证明证据真实性，只登记agent实际观察与文件SHA；不先写一份`success:true`蒙混通过。未完成时：

```powershell
& $pythonPath -X utf8 -B $sessionScript pending --work-dir $workRoot --step $step --reason user-action-required
```

reason可取`user-action-required/device-locked/tool-unavailable/evidence-missing/verification-failed/other`。用户报告不等于独立验证。尚未进入real_flow且模拟器实测不兼容时：

```powershell
& $pythonPath -X utf8 -B $sessionScript set-route --work-dir $workRoot --route physical --reason emulator-incompatible --evidence-file $fallbackEvidence
```

它会保留历史并要求重验route_device及后续。真机约束不能转emulator；开始捕获后换路线须独立会话。

## 模拟器准备

先读 [镜像选择和系统CA边界](emulator.md)。标准位置是在work_root生成本次`emulator-config.json`，将task_root指向另一个专用子目录；首次使用该子目录不得含无归属的工具或数据。helper也能接受目录里仅有当前配置文件。已有路径与下载归档元数据来自当前实测/官方资料；尚未发现的镜像与profile先为null，不能猜值：

| 配置字段 | 填写规则 |
|---|---|
| `schema_version` | 整数1 |
| `task_root` | 工作目录中的专用模拟器子目录，不能在Skill内 |
| `sdk_root` | 可复用现有SDK；新装放专用tools目录，不覆盖原有构件 |
| `java_home` | 兼容JDK根；新装路径位于task_root内 |
| `system_image` | 初始可为null；discover后选实际完整包ID，满足App minSdk/ABI |
| `avd_name` | 本任务唯一新名字，不是用户已有AVD |
| `device_profile` | 初始可为null；discover后选实际device profile ID |
| `port` | 可选；不填则按当前emulator help-port选空闲端口 |
| `boot_timeout_seconds` | 可选、有界启动等待；具体范围先看helper --help |
| `writable_system` | 可选布尔值，默认false；仅必要的系统CA分支设true |
| `commandline_tools` | 缺管理工具时填对象`url/sha256`，来自Google当前官方归档及hash |
| `jdk_archive` | 缺JDK时填对象`publisher:adoptium,url,sha256`，匹配主机官方构件 |

先获取并保存官方元数据来源与实际版本，不使用未知镜像链接或缺hash归档。已有工具路径/镜像可复用。先执行以下两步，不将check缺工具误当必须已选镜像：

```powershell
$emulatorScript = Join-Path $skillRoot 'scripts/prepare_emulator.py'
& $pythonPath -X utf8 -B $emulatorScript --config $emulatorConfig --action check
& $pythonPath -X utf8 -B $emulatorScript --config $emulatorConfig --action discover
```

discover会准备缺失的已核实JDK/管理工具，返回实际SDK包、device profiles与工具路径，不创建AVD。依据这些候选及APK minSdk/ABI填写同一配置的system_image/device_profile；保存来源，然后再执行：

```powershell
& $pythonPath -X utf8 -B $emulatorScript --config $emulatorConfig --action prepare
& $pythonPath -X utf8 -B $emulatorScript --config $emulatorConfig --action start
& $pythonPath -X utf8 -B $emulatorScript --config $emulatorConfig --action verify
```

check/discover/prepare发现缺项时按next_action处理，不直接start。首次创建AVD前可按实际列表修正镜像/profile；创建并验证归属后配置绑定，换镜像须新建独立任务目录，不能覆盖旧AVD。prepare负责工具/镜像、独立环境和AVD；许可或硬件配置问题需agent查看实际提示并在可用本地终端按步骤处理，只有无法代办才提醒用户。人工完成创建后重新prepare，让helper核对先前创建意图、目录/config.ini/镜像/profile，不能把任意已有AVD认领为本任务。start仅启动进程；verify检查实例/开机/API/ABI，**不保证网络、App、VPN或HTTPS**。恢复时action stop核实本任务AVD才停止，不删除共享工具。

需要系统CA时仍按emulator参考验证本任务隔离AVD、root/remount/信任目录能力。helper不root、不写CA；必要时先用action stop停止本任务实例，将同一配置的writable_system设为true，再action start与verify，记录实际启动参数。不能绕过helper另启一个未登记实例；重启后重新验证CA/VPN/MCP。

## 移动鉴权

由agent先核实设备/当前MCP面板，再导出本次UI dump到private目录。不要把原XML打印到对话，里面可能有token。ADB固定输出目标也需本任务管理：

```powershell
& $adbPath -s $serial shell uiautomator dump /sdcard/proxypin-capture-ui.xml
& $adbPath -s $serial pull /sdcard/proxypin-capture-ui.xml $uiDumpFile
& $pythonPath -X utf8 -B (Join-Path $skillRoot 'scripts/mobile_mcp.py') inspect --ui-dump $uiDumpFile
```

inspect只输出安全节点索引/长度/候选属性。索引包括所有node，必须基于当前已识别MCP token字段选取`$tokenNode`；不能因为是长文本就猜，也不能复用上一份dump的索引。未知值隐藏是正常行为。多个不明确候选、UI无可读值或masked值时不用尝试猜token；走本机终端getpass隐藏输入。

优先建立本任务forward：

```powershell
& $adbPath -s $serial forward tcp:0 ("tcp:" + $ctx.mobile_mcp_port)
& $adbPath -s $serial forward --list
```

保存返回的host端口，核对映射所属serial，形成实际loopback endpoint；不要把0.0.0.0监听地址当连接地址。测试工具列表时安全交接：

```powershell
& $pythonPath -X utf8 -B (Join-Path $skillRoot 'scripts/mobile_mcp.py') run --ui-dump $uiDumpFile --token-node $tokenNode --client mcp -- --endpoint $endpoint --output $toolsFile
```

工具只把token放子进程环境并强制require-token，不存新凭据文件、不改父环境。后续mcp/正文命令用同一wrapper和本次dump；重启/重置token后重新读取。导出时`--client export`，后面是export_capture的真实参数。

若无可读UI：在**用户本机可交互TTY**运行现有client的`--require-token`，让用户在隐藏提示输入；不能在聊天收token，也不能在非交互命令里等待一个不存在的输入框。无安全输入工具时保持mcp阶段待完成，说明需要本机交互终端或已配置原生连接。macOS/Linux同样使用此Python wrapper或隐藏输入，不需PowerShell。

## 工具调用与完整正文

桌面loopback无需token；移动命令必须走上一节wrapper或已安全设置的环境/本机隐藏输入。以下直接客户端示例仅用于已经满足当前鉴权方式的环境：

```powershell
$mcpScript = Join-Path $skillRoot 'scripts/proxypin_mcp.py'
& $pythonPath -X utf8 -B $mcpScript --endpoint $endpoint --output $toolsFile
& $pythonPath -X utf8 -B $mcpScript --endpoint $endpoint --tool get_proxy_status --output $statusFile
```

先读toolsFile中的`tools[].name/inputSchema`。参数写到本次JSON对象文件，用`--arguments-file`传，不在shell里拼接嵌套JSON；它与`--arguments`互斥。文件中必须是真实flow ID、时间和schema允许字段，不含MCP token。例如按当前支持schema生成列表参数，触发前保存基线，触发后重新查询；不能硬塞不支持字段。

```powershell
& $pythonPath -X utf8 -B $mcpScript --endpoint $endpoint --tool list_flows --arguments-file $listArgsFile --output $flowListFile
& $pythonPath -X utf8 -B $mcpScript --endpoint $endpoint --tool get_flow_detail --arguments-file $detailArgsFile --output $detailFile
& $pythonPath -X utf8 -B $mcpScript --endpoint $endpoint --body-flow-id $ctx.flow_id --body-output $bodyFile
```

detail参数指定实际`id`和`redact:true`，预览不能代替完整正文。body自动校验分页/UTF8，正文只落private文件，stdout仅元信息；binary/unavailable失败不发布半份结果。历史正文可加实际`--history-id`。输出目录必须先存在，默认拒绝覆盖，改新文件名或确需替换本任务旧文件时显式`--overwrite`。`--output`只保证当前MCP Bearer脱敏，不能承诺剔除工具返回的所有未知App秘密，读detail时保持服务脱敏。

## 字段映射与样本导出

把完整正文和实际host/path对应起来。以下是**虚构语法说明，不能作为采集数据或默认profile使用**：

```json
{"version":1,"rules":[{"match":{"host":"api.example.test","path":"/items"},"decode":{"type":"json","success":{"path":"code","equals":0}},"records":"data.items.*","fields":{"name":"title","url":"images.*.url","id":"sku"}}]}
```

填写顺序固定：实际请求host/path → 已确认JSON/包装解码 → 实际记录列表路径 → 每条对象的name/url/id相对路径 → 实际业务成功值。记录路径`*`展开数组；名称/ID是单值，URL可展开多图。没有业务码时不造`code:0`；有success字段时按其实际类型和值填写。Base64必须有已验证的实际包装path及confirmed:true，业务success在包装解码前检查。

网络分析不加mapping；商品样本一次只选一个真实flow：

```powershell
$exportScript = Join-Path $skillRoot 'scripts/export_capture.py'
& $pythonPath -X utf8 -B $exportScript --endpoint $endpoint --flow-id $ctx.flow_id --mapping $profileFile --output-dir $sampleDir
```

移动版经wrapper `--client export -- ...`执行，不漏require-token。检查`report.json`和`errors.json`：必须看到mapping命中、实际商品行、有效名称/URL、已判断的业务状态。`run_complete:true`仅表示选定flow处理没有错误；`product_result_ready`和实际计数仍须检查，合法空页不是商品样本成功。复杂跨层关系不支持时说明原因并使用已验证适配提取器，不让用户手写映射。

批量导出已有flow可用实际host/since-ms/limit/max-pages；先估计已捕获数量再明确选择上限。此脚本不自动遍历业务API，导出flow数量上限不代表商品总量或全App覆盖。

## 样本与最终交付校验

在sample_output阶段校验样本；批量完成后在batch或deliver阶段再次校验final。样本文件保持不变，最终文件放独立final目录：

```powershell
& $pythonPath -X utf8 -B $sessionScript validate-output --work-dir $workRoot --products $productsFile
```

它要求非空JSON对象数组，检查name/url/敏感值/示例域名，保留多商品同URL，复制当前download_images.py到JSON旁并默认预览；**不联网、不写图片**。确认是图片资源签名时可用`--allow-signed-image-urls`，每行仍需expiry_status=not_checked，账号认证链接仍拒绝。随后实际抽查来源并pass该阶段。请求分析用`--requests`，其他结构数据用`--data`；没有假商品转成requests的自动伪装。

下载器与JSON应一起链接给用户。解释器未在PATH时最终指令写成本机实际可运行的一行，而不是留`$ctx`或占位符，例如由agent把真实路径代入：

```powershell
& $pythonPath -X utf8 (Join-Path $finalDir 'download_images.py') (Join-Path $finalDir 'products.json') --download
```

此处只在用户明确要下载时执行；默认任务只验证预览。用户要自行执行时给**变量已替换的最终命令**，不让他写代码或猜路径。

## 精确恢复

先导出，再恢复本任务配置。forward只删记录的那条：

```powershell
& $adbPath -s $serial forward --remove ("tcp:" + $ctx.forward_port)
```

模拟器按配置action stop核对AVD；VPN/录制/MCP分别恢复并验证，不用kill-server/remove-all或关闭全局代理替代。UI dump在设备和电脑上都属于临时文件，按用户清理范围精确处理，不能删除别的目录。helper的complete只表示阶段记录完整，真实业务成果与未恢复项仍在最终报告中说明。

## 失败恢复登记

业务无法继续时先按当前step登记pending，实际执行上面的精确恢复，再将观察记录为独立恢复证据：

```powershell
& $pythonPath -X utf8 -B $sessionScript recovery --work-dir $workRoot --evidence-file $restoreEvidence --evidence-kind agent-observed
& $pythonPath -X utf8 -B $sessionScript status --work-dir $workRoot
```

该命令不推进业务阶段，不标complete。未能验证的恢复项留在记录中明确说明，不能用用户一句“恢复了”取代实际观察。若准备继续任务，先重新验收被停止/恢复的连接与配置；旧观测不说明它们当前仍有效。
