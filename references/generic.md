# 通用配置与正文快照

这些 JSON 由执行 skill 的助手从用户的官方文档入口自动整理。用户只提供代码仓和入口；不能把生成这些文件的工作交给用户。此处虚构的 ExampleLib 只说明格式，不是已验证产品规则。

## profile.json

```json
{
  "name": "ExampleLib",
  "docs_entry": "https://docs.example.org/library/v2/",
  "version": "2.0",
  "official_hosts": ["docs.example.org"],
  "api_roots": ["https://docs.example.org/library/v2/api/"],
  "rules": [
    {
      "kind": "python_module",
      "value": "example_lib",
      "evidence_url": "https://docs.example.org/library/v2/api/overview.html",
      "reason": "官方明确说明通过 example_lib 导入该库"
    }
  ]
}
```

`kind` 支持 `python_module`、`namespace`、`symbol_prefix`、`symbol`。模块可精确到子模块，命名空间保留限定路径；前缀规则必须有官方依据，不能因为仓库里某些名字相似就扩大规则。额外主机仅在官网链接证明归属后加入。`api_roots` 使用确定的目录前缀（以 `/` 结尾）或具体页面 URL，不能用整个域名替代 API 目录范围。

PTA 的模块名和扩展注册路径需从实际入口确认，不能把产品缩写自动转换成 Python 模块名。规则及文档入口、版本参与扫描指纹。

## documents.json

```json
{
  "docs_entry": "https://docs.example.org/library/v2/",
  "version": "2.0",
  "coverage_reviewed": false,
  "coverage_note": "",
  "failures": [],
  "pages": [
    {
      "url": "https://docs.example.org/library/v2/api/run.html",
      "title": "example_lib.run",
      "version": "2.0",
      "fetched_at": "2026-09-17T00:00:00Z",
      "body_file": "bodies/run.md"
    }
  ]
}
```

正文文件相对 manifest 所在目录。保存实际获取的完整可读 API 正文，不保存搜索摘要或自行编写的接口说明。动态页面用浏览器读取，PDF 用可用的 PDF 工具提取，并保存原始官方 URL。保留版本证据和目录遍历记录；记录未展开目录、抓取失败及无法确定的页面版本。每个失败项为 `{"url": "...", "error": "..."}`。

只有遍历确认全部所选 API 目录、分页和接口页并核实版本后，才设置 `coverage_reviewed: true`，且在 `coverage_note` 写出目录链接、页面数量、排除范围和复核依据。后端会校验 URL 范围、版本和正文文件，但无法替代助手证明网站归属及目录完整性。

## 复核

通用后端的所有候选默认需要人工确认归属。检查别名作用域、重绑定、本仓同名模块、函数或类后，把真实直接依赖标为 `target`。仅凭正则前缀或导入别名表不算确认。候选仅在文档存在不能证明归属；直接依赖但未在文档找到的符号仍要保留。

逐页名称匹配保留完整限定名，不自动把 `library.Class.run` 缩成 `run`。API 页只写叶子名时，确认其类/模块后使用 `evidence_url` 和 `evidence_note`；该 URL 必须在当前正文快照内。仅注释或本仓实现以 `local/other` 排除。

`review.scope_reviewed` 覆盖所有相关源码及扫描器不支持的语法；`review.catalog_reviewed` 覆盖 API 目录与版本，两个开关均不能因脚本成功退出而自动设为 true。以正文、产品、规则和入口指纹防止跨目标复用旧复核。

CANN 旧版脚本和报告命令仍可用。通用流程的报告入口为 `scripts/audit.py report`；其 `analyze`/`scan` 子命令仍是 CANN 专用，不可用来扫描 PTA 等其他对象。


## 批量配置

多个入口时，每个对象仍使用上文独立的 profile 和 documents.json。由助手生成 `targets.json`，不要求用户填写：

```json
{
  "targets": [
    {"docs_entry": "https://docs.example.org/a/", "profile": "a/profile.json", "docs_manifest": "a/documents.json"},
    {"docs_entry": "https://docs.example.org/b/", "profile": "b/profile.json", "docs_manifest": "b/documents.json"},
    {"docs_entry": "https://docs.example.org/unavailable/", "error": "官方入口无法读取，尚未确定依赖对象"}
  ]
}
```

所有路径相对 `targets.json`，每项 docs_entry 必须与其 profile 和 manifest 一致。批量输入、正文及输出均放在被扫描仓库之外。相同入口去重，入口顺序调整不改变复核目录；对象或版本变化会使旧复核失效。需要重新复核时另存旧 review 并生成新记录，不自动沿用完成标记。

批量后端一次检出仓库、一次读取源码，给所有对象使用相同快照。每库有独立 scan/catalog/review 文件，目录以入口指纹标识；产品显示名称相同不会覆盖文件。初次报告生成后逐库复核，使用 `batch_audit.py report` 重新生成总报告。不要拼接过期 Markdown，必须从当前各库 JSON 及指纹重新验证。

任何入口失败、正文不完整或仍有待核实项，整体保持阶段性状态；独立完成的库仍可显示完成。跨库同名符号只能用该库自己快照内的证据匹配。所有库均无待核实且通过各自覆盖复核后，才允许整体标记完成。
