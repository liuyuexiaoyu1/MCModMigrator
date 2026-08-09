# MC 模组客户端迁移 / 更新工具

把已装好加载器的旧客户端（或服务端）的模组自动迁移到新客户端：解析源 `mods` 目录里的每个 jar，去 **Modrinth** 搜索高置信度匹配，下载适配**目标加载器 + 目标 MC 版本**的模组，并自动补装依赖（Fabric API、前置库等）。随后按你的勾选迁移数据目录。

支持 **Fabric / Quilt / Forge / NeoForge**，支持启动器**版本隔离**目录结构，也支持**服务端**目录。

## 启动

```bat
.venv\Scripts\python mod_migrator.py
```

1. **迁移模式**（顶部唯一一处选择「客户端 / 服务端」，仅支持同类型迁移）：选 **C2C**（客户端→客户端，支持版本隔离）或 **S2S**（服务端→服务端，mods 直接在根目录，无版本隔离）。
2. **源**：客户端可直接选 `.minecraft` 根目录（自动列出已装版本，带加载器标签，如 `1.20.1-fabric [Fabric]`），**也可以直接选 `versions/<版本>` 隔离文件夹**——自动回填根目录、选中该版本，省略选客户端步骤；服务端直接选根目录，加载器自动从 mods 里的 jar 猜测。
3. **目标**：同样支持直接选 `versions/<版本>` 隔离文件夹（新版本目录只有 json 也会按隔离布局处理）；客户端选目录和版本（版本留空 = 不隔离）；服务端二选一——**迁移到新的空服务端**（选目标目录）或**直接覆盖当前服务端 mods**（目标即源目录，仅更新 mods，此时 MC 版本必须手动选择）。
4. **目标加载器 / 目标 MC 版本**：加载器默认取源加载器；MC 版本**无需手写**——程序启动后**异步**从 Mojang 官方清单（`version_manifest_v2.json`）拉取版本列表（完成前显示「正在拉取版本列表...」，失败可手动输入兜底），勾选「显示所有版本（含快照）」可看到快照/老版本；选择目标客户端版本后自动读取其 MC 版本号填入。
5. **选项**：模组匹配弹窗确认开关、自动安装依赖、以及数据迁移类别独立勾选（随模式增减）：
   - `config` 目录（源配置覆盖目标）
   - `options.txt`（仅 C2C 显示）
   - `saves / world` 存档（只补缺失，**不覆盖**目标已有世界）
   - 模组生成的杂项目录（journeymap、waystones 等非原版目录）
   - 资源包 / 光影 / `servers.dat`（用户内容，默认不勾）
   - 服务端文件（`server.properties`、`eula.txt`、白名单等，仅 S2S 显示）
6. 点「开始迁移」，日志实时滚动；中低置信度的匹配会弹窗询问。结束后自动生成 `迁移报告_时间戳.txt`，未匹配的模组附 Modrinth 搜索链接供手动处理。

> 源与目标是同一个客户端时进入「更新模组」模式：只把 mods 更新到该加载器/版本的最新版，不迁移数据。

## 使用（命令行）

```bat
:: 客户端迁移（--target-mc 可省略：自动从目标客户端版本读取）
.venv\Scripts\python mod_migrator.py --cli --src-root ".minecraft" --src-version 1.20.1-fabric ^
    --target-root ".minecraft" --target-version 1.21.1-fabric ^
    --target-loader fabric --migrate config,options,saves,stray

:: 服务端迁移（无需 --src-version / --target-version；MC 版本从 Mojang 清单交互选择）
.venv\Scripts\python mod_migrator.py --cli --src-root "server" --target-root "server" ^
    --target-loader fabric --target-mc 1.21.1 --migrate config,saves,stray,server

:: 服务端覆盖模式：直接在源服务端更新 mods（必须指定/选择 --target-mc）
.venv\Scripts\python mod_migrator.py --cli --src-root "server" --overwrite ^
    --target-loader fabric --target-mc 1.21.1

:: 什么都不带时按提示交互（版本列表自动从 Mojang 拉取，客户端目标自动读取版本）
```

参数：`--yes` 全部自动确认；`--skip-deps` 不装依赖；`--skip-data` 不迁数据；`--migrate a,b,c` 指定迁移类别（`config,options,saves,stray,optional,server`）；`--overwrite` 服务端覆盖模式。

## 匹配策略（置信度）

1. 源 jar 的 **sha1 与 Modrinth 完全一致** → 直接采用该项目（100%）。
2. **wrapper 模组**（如 GCA：外层 `gca_wrapper`，真实模组内嵌在 `META-INF/jars/*.jar`）：解析外层元数据的同时提取内嵌真实模组 jar 的 id/name，按 wrapper 基底 id（去 `_wrapper` 后缀）优先用内嵌元数据搜索。
3. 按名称搜索（依次放宽过滤：目标版本+加载器 → 仅加载器 → 全部），结合 modid==slug、标题相似度、词元重合度打分：
   - **≥ 0.85** 自动下载；**0.60–0.85** 弹窗/询问确认；**< 0.60** 进手动清单。
4. **fork 版防护（作者先验）**：jar 内作者列表与 Modrinth 项目团队成员**严格一致**（归一化后集合相等）→ 直接通过；**作者不一致**时验证同版本号——项目里有同版本号 + 同游戏版本的文件且 **sha1 与本地 jar 一致**→ 仍可通过；两条路都不通才判定疑似 fork 版模组进手动清单。无作者信息的 jar 无法校验，按置信度处理。
5. 下载时按「目标加载器 + 目标 MC 版本」挑选**最新发布**的版本，没有精确版本时只尝试同系列补丁兼容（如目标 1.20.1 可用 1.20 版）并给出警告；**完全没有适配目标 MC 版本的模组绝不下载其他版本的 jar，直接进手动清单**。
6. 依赖递归解析：Modrinth 版本信息里 `required` 的依赖项目会一并下载。

## 依赖与冲突（内存图，下载时即时构建）

- 每个模组**下载完成时**立即解析其 jar 内声明的依赖项与冲突项（fabric/quilt 的 `depends`/`conflicts`、Forge toml 的 `[[dependencies.X]]`、mcmod.info），构建内存中的依赖/冲突图——**结束时直接读内存，不重新扫描**。
- 排除默认依赖：`minecraft`、`java`、`fabricloader`、`fabric`、`quilt_loader`、`forge`、`neoforge` 等。
- **版本钉住**：若模组 A 依赖模组 B 的某版本区间（如 `>=2.0` / `[1.0,2.0)`），下载的 B 版本不满足时，自动换成满足依赖区间的最新版本并替换文件。
- **依赖-冲突冲突**：某模组同时被依赖又与别的模组冲突时，迁移结束后弹窗/询问二选一：**删除该模组及依赖它的模组**，或**删除与它冲突的模组**（也可忽略）。GUI 弹窗三按钮，CLI 交互选择，`--yes` 自动忽略。
- 结束输出依赖图统计：`依赖图统计: N 个模组, M 条依赖, K 条冲突`。

## 网络与下载

- **系统代理**：默认使用系统代理（GUI「使用系统代理」可关；CLI `--no-system-proxy` 直连；也可用 `HTTP(S)_PROXY` 环境变量）。
- **并行分析 + 并行下载**：解包/匹配/挑版本的分析阶段用独立线程池并行（GUI「分析线程」1–8 可调；CLI `--analysis-threads N`），下载阶段另用下载线程池并行（GUI「下载线程」1–16；CLI `--threads N`）。去重、确认弹窗、提交下载按原顺序在主线程串行处理，行为与日志顺序不变。
- **限速轮询**：Modrinth 返回 429 时按 `Retry-After` 暂停（所有线程模块级互斥统一等待），静默轮询重试，不刷屏。

## 日志分级着色

- **error 红色** / **warn 黄色** / **info 默认黑色**。

## 数据迁移规则

- `config`：整体复制并**覆盖**目标（保证新客户端拿到你的配置）。
- `options.txt`：覆盖复制。
- `saves / world`：目标中已存在的**同名世界自动加 `_old`**（再重名继续 `_old_old`）迁移过来——源存档一个不丢、目标旧存档不被覆盖。
- 杂项目录：游戏根目录里除原版/启动器生成的文件或文件夹（assets、libraries、logs、screenshots、launcher 配置等）之外、也非用户内容分类（资源包/光影/servers.dat）的目录和文件，视为模组生成的，按勾选迁移。
- 服务端：额外迁移 `server.properties`、`eula.txt`、`whitelist.json`、`ops.json`、banned 列表、`server-icon.png` 等。

## 打包为单文件 exe

```bat
build_exe.bat
```

或手动执行：`pyinstaller --onefile --windowed --name MCModMigrator mod_migrator.py`。产物在 `dist\MCModMigrator.exe`（约 50MB，无需安装 Python）。

- GUI 模式无控制台；`--cli` 模式运行时程序会自动弹出一个控制台窗口。
- 部分杀毒软件对 PyInstaller 单文件 exe 有误报，属正常现象，可加白名单。
- 验证打包产物：`.venv\Scripts\python tests\test_exe_e2e.py`。

## 目录结构（模块解耦）

```
mod_migrator.py        入口薄壳
mc_migrator/
├── core.py            共享常量与工具
├── clients.py         MC 客户端 / 服务端目录识别
├── mod_parser.py      模组 jar 元数据解析
├── modrinth.py        Modrinth API、匹配评分、下载、依赖解析
├── versions.py        Mojang 版本清单
├── migrator.py        迁移编排
├── cli.py             命令行交互
├── gui.py             PySide6 图形界面
└── __main__.py        入口
```
