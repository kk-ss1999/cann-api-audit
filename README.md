# CANN 公开接口核对

给一个代码仓，找出它**直接使用的 CANN 接口**，与运行时最新 CANN 官方公开 API 文档按名称核对，输出 Markdown 报告。

支持本地目录和远程 Git URL。

> 推荐通过 AI 助手使用这个 skill：脚本负责提取候选、下载文档和生成报告，助手负责核实接口归属、宏展开和文档缺口。单独运行脚本得到的是待复核结果，不是完整审计结论。

## 快速开始：让助手执行

### 1. 下载

需要 **Python 3.10+、Git**，以及访问本私仓和昇腾官方文档的网络权限。脚本仅使用 Python 标准库，无须安装 CANN、PyTorch 或准备 NPU。

在准备保存 skill 的目录中执行：

```sh
git clone https://github.com/kk-ss1999/cann-api-audit.git
```

这是私有仓库，需要当前 Git 凭据有访问权限。也可以使用已登录的 GitHub CLI：

```sh
gh repo clone kk-ss1999/cann-api-audit
```

### 2. 给助手一个仓库

如果助手已经加载这个 skill，直接发送：

```text
使用 $cann-api-audit 扫描 D:\Ascend\Projects\vllm-ascend，
与最新 CANN 官方公开 API 文档核对，
把报告保存到 D:\reports\vllm-ascend-cann.md。
```

远程仓库也可以：

```text
使用 $cann-api-audit 扫描 https://github.com/vllm-project/vllm-ascend.git，
输出 Markdown 报告，重点列出官方文档未找到的接口及其代码位置。
```

如果尚未加载 skill，把下载位置告诉助手即可，例如：

```text
读取 D:\tools\cann-api-audit\SKILL.md，按照其中的流程，
扫描 D:\Ascend\Projects\vllm-ascend，输出报告到 D:\reports\cann-report.md。
请完成接口归属和文档覆盖复核，不要只交付自动扫描结果。
```

将示例中的路径替换为实际位置。正常使用时，不需要你手工编辑中间 JSON 文件，助手会完成这些工作。

## 扫描和核对范围

| 项目 | 范围 |
| --- | --- |
| 仓库内容 | 正式源码、测试、示例、文档代码块，以及构建和配置中的直接接口引用 |
| 接口种类 | 函数、类型、结构体、枚举及枚举值、常量、宏、Ascend C 类和方法 |
| 依赖边界 | 只看仓库直接使用的接口，不继续分析 `torch_npu` 等第三方组件内部的调用 |
| 文档范围 | 最新 CANN 官方文档中的“API参考、算子库、通信库、加速库”四类公开 API 页面 |
| 对齐标准 | 同一接口名称被官方公开 API 文档收录；不检查参数、返回值或运行时兼容性 |
| 本地工作区 | 扫描当前磁盘内容，包含未提交文件；跳过常见缓存、生成物、二进制和符号链接，并记录缺口 |
| 远程仓库 | 浅克隆默认分支并记录 commit，不自动初始化依赖子模块 |

默认从 [CANN 最新头文件和库文件说明](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/latest/API/headerliblist/hfandlf_09_0001.html) 解析实际文档版本，不固定使用某个版本号。

“编程语言、调试与分析工具、参考/基础数据结构和接口、更多/应用开发、TBE 与 AI CPU 算子开发、LLM DataDist、DataFlow、ISP”等其他章节不用于接口名称对齐，即使页面标题或路径含有 `API`。

## 如何读报告

扫描器只收集具有接口归属依据的候选：

| 层级 | 处理方式 |
| --- | --- |
| 明确前缀、命名空间、Python 导入、动态绑定或接收者类型 | 进入接口表，与官方文档核对 |
| `using namespace AscendC` 下的未限定名称 | 只有同名命中官方正文或经人工确认后才进入接口表 |
| 仅因文件包含 CANN 头文件而出现的普通 token | 不收集、不整理、不统计 |

因此，局部变量、模板参数、头文件保护宏和本地普通函数不会仅凭“所在文件使用了 CANN”就进入扫描结果。报告也不展示扫描器的内部识别线索。

| 状态 | 含义 |
| --- | --- |
| **官方文档未找到** | 已确认是直接 CANN 依赖，完成扫描范围、文档覆盖和二次检索复核后，仍未找到名称 |
| **待核实** | 接口归属不明确、文档获取不完整，或仍有宏、别名等需要核实 |
| **已匹配** | 已确认 CANN 归属，并找到对应的官方文档证据 |

每个接口附代码文件、行号和代码片段；匹配项附官方文档链接。报告也记录仓库 commit、工作区状态、实际 CANN 版本、扫描时间和文档覆盖情况。纯本地实现、其他组件接口以及仅在注释或说明文字中出现的名称保存在内部复核数据中，不进入 Markdown 报告。

**没有“官方文档未找到”条目，不等于全部通过。** 还要查看报告顶部的结论状态和“待核实”项。“未找到”也不直接等于接口不存在或必然属于内部接口。

## 直接运行脚本

下面的命令都在下载的 `cann-api-audit` 仓库根目录执行。Windows、Linux 和 macOS 均可使用，将 `python` 替换为本机 Python 3 的命令即可。

建议把报告放在**被扫描仓库之外**，避免后续扫描把历史报告作为新的输入。

### 扫描并获取官方文档

Windows 示例：

```sh
python scripts/audit.py analyze "D:/Ascend/Projects/vllm-ascend" --output "D:/reports/cann-report.md"
```

Linux / macOS 示例：

```sh
python scripts/audit.py analyze /workspace/vllm-ascend --output /tmp/cann-report.md
```

远程仓库示例：

```sh
python scripts/audit.py analyze https://github.com/vllm-project/vllm-ascend.git --output ../cann-report.md
```

也支持 `git@github.com:owner/repo.git` 等 SSH 地址。私仓需要 Git 已配置好访问凭据，不要把访问令牌写进 URL。

### 只扫描源码

```sh
python scripts/audit.py scan /workspace/vllm-ascend --output /tmp/cann-scan.json
```

此命令不下载官方文档，也不生成最终 Markdown 报告。使用本地目录作为输入时无需联网；远程 URL 仍需要克隆仓库。

### 只验证流程是否能跑通

```sh
python scripts/audit.py analyze /workspace/vllm-ascend --output /tmp/cann-smoke.md --max-pages 20
```

`--max-pages 20` 限制的是获取的文档正文页数，仍会发现完整版本目录。**这只是冒烟测试，不能用它替代全量文档核对。**

### 完成复核后重新生成报告

`analyze` 会生成以下文件：

| 文件 | 用途 |
| --- | --- |
| `cann-report.md` | 阅读和交付的报告 |
| `cann-report.scan.json` | 接口候选、内部判定数据、源码位置及扫描缺口 |
| `cann-report.catalog.json` | 实际文档版本、获取覆盖情况、匹配证据及指纹 |
| `cann-report.review.json` | 助手或人工填写的归属判定、排除结论、补充条目和复核记录 |

自动生成的 review 默认未复核；首次报告只保留具备接口证据、但仍需人工确认归属或文档缺口的“待核实”项。复核后再执行：

```sh
python scripts/audit.py report /tmp/cann-report.scan.json --catalog /tmp/cann-report.catalog.json --review /tmp/cann-report.review.json --output /tmp/cann-report.md
```

复核格式和判定要求见 [人工复核文件](references/method.md#人工复核文件)。需要实际检查源码和文档后才能填写完成标记；脚本会检查扫描指纹及已复核的文档指纹，避免沿用过期结论。

## 缓存、耗时和常见问题

### 第一次为什么比较慢？

首次运行需要获取官方文档目录，以及“API参考、算子库、通信库、加速库”中的所选 API 正文；源码规模也会影响候选数量和报告大小。脚本会输出扫描和下载进度。

缓存默认保存在当前用户目录下的 `.cache/cann-api-audit`，按实际 CANN 版本隔离。每次执行重新解析 `latest`，有效的版本内缓存可复用。中断后重跑可复用已缓存的文档；远程 URL 输入会重新创建独立克隆目录。

### 如何换缓存目录或强制更新？

```sh
python scripts/audit.py analyze /workspace/vllm-ascend --output /tmp/cann-report.md --cache-dir /data/cann-cache
python scripts/audit.py analyze /workspace/vllm-ascend --output /tmp/cann-report.md --refresh
```

### 为什么有些接口不能直接判定？

扫描器是静态候选提取器，不是完整的 C++ 编译器。仓库自定义的 `aclnn*` 接口、宏拼接、Python 导入遮蔽、动态加载和类方法同名都需要复核。实例类型无法还原时，需要助手补充检查。仅由文件级 CANN 上下文产生的普通词法名称不会被收集。

### 官方文档下载失败怎么办？

查看报告中的“文档获取缺口”，确认网络后重跑。抓取失败会保留为缺口，不会被当作“接口未公开”。如果网站目录结构发生变化，需要修正文档获取逻辑，或使用 `--extra-pages` 补充已验证的当前版本 API 页面，具体见 [官方文档获取](references/method.md#官方文档获取)。

### 提示复核指纹不一致怎么办？

说明源码内容或已复核文档发生变化。重新检查变化，再更新 review 中的指纹和相关判定；不要只复制新指纹绕过复核。也可以换一个新的报告文件名开始新一轮核对，保留旧结果用于对照。

## 开发与验证

运行行为测试，无须 NPU：

```sh
python -m unittest discover -s scripts/tests -v
```

查看命令参数：

```sh
python scripts/audit.py --help
python scripts/audit.py analyze --help
```

文件入口：

- [SKILL.md](SKILL.md)：助手执行流程。
- [references/method.md](references/method.md)：接口归属、文档证据及复核规则。
- [scripts/audit.py](scripts/audit.py)：命令行入口和报告生成。
- [scripts/scan_repo.py](scripts/scan_repo.py)：代码扫描。
- [scripts/cann_docs.py](scripts/cann_docs.py)：官方文档获取与匹配。
- [scripts/tests/test_audit.py](scripts/tests/test_audit.py)：行为测试。
