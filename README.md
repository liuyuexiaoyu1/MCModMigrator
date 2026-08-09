# MCModMigrator

把旧客户端（或服务端）的模组和配置自动迁移到新客户端。

支持 **Fabric / Quilt / Forge / NeoForge**。

## 启动

```bat
.venv\Scripts\python mod_migrator.py
```

1. **迁移模式**（顶部唯一一处选择「客户端 / 服务端」，仅支持同类型迁移）
2. **源**：客户端可直接选 `.minecraft` 根目录（自动列出已装版本，带加载器标签，如 `1.20.1-fabric [Fabric]`），**也可以直接选 `versions/<版本>` 隔离文件夹**——自动回填根目录、选中该版本，省略选客户端步骤；服务端直接选根目录，加载器自动从 mods 里的 jar 猜测。
3. **目标**：同样支持直接选 `versions/<版本>` 隔离文件夹；客户端选目录和版本；服务端二选一——**迁移到新的空服务端**（选目标目录）或**直接覆盖当前服务端 mods**。
4. **目标加载器 / 目标 MC 版本**：加载器默认取源加载器；程序启动后**异步**从Mojang拉取版本列表，勾选「显示所有版本」可看到快照；选择目标客户端版本后自动读取其 MC 版本号填入。
5. **选项**：模组匹配弹窗确认开关、自动安装依赖、以及数据迁移类别独立勾选：
   - `config` 目录
   - `options.txt`
   - `存档
   - 模组生成的杂项目录（journeymap、waystones 等非原版目录）
   - 资源包 / 光影
   - 服务端文件（`server.properties`、`eula.txt`、白名单等）
6. 中低置信度的匹配会弹窗询问。结束后自动生成 `迁移报告_时间戳.txt`，未匹配的模组附 Modrinth 搜索链接供手动处理。

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
2. **wrapper 模组**（如 GCA：外层 `gca_wrapper`，真实模组内嵌在 `META-INF/jars/*.jar`）：解析外层元数据的同时提取内嵌真实模组 jar 的 id/name。
3. 按名称搜索（依次放宽过滤：目标版本+加载器 → 仅加载器 → 全部），结合 modid==slug、标题相似度、词元重合度打分：
   - **≥ 0.85** 自动下载；**0.60–0.85** 弹窗/询问确认；**< 0.60** 进手动清单。
4. **fork防护**：jar 内作者列表与 Modrinth 项目团队成员**严格一致**→ 通过；**作者不一致**时验证同版本号——项目里有同版本号 + 同游戏版本的文件且 **sha1 与本地 jar 一致**→ 仍可通过。
5. 下载时按「目标加载器 + 目标 MC 版本」挑选**最新发布**的版本，没有精确版本时只尝试同系列补丁兼容并给出警告。
6. 依赖递归解析：Modrinth 版本信息里 `required` 的依赖项目会一并下载。

## 依赖与冲突（内存图，下载时即时构建）

- **版本钉住**：若模组 A 依赖模组 B 的某版本区间（如 `>=2.0` / `[1.0,2.0)`），下载的 B 版本不满足时，自动换成满足依赖区间的最新版本并替换文件。
- **依赖-冲突冲突**：某模组同时被依赖又与别的模组冲突时，迁移结束后弹窗/询问二选一：**删除该模组及依赖它的模组**，或**删除与它冲突的模组**（也可忽略）。`--yes` 自动忽略。

## 网络与下载

- **并行分析 + 并行下载**：独立线程池并行（GUI「分析线程」1–8 可调；CLI `--analysis-threads N`），下载阶段另用下载线程池并行（GUI「下载线程」1–16；CLI `--threads N`）。
- **限速轮询**：Modrinth 返回 429 时按 `Retry-After` 暂停。

## 数据迁移规则

- `config`：整体复制并**覆盖**目标。
- `options.txt`：覆盖复制。
- `saves / world`：目标中已存在的**同名世界自动添加 `_old`后缀**再迁移。
- 杂项目录：游戏根目录里除原版/启动器生成的文件或文件夹（assets、libraries、logs、screenshots、launcher 配置等）之外、也非用户内容分类（资源包/光影/servers.dat）的目录和文件，视为模组生成的，按勾选迁移。
- 服务端：额外迁移 `server.properties`、`eula.txt`、`whitelist.json`、`ops.json`、`server-icon.png` 等。

## 打包为单文件 exe

```bat
build_exe.bat
```

或手动执行：`pyinstaller --onefile --windowed --name MCModMigrator mod_migrator.py`。产物在 `dist\MCModMigrator.exe`（约 50MB，无需安装 Python）。

- GUI 模式无控制台；`--cli` 模式运行时程序会自动弹出一个控制台窗口。
- 部分杀毒软件对 PyInstaller 单文件 exe 有误报，属正常现象，可加白名单。
- 验证打包产物：`.venv\Scripts\python tests\test_exe_e2e.py`。

## CI 自动构建与发布（GitHub Actions）

`.github/workflows/build.yml`

## 目录结构

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
