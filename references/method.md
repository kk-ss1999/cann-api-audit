# 扫描、证据与复核

## 归属判定

扫描脚本提取候选，不是 C++ 编译器。跨文件 include 链、命名空间、类型接收者、Python AST 导入别名和动态绑定提供线索；最终由执行 skill 的 agent 验证。

| 情况 | 处理 |
| --- | --- |
| `aclrtMalloc`，且调用/声明可追溯至 CANN 头文件 | 确认归属后核对同名公开 API |
| `HcclCommInitRootInfo` 通过 ctypes 从 HCCL 库绑定 | 属于直接依赖，字符串中的函数名也扫描 |
| `import acl as a; a.rt.malloc(...)` | 规范化为 `acl.rt.malloc`，保留原始写法；不能与 C 的 `aclrtMalloc` 自动等同 |
| `AscendC::LocalTensor<T>` 或 `using namespace AscendC` | 核实类型来源；方法按接收者所属类核对，例如 `AscendC::LocalTensor::GetValue` |
| `using AC = ...` / namespace alias / 宏 token paste / 多层模板推导 | 检查 scanner 的缺口列表及相关源码，必要时在 review 中补充，不许忽略 |
| `EXEC_NPU_CMD(aclnnFoo, ...)` | 提取宏参数；检查宏是否派生 `aclnnFooGetWorkspaceSize` 等实际调用，补充衍生名称并引用宏定义 |
| 仓库自己实现的 `aclnnFoo` | 本地接口，不能因为前缀相同就报“CANN 未公开” |
| 自己定义的 ctypes 类型别名如 `aclrtStream_t` | 不等同于 CANN 原始类型名；依据实际映射核实，不能随意删 `_t` 匹配 |
| 仅出现于普通注释或说明文字 | 不作为使用；Markdown 的代码块和行内代码可成为文档引用候选 |
| `torch_npu` / `torch.ops.npu` / ATen 包装接口 | 不追踪内部 CANN 调用；外层名称不能冒充 CANN API |

接口名称保留大小写，按标识符边界匹配，`aclrtMallocHost` 不能使 `aclrtMalloc` 通过。保留命名空间、Python 模块和 C++ 接收者类型；同名方法只有所属类/模块证据吻合才自动匹配。文档中的路径、脚本、网页导航、旧版链接不是当前接口的正文证据。文档中只有示例自定义实现或明确“非公开”描述时，需人工推翻自动候选匹配。

默认枚举本地目录中的文本文件（含未跟踪文件）；跳过符号链接以免跑出仓库，跳过常见缓存和二进制，逐项统计。条件编译分支全部扫描，不声称等价于特定硬件编译结果。CANN 头文件外部实现不存在于本仓时不追踪其代码，仅用 include/公开文档建立归属。

## 官方文档获取

默认入口：

- [用户指定的头文件和库文件说明](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/latest/API/headerliblist/hfandlf_09_0001.html)
- [CANN latest 总览](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/latest/index/index.html)

2026-09-14 验证该入口的 canonical 指向 `920beta2`。这是验证时观测，不是固定版本约束。每次执行重新解析 latest，并按解析后的版本生成文档目录。

网站公开页面使用以下接口（不是稳定 SDK；结构变化必须显式报缺口）：

1. 页面 `<link rel="canonical">` 给出实际版本；页面 `<title>` 可能陈旧，不单独依赖它判断版本。
2. `/ascendgateway/ascendservice/doc/version/new/tree?route=...` 给出版本的章节。请求携带官方网站 Referer。
3. `/ascendgateway/ascendservice/doc/node/tree/{codePath}` 给出章节完整目录，字段含 `nodeName`、`nodeUrl`、`children`。保留路径与上级标题，检查未展开子节点。
4. `/doc_center/source/{nodeUrl}` 获取 HTML/Markdown 正文；HTML 抽正文文本，Markdown 保留可见文本和代码，去掉链接 URL/元数据。抓取失败可尝试公开展示页的 Nuxt 正文；仍失败则记录，不能缓存为成功。

脚本扫描每个章节的目录，而非只看 `/API/`。除整个 API 分类外，通信库、加速库和其他章节中标题/路径标注 API、接口参考或数据结构的子树均纳入。目录筛选仍需 agent 核对；`catalog_reviewed` 只有在检查遗漏、外链、未展开节点和版本后才置为 true。API 在新版本被移至其他官方产品或站点时，必须核实其与当前 CANN 版本的对应关系，不能无依据沿用旧版。脚本只接受同一版本 hiascend 文档页；其他官方来源由 agent 作为人工证据记录。

缓存包含获取时间和实际 URL；版本隔离不等于永久有效。脚本对超过一天的缓存重新请求；使用 `--refresh` 可主动更新。`--max-pages`、目录失败、正文失败均令文档覆盖不完整。退出后重跑相同命令可复用已下载正文。

补充漏选页面的 JSON：

```json
[
  {"url": "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/VERSION/PATH", "title": "实际页面标题", "reason": "目录检查发现该页属于当前版本 API 参考"}
]
```

将 `VERSION/PATH` 替换为实际已验证页面路径。通过 `--extra-pages FILE.json` 添加，不用未经验证的通用域名搜索结果充当覆盖证明。

## 人工复核文件

脚本首次创建同名 `.review.json`，不会覆盖已有复核。每次扫描含内容指纹；旧复核与扫描指纹不一致时不能直接沿用，需重新确认并更新指纹。目录复核后，将 `.catalog.json` 中的 `catalog_fingerprint` 复制到 review；正文、覆盖范围或版本变化时，旧目录复核不能继续生效。

```json
{
  "scan_fingerprint": "从 scan.json 复制实际值",
  "scope_reviewed": false,
  "catalog_reviewed": false,
  "catalog_fingerprint": "复核后从 catalog.json 复制实际值",
  "notes": [],
  "symbols": {
    "aclrtMalloc": {
      "origin": "cann",
      "kind": "函数",
      "reason": "该调用所在文件包含 acl/acl.h，且官方 Runtime API 页声明该名称"
    },
    "aclnnMyLocalOp": {
      "origin": "local",
      "reason": "在本仓 op_host/my_op.cpp:42 定义并导出"
    }
  },
  "additional_occurrences": []
}
```

`origin` 可为 `cann`、`local`、`other`、`uncertain`；必须填写非空 reason。可填写 `canonical` 修正已证实的别名，以及 `evidence_url` 和 `evidence_note` 提供已人工打开核实的官方文档证据；后者会明确标为人工证据。确认找不到时可设 `searched: true` 与 `search_note` 记录二次核查，但仅在完整正文覆盖且两个复核开关都通过后，报告才归类“官方文档未找到”。

`additional_occurrences` 每项使用 `name`、`kind`、`path`、`line`、`source`、`category`、`signals`，格式与 `.scan.json` 的 occurrences 相同。文件和行号必须确实存在；用于补充解析器无法还原的宏拼接或类型推断。若宏展开产生衍生名称，定位到触发宏的调用行，并在 signals 中写明宏定义位置。

## 可重复验证

```text
python -m unittest discover -s SKILL_DIR/scripts/tests -v
python SKILL_DIR/scripts/audit.py scan REPOSITORY --output scan.json
python SKILL_DIR/scripts/audit.py analyze REPOSITORY --output smoke.md --max-pages 20
```

前两条不联网（远程仓库输入除外）。最后一条只验证联网流程和报告，不是完整审计。重点检查：无 CANN 仓库、局部同名接口、类型/枚举/宏、C++ 模板方法、Python 别名和动态绑定、文档代码块、未检出子模块、失败/过期文档、旧版链接、名字子串误匹配、方法同名冲突、远程克隆失败、带空格路径和报告转义。
