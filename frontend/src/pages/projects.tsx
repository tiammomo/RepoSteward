import { useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useRead, type Project } from "../api/client";
import {
  Badge,
  Command,
  Detail,
  Empty,
  Notice,
  ReadState,
  SafeLink,
  when,
} from "../components";

export function ProjectPicker({
  projects,
  value,
  target = "",
}: {
  projects: Project[];
  value: string;
  target?: string;
}) {
  const navigate = useNavigate();
  return (
    <label className="field">
      当前项目
      <select
        aria-label="当前项目"
        value={value}
        onChange={(e) => navigate(`/projects/${e.target.value}${target}`)}
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.repository}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ProjectsPage() {
  const query = useRead("projects");
  const [search, setSearch] = useState("");
  return (
    <>
      <header className="page-heading">
        <h1>项目</h1>
        <p>已登记的仓库与本地工作区。</p>
      </header>
      <ReadState query={query} />
      <input
        type="search"
        aria-label="查找项目"
        placeholder="查找项目或仓库…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="table-wrap">
        <table className="projects-table">
          <thead>
            <tr>
              <th>项目</th>
              <th>用途</th>
              <th>工作区</th>
              <th>下一步</th>
            </tr>
          </thead>
          <tbody>
            {query.data?.projects
              .filter((p) =>
                `${p.name} ${p.repository}`
                  .toLowerCase()
                  .includes(search.toLowerCase()),
              )
              .map((p) => (
                <tr key={p.id}>
                  <td>
                    <Link to={`/projects/${p.id}`}>{p.name}</Link>
                    <small>{p.repository}</small>
                  </td>
                  <td>
                    <Badge value={p.policy.mode} />
                  </td>
                  <td>
                    {p.workspace_count} 个
                    {p.missing_workspaces > 0 && (
                      <small>{p.missing_workspaces} 个路径不可用</small>
                    )}
                  </td>
                  <td>
                    <Link to={`/projects/${p.id}`}>代码导览</Link> ·{" "}
                    <Link to={`/projects/${p.id}/tasks`}>开发任务</Link>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {query.data && !query.data.projects.length && (
        <Empty title="从一个项目开始">
          目前可通过终端关联已有目录。
          <Command value="reposteward project link /absolute/path/to/project" />
        </Empty>
      )}
      {!!query.data?.omitted && (
        <Notice>
          另有 {query.data.omitted} 个项目未展示，可通过 CLI 查询。
        </Notice>
      )}
    </>
  );
}

function Evidence({
  project,
  binding,
  evidence,
  line,
}: {
  project: string;
  binding: string;
  evidence: string;
  line: number;
}) {
  const [open, setOpen] = useState(false);
  const query = useRead(
    "code",
    {
      project_id: project,
      binding_id: binding,
      evidence_id: evidence,
      start_line: String(line),
    },
    open,
  );
  return (
    <>
      <button onClick={() => setOpen(!open)}>查看代码依据</button>
      {open && (
        <div className="evidence">
          <ReadState query={query} />
          {query.isFetching && <p role="status">正在核对代码…</p>}
          {query.data && !query.isFetching && (
            <>
              {query.data.status === "current_source" ? (
                <pre>{query.data.text}</pre>
              ) : (
                <Notice>代码依据已变化，请重新扫描后读取。</Notice>
              )}
              {query.data.truncated && (
                <p className="muted">当前只展示部分代码。</p>
              )}
              <Detail title="证据来源" value={query.data} />
            </>
          )}
        </div>
      )}
    </>
  );
}

export function ProjectPage() {
  const { projectId = "", workspaceId } = useParams();
  const projects = useRead("projects");
  const [search, setSearch] = useSearchParams();
  const [input, setInput] = useState(search.get("focus") || "");
  const [client, setClient] = useState("codex");
  const navigate = useNavigate();
  const project = projects.data?.projects.find((p) => p.id === projectId);
  const workspaces = project?.workspaces || [];
  const binding = workspaceId
    ? workspaces.find((w) => w.id === workspaceId)
    : workspaces[0];
  const query = useRead(
    "workspace",
    {
      project_id: projectId,
      binding_id: binding?.id || "",
      focus: search.get("focus") || "",
    },
    Boolean(binding),
  );
  const result = query.data;
  return (
    <>
      <header className="page-heading">
        <h1>{project?.name || "项目空间"}</h1>
        <p>{project?.repository || "工作区、代码导览与开发接续。"}</p>
      </header>
      <ReadState query={projects} />
      {projects.data && (
        <ProjectPicker projects={projects.data.projects} value={projectId} />
      )}
      <div className="tabs">
        <Link aria-current="page" to={`/projects/${projectId}`}>
          工作区与代码导览
        </Link>
        <Link to={`/projects/${projectId}/tasks`}>开发任务</Link>
      </div>
      {project && !binding && (
        <Empty title="尚未关联工作区">
          使用 project link 关联本地目录后，即可阅读代码导览。
        </Empty>
      )}
      {binding && (
        <>
          <div className="toolbar">
            <label className="field">
              本地工作区
              <select
                aria-label="本地工作区"
                value={binding.id}
                onChange={(e) =>
                  navigate(
                    `/projects/${projectId}/workspaces/${e.target.value}`,
                  )
                }
              >
                {workspaces.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.root}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <form
            className="toolbar"
            onSubmit={(e) => {
              e.preventDefault();
              setSearch(input ? { focus: input } : {});
            }}
          >
            <input
              aria-label="导览关注点"
              placeholder="关注哪个模块或问题？"
              maxLength={2000}
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <button type="submit">查阅导览</button>
          </form>
          <ReadState query={query} />
          {result && (
            <>
              <section className="panel">
                <div className="row">
                  <Badge
                    value={result.guide.status}
                    tone={result.guide.status === "current" ? "good" : "warn"}
                  />
                  <Badge value={result.policy.mode} />
                  <span>
                    {result.workspace.branch || "游离 HEAD"} ·{" "}
                    {result.workspace.head.slice(0, 10)}
                    {result.workspace.dirty && " · 有本地修改"}
                  </span>
                </div>
                <p className="muted">
                  导览来源 · {when(result.guide.scanned_at)}
                </p>
                {result.guide.status !== "current" && (
                  <>
                    <Notice>
                      代码导览尚未就绪或已过期。请显式扫描后重新读取。
                    </Notice>
                    <Command value={result.commands.scan} />
                  </>
                )}
              </section>
              <section className="panel">
                <h2>建议阅读路线</h2>
                <p className="muted">
                  根据静态事实与仓库声明组织，每一步均可查看代码依据。
                </p>
                {(result.guide.reading_path || []).map((step) => (
                  <article
                    className="reading-step"
                    key={step.source.evidence_id}
                  >
                    <span className="step-number">{step.step}</span>
                    <div>
                      <h3>{step.path}</h3>
                      <p>{step.summary || step.reason}</p>
                      <p className="muted">
                        {step.reason} · {step.language}
                      </p>
                      <div className="actions">
                        <Evidence
                          project={projectId}
                          binding={binding.id}
                          evidence={step.source.evidence_id}
                          line={step.source.line}
                        />
                        {step.source.url && (
                          <SafeLink href={step.source.url}>
                            在 GitHub 查看
                          </SafeLink>
                        )}
                      </div>
                    </div>
                  </article>
                ))}
                {!result.guide.reading_path?.length && (
                  <p className="muted">当前范围内没有可用的阅读路线。</p>
                )}
                <Detail title="入口、关联模块与覆盖说明" value={result.guide} />
              </section>
              <section className="panel">
                <h2>在目标项目继续</h2>
                <label className="field">
                  Coding Agent 客户端
                  <select
                    aria-label="Coding Agent 客户端"
                    value={client}
                    onChange={(e) => setClient(e.target.value)}
                  >
                    <option value="codex">Codex</option>
                    <option value="claude-code">Claude Code</option>
                    <option value="copilot-vscode">Copilot · VS Code</option>
                  </select>
                </label>
                <Command value={result.commands.mcp_clients[client] || ""} />
                <p className="muted">
                  复制的是 POSIX 终端配置预览命令。实际会话由你使用的 Agent
                  提供。
                </p>
              </section>
              {!!project?.workspace_check_omitted && (
                <Notice>
                  另有 {project.workspace_check_omitted} 个工作区未检查。
                </Notice>
              )}
            </>
          )}
        </>
      )}
      {projects.data && !project && (
        <Empty title="项目不在当前列表中">
          请重新读取，或使用 CLI 检查登记信息。
        </Empty>
      )}
    </>
  );
}
