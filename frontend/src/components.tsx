import { useState, type ReactNode } from "react";

const names: Record<string, string> = {
  current: "当前有效",
  stale: "已过期",
  not_scanned: "尚未扫描",
  missing: "尚未创建",
  compatible: "兼容",
  migration_required: "需要升级",
  newer_than_supported: "安装版本过旧",
  unavailable: "暂不可用",
  snapshot_required: "等待稳定快照",
  not_checked: "未核对当前适用性",
  matches: "适用于当前快照",
  not_verified: "当前未验证",
  passed: "历史验证通过",
  failed: "失败",
  running: "进行中",
  ready: "准备完成",
  submitted: "已提交",
  merged: "已合并",
  blocked: "有阻塞",
  complete: "完成",
  cached: "GitHub 缓存",
  not_refreshed: "尚未同步",
  refresh_failed: "同步失败",
  maintainer: "维护",
  contributor: "贡献",
  unconfigured: "待配置",
  unknown: "未知",
};
export const statusName = (value: string) => names[value] || value || "未知";
export const obj = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
export const arr = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];
export const str = (value: unknown): string =>
  typeof value === "string"
    ? value
    : value == null
      ? ""
      : JSON.stringify(value);
export const when = (value: unknown) =>
  value && Number.isFinite(new Date(str(value)).getTime())
    ? new Date(str(value)).toLocaleString("zh-CN", { hour12: false })
    : "尚无时间记录";

export function Badge({ value, tone = "" }: { value: string; tone?: string }) {
  return <span className={"badge " + tone}>{statusName(value)}</span>;
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      <div>{children}</div>
    </div>
  );
}
export function Notice({ children }: { children: ReactNode }) {
  return <div className="notice">{children}</div>;
}
export function Detail({ title, value }: { title: string; value: unknown }) {
  return (
    <details>
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
export function List({
  items,
  empty = "尚无记录",
}: {
  items: unknown;
  empty?: string;
}) {
  return arr(items).length ? (
    <ul>
      {arr(items).map((item, i) => (
        <li key={i}>{str(item)}</li>
      ))}
    </ul>
  ) : (
    <p className="muted">{empty}</p>
  );
}
export function Copy({
  value,
  label = "复制",
}: {
  value: string;
  label?: string;
}) {
  const [result, setResult] = useState("");
  return (
    <span className="copy">
      <button
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setResult("已复制");
          } catch {
            setResult("请手动选择文本复制");
          }
        }}
      >
        {label}
      </button>
      <span role="status">{result}</span>
    </span>
  );
}
export function Command({ value }: { value: string }) {
  return (
    <div className="command">
      <code>{value}</code>
      <Copy value={value} />
    </div>
  );
}
export function SafeLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  try {
    const url = new URL(href);
    if (url.protocol === "https:" && !url.username && !url.password)
      return (
        <a href={url.href} target="_blank" rel="noopener noreferrer">
          {children} ↗
        </a>
      );
  } catch {
    /* inert remote input */
  }
  return <span>{children}</span>;
}
export function ReadState({
  query,
}: {
  query: { isPending: boolean; error: Error | null };
}) {
  if (query.error)
    return (
      <div className="notice error" role="alert">
        {query.error.message}
      </div>
    );
  return query.isPending ? (
    <p role="status" className="muted">
      正在读取本地数据…
    </p>
  ) : null;
}
export function Pairs({ values }: { values: [string, unknown][] }) {
  return (
    <dl>
      {values.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{str(value) || "未知"}</dd>
        </div>
      ))}
    </dl>
  );
}
