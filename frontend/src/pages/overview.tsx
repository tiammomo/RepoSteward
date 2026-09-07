import { Link } from "react-router-dom";
import { useRead } from "../api/client";
import {
  Badge,
  Command,
  Copy,
  Detail,
  Empty,
  Notice,
  ReadState,
  when,
} from "../components";

export function OverviewPage() {
  const query = useRead("overview");
  const result = query.data;
  return (
    <>
      <header className="page-heading">
        <h1>维护总览</h1>
        <p>查看需要关注的事项，再进入项目继续处理。</p>
      </header>
      <ReadState query={query} />
      {result && (
        <>
          <div className="stats">
            <div>
              <strong>{result.projects.length}</strong>
              <span>当前范围内项目</span>
            </div>
            <div>
              <strong>
                {result.projects.reduce((n, p) => n + p.items.length, 0)}
              </strong>
              <span>本页关注项</span>
            </div>
            <div>
              <strong>{result.complete ? "范围内完整" : "部分数据"}</strong>
              <span>覆盖不代表已实时同步</span>
            </div>
          </div>
          {!result.projects.length && (
            <Empty title="从一个项目开始">
              关联本地目录并配置仓库策略后，它会出现在维护总览。
              <Command value="reposteward project link /absolute/path/to/project" />
            </Empty>
          )}
          {result.projects.map((project) => (
            <section className="panel" key={project.project_id}>
              <header className="section-heading">
                <h2>
                  <Link to={`/projects/${project.project_id}`}>
                    {project.repository}
                  </Link>
                </h2>
                <Badge value={project.sources.status} />
              </header>
              <p className="muted">
                最近 GitHub 缓存 · {when(project.sources.fetched_at)}
              </p>
              {project.items.map((item) => (
                <article className="attention" key={item.id}>
                  <div>
                    <div className="row">
                      <Badge
                        value={item.priority >= 90 ? "需要介入" : "待核对"}
                        tone={item.priority >= 90 ? "warn" : ""}
                      />
                      <span className="muted">
                        {item.pull_number
                          ? `PR #${item.pull_number}`
                          : item.issue_number
                            ? `Issue #${item.issue_number}`
                            : "本地记录"}
                      </span>
                    </div>
                    <p>{item.summary}</p>
                    <small>{when(item.source_updated_at)}</small>
                  </div>
                  <div className="actions">
                    {item.run_id && (
                      <Link
                        to={`/projects/${project.project_id}/tasks/${item.run_id}`}
                      >
                        查看任务
                      </Link>
                    )}
                    {item.next_command && (
                      <Copy label="复制下一步" value={item.next_command} />
                    )}
                  </div>
                </article>
              ))}
              {!project.items.length && (
                <p className="muted">当前范围内没有需要介入的事项。</p>
              )}
              {project.sources.error && (
                <Notice>{project.sources.error}</Notice>
              )}
              {project.omitted > 0 && (
                <Notice>另有 {project.omitted} 项未展示。</Notice>
              )}
            </section>
          ))}
          {!!result.omitted_projects && (
            <Notice>另有 {result.omitted_projects} 个项目未展示。</Notice>
          )}
          {!!result.excluded_projects && (
            <Notice>
              {result.excluded_projects}{" "}
              个项目因策略、来源或工作区不可用未进入维护总览。
            </Notice>
          )}
          <p className="muted">
            “重新读取”只读取本地数据。更新 GitHub 缓存请执行：
          </p>
          <Command value="reposteward overview refresh" />
          <Detail title="来源与覆盖信息" value={result} />
        </>
      )}
    </>
  );
}
