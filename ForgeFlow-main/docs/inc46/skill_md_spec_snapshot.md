# SKILL.md 官方规范快照（INC46 T18）

> 本快照是 ForgeFlow 七段契约 ↔ SKILL.md 映射（`forgeflow/skills/spec_mapping.py`）
> 与规范校验器（`forgeflow/skills/spec_validator.py`）的**契约基线**。上游规范变更
> 由 `spec_mapping.spec_drift_alert` 比对内容哈希发现，告警但不阻塞发布。

## 来源与完整性（红线 10：快照缺失 ⇒ 不得 PASS）

| 项 | 值 |
|---|---|
| 页面 URL | https://agentskills.io/specification |
| Markdown 原文 URL | https://agentskills.io/specification.md |
| 取得日期 | 2026-10-03（spec_fetch.json 记录 2026-10-03T17:40:03） |
| Markdown 原文大小 | 7697 字节 |
| **Markdown 原文 sha256** | `4c649bdf0e0a51c9e215d9f91009ecdca05ee9d073edb6f37c20265bbd829e11` |
| HTML 页面 sha256（备查） | `103a08f0524ee81898ae1e6cbd7daefba102a89108a0e99f146e07c880851ea2`（435111 字节） |
| 原文存档 | `D:\Agentxm\Multi-Agent\_inc46_harness\spec_ef4db182.bin`（逐字节未改） |
| 抓取元数据 | `D:\Agentxm\Multi-Agent\_inc46_harness\spec_fetch.json` |

## 关键章节摘录（原文照录，未经改写）

### 目录结构（必需 SKILL.md；可选 scripts/ references/ assets/）

```
skill-name/
├── SKILL.md          # Required: metadata + instructions
├── scripts/          # Optional: executable code
├── references/       # Optional: documentation
├── assets/           # Optional: templates, resources
└── ...               # Any additional files or directories
```

### Frontmatter 字段表

| Field | Required | Constraints |
| - | - | - |
| `name` | Yes | Max 64 characters. Lowercase letters, numbers, and hyphens only. Must not start or end with a hyphen. |
| `description` | Yes | Max 1024 characters. Non-empty. Describes what the skill does and when to use it. |
| `license` | No | License name or reference to a bundled license file. |
| `compatibility` | No | Max 500 characters. Indicates environment requirements (intended product, system packages, network access, etc.). |
| `metadata` | No | Arbitrary key-value mapping for additional metadata (a map from string keys to string values). |
| `allowed-tools` | No | Space-separated string of pre-approved tools the skill may use. (Experimental) |

### `name` 规则（§name field）

* Must be 1-64 characters
* May only contain unicode lowercase alphanumeric characters (`a-z`, `0-9`) and hyphens (`-`)
* Must not start or end with a hyphen (`-`)
* Must not contain consecutive hyphens (`--`)
* Must match the parent directory name

合法例：`pdf-processing` / `data-analysis` / `code-review`；
非法例：`PDF-Processing`（大写）/ `-pdf`（首连字符）/ `pdf--processing`（连续连字符）。

### `description` 规则（§description field）

* Must be 1-1024 characters
* Should describe both what the skill does and when to use it
* Should include specific keywords that help agents identify relevant tasks

### 可选字段细则

* `license`：许可证名或随附许可文件名，宜短。
* `compatibility`：1–500 字符；仅在有特定环境要求时提供（产品、系统包、网络等）。
* `metadata`：字符串键 → 字符串值的映射；键名宜足够独特避免冲突。
* `allowed-tools`：空格分隔的预批准工具字符串；**实验性**，各实现支持程度不一
  （例：`allowed-tools: Bash(git:*) Bash(jq:*) Read`）。

### 正文（§Body content）

frontmatter 之后为 Markdown 正文，无格式限制。建议章节：分步说明、输入输出示例、
常见边界情况。agent 激活技能时会加载整个文件，长文应拆入引用文件。

### 可选目录约定

* `scripts/`：可执行代码；应自包含或写明依赖，含友好的错误信息，妥善处理边界。
* `references/`：按需加载的文档（`REFERENCE.md`、`FORMS.md`、领域文件如 `finance.md`）；
  单文件宜聚焦——按需加载，小文件省上下文。
* `assets/`：静态资源（模板、图片、数据文件如查找表与 schema）。

### 渐进披露（§Progressive disclosure）

1. **Metadata（约 100 tokens）**：`name` 与 `description` 启动时加载；
2. **Instructions（建议 < 5000 tokens）**：激活时加载整个 SKILL.md 正文；
3. **Resources（按需）**：`scripts/`、`references/`、`assets/` 需要时才加载。

> Keep your main `SKILL.md` under 500 lines. Move detailed reference material to
> separate files.

### 文件引用（§File references）

使用相对技能根目录的相对路径（如 `references/REFERENCE.md`、`scripts/extract.py`）；
**引用距 SKILL.md 保持一层深**，避免深层嵌套引用链。

### 校验（§Validation）

使用参考实现 [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref)：
`skills-ref validate ./my-skill`，校验 frontmatter 合法性与全部命名约定。
（本环境未安装该二进制 ⇒ ForgeFlow 侧相关校验按 A5 裁决记 **skipped+原因**，不冒充 PASS。）

## 漂移检测

* 基线 = 上表「Markdown 原文 sha256」。
* 复核流程：重新抓取 https://agentskills.io/specification.md → 计算 sha256 → 调
  `forgeflow.skills.spec_mapping.spec_drift_alert(新哈希)`；返回 `None` 表示无漂移，
  否则返回告警文本（**不阻塞发布**）。告警出现时应人工复核规范差异并更新本快照
  （含取得日期与哈希）。
