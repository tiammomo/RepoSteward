import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import {
  QueryClient,
  QueryClientProvider,
  useQueryClient,
} from "@tanstack/react-query";
import { connectSession, useRead } from "./api/client";
import { Badge, Command, Empty, Notice } from "./components";
import { OverviewPage } from "./pages/overview";
import { ProjectPage, ProjectsPage } from "./pages/projects";
import { TasksPage } from "./pages/tasks";
import { SettingsPage } from "./pages/settings";
import "./style.css";

class PageBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <Notice>页面暂不可用，请重新读取或查看设置诊断。</Notice>
    ) : (
      this.props.children
    );
  }
}

function App({ connected }: { connected: boolean }) {
  const cache = useQueryClient();
  const location = useLocation();
  const session = useRead("session", {}, connected);
  const projects = useRead("projects", {}, connected);
  return (
    <div className="shell">
      <a className="skip" href="#content">
        跳到主要内容
      </a>
      <aside>
        <Link to="/" className="brand">
          <span>R</span>
          <div>
            RepoSteward<small>本地维护工作台</small>
          </div>
        </Link>
        <nav aria-label="主导航">
          <NavLink to="/" end>
            维护总览
          </NavLink>
          <NavLink to="/projects">项目空间</NavLink>
          <NavLink to="/tasks">开发任务</NavLink>
          <NavLink to="/settings">设置与诊断</NavLink>
        </nav>
        <div className="sidebar-projects">
          <p>已登记项目</p>
          {projects.data?.projects.slice(0, 8).map((p) => (
            <Link to={`/projects/${p.id}`} key={p.id}>
              {p.name}
              <small>{p.repository}</small>
            </Link>
          ))}
        </div>
        <p className="sidebar-note">
          项目事实与开发上下文
          <br />
          保存在你的本机。
        </p>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            工作空间 <span className="muted">/</span>{" "}
            {location.pathname.startsWith("/projects")
              ? "项目"
              : location.pathname.startsWith("/settings")
                ? "设置"
                : "维护"}
          </span>
          <div className="row">
            <Badge
              value={
                session.error
                  ? "会话未连接"
                  : connected
                    ? "本地已连接"
                    : "等待连接"
              }
              tone={session.error ? "warn" : "good"}
            />
            <button onClick={() => cache.invalidateQueries()}>重新读取</button>
          </div>
        </header>
        <main id="content" tabIndex={-1}>
          {!connected ? (
            <Empty title="连接你的本地工作空间">
              请使用终端打印的完整会话链接打开。
              <Command value="reposteward web" />
            </Empty>
          ) : session.error ? (
            <Notice>
              {session.error.message}
              <Command value="reposteward web" />
            </Notice>
          ) : (
            <PageBoundary key={location.pathname}>
              <Routes>
                <Route path="/" element={<OverviewPage />} />
                <Route path="/projects" element={<ProjectsPage />} />
                <Route path="/projects/:projectId" element={<ProjectPage />} />
                <Route
                  path="/projects/:projectId/workspaces/:workspaceId"
                  element={<ProjectPage />}
                />
                <Route
                  path="/projects/:projectId/tasks"
                  element={<TasksPage />}
                />
                <Route
                  path="/projects/:projectId/tasks/:runId"
                  element={<TasksPage />}
                />
                <Route
                  path="/projects/:projectId/tasks/:runId/review"
                  element={<TasksPage review />}
                />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/review" element={<TasksPage review />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route
                  path="*"
                  element={
                    <Empty title="没有这个页面">
                      <Link to="/">返回总览</Link>
                    </Empty>
                  }
                />
              </Routes>
            </PageBoundary>
          )}
        </main>
      </div>
    </div>
  );
}

const queryClient = new QueryClient();
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <BrowserRouter>
      <App connected={connectSession()} />
    </BrowserRouter>
  </QueryClientProvider>,
);
