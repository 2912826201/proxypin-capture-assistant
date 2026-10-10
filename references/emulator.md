# Android 隔离模拟器抓包路线

用于新建 Android 自动化抓包环境。由 agent 准备工具、建立任务专用 AVD、安装和操作 App、连接移动版 ProxyPin VPN/MCP，交付真实 JSON 与图片下载脚本。这里的命令是 agent 执行模板，不让零基础用户复制或修改。

按需读：设备选择 → 工具与镜像 → 实例启动 → VPN/MCP → 必要的系统 CA → 业务验收 → 恢复与清理。当前官方工具的版本、帮助和实际执行结果优先于示例。

## 设备选择与兼容性

- 已有可用会话/HAR 或抓取路线时继续使用。用户指定真实手机、需要其现有登录会话、摄像头/蓝牙等硬件或设备完整性时使用真机，不迁移其账户数据。
- 其他新建自动化环境优先检查专用模拟器。依据主机架构、目标 APK 的 minSdk/ABI、Google 服务依赖和实际启动选择镜像。先在未拦截状态验证目标页面正常联网；安装成功不等于业务可用。
- 发现模拟器拒绝、完整性校验、必需硬件缺失或无法兼容的 ABI 时说明具体原因并转真机。只有有证据支持的镜像调整才重试，不无限更换版本或降级 App。
- 系统 CA 解决抓包链路的证书信任，不能替代登录、短信/MFA、证书固定或服务端权限。需要本人验证时主动给具体一步提示，继续不依赖该验证的准备。

## 工具与镜像准备

先按 [初始化](bootstrap.md) 发现现有运行时/ADB。模拟器还需要 SDK 管理工具、兼容 Java、emulator 和系统镜像；Windows helper 不包含这些。复用可用共享工具，缺项才从官方来源准备，不为抓包安装完整 Flutter、Gradle、NDK 或 IDE。

1. 探测 OS/CPU、内存、磁盘空间、已有 SDK/JDK、AVD 和在线设备。记录工具绝对路径与版本，任务数据放独立目录。所需空间按实际镜像及运行需求检查，不固定电脑盘符。
2. 读取当前 [Android CLI 文档](https://developer.android.com/tools/agents/android-cli) 和已知问题。2026-10 的新 CLI 在 Windows 尚不支持 `android emulator`，采用官方 command-line tools 的 `sdkmanager/avdmanager/emulator` 兼容入口；其他主机按实际功能选择。不能将旧 SDK 中同名 `android` 程序当成新 CLI。新入口不支持所需镜像、专用目录、端口或 writable-system 参数时使用兼容入口，不创造参数。
3. 从 [Android 官方下载页](https://developer.android.com/studio) 获取当前匹配系统的 command-line tools 归档及可用 SHA256，验证后安全解压，检查归档路径不能逃出目标目录。正确结构包含 `<sdk>/cmdline-tools/<version-or-latest>/bin` 与 `lib`，不能多套一层 `cmdline-tools`。Java 先验证已有 JDK/Android Studio JBR 能运行该 `sdkmanager --version`；缺失时从受支持 JDK 的官方发行方准备匹配归档并校验，只设置任务进程环境，不改全局 PATH。
4. 从已核实的官方/用户来源准备目标 APK，使用实际可用 `apkanalyzer` 或等价工具检查包名、minSdk 和 `lib/<abi>/`；拆分包检查整套构件。模板：

   ```text
   <apkanalyzer> manifest application-id <target.apk>
   <apkanalyzer> manifest min-sdk <target.apk>
   <apkanalyzer> files list <target.apk>
   <sdkmanager> --sdk_root=<sdk> --list --channel=0
   <avdmanager> list device
   ```

5. 从实际包列表选稳定镜像：API 满足 App 最低要求，原生 ABI 与镜像兼容。x86_64 主机优先同架构，Apple Silicon 优先 arm64-v8a；ARM-only APK 不能假定有 x86 转译。Google Play Store 镜像不能 `adb root`；官方 AOSP 可用于 root 排错但没有 Google 服务，Google APIs 标签也不能保证 root。只有确实需要系统 CA 时才据此选支持该能力且 App 兼容的镜像。
6. 按当前 SDK 工具流程处理许可证，安装实际缺少的 `platform-tools`、`emulator`、所选镜像。许可/管理员步骤只有宿主权限或本人身份确实阻止代办时才交给用户。不要无条件更新用户整个共享 SDK。

   ```text
   <sdkmanager> --sdk_root=<sdk> --licenses
   <sdkmanager> --sdk_root=<sdk> --channel=0 "platform-tools" "emulator" "<observed-system-image-id>"
   <emulator> -accel-check
   ```

7. 加速检查失败先读实际诊断。当前 Windows 优先 WHPX，macOS 使用 Hypervisor.Framework，Linux 使用 KVM；以当前官方支持为准。BIOS 虚拟化、主机重启或本人管理员密码实在无法代办时主动说明当前最小动作。`-gpu software` 只处理图形问题，不能弥补 CPU 虚拟化缺失；不通过关闭主机安全功能解决。条件不成立时转真机，避免长时间无进展安装循环。

参考：[sdkmanager](https://developer.android.com/tools/sdkmanager)、[JDK/JBR](https://developer.android.com/build/jdks)、[apkanalyzer](https://developer.android.com/tools/apkanalyzer)、[镜像与 root](https://developer.android.com/studio/run/managing-avds)、[加速](https://developer.android.com/studio/run/emulator-acceleration)。每次使用重新核实当前支持情况。

## 专用实例与启动

记录 `setup-state.json` 的模拟器部分：唯一 AVD 名、归属、镜像包/版本、工具路径、绝对目录、PID/serial、端口映射、CA 指纹和验证状态。状态沿用 `prepared/installed/verified/pending_user/blocked` 等；不存 token、登录会话、私钥或原始敏感头。记录哪些工具原有共享、哪些文件本任务新建，供恢复使用。

将 `ANDROID_HOME` 指向已核实 SDK；任务进程的 `ANDROID_USER_HOME`、`ANDROID_EMULATOR_HOME`、`ANDROID_AVD_HOME` 指向任务目录中各自位置，保存原值，不改 `HOME` 或用户全局设置。创建前确认名称不存在，不用 `-f` 覆盖 AVD。

```text
<avdmanager> list avd
<avdmanager> create avd -n <unique-task-name> -k "<observed-image-id>" -p "<task-root>/avd/<unique-task-name>.avd" -d "<observed-device-id>"
<emulator> -avd <unique-task-name> -port <checked-even-port> -no-snapshot -no-window -gpu auto
```

创建时按实际提示回答硬件配置问题，不盲目向所有提示输入 yes。启动端口使用当前帮助允许范围的空闲偶数 console port，连同相邻 ADB port 一起检查；记录预期 `emulator-<port>`。Windows 后台进程使用 `Start-Process -WindowStyle Hidden` 并保存 PID/日志；能用 ADB 操作就不要求用户打开窗口，必要时显示对应模拟器界面。图形异常再按当前 help 使用支持的 software 模式，不沿用已弃用参数。

初次启动禁用快照以得到可复现状态。**仅需要系统 CA、且镜像支持时**追加 `-writable-system`，必要时停止本任务实例后以该选项重新启动。此选项的临时系统副本退出时销毁，不能保证 CA 随重启保留。快照兼容性取决于镜像/AVD/工具版本，也不能代替复核。

```text
<adb> devices -l
<adb> -s <serial> emu avd name
<adb> -s <serial> wait-for-device
<adb> -s <serial> shell getprop sys.boot_completed
<adb> -s <serial> shell getprop ro.build.version.sdk
<adb> -s <serial> shell getprop ro.product.cpu.abilist
```

将 AVD 名与任务记录匹配后才操作；所有设备命令显式 `-s`，不选列表第一台，也不借机操作用户已有 AVD。等待有总超时并持续报告进度，`wait-for-device` 不代表开机完成。网络须实际验证，Wi-Fi 开关开启不证明有 IP/可联网；无网络时由 agent 检查当前模拟网络与设置。

按 [安装流程](bootstrap.md#安装-proxypin-与目标手机-app) 安装官方 ProxyPin 与目标 App，验证实际包名、版本和目标页面。模拟器不需要真机 USB 调试授权。先验证 App 不经拦截可用，保留这个基线；若需要登录，本人验证按 [手动提示](manual-steps.md) 处理。不得靠卸载用户真机 App、迁移私有数据或重签 APK 补兼容性。

依据：[环境变量](https://developer.android.com/tools/variables)、[avdmanager](https://developer.android.com/tools/avdmanager)、[启动与 writable-system](https://developer.android.com/studio/run/emulator-commandline)、[快照](https://developer.android.com/studio/run/emulator-snapshots)。

## ProxyPin VPN 与移动 MCP

采用模拟器内移动版 ProxyPin 的本地 VPN 路线，由 agent 读当前 UI 配置并开始抓包。目标 App 绕开系统 HTTP 代理时，这比只改全局 HTTP proxy 更适合检查真实 App 流量；不同时叠加未验证的电脑代理、Wi-Fi 代理或路由。

1. 记录录制、SSL 范围、App 过滤与 VPN 原状态。通过 UI 开始并处理可操作的 VPN 确认；本人验证/工具限制才提示用户。检查目标包没有被过滤遗漏，并触发新的目标请求确认流量进入当前实例。
2. 设置 → MCP 服务，读取本次面板地址与 Bearer。面板含 token 的 UI dump 只在本地解析，不原样输出到对话/日志/截图；凭据用安全环境或隐藏输入。端口以面板为准，不猜固定值或每次随机。
3. 优先用当前 serial 创建本机随机端口转发，保存映射归属：

   ```text
   <adb> -s <serial> forward tcp:0 tcp:<observed-mcp-port>
   <adb> -s <serial> forward --list
   python scripts/proxypin_mcp.py --endpoint http://127.0.0.1:<returned-host-port>/mcp --require-token
   ```

   Android 内运行的仍是移动 MCP，loopback 转发不取消 Bearer。现有客户端/导出器使用 `--require-token`，token 不放命令参数或结果文件。实际验证 `initialize`、`tools/list`，再依据当前 schema 读状态和流列表；成功便不必要求手机与电脑同 Wi-Fi。
4. **MCP 连通、录制、VPN 接管、所需 HTTPS 正文可读分别验收。** MCP `toggle_recording` 或 `recording=true` 不能证明移动 VPN 已启动。仅需 URL 时先检查已有请求元信息；业务名称/图片映射所需 JSON 不可读才进入证书分支。

MCP/ADB 重启可能使转发或会话失效：重新核对实例、面板与映射，不使用历史 token。断开 MCP 前先导出选定流和结果，实时索引可能被清空。

参考：[ProxyPin MCP](https://github.com/wanghongenpin/proxypin/wiki/MCP%E6%9C%8D%E5%8A%A1)、[移动 UI 与 VPN](https://github.com/wanghongenpin/proxypin/blob/v1.3.3/lib/ui/mobile/mobile.dart)、[MCP 录制动作](https://github.com/wanghongenpin/proxypin/blob/v1.3.3/lib/mcp/protocol/mcp_actions.dart)、[ADB](https://developer.android.com/tools/adb)。源码链接解释已核实版本；执行时读取当前工具/界面。

## 必要的系统 CA

先验证路由、拦截范围与当前用户 CA。确因信任问题缺少所需正文时，才在**本任务新建的隔离 AVD**实施此分支。已授权准备测试模拟器时，由 agent 完成必要设置并说明影响，不为每个步骤重复请求确认；用户已有 AVD、真机或范围外设备不在该授权内。宿主要求本人确认时遵守并给具体提示。

1. 从当前 ProxyPin 实例导出公有根证书，不使用 Skill 内置/历史 CA 或私钥。确认格式、有效期和 CA 属性，记录 DER SHA-256 指纹。重新生成 CA 或更换实例时重新导出。
2. 目标网络栈决定信任目录。当前 Dart SDK 的 Android 实现仍读取 `/system/etc/security/cacerts`；Android 14+ Conscrypt 可能从 APEX 读取。目标 App 内嵌版本或自定义信任须实测，不能只按 API 版本统一选目录。App 兼容时可选择较简单、可 root 的受支持镜像，但不能强制降级 App 或绕过其最低要求。
3. 先核对本次 AVD 身份与启动参数，再探测权限；不要仅凭镜像标签保证 root：

   ```text
   <adb> -s <serial> emu avd name
   <adb> -s <serial> shell getprop ro.build.type
   <adb> -s <serial> shell getprop ro.debuggable
   <adb> -s <serial> root
   <adb> -s <serial> wait-for-device
   <adb> -s <serial> shell id
   <adb> -s <serial> remount
   ```

   UID 为 0 才继续；读取 remount 的实际结果，必要重启仅限本任务实例并重新验收。不要默认解锁、刷机、关闭 verity 或 SELinux。能力不足时报告该镜像不支持此分支，依据 App 依赖评估一次有理由的兼容镜像调整或其他路线。root 重启 adbd 后重建本任务转发。
4. 用已验证的 OpenSSL 或等价实现转换并计算 `subject_hash_old`，不猜文件名。PEM 已存在时不按 DER 解析：

   ```text
   <openssl> x509 -inform DER -in <exported-ca> -outform PEM -out <pem-ca>
   <openssl> x509 -in <pem-ca> -noout -subject -issuer -dates -fingerprint -sha256
   <openssl> x509 -in <pem-ca> -noout -subject_hash_old
   ```

   同 hash 不等于同证书。检查目录中 `<hash>.0`、`.1` 等连续索引，比较证书 DER 指纹：相同复用，不同取第一个未占用索引；不覆盖原系统证书。缺少工具时由 agent 准备兼容官方发行工具，不让用户手算。
5. 只有已验证可写且目标栈实际使用的目录才写入。对普通 system 目录，用明确 serial 先 push 到本任务临时位置，再复制到选定 `<hash>.<index>`，采用当前目录要求的 root 所有者、可读权限并执行 `restorecon`；用 `ls -lZ` 对照原有 CA，读取回传后比较指纹。不要写死 SELinux 为 `system_file`。如证书原已存在，记录为复用而非本任务新增，结束时不能删除它。
6. `adb remount` 成功不代表 APEX 可写。只读 APEX、未知目录或上下文无法验证时停止该分支，不盲目 push/bind mount、替换整个证书库或关闭 SELinux。若目标必须使用这条尚不可验证的路线，如实报告限制，转可用真机/自有调试构建/官方导出，不声称抓取成功。
7. 重新启动目标 App 使信任状态重新载入，**不清账号数据**；重建必要转发并复核 VPN、MCP。触发一条本次真实业务 HTTPS，读取 URL、响应状态与有效业务正文才证明目标 App 解密成功。系统证书界面显示、文件存在、浏览器成功都只算局部证据。

每次模拟器重启重新检查本次 CA 指纹、目录和业务正文；临时系统副本中的证书可能已消失。不要靠旧快照或“上次装过”跳过验证。正确信任仍失败时依据证据判断 pinning/自定义信任，不默认注入、改包或关闭 TLS 验证。

来源：[Conscrypt](https://source.android.com/docs/core/ota/modular-system/conscrypt)、[实际信任目录及 hash 索引源码](https://android.googlesource.com/platform/external/conscrypt/+/refs/heads/main/platform/src/main/java/org/conscrypt/TrustedCertificateStore.java)、[Dart 信任实现](https://github.com/dart-lang/sdk/blob/main/runtime/bin/security_context_linux.cc)、[APEX](https://source.android.com/docs/core/ota/apex)、[OpenSSL x509](https://docs.openssl.org/3.1/man1/openssl-x509/)、[CA 上下文](https://android.googlesource.com/platform/system/sepolicy/+/master/apex/com.android.conscrypt-file_contexts)、[网络安全配置](https://developer.android.com/privacy-and-security/security-config)。

## 业务验收与批量输出

先完成小闭环：由 agent 在 App 中进入用户目标列表和详情 → 捕获新请求 → 分页读完整正文 → 从真实字段关联名字、ID 与图片 URL → 导出 JSON 数组 → 默认预览下载脚本。操作前记录 flow/time 基线，先取得真实 App 请求，再按 [抓取流程](workflow.md) 执行已证实只读分页；agent 重放与 App 产生的流分别记录。

用户要商品图片时按 [输出约定](output-contract.md) 交付 `products.json`、`download_images.py`，不默认下载。报告真实记录数、入口/分类/分页覆盖、失败与未访问范围；可见目录不代表后端全部。遇限流、登录失效或重复错误停止扩展，不能为了“全量”无限请求。

报告验证边界：工具/AVD 就绪、App 未拦截基线、VPN 接管、MCP、CA/所需正文、样本导出各有独立证据。未验证的项目保持未验证；历史某 App 的成功不代表当前 App。普通可 root AVD 不保证通过 Play Integrity，[官方设备完整性说明](https://developer.android.com/google/play/integrity/verdicts) 中的虚拟完整性也不等于任意模拟器均可用。

## 恢复与清理

先导出并验证结果，再停止本次 VPN、录制和 MCP，恢复本任务改变的 SSL/过滤等配置。UI 可控时由 agent 做；本人动作无法代办时主动提示，未确认就列为待恢复。仅检查/恢复当前目标实例，不影响其他 ProxyPin 会话。

- 删除本任务记录的具体 ADB forward：`<adb> -s <serial> forward --remove tcp:<owned-host-port>`。不使用 `--remove-all` 或 `adb kill-server` 代替精确处理。
- 停止模拟器前再次核对 AVD 名与 serial；使用该实例的 `emu kill` 或已记录且身份仍匹配的 PID，只停止本任务 AVD。
- 保留 AVD 供下次用时，尽可能先移除本任务新增 CA 文件，核对指纹与归属；复用证书不删。无法验证恢复则明确记录。临时 system CA 随退出消失也需在下次启动复核，不能代替恢复报告。
- 删除 AVD/缓存按用户清理要求执行。操作前解析并检查绝对路径、AVD 名和任务归属，目标必须位于任务目录且无路径逃逸/意外重解析点；仅删除本任务登记的 AVD 数据及配置，不删除全部 `.android` 或共享 SDK/JDK/Platform-Tools。保留正式 Skill 与交付结果，除非用户也要求清理这些结果。
- 恢复任务进程原环境变量。安装包、CA 公共文件、UI dump/截图与日志按任务要求处理；含凭据的临时内容不进入 Skill/Git。不能把卸载用户真机 App、清账号数据、关闭全局代理或删除其他 AVD 作为模拟器收尾。

这条路线的可复现性来自独立环境与分层验证。新电脑的工具下载、硬件加速和目标 App 兼容性仍须现场验证，不能承诺所有设备无人操作或所有 App 都可解密。
