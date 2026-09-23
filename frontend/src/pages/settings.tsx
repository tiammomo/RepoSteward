import { useRead } from "../api/client";
import {
  Badge,
  Command,
  Detail,
  List,
  Pairs,
  ReadState,
  obj,
  str,
} from "../components";

export function SettingsPage() {
  const query = useRead("settings");
  const result = query.data;
  return (
    <>
      <header className="page-heading">
        <h1>设置与诊断</h1>
        <p>核对安装、配置来源和本地台账。</p>
      </header>
      <ReadState query={query} />
      {result && (
        <>
          <section className="panel">
            <h2>当前安装</h2>
            <Pairs
              values={[
                ["包版本", result.installation.version],
                ["Python", result.installation.python],
                ["代码位置", result.installation.module_path],
                [
                  "来源提交",
                  result.installation.source_revision || "请核对安装记录",
                ],
              ]}
            />
          </section>
          <section className="panel">
            <h2>本地台账</h2>
            {Object.entries(result.databases).map(([key, value]) => {
              const database = obj(value);
              return (
                <article className="attention" key={key}>
                  <div>
                    <h3>{key === "tasks" ? "任务数据库" : "项目注册表"}</h3>
                    <p className="muted">{str(database.path)}</p>
                    <small>
                      当前 {str(database.schema) || "未知"} / 支持{" "}
                      {str(database.supported_schema)}
                    </small>
                  </div>
                  <Badge value={str(database.status)} />
                </article>
              );
            })}
          </section>
          <section className="panel">
            <h2>生效配置与来源</h2>
            <Detail title="查看配置来源" value={result.configuration} />
          </section>
          <section className="panel">
            <h2>诊断建议</h2>
            <List items={result.next_actions} />
          </section>
          <Command value="reposteward doctor --local" />
          <p className="muted">
            配置改变后需重启工作台。本机会话最长有效 12 小时。
          </p>
        </>
      )}
    </>
  );
}
