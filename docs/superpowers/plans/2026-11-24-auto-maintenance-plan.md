# 自动维护实施计划

日期：2026-11-24（进度更新：2026-11-26）
依据：`docs/superpowers/specs/2026-11-17-auto-maintenance-design.md`（状态：已修订，待批准）
状态：执行中（PR 1–5 已合入 `main`，M3 端到端验收进行中）

## 0. 总体策略

- **分 6 个 PR（PR 1–5 加 PR 4b）+ 3 段手动 GitHub 配置**，按依赖顺序推进。每个 PR 独立可验证、可回滚，合入前全部本地验证通过。
- **顺序的硬约束**（为什么不能乱）：
  1. 必需检查的名字只有在 PR 3 的工作流真实跑过一次后才能确定，所以 **Ruleset 必须在 PR 3 合入之后创建**。
  2. `automerge.yml` 一旦合入 `main` 就生效，所以它必须在 **Ruleset 已启用、两个 App 与 Environment 已就绪**之后才合入（PR 4）。
  3. 发布流水线（PR 5）会推 GHCR `:latest`，被所有宿主机拉取，必须最后上线，且先彩排（见 PR 5）。
- PR 1–3 在 Ruleset 创建前由管理员直接合并；M2 已启用 `main-checks` 与 `main-review`，PR 4、4b、5 及后续维护者自提 PR 均受 Ruleset 约束，使用方法 B 的管理员 `For pull requests only` 绕过审批（检查必须全绿，零豁免）。
- 每个 PR 用 `claude/*` 或 `chore/auto-maint-N-*` 分支，PR 描述附验证输出。**所有提交与推送由维护者确认后再做**，本计划不自动提交。

| 阶段 | 内容 | 依赖 | 谁做 | 状态 |
| --- | --- | --- | --- | --- |
| PR 1 | 上游残留清理、脚本与文档修复 | 无 | 实现者 | 已合并（#10） |
| PR 2 | `.go-version`、`tools/go.mod`、GoReleaser v2 配置**与旧 `release.yml` 的调用同 PR 原子切换**、Docker digest、文档依赖锁定 | PR 1 | 实现者 | 已合并（#11） |
| PR 3 | 只读 CI 体系、可信镜像发布基础、Dependabot、CodeQL/文档工作流 | PR 2 | 实现者 | 已合并（#12） |
| M1 | 建 2 个 GitHub App、2 个 Environment、安全设置 | 可与 PR 1–3 并行 | 维护者 | 已完成（2026-11-25） |
| M2 | 创建 `main-checks`、`main-review` Ruleset | PR 3 合入并跑过 | 维护者 | 已完成（2026-11-25） |
| PR 4 | `automerge.yml`、`go-toolchain-sync.yml` 及其校验脚本 | M1、M2 | 实现者 | 已合并（#14）；真实样本已排除正文信号，正向自动合并待验收 |
| PR 4b | macOS 发布目标（`darwin/amd64`、`darwin/arm64`），发布契约 6 → 8 个归档 | PR 4 | 实现者 | 已合并（#27，原 #15 合入堆叠分支后重提） |
| PR 5 | `release.yml`、`scheduled-release.yml`、证明与 SBOM | PR 4b | 实现者 | 已合并（#34，原 #16 合入堆叠分支后重提）；dry-run 托管彩排已通过（`45745244198`），首次正式发布待触发 |
| M3 | 端到端验收 | 全部 | 维护者 + 实现者 | 进行中 |

## 0.1 开始前需要你决定的 4 件事（有默认值，不回复则按默认）

1. **工具调用方式待验证（待验证假设，不是结论）。** 设计稿写的是 `go tool -modfile=tools/go.mod staticcheck ./...`。我怀疑 `-modfile` 会把主模块依赖集换成 `tools/go.mod`，导致 `staticcheck`/`govulncheck` 分析根目录代码时找不到根模块依赖，但尚未实测。**PR 2 的 Task 2.2 先实测两种写法**：(a) 设计稿原写法；(b) `go build -modfile=tools/go.mod -o .tools/ <工具包>` 编译成二进制后在仓库根目录运行（`.tools/` 加入 `.gitignore`）。(a) 可用就保持设计稿不变；仅 (a) 不可用时才采用 (b)，并同步修订设计稿第 5.2、7 节。两种方式依赖版本都由 `tools/go.mod` + `tools/go.sum` 锁定，安全性相同。下文命令以 (b) 书写，仅为占位。
2. **新增最小范围的 `.gitattributes`（仅 `*.sh`、`go.mod`、`go.sum`、`*.yml`、`Dockerfile*` 强制 `eol=lf`）。** 设计稿未列出，但本机 Windows `core.autocrlf=true` 已导致 `go mod tidy -diff` 假阳性，也会让 `.sh` 脚本在 Linux CI 上出错。**不使用 `* text=auto`，也不对全仓做 `--renormalize`**，避免产生全仓行尾改写的噪声 diff。默认加。
3. **新增 `actionlint` 到 `tools/go.mod`。** 设计稿 §7 要求 `actionlint`，本机未安装；做成 `tools` 依赖就能本地与 CI 同一版本运行并由 Dependabot 维护。默认加。
4. **校验脚本放在 `.github/scripts/`（Python 标准库，带单元测试）。** 自动合并判定是整套设计里风险最高的逻辑，必须能用固定样本离线测试，不能只写在 YAML 里。建议把 `.github/scripts/**` 也列入"敏感路径硬禁区"（设计稿 §5.1 第 3 条目前只列了 workflows、actions、CODEOWNERS、dependabot.yml）。默认加。

---

## PR 1：清理上游残留与脚本/文档修复

分支：`chore/auto-maint-1-cleanup`。**不改任何 CI 行为。**

- [ ] **1.1 删除文件**：`oryxBuildBinary`、`scripts/codecov.sh`、`.github/stale.yml`、`.devbots/`、`.codacy.yml`、`.all-contributorsrc`。
  - 先 `git rm`，再 `git grep -n -E 'oryxBuildBinary|codecov\.sh|stale\.yml|devbots|codacy|all-contributors'` 确认没有其他引用（`docs/index.md` 的徽章、CONTRIBUTING 在 1.6 处理）。
- [ ] **1.2 `.gitignore`** 加入 `/oryxBuildBinary`、`/.tools/`。
- [ ] **1.3 `.gitattributes`**（见 0.1 第 2 项，仅限定文件类型，**不做全仓 renormalize**）。加完后 `git status` 应只多出这一个文件。
- [ ] **1.4 `.github/CODEOWNERS`** 重写为单行 `* @Shouyupedia`。
- [ ] **1.5 `scripts/build-tplprev.sh`**：改为 `set -euo pipefail`；`misc/wasm/wasm_exec.js` → `lib/wasm/wasm_exec.js`（已核实 Go 1.27 的 `lib/wasm/` 下有该文件）；`cd "$(git rev-parse --show-toplevel)"` 加引号；`GOARCH=wasm GOOS=js` 输出路径不变。
  - 验证：`bash scripts/build-tplprev.sh && ls docs/assets/wasm_exec.js docs/assets/tplprev.wasm`（这两个文件已在 `.gitignore`）。
- [ ] **1.6 模板与说明文档**：
  - `.github/ISSUE_TEMPLATE/bug.yml`：镜像名改为 `ghcr.io/shouyupedia/watchtower:latest`，删除"其他 fork 不受支持 / v2tec"整句。
  - `.github/ISSUE_TEMPLATE/config.yml`：联系链接改为本仓库。**注意**：远端 `has_issues: false`，Issue 模板暂不生效；保持现状并在 M1 里让你决定是否启用 Issues。
  - `.github/pull_request_template.md`：去掉面向社区贡献者的措辞，保留检查项。
  - `SECURITY.md`：邮箱改为本仓库私密漏洞报告入口（`https://github.com/shouyupedia/watchtower/security/advisories/new`）。
  - `code_of_conduct.md`：改为引用 Contributor Covenant 2.1。
  - `CONTRIBUTING.md`：示例镜像名改为本仓库；更正 `Dockerfile.self-contained` 说明（先读 `dockerfiles/Dockerfile.self-contained` 确认它现在构建本地源码）。
  - `scripts/dependency-test.sh:66` 镜像名改为本仓库。
- [ ] **1.7 `docs/*.md`**（`git grep -n -E 'containrrr' -- docs ':!docs/superpowers'` 约 60 处）：
  - `containrrr/watchtower` → `ghcr.io/shouyupedia/watchtower`（`docs/arguments.md`、`http-api-mode.md`、`index.md`、`introduction.md`、`notifications.md`、`private-registries.md`、`remote-hosts.md`、`running-multiple-instances.md`、`secure-connections.md`、`usage-overview.md`）。
  - `docs/introduction.md:11` 是示例输出，同步改镜像名。
  - `containrrr.dev/watchtower/...`、`containrrr.github.io/watchtower/...` → 站内相对链接（`arguments.md:250,330,362`、`container-selection.md:6,34,58,81`、`notifications.md:22`、`running-multiple-instances.md:1`）。
  - `docs/updating.md:5` 与 `docs/usage-overview.md:1`：Docker Hub 改为 GHCR（`usage-overview.md` 里 `armhf-<tag>` 镜像说法按 GHCR 实际发布的多架构清单改写，先读 `dockerfiles` 与发布流程确认）。
  - `docs/index.md:11-36`：删除 CircleCI、Codecov、GoDoc、Go Report Card、Codacy、All Contributors、Docker Hub 徽章，保留许可证徽章并指向本仓库。
  - **保留**：`containrrr.dev/shoutrrr/...`（真实依赖）、`com.centurylinklabs.*`。
- [ ] **1.8 `mkdocs.yml`**：`repo_url`、`magiclink.user` 指向本仓库（`site_url` 在 PR 3 中移除，不发布 GitHub Pages）；增加 `exclude_docs: superpowers/`（需 mkdocs ≥ 1.5，PR 2 锁定版本时核对）。
- [ ] **1.9 README**：增加 `gh attestation verify` 说明的占位小节留到 PR 5，这里不动。

**PR 1 验收**

```bash
git grep -n -E 'containrrr' -- ':!docs/superpowers' ':!go.mod' ':!go.sum' ':!*.go' ':!pkg/container/mocks'
# 预期只剩：README 致谢、shoutrrr 链接、com.centurylinklabs 标签、mkdocs 主题色板名 containrrr
go build ./... && go vet ./... && go test ./...
bash scripts/build-tplprev.sh
```

---

## PR 2：工具链、GoReleaser v2、依赖锁定

分支：`chore/auto-maint-2-toolchain`。

- [ ] **2.1 `.go-version`**：写入当前 1.26 系列最新补丁版（`curl -s 'https://go.dev/dl/?mode=json' | python -c ...` 取 `1.26.*` 最新稳定版；`go.mod` 的 `go 1.26.0` 不变）。选 1.26 系列的理由：保持与现有 `go.mod` 与 Dockerfile 一致，首次 `go-toolchain-sync` 会自动提 `1.26 → 1.27` 跨次版本 PR 供你审核（设计稿 §6 留给构建者的决定）。
- [ ] **2.2 `tools/go.mod` 先做 spike，再定稿**：
  ```bash
  mkdir tools && cd tools
  go mod init github.com/containrrr/watchtower/tools   # go 指令与根模块一致
  go get -tool honnef.co/go/tools/cmd/staticcheck@latest golang.org/x/vuln/cmd/govulncheck@latest \
         github.com/goreleaser/goreleaser/v2@latest github.com/rhysd/actionlint/cmd/actionlint@latest
  cd ..
  go build -modfile=tools/go.mod -o .tools/ honnef.co/go/tools/cmd/staticcheck golang.org/x/vuln/cmd/govulncheck github.com/goreleaser/goreleaser/v2 github.com/rhysd/actionlint/cmd/actionlint
  .tools/staticcheck -version && .tools/govulncheck -version && .tools/goreleaser --version
  ```
  - **实测** 设计稿写法 `go tool -modfile=tools/go.mod staticcheck ./...` 与 `govulncheck ./...`、`goreleaser` 是否可用（见 0.1 第 1 项，尚属待验证假设）。结论写进 PR 描述；仅当设计稿写法确实不可用时才**更新设计稿第 5.2 与 7 节**。
  - 新增脚本 `scripts/tools.sh`（`set -euo pipefail`）统一"构建工具到 `.tools/`"，本地和 CI 共用，避免命令散落。
  - 确认 Dependabot 对 `/tools` 的 `tool` 依赖可见：合入后在 Dependabot 日志里核对（M3）。
- [ ] **2.3 staticcheck 基线**：`.tools/staticcheck ./...`，修复现有告警（预期很少）。
- [ ] **2.4 govulncheck 基线**：`.tools/govulncheck ./...`。
  - 能升级的先升级（如 `golang.org/x/net`），每个升级单独一次 `go get` + `go mod tidy` + `go test ./...`。
  - 剩余无修复的漏洞写入 `.github/govulncheck-exceptions.json`：`[{"id":"GO-xxxx-xxxx","reason":"...","expires":"YYYY-MM-DD"}]`。
  - 新增 `scripts/govulncheck-gate.py`（标准库）：解析 `govulncheck -format json` 输出，已豁免且未过期的放行，其余失败；**过期的豁免本身触发失败**。带单元测试 `scripts/test_govulncheck_gate.py`。
- [ ] **2.5 GoReleaser v2 迁移 `goreleaser.yml`，并在同一个 PR 内原子切换旧 `release.yml` 的 GoReleaser 调用**：
  - **为什么必须同 PR**：旧 `release.yml` 用 `goreleaser-action` 固定 `v0.155.0`，若 v2 配置先合入、`release.yml` 到 PR 5 才替换，中间任何正式 tag 都会让 v0.155.0 去读 v2 配置而失败或产出错误产物。所以本 PR 同时把旧 `release.yml` 的 `goreleaser-action` + `version: v0.155.0` 步骤替换为"构建 `tools/go.mod` 中的 GoReleaser 并运行 `release --clean`"（只改这一步与 `go-version-file`，其余留给 PR 5）。本 PR 合入后到 PR 5 之间的手动 tag 发布仍可用。
  - 加 `version: 2`、`project_name: watchtower`（避免依赖目录名）。
  - **目标平台按 `v1.0.0` 的 6 个归档显式列出**，不再靠 `goos × goarch − ignore` 推导：`linux/{amd64,386,arm(v6),arm64}` 与 `windows/{amd64,386}`。**必须排除 `windows/arm64`**（现有 `goos×goarch` 加 `ignore: windows/arm` 会多产出 `windows/arm64`，而 `v1.0.0` 没有该资产）。用 `builds[].targets` 或 `ignore: [windows/arm, windows/arm64]` 实现。注意设计稿 §5.1 第 4 条目前写的是"排除 `windows/arm`"，与 `v1.0.0` 契约不符，**需同步修订为"仅 6 个目标，排除 windows/arm 与 windows/arm64"**。
  - `archives[].format` → `formats: [tar.gz]`；`format_overrides[].format` → `formats: [zip]`；`replacements` 已移除，改用 `name_template` 内的模板条件：
    `{{ .ProjectName }}_{{ .Os }}_{{ if eq .Arch "arm" }}armhf{{ else if eq .Arch "arm64" }}arm64v8{{ else }}{{ .Arch }}{{ end }}`。
  - 保留 `ldflags`、`ignore: windows/arm`、`files: LICENSE.md`、`goarm` 默认（6）与 `armhf` 名一致。
  - `.tools/goreleaser check`，再 `.tools/goreleaser release --snapshot --clean`。
  - **产物契约核对脚本** `scripts/check-release-contract.sh`（snapshot 的 `dist/` 对比现有 v1.0.0 资产清单）：
    - 恰好 7 个发布文件、文件名完全一致、**不得多出任何其他归档**（尤其 `windows_arm64`）：`watchtower_linux_{386,amd64,arm64v8,armhf}.tar.gz`、`watchtower_windows_{386,amd64}.zip`、`watchtower_<version>_checksums.txt`（`dist/` 里版本号是 snapshot 版本，比较时按模式匹配）。
    - **校验和文件**：逐行核对其中恰为上述 6 个归档的 sha256（用 `sha256sum -c` 在 `dist/` 里验证），行数为 6，文件名与 `v1.0.0` 的 `watchtower_1.0.0_checksums.txt` 格式一致（可对比 `gh release download v1.0.0 -p '*checksums.txt'` 的行格式）。
    - 每个归档内含 `watchtower`（Windows 为 `.exe`）与 `LICENSE.md`。
    - `dist/watchtower_linux_amd64*/watchtower -v` 输出含注入的版本号。
    - 可选对照：`gh release download v1.0.0 -D <空目录>`，比较归档内文件列表；下载物按不可信数据对待，放独立空目录。
  - 本地与 CI 的契约核对脚本必须在本 PR 通过后才可合入。
- [ ] **2.6 `docs-requirements.txt` 锁定**：
  ```bash
  uv pip compile docs-requirements.in --generate-hashes --python-version <钉死的版本> -o docs-requirements.txt
  ```
  - 把现有三项直接依赖（`mkdocs`、`mkdocs-material`、`md-toc`）移到 `docs-requirements.in`（Dependabot `pip` 只读 `requirements*.txt`，所以实际被 Dependabot 维护的是带 hash 的 `docs-requirements.txt`；PR 4 的 `pip` 白名单只允许 `docs-requirements.txt` 改动）。**需实测** Dependabot 对 `uv pip compile` 产物（含 `# via` 注释、`--hash` 续行）的更新行为，不满足就改用 `pip-compile` 输出格式；这项放在 M3 观察首批 `pip` PR 时确认。
  - 钉死文档构建用的 Python 小版本（写入 `.python-version`，供 `actions/setup-python` 的 `python-version-file` 读取）。
  - 验证：干净 venv 里 `pip install --require-hashes -r docs-requirements.txt && bash scripts/build-tplprev.sh && mkdocs build --strict`。
- [ ] **2.7 Dockerfile 钉 digest**：三个 `dockerfiles/Dockerfile*` 的 `golang:1.26-alpine` → `golang:<.go-version>-alpine@sha256:<digest>`。digest 用 `docker buildx imagetools inspect golang:<ver>-alpine` 获取（多架构索引 digest）。
  - 验证：`docker buildx build --platform linux/amd64,linux/arm64 -f dockerfiles/Dockerfile .`（不推送）；`Dockerfile.self-contained` 与 `Dockerfile.dev-self-contained` 各构建一次。
- [ ] **2.8 一致性自检脚本** `scripts/check-go-version.py`：断言 `.go-version` 与三个 Dockerfile 的 tag、`go.mod`/`tools/go.mod` 的 `go` 指令次版本一致，CI 里作为一个检查（防止 `go-toolchain-sync` 之外的手改造成漂移）。

**PR 2 验收**

```bash
go build ./... && go vet ./... && go test ./... && go mod verify && (cd tools && go mod verify)
# 在 LF 环境（临时副本或 CI）：go mod tidy -diff && (cd tools && go mod tidy -diff)
bash scripts/tools.sh && .tools/staticcheck ./... && .tools/govulncheck ./...
.tools/goreleaser check && .tools/goreleaser release --snapshot --clean && bash scripts/check-release-contract.sh
```

---

## PR 3：只读 CI 体系与可信发布基础

分支：`chore/auto-maint-3-ci`。注意：此 PR 本身就是新 CI 的第一次真实运行，**作为"必需检查名"的取样来源**。

- [ ] **3.1 通用约定**（所有工作流）：顶层 `permissions: {}`；任务级最小权限；`actions/checkout` 一律 `persist-credentials: false`；`actions/setup-go` 用 `go-version-file: .go-version`；所有 `uses:` 钉完整 SHA 并附版本注释。
  - SHA 获取：`git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<tag>*'`，注意附注 tag 要取 `^{}` 解引用后的 commit SHA，逐个核对。
- [ ] **3.2 `.github/actions/docker-build/action.yml`**（composite，无权限声明）：输入 `push`、`tags`、`version`、`platforms`、`disable_cache`；封装 `docker/setup-buildx-action` + `docker/build-push-action`，传 `WATCHTOWER_VERSION` 与 OCI 标签。**不装 QEMU**。
- [ ] **3.3 `ci.yml`**（`workflow_call`，输入 `ref`、`upload_coverage`；`contents: read`）。任务（**名字一旦确定就是必需检查名，后续不要随意改**）：
  `lint`（staticcheck）、`test`（ubuntu/macos/windows 矩阵）、`mod-check`（根与 `tools` 的 `go mod verify` + `go mod tidy -diff`）、`version-consistency`（2.8）、`vuln`（govulncheck + 2.4 的门控脚本）、`script-tests`（`python -m unittest` 跑 `scripts/` 与 `.github/scripts/` 的测试）、`goreleaser-snapshot`（含 2.5 契约核对）、`docker-build`（只读多架构构建）、`docs-build`（`--require-hashes` + `build-tplprev.sh` + `mkdocs build --strict`）、`workflow-lint`（actionlint + zizmor）。
  - 覆盖率上传：仅在 `upload_coverage` 为真且 `CODECOV_TOKEN` 存在时执行，`fail_ci_if_error: false`。
  - zizmor 接入：用 pip 安装并固定版本 + `--require-hashes`（单独的 `.github/zizmor-requirements.txt`），最低阈值先设"任何发现均失败"，必要豁免逐条写理由到 `.github/zizmor.yml`。
- [ ] **3.4 `pull-request.yml`**：`pull_request` → `main`；调用 `ci.yml`；`dependency-review` 任务（`contents: read`，`comment-summary-in-pr: never`）；`concurrency: pr-${{ github.event.pull_request.number }}`，`cancel-in-progress: true`。
- [ ] **3.5 `push-main.yml`**（取代 `release-dev.yaml`）：`push` → `main` + `workflow_dispatch`；调用 `ci.yml`，通过后调用 `docker-publish.yml` 推 `:latest-dev`。
- [ ] **3.6 `docker-publish.yml`**（取代 `docker.yml`，仅 `workflow_call`）：权限 `contents: read`、`packages: write`、`id-token: write`、`attestations: write`。本 PR 只实现 `:latest-dev` 路径与镜像证明/SBOM；版本 tag 与 `:latest` 的"不覆盖、仅最高版本才推进"逻辑在 PR 5 加入（本 PR 内对 `tag` 输入非空直接报错，防止提前误用）。
- [ ] **3.7 `codeql-analysis.yml`**：顶层 `permissions: {}`，任务级 `actions: read`、`contents: read`、`security-events: write`；钉 SHA；删除 `git checkout HEAD^2` 步骤。
- [ ] **3.8 移除 GitHub Pages 发布**：删除 `publish-docs.yml` 并移除 `mkdocs.yml` 中的 `site_url`（不发布 GitHub Pages；`ci.yml` 的只读 `docs-build` 仍运行 `mkdocs build --strict` 校验文档）。
- [ ] **3.9 `.github/dependabot.yml`** 按设计稿 §5.3：`gomod /`、`gomod /tools`（`allow: dependency-type: all`）、`docker /dockerfiles`（`golang` 忽略 semver 三类更新）、`pip /`、`github-actions /`；各自 `cooldown`（gomod 3/7/30 天，其余 default-days 7）与分组。用 JSON Schema 校验（`check-jsonschema --builtin-schema vendor.dependabot`，装入 `.github/zizmor-requirements.txt` 同类固定文件或 `workflow-lint` 里 pip 固定安装）。
- [ ] **3.10 删除**：`greetings.yml`、`dependabot-approve.yml`、`release-dev.yaml`、`publish-docs.yml`（不发布 GitHub Pages；`docker.yml` 保留 tag 触发路径至 PR 5）。**旧 `release.yml` 的 GoReleaser 步骤已在 PR 2 切换到 v2，本 PR 不再触碰**（PR 5 整体替换）。

**PR 3 验收**

```bash
.tools/actionlint                       # 全部工作流零告警
zizmor --min-severity low .github/      # 或固定版本的等价命令，零告警
git ls-remote ... # 每个 uses SHA 与版本 tag 逐一核对（可写成 scripts/verify-action-pins.sh）
```
- 推送分支后在 GitHub 上观察 `Pull Request`、`CodeQL` 全绿；**记录实际出现的 check 名**（形如 `<调用任务> / <被调用任务>`），供 M2 使用：`gh api repos/{owner}/{repo}/commits/<sha>/check-runs --jq '.check_runs[].name'`。
- 合入后观察 `push-main.yml` 成功推出 `:latest-dev`，并用 `gh attestation verify oci://ghcr.io/shouyupedia/watchtower:latest-dev --owner shouyupedia` 验证证明。

---

## M1：GitHub 手动配置（维护者，可与 PR 1–3 并行）

**状态**：已完成（2026-11-25）。按设计稿 §8 第 1、2、4、6 项执行：

- [x] 创建 `watchtower-updater`、`watchtower-automerge` 两个私有 App（仅 `Contents` 与 `Pull requests` 读写，关闭 Webhook，Only on this account），分别生成私钥并**只安装到本仓库**。
  - **2026-11-25 核实**：`watchtower-automerge`（Integration `5915895`）已通过 `main-review` Ruleset 确认安装到本仓库；`watchtower-updater` 安装范围与两 App 的权限/Webhook 无法用当前 OAuth token 调 `/user/installations` 查询（HTTP 403），由维护者在网页端确认。
- [x] 创建 Environment `updater`、`automerge`，Deployment branches 限定为 `main`；写入 `UPDATER_APP_ID`、`UPDATER_APP_PRIVATE_KEY`、`AUTOMERGE_APP_ID`、`AUTOMERGE_APP_PRIVATE_KEY`。
  - **2026-11-25 核实**：`gh api` 确认两个 Environment 均为自定义分支策略且仅允许 `main`，四个 Environment secrets 均已配置。
- [x] 打开 Dependency graph、Dependabot alerts、Dependabot security updates、Private vulnerability reporting。**提醒**：Dependabot security updates 开启后它的 PR 也会出现，PR 4 之前它们只会等待人工审核，符合预期。
  - **2026-11-25 核实**：`gh api` 确认 Private vulnerability reporting（`enabled: true`）、漏洞告警（`vulnerability-alerts` 返回 `204`）、Dependabot 安全更新（`automated-security-fixes` 返回 `enabled: true, paused: false`）均已开启；fork 上的 Dependabot version updates 需在网页端 `Insights → Dependency graph → Dependabot` 及 M3 确认。
- [x] 决定是否启用 Issues（影响 PR 1.6 的模板是否有意义）。
  - **2026-11-25 核实**：`gh api` 确认当前保持关闭（`has_issues: false`）。
- [x] 私钥只保存在 Environment secrets，不要落地到本机文件或交给任何工具（本机 `.pem` 清理与凭据管控由维护者确认）。

## M2：Ruleset（维护者，PR 3 合入、检查名确定后）

**状态**：已完成（2026-11-25）。

- [x] 用 PR 3 记录的真实 check 名创建两个 Ruleset（Enforcement = Active，目标分支 `main`）：
  - **`main-checks`（ID `28051307`，`active`，Bypass 为空 `[]`）**：规则含 `deletion`、`non_fast_forward`、`pull_request`（`required_approving_review_count: 0`，`allowed_merge_methods: ["merge"]`）、`required_status_checks`（`strict_required_status_checks_policy: true`，`do_not_enforce_on_create: false`）、`code_scanning`（`CodeQL`，`alerts_threshold: errors`，`security_alerts_threshold: high_or_higher`）。14 项必需检查（来源均为 GitHub Actions，`integration_id: 15368`）：`CI / lint`、`CI / test (ubuntu-latest)`、`CI / test (windows-latest)`、`CI / test (macos-latest)`、`CI / mod-check`、`CI / version-consistency`、`CI / vuln`、`CI / script-tests`、`CI / goreleaser-snapshot`、`CI / docker-build`、`CI / docs-build`、`CI / workflow-lint`、`dependency-review`、`Analyze (go)`。
  - **`main-review`（ID `28051631`，`active`）**：`required_approving_review_count: 1`、`require_code_owner_review: true`、`dismiss_stale_reviews_on_push: true`、`require_last_push_approval: true`；Bypass 仅两项且均为 `pull_request` 模式：`RepositoryRole` `5`（Repository admin）与 `Integration` `5915895`（`watchtower-automerge`）。
  - **PR 4 注意**：`main-checks` 当前将 `allowed_merge_methods` 限定为 `["merge"]`，实现 PR 4.3 的 REST 合并调用时须使用 `merge_method=merge`（或按需调整 Ruleset 允许的合并方式）。
- [x] 把 App 加入 Bypass 前，确认 App 已安装：`watchtower-automerge`（Integration `5915895`）已成功加入 `main-review` 的 `bypass_actors`。
- [x] 核实生效：2026-11-25 `gh api repos/{owner}/{repo}/rules/branches/main` 确认两条 Ruleset 全部规则生效。
- [x] 干跑验证两条规则（测试 PR #13，分支 `chore/ruleset-dry-run`，**不合并**）：
  - 15 项检查全绿（14 项必需检查 + `CodeQL`，含 `CI / test (macos-latest)`）；检查运行期间与全绿后 API 均返回 `mergeStateStatus: BLOCKED`、`reviewDecision: REVIEW_REQUIRED`。
  - PR #13 已关闭（不合并），远端/本地测试分支与临时 worktree 均已删除。
- [x] 把 `allow_auto_merge` 保持 `false`（2026-11-25 `gh api` 实测确认 `false`）。

---

## PR 4：自动合并与 Go 工具链同步

分支：`chore/auto-maint-4-automerge`。**已合并（#14，2026-11-25）**；离线单测（243 通过）、离线回放、actionlint 与 zizmor 已通过，4.1a 真实样本校准与线上自动合并验收延至 M3。

- [x] **4.1 测试先行：`.github/scripts/automerge_verify.py` 与 `.github/scripts/test_automerge_verify.py`**（纯函数 + 可注入的 HTTP/GitHub 客户端，标准库）。每条"反目标"和白名单规则对应至少一个**拒绝用例**，任何输入缺失都必须返回 `eligible=false`：
  - 事件与绑定：非 `pull_request` 事件、非本仓库 head、PR 非 open/是 draft、`head.sha` 与 `workflow_run.head_sha` 不一致、找到 0 个或多于 1 个 PR。
  - 必需检查：`Pull Request` 或 `CodeQL` 缺失/未完成/失败，另一个仍在运行（静默退出）。
  - 身份与提交：作者不是 `dependabot[bot]`/`watchtower-updater[bot]`；提交数 ≠ `pr.commits`；存在未验证签名、作者不符、committer 不是 `web-flow` 或该机器人本身的提交；存在合并提交。
  - 安全更新与禁区：带 security 标签/元数据；改动含 `.github/workflows/**`、`.github/actions/**`、`.github/scripts/**`、`CODEOWNERS`、`dependabot.yml`。
  - `go_modules`：文件越界；`go`/`toolchain`/`replace`/`exclude`/`retract`/`tool` 指令变化；主版本升级；新增/升级模块未满冷却期；缺少可信首次公开可见记录。
  - `docker`：除 `@sha256` 外任何字符变化；镜像名/标签变化；多行改动；`tag_last_pushed` 缺失或未满 7 天。
  - `pip`：缺 `--hash`；出现 `--index-url`/`--extra-index-url`/git URL；主版本升级；PyPI 上传时间缺失或未满 7 天。**冷却期必须覆盖锁文件中该包该版本允许的每一个 `--hash`**：把每个 hash 对应到 PyPI JSON 中的具体文件，各自的 `upload_time_iso_8601` 都要满 7 天（取**最晚**上传的文件，而不是最早的）；有任何 hash 找不到对应文件或无时间戳即 `eligible=false`。
  - 普通更新 vs 安全更新：见 4.1a。
  - `github_actions`：永远 `false`。
  - Go 工具链 PR：跨次版本；`go.mod` 有变化；`.go-version` 补丁号非递增；Dockerfile 不是 `golang:<新版本>-alpine@sha256:` 同步；冷却期未满。
  - 用真实 API 响应样本（脱敏后）作为 fixtures 放 `.github/scripts/fixtures/`。
- [ ] **4.1a 正向验收：用真实 Dependabot PR 校准"普通更新 / 安全更新"的区分（先 spike，再写判定）**：
  - 收集真实样本：`gh pr list --author 'app/dependabot' --state all --json number,title,body,labels,headRefName` 取历史普通 PR；在 M1 开启 Dependabot security updates 后，取一个真实的安全更新 PR（或用 Dependabot 告警对应的 PR）；脱敏后存入 `fixtures/`。
  - 找出只读任务**可靠**的区分信号（分支名、PR 正文固定段落、标签、提交元数据、Dependabot alerts API 是否对只读令牌可用等），并为每一类信号写正反用例。
  - **证据不足的处理**：只要无法确定一个 PR 不是安全更新（信号缺失、结构异常、API 不可读），一律 `eligible=false` 转人工；宁可让普通更新多走一次人工审核，也不能让安全更新自动合并。
  - 通过标准：真实普通更新样本 → 判定为普通；真实安全更新样本 → `eligible=false`；缺字段样本 → `eligible=false`。
  - **2026-11-26 部分完成：真实样本校准**。离线复核真实样本后确认：Dependabot 的普通更新与安全更新可以使用完全相同的正文结构与标签，正文信号不能作为区分依据，因此不加入白名单。样本来源与逐条证据只保留在本地校准记录，不写入公开文档。
  - 另一个候选方向是分组更新（已验证签名提交中的 `dependency-group` 元数据）。它对安全更新同样适用，还需校验受信任配置中的组用途和实际改动目录；单个外部样本不足以放行本仓库的 Go、Docker 或 pip 更新。
  - **正向自动合并仍待验收**：在取得可绑定到具体 PR 与 head SHA 的普通更新凭据前，Dependabot PR 继续转人工审核，上方通过标准尚未满足。
- [x] **4.2 实现冷却期查询**：
  - Go 模块：读 `https://index.golang.org/index?since=...` 查询模块版本首次入库 `Timestamp`；先**实测索引接口分页与按模块过滤能力**（它按时间排序、不支持按模块查询，需要评估扫描成本）。若无法在合理请求数内定位，则 fail-closed（`eligible=false`），并在 PR 描述中说明 Go 模块自动合并因此退化为人工审核，由你决定是否接受或另寻可信来源。**这是整个计划里最大的不确定点，先 spike 再继续。**
    - **4.2 实测结论（2026-11-25）**：`cooldown.go_first_seen` 仅将 `proxy.golang.org` 的 `.info` `Time` 作为前向扫描起点提示（`since = Time - 60s`），不将其当作可信发布时间；请求预算固定为 10 次（1 次 `.info` + 最多 9 页 `index.golang.org/index`，每页上限 2000 条）。实测样本中约 6/12 个 Go 模块命中，`golang.org/toolchain` 未在 10 次请求预算内命中。按 fail-closed 原则：未命中的 Go 模块升级 PR 判定 `eligible=false` 转人工审核；`go_toolchain_sync.py` 未命中时拒绝创建/更新工具链 PR（`needs_sync=False`），`automerge_verify.py` 亦判定 `eligible=false`（均未实现“首次观察起算”回退）。
  - Docker：Docker Hub `https://hub.docker.com/v2/repositories/library/golang/tags/<tag>` 的 `tag_last_pushed`，并核对 digest 与 PR 中一致（缺失或不匹配时 fail-closed）。
  - PyPI：`https://pypi.org/pypi/<pkg>/<version>/json` 的 `upload_time_iso_8601`（覆盖锁文件中该包该版本允许的每一个 `--hash`，取对应文件的**最晚**上传时间；任一 hash 缺失或无时间戳即 fail-closed，与 4.1 保持一致）。
- [x] **4.3 `automerge.yml`**：
  - 触发：`workflow_run`（`["Pull Request","CodeQL"]`，`completed`）；顶层 `permissions: {}`；`concurrency` 按目标 PR 分组。
  - `verify`：`contents: read`、`pull-requests: read`、`checks: read`、`actions: read`；`actions/checkout` 显式 `ref: ${{ github.event.repository.default_branch }}` 检出 `main` 取脚本，**不检出 PR 代码、不下载 artifact**；`GITHUB_TOKEN` 只读；输出 `eligible`、`pr_number`、`head_sha`。
  - `merge`：`needs: verify`，`if: needs.verify.outputs.eligible == 'true'`，`environment: automerge`；`actions/create-github-app-token`（钉 SHA）签发令牌；`gh api -X PUT repos/{owner}/{repo}/pulls/<n>/merge -f sha=<head_sha> -f merge_method=merge`（对齐 M2 已生效的 `main-checks` Ruleset，见 `d3484b7`：`allowed_merge_methods: ["merge"]`）；单次带 `sha` 保护请求，**不调用 `update-branch`**；HTTP 405/409 视为正常结束，不重试。
  - 所有从事件载荷取的值经环境变量传递，**不直接内插进 `run:`**（zizmor 会检查）。
- [x] **4.4 `.github/scripts/go_toolchain_sync.py` + 测试**：
  - 检查：`go.dev/dl/?mode=json` 最新稳定版、Docker Hub digest 与 `tag_last_pushed`、可信首次可见时间，均满 7 天才采用；已一致且无落后同步分支则退出。
  - 同次版本补丁：只改 `.go-version` 与 `dockerfiles/Dockerfile*`；跨次版本：额外改 `go.mod`、`tools/go.mod` 的 `go` 指令并跑 `go mod tidy`。
  - 提交：GraphQL `createCommitOnBranch`（`expectedHeadOid` 防并发），分支名固定 `automation/go-toolchain`，单提交；已有同名开放 PR 则更新该分支。`push → main` 触发时仅在该分支落后时重放。
- [x] **4.5 `go-toolchain-sync.yml`**：每周一 02:17 UTC + `push` → `main` + `workflow_dispatch`；检查任务只读；提 PR 任务绑定 `environment: updater`，用 `watchtower-updater` 令牌。
- [x] **4.6 同步修订设计稿**：把 4.2 的实测结论与 0.1 的偏离写回设计稿（状态改为"已批准"由你决定）。
  - **2026-11-25 已同步至 `docs/superpowers/specs/2026-11-17-auto-maintenance-design.md`**：记录 M2 活跃 Ruleset 仅允许 `merge_method=merge`（单次 `sha` 保护请求、HTTP 405/409 不重试）、4.2 `index.golang.org` 10 次请求实测（~6/12 命中与 `golang.org/toolchain` 未命中导致依赖 PR 转人工且阻止工具链同步开 PR）、4.1a 合成夹具与空校准集使全部 Dependabot PR 暂转人工、PyPI 取全部允许 hash 最晚上传时间、Docker 独立核验 fail-closed，以及 0.1 各项偏离（`go tool`/`go build` 实测结果、最小 `.gitattributes`、`tools/go.mod` 增补 `actionlint`、`.github/scripts/` 与 `.github/scripts/**` 敏感路径硬禁区）。设计稿状态字段保持“已修订，待批准”交维护者决定。

**PR 4 验收**

```bash
python -I -m unittest discover -s .github/scripts -v     # 全部通过，且包含 4.1 列的所有拒绝用例
.tools/actionlint && zizmor .github/                      # 零告警
```
- 合入前**离线回放**：对近期真实的 PR（如 #8、#9、将来的首批 Dependabot PR）用脚本只跑 `verify`（不合并），确认结果符合预期（人工 PR 一律 `eligible=false`）。
- 合入后立即检查 Actions 页：`automerge.yml` 只在 PR 工作流完成后触发，不应对现有 PR 产生任何合并。

---

## PR 4b：macOS 发布目标

分支：`chore/auto-maint-4b-macos`。**已合并（#27，2026-11-25；原 #15 误合入堆叠分支后重提）**；8 个归档契约、`govulncheck` 8 目标、`test (macos-latest)` 冒烟与文档均已合入 `main`。

**维护者决定（2026-11-24）**：macOS 加入发布目标；`test (macos-latest)` 保留，并**继续阻塞**合并与发布。PR 2 的 2.5 节"恰好 6 个归档"是当时与 `v1.0.0` 对齐的契约，自本 PR 起由下面的 8 个归档取代；`windows/arm64` 仍然排除。

**范围**：只增加二进制归档。GHCR 镜像仍只有 Linux 架构，不变。

**已实测（2026-11-24，`3c20231`，go1.26.11，Windows 主机）**：`CGO_ENABLED=0` 下 `darwin/amd64` 与 `darwin/arm64` 均可交叉编译，仓库内没有平台专用 Go 文件。**尚未实测**：产物在 macOS 上的运行。

- [x] **4b.1 `goreleaser.yml`**：`targets` 增加 `darwin_amd64`、`darwin_arm64`，更新"六个目标"的注释。不做 universal binary。
  - 归档名为 `watchtower_darwin_amd64.tar.gz` 与 `watchtower_darwin_arm64.tar.gz`。现有 `name_template` 会把所有 `arm64` 写成 `arm64v8`，需改为仅 Linux 使用 `arm64v8`；**已有 6 个归档名不得变化**。
- [x] **4b.2 `scripts/check-release-contract.sh`**：归档 6 → 8、校验和 6 → 8 行（发布文件共 9 个），补 `target_dirs` 与 `archive_target_dir`（`darwin` 的目录名以 snapshot 实际输出为准），更新头部契约说明。
  - 负面验证：缺任一 `darwin` 归档、多出 `windows_arm64`、`darwin` 归档内二进制与构建输出不一致，均须失败。
- [x] **4b.3 `scripts/run-govulncheck.sh`**：增加 `darwin/amd64/`、`darwin/arm64/`，共 8 个目标，更新头部注释。若出现 `darwin` 独有漏洞，按 2.4 的规则处理。
- [x] **4b.4 macOS 冒烟**：在 `ci.yml` 现有 `test` 任务内加仅 macOS 执行的步骤，**不新增任务、不改检查名**（M2 之后检查名已冻结）：按发布参数（`CGO_ENABLED=0`、相同 `ldflags`）构建 `darwin/arm64` 与 `darwin/amd64`，`file` 核对架构，`codesign -v` 核对 `arm64` 的 ad-hoc 签名，用不连接 Docker 的命令（`--help`）运行本机架构的二进制。
  - 先确认 `macos-latest` 的实际架构；能用 Rosetta 时再运行 `amd64`，不能则在 PR 描述写明 `amd64` 只验证了构建。
  - 若改动了行号，同步更新 `.github/zizmor.yml` 中按行号的豁免。
- [x] **4b.5 不做签名与公证（维护者已决定，2026-11-24）**：Developer ID 签名与公证需要付费 Apple Developer 账号，还会给发布任务增加长期密钥。产物只有 Go 链接器的 ad-hoc 签名，浏览器下载的文件会被 Gatekeeper 拦截；文档写明先用 `gh attestation verify` 校验（证明在 PR 5 提供），再由用户自行解除隔离。
- [x] **4b.6 文档**：补充 macOS 二进制的下载与运行说明，包括最低 macOS 版本（按 `.go-version` 对应的 Go 发行说明核对，跨次版本升级 Go 时复核），以及 Docker Desktop 的 socket 路径可能不是 `/var/run/docker.sock`、需设置 `DOCKER_HOST`（实测后再写）。
- [x] **4b.7 同步修订设计稿**：§5.1 第 4 条的目标平台改为 8 个归档（该处仍写"排除 `windows/arm`"，一并更正为同时排除 `windows/arm64`），§7 验证清单同步加入 macOS。

**PR 4b 验收**

```bash
go tool -modfile=tools/go.mod goreleaser check
go tool -modfile=tools/go.mod goreleaser release --snapshot --clean && bash scripts/check-release-contract.sh dist
bash scripts/run-govulncheck.sh          # 8 个目标全部通过
go tool -modfile=tools/go.mod actionlint && zizmor --min-severity low .github/
```
- `test (macos-latest)` **必须真实跑完并通过**才能合并，不能以"排队中"视为通过：这是唯一在 macOS 上运行产物的检查。
- 与 `v1.0.0` 相比，原有 6 个归档名不变，只新增 2 个 `darwin` 归档。

---

## PR 5：发布流水线

分支：`chore/auto-maint-5-release`。**已合并（#34，2026-11-25；原 #16 误合入堆叠分支后重提）**；本地彩排与 GitHub Actions 托管彩排（`dry_run=true`，run `45745244198`，2026-11-26）已通过，**首次正式发布（`v1.0.1`）待维护者确认后触发**。

- [x] **5.1 发布记录与中断恢复（需你确认，设计稿 §6 留给构建者）**：
  - **记录载体**：GitHub draft Release + `release-record.json` 资产，记录 `tag`、`sha`、各产物 sha256；所有产物与证明就绪后才把 draft 转为正式发布。`v1.0.0` 没有 draft 与记录，天然落在迁移边界之外。
  - **写入顺序（覆盖"创建 tag 后、写入恢复记录前中断"）**：在发布锁内按 ① 创建 draft Release 与记录（绑定 `next_tag`、`sha`）→ ② 创建**带标记的附注 tag**（注释形如 `watchtower-auto-release sha=<sha>`）并推送 → ③ 构建发布。由于记录先于 tag 写入，常规中断不会出现"有 tag 无记录"。若仍出现（如 ① 与 ② 之间的人为/平台异常），**附注 tag 自身的标记就是第二份记录**：`scheduled-release.yml` 的恢复模式条件 = `latest_tag` 的附注带该标记 **且** 不存在完整的正式 Release；此时按 tag 注释里的 `sha` 补建缺失的 draft/记录，再补齐缺失产物。**没有该标记的 tag（如 `v1.0.0` 与维护者手动 tag）一律不进入自动恢复。**
  - 在 `scheduled-release.yml` 的单元测试里加入中断点用例：仅有 draft 无 tag；有标记 tag 无 draft；有 tag 与部分资产；`v1.0.0` 型无标记 tag。
- [x] **5.2 发布锁只由一个工作流层持有，`release.yml` 保持可复用且自身不声明 `concurrency`**：
  - 可复用工作流若与调用方声明了同名 `concurrency` 组，会与调用方互相占锁而死锁/被取消。因此 `release-publish` 锁**只在入口工作流声明**：`scheduled-release.yml` 的整个运行，以及处理手动 tag 推送的薄入口（`release-tag.yml`，仅 `push` tag → 调用 `release.yml`）；二者共用同一个组名 `release-publish`、`cancel-in-progress: false`。被调用的 `release.yml`（`workflow_call`：`tag`、`sha`、`skip_ci`）与 `docker-publish.yml` 内**不写** `concurrency`。
  - 这一并发行为（同组只保留一个等待运行，较新的待定运行会顶掉较旧的）需用测试 tag 在彩排环境实测；若手动 tag 的待定运行可能被顶掉，则在 `release-tag.yml` 的验证步骤里检查"该 tag 是否已有完整发布"，并在 PR 描述写明处理方式。
  - **macOS 排队与锁的关系**：`ci.yml` 含继续阻塞的 macOS 测试，排队可能很久。若锁覆盖 `scheduled-release.yml` 的整个运行，等待 macOS 期间会一直占锁，也更容易顶掉待定的手动 tag 运行。实现时优先让锁只覆盖"打 tag 与发布"阶段，CI 在锁外先跑完，取锁后重新核对 `head_sha` 与 `next_tag`；上一条的"整个运行"以此处实测结论为准。
  - 相应地，设计稿 §5.2 中"`release.yml` 触发 tag push + workflow_call 且持锁"的描述需同步修订为"入口工作流持锁"。
  - [ ] **5.2 线上并发验收**：在托管环境实测同组 `release-publish` 的等待与顶替行为（待彩排/发布阶段验证）。
- [x] **5.2a `release.yml`**（改，可复用）：显式解析 `tag` 与 `sha`；`skip_ci=false` 时先调用 `ci.yml`；发布构建禁用 Go 缓存；GoReleaser 用 `.tools/goreleaser`（`release.draft: true`）；对归档与 `checksums.txt` 调 `actions/attest-build-provenance`；绝不覆盖已有 Release 资产。
- [x] **5.3 `docker-publish.yml` 补全**：版本 tag 存在性检查（已存在则比对 `(tag, sha, digest)` 记录，不一致即失败）；SBOM；仅在串行锁内核实该 `tag` 是 `main` 上最高正式版本、版本镜像与证明就绪后才推进 `:latest`；禁用 Buildx 缓存。
- [x] **5.4 `scheduled-release.yml`**：每周二 03:23 UTC + `workflow_dispatch`；判定、验证、打 tag、发布四段按设计稿 §5.2 实现；发布路径白名单判定（Go 源码、`go.mod`/`go.sum`、`tools/go.*`、`.go-version`、`dockerfiles/**`、`goreleaser.yml`、`.github/workflows/release.yml` 等，排除 `docs/**`、`*.md`、模板、CODEOWNERS）做成可测试的 Python 函数（`.github/scripts/release_paths.py` + 单元测试）。
- [x] **5.5 README** 增加 `gh attestation verify` 校验说明（二进制与镜像）。
- [ ] **5.6 彩排（必须）**：发布会推公开的 tag 与 `:latest`，一旦错了无法撤回（所有宿主机会自动拉取）。因此：
  - 先在**一个临时私有副本仓库**或用 `workflow_dispatch` 的 dry-run 开关（不创建 tag、不推 GHCR，只跑到 snapshot 与 `docker build`）走通；
  - 真实首次发布用 `workflow_dispatch` 手动触发，并在 `main-checks` 已启用的状态下进行；
  - 首次发布版本应是 `v1.0.1`（`v1.0.0` 之后的第一个补丁）。
  - **2026-11-26 状态**：本地 GoReleaser snapshot（8 归档）与 4 架构 Linux Docker（`--no-push`）已在 #34 验证通过；`main` 上的 `scheduled-release.yml` 托管彩排（`dry_run=true`，run `45745244198`）已通过（`dry-run-docker` 2m8s、`dry-run-snapshot` 5m37s，`plan` / `ci` / `publish` 按预期 `skipped`）。最新正式版本仍为 `v1.0.0`；OIDC 签名、GHCR 推送、`:latest` 推进与发布幂等性留待维护者确认后的首次正式发布（`v1.0.1`，`dry_run=false`）验证。

**PR 5 验收**

```bash
python -I -m unittest discover -s .github/scripts -v
.tools/actionlint && zizmor .github/
```
- 彩排发布：产物为 PR 4b 契约的 8 个归档加校验和文件（原有 6 个归档名与 v1.0.0 一致），且 2 个 `darwin` 归档同样有来源证明；`gh attestation verify` 通过；`:latest` 指向新 digest；重复触发同一 tag 不产生任何变更（幂等）。

---

## M3：端到端验收（设计稿 §8 第 7、8 项）

- [ ] 手动触发 `go-toolchain-sync.yml`：生成 `1.26 → 1.27` PR，提交显示 Verified，`automerge.yml` 输出 `eligible=false`，被 `main-review` 拦下；你 Approve 后合入。
  - **2026-11-26 状态**：近期 `push` 到 `main` 的运行成功（如 `45743498035`），手动 `workflow_dispatch` 尚未执行。另需注意 4.2 记录的已知配置限制：`golang.org/toolchain` 在 `index.golang.org` 的 10 次请求预算内未命中时脚本会 fail-closed 返回 `needs_sync=False`，不创建 PR。
- [ ] 观察首批 Dependabot PR：低风险补丁/次版本在 `Pull Request` + `CodeQL` 全绿且冷却期满足后由 `watchtower-automerge` 合入；GitHub Actions PR 与安全更新 PR 被拦；同时核对 `/tools` 的 `tool` 依赖是否被 Dependabot 识别、`pip` PR 是否保持 `--hash` 格式（2.6 的遗留验证）。
  - **2026-11-26 状态**：
    - 已核实 Dependabot 识别 `/tools` 依赖并开启多条升级 PR（#17–#26、#28–#33 等）。
    - #19、#24、#32、#33 均由维护者账号合并；近期 `Automerge` 运行（如 `45743494426`）中 `Verify automerge eligibility` 成功、`Merge verified pull request` 为 `skipped`。4.1a 的离线复核表明正文信号无法区分普通更新与安全更新，当前白名单为空，普通更新的机器人合并仍需取得与 PR head SHA 绑定的可信证据。
    - GitHub Actions PR、安全更新 PR 与 `pip` PR（`--hash` 格式）待后续观察。
    - **2026-11-26 版本更新开启后**：
      - 根目录作业记录了 `security-updates-only: false`，并生成分组 PR #36（`go-minor-patch`）。它的原始 head `2218e649` 由 Dependabot 签名，元数据列出 9 个模块，均属该组，update-type 均为 minor 或 patch。go.mod 中另有 12 个传递依赖连带升级，元数据未列出。
      - #36 删除了 11 个基线标为 `// indirect` 的模块，促成了 #35 中的间接依赖删除规则。#36 后来被维护者 force-push 为无签名提交 `6f87cb6` 再合并，所以签名样本应以 `2218e649` 为准。
      - `/tools` 作业因若干 `go-openapi/*` 包 `dependency_file_not_resolvable` 而失败，但仍生成分组 PR #37（178 项更新）。#37 的提交信息在 65536 字符处被截断，元数据只列出前 32 项，其中 1 项为 semver-major。它与 `main` 冲突，因此没有触发 CI。
      - 现有 10 个逐包 PR 的签名提交都不带 `dependency-group`。
      - 维护者确认 “Grouped security updates” 为关闭。
      - 据此实现候选信号 `dependabot-group-v1`，规则见设计稿 §3「拒绝安全更新」。离线回放中，#36 的签名数据命中该信号，#37 因元数据截断被拒。该信号是否加入 `CALIBRATED_NORMAL_UPDATE_SIGNALS`，由维护者单独决定。
- [ ] 手动触发 `scheduled-release.yml`：固定 SHA → CI → tag → GoReleaser v2 → GHCR + 证明全链路通过。
  - **2026-11-26 状态**：`main` 上的 `dry_run=true` 托管彩排（run `45745244198`）已通过；待维护者确认后触发正式发布 `v1.0.1`（`dry_run=false`）验证全链路。
- [ ] Actions 页确认 `go-toolchain-sync.yml` 与 `scheduled-release.yml` 为 `active`；把"60 天无活动会被停用"写进你的日历提醒（`scheduled-release` 本身不产生活动，合入频繁时自然续期，但无新提交时需要关注）。
  - **2026-11-26 已核实**：`gh workflow list --all` 确认 `Go Toolchain Sync` 与 `Scheduled release` 均为 `active`；日历提醒待维护者自行设置。
- [ ] 负面验收：用一个测试 PR（改一行文档）确认**非机器人 PR 不会被自动合并**；用无 admin 权限的身份开 PR，确认无法绕过 `main-review`。
  - **2026-11-26 已核实（前半项）**：PR #35（`b4b30c5`，仅改计划文档）在 `Pull Request`（run `45749026165`）与 `CodeQL`（run `45749025947`）全绿后触发 `Automerge`（run `45749585531`），`Verify automerge eligibility` 明确输出 `automerge verify: eligible=false: author Shouyupedia is not allowed`，`Merge verified pull request` 为 `skipped`，PR 保持 `OPEN`，随后由维护者手动合并（2026-11-26，合并提交 `ccd47f7`）。后半项（无 admin 权限身份无法绕过 `main-review`）待验收。

## 风险与回滚

| 风险 | 缓解 / 回滚 |
| --- | --- |
| Go 模块"首次公开可见时间"没有可行的可信来源（4.2） | 实测 `index.golang.org` 在 10 次请求预算下约命中 6/12 个 Go 模块且未命中 `golang.org/toolchain`；fail-closed 使未命中的依赖 PR 退化为人工审核、并阻止 `go-toolchain-sync.yml` 创建工具链 PR；其余生态不受影响；由你决定是否接受或另寻可信来源 |
| 必需检查名变动导致 PR 永远等不到检查 | 检查名在 M2 之后冻结；变更需同时改 Ruleset，属维护者操作 |
| `automerge.yml` 逻辑错误导致误合并 | PR 4 的拒绝用例全覆盖 + 离线回放；`main-checks` 零豁免兜底；紧急时在 `automerge` Environment 删除 App 私钥即停用 |
| 发布了错误的 `:latest` | 彩排；`:latest` 只在串行锁内、产物齐全后推进；`v1.0.0` 与已发版本不会被覆盖，可用同一流程发下一补丁修复 |
| Windows 本机 CRLF 干扰 `go mod tidy -diff` | `.gitattributes`（0.1 第 2 项）；以 Linux CI 结果为准 |
| macOS runner 排队，阻塞 PR 合并、`:latest-dev` 与正式发布（维护者已决定保留阻塞） | 接受延迟；排队超时或被取消时重跑该任务，不以"排队中"视为通过；定时发布失败即停在不发布一侧，可用 `workflow_dispatch` 重试。**已发生（2026-11-24，PR #11）**：macOS arm64 容量不足，任务未分到机器，15 分钟后被取消并使检查显示失败。无人值守的自动合并与定时发布会因此停住；PR 5 前需决定是否自动重跑，或改用其他 macOS runner 标签 |
| macOS 二进制未做 Developer ID 签名与公证，被 Gatekeeper 拦截 | 已接受（4b.5）；文档说明与来源证明校验 |
| 60 天无活动停用定时工作流 | M3 巡检提醒 |
| 有人打开仓库设置 “Grouped security updates”，安全更新可能带上组名（校验器读不到该设置） | 打开前先把 `dependabot-group-v1` 移出 `CALIBRATED_NORMAL_UPDATE_SIGNALS`；`dependabot.yml` 中出现 `applies-to` 或 `security-updates` 时，校验器自动拒绝 |
