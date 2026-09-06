"use strict";

(() => {
  const content = document.getElementById("content");
  const connection = document.getElementById("connection");
  const labels = {today: "今日待处理", projects: "项目空间", tasks: "任务接续", review: "审阅依据", settings: "设置与诊断"};
  const statusNames = {current: "当前有效", stale: "已过期", not_scanned: "尚未扫描", compatible: "兼容", missing: "尚未创建", migration_required: "需要升级", newer_than_supported: "安装版本过旧", snapshot_required: "等待稳定快照", unavailable: "暂不可用", not_checked: "未核对当前适用性", matches: "匹配当前快照", not_verified: "当前未验证", passed: "历史验证通过", failed: "验证失败", running: "进行中", ready: "准备完成", submitted: "已提交", merged: "已合入", blocked: "有阻塞", complete: "完成", cached: "线上缓存", not_refreshed: "未刷新线上状态", refresh_failed: "线上刷新失败", maintainer: "维护者", contributor: "贡献者", unconfigured: "未配置策略", unknown: "未知"};
  const storageKey = "reposteward-local-session";
  let token = "";
  try { token = sessionStorage.getItem(storageKey) || ""; } catch { /* Storage can be disabled. */ }
  if (location.hash.startsWith("#session=")) {
    const candidate = location.hash.slice(9);
    if (/^[A-Za-z0-9_-]{43}$/.test(candidate)) {
      token = candidate;
      try { sessionStorage.setItem(storageKey, token); } catch { /* Keep it only in memory. */ }
    }
    history.replaceState(null, "", location.pathname + "#today");
  }
  let controller = new AbortController();
  let generation = 0;
  let projectCache = [];
  let selectedProject = "";
  let noticeTimer;

  function node(tag, className, ...children) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    for (const child of children.flat(Infinity)) {
      if (child !== null && child !== undefined) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  }
  const values = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === "string" ? value : JSON.stringify(value ?? "");
  const name = value => statusNames[value] || value || "未知";
  function badge(value, tone = "") { return node("span", "badge " + tone, name(value)); }
  function button(label, action, primary = false) {
    const b = node("button", "button" + (primary ? " primary" : ""), label);
    b.type = "button"; b.addEventListener("click", action); return b;
  }
  function notify(message) {
    const box = document.getElementById("notice");
    clearTimeout(noticeTimer); box.textContent = message; box.hidden = false;
    noticeTimer = setTimeout(() => { box.hidden = true; }, 3000);
  }
  async function copy(value) {
    try { await navigator.clipboard.writeText(value); notify("已复制，可在目标项目中继续。"); }
    catch { notify("浏览器未允许复制，请选择下方文本手动复制。"); }
  }
  function command(value) {
    return node("div", "command", node("code", "", value), button("复制", () => copy(value)));
  }
  function details(title, value) {
    return node("details", "", node("summary", "", title), node("pre", "", JSON.stringify(value, null, 2)));
  }
  function list(items, empty = "尚无记录") {
    return values(items).length ? node("ul", "plain", values(items).map(item => node("li", "", text(item)))) : node("p", "muted", empty);
  }
  function date(value) {
    const d = new Date(value);
    return value && Number.isFinite(d.getTime()) ? d.toLocaleString("zh-CN", {hour12: false}) : "时间未知";
  }
  function pairs(entries) {
    return node("dl", "key-values", entries.flatMap(([key, value]) => [node("dt", "", key), node("dd", "", value ?? "未知")]));
  }
  function link(label, raw) {
    try {
      const url = new URL(raw);
      if (url.protocol !== "https:" || url.username || url.password) return node("span", "meta", label);
      const a = node("a", "", label); a.href = url.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
    } catch { return node("span", "meta", label); }
  }
  function hero(title, subtitle, eyebrow = "LOCAL WORKSPACE") {
    return node("section", "hero", node("div", "eyebrow", eyebrow), node("h1", "", title), node("p", "", subtitle));
  }
  function errorBox(error) { return node("div", "callout error", error.message || "读取失败，请重试。"); }
  function empty(title, message) { return node("div", "empty", node("strong", "", title), message); }
  function go(view, project = "", item = "") {
    location.hash = [view, project, item].filter(Boolean).map(encodeURIComponent).join("/");
  }
  async function api(route, params = {}) {
    const response = await fetch("/api/" + route + (Object.keys(params).length ? "?" + new URLSearchParams(params) : ""), {
      headers: {Authorization: "Bearer " + token}, signal: controller.signal, cache: "no-store", credentials: "omit"
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) { connection.textContent = "会话未连接"; connection.className = "badge warn"; }
      throw new Error(data.error?.message || "读取失败，请重试。");
    }
    return data;
  }
  function omitted(count, unit = "项") { return count ? node("p", "callout", `另有 ${count} ${unit}未显示；可使用 CLI 查询完整范围。`) : document.createDocumentFragment(); }
  function section(title, detail = "") { return node("div", "section-head", node("h2", "", title), node("span", "meta", detail)); }

  async function today(host) {
    host.append(hero("今天，先处理重要的事。", "集中查看需要你介入的事项，再回到目标项目继续工作。"));
    let result;
    try { result = await api("overview"); } catch (error) { host.append(errorBox(error)); return; }
    const projects = values(result.projects);
    const items = projects.flatMap(p => values(p.items));
    const unknown = projects.filter(p => !p.complete).length;
    const stat = (label, number, note) => node("div", "stat", node("div", "stat-label", label), node("div", "stat-number", number), node("div", "stat-note", note));
    host.append(node("div", "stats", stat("已关联项目", projectCache.length, "从项目空间进入工作区"), stat("本页待处理", items.length, "按维护者介入优先级排列"), stat("数据覆盖", result.complete ? "完整" : "部分", unknown ? `${unknown} 个项目有未知或省略` : "依据本地台账与线上缓存")));
    host.append(section("需要关注", `本次读取 · ${date(result.observed_at)}`));
    if (!projects.length) host.append(empty("从一个项目开始", "在终端关联项目并配置仓库策略后，它会出现在这里。"), command("reposteward project link /absolute/path/to/project"));
    for (const project of projects) {
      const source = project.sources || {};
      const panel = node("section", "panel", node("div", "panel-title", button(project.repository, () => go("projects", project.project_id)), node("div", "row", badge(source.status, source.status === "cached" ? "" : "warn"), node("span", "meta", date(source.fetched_at)))));
      if (!values(project.items).length) panel.append(node("div", "panel-pad muted", "当前范围内没有需要介入的事项。"));
      for (const item of values(project.items)) {
        const actions = node("div", "actions");
        if (item.run_id) actions.append(button("查看任务", () => go("tasks", project.project_id, item.run_id)));
        if (item.next_command) actions.append(button("复制下一步", () => copy(item.next_command)));
        panel.append(node("div", "item", node("div", "", node("div", "row", badge(item.priority >= 90 ? "需要介入" : "待核对", item.priority >= 90 ? "warn" : ""), node("span", "meta", item.pull_number ? `PR #${item.pull_number}` : "本地任务")), node("div", "item-title", item.summary), node("div", "meta", "来源更新 · " + date(item.source_updated_at))), actions));
      }
      if (source.error) panel.append(node("div", "panel-pad", errorBox(new Error(source.error))));
      if (project.omitted) panel.append(node("div", "panel-pad", omitted(project.omitted)));
      host.append(panel);
    }
    host.append(omitted(result.omitted_projects, "个项目"));
    if (result.excluded_projects) host.append(node("p", "meta", `${result.excluded_projects} 个项目因策略、来源或工作区不可用未进入待办；请到项目空间检查。`));
    host.append(details("本页来源与覆盖信息", result), node("p", "meta", "“重新读取”只读取本地数据。更新 GitHub 缓存请在终端执行："), command("reposteward overview refresh"));
  }

  function projectSelector(host, projectId, onChange) {
    const select = node("select", ""); select.id = "project-select";
    for (const project of projectCache) {
      const option = node("option", "", project.repository); option.value = project.id; select.append(option);
    }
    select.value = projectId;
    select.addEventListener("change", () => { selectedProject = select.value; onChange(select.value); });
    host.append(node("div", "toolbar", node("label", "", "当前项目"), select));
    select.setAttribute("aria-label", "当前项目");
  }
  async function projects(host, projectId, bindingId) {
    host.append(hero("每个项目，都有清晰的入口。", "从工作区、代码导览和阅读路线，快速找回项目的结构。", "PROJECT SPACES"));
    if (!projectId) {
      const grid = node("div", "grid");
      const input = node("input", ""); input.type = "search"; input.placeholder = "查找项目或仓库…"; input.setAttribute("aria-label", "查找项目");
      const draw = () => {
        grid.replaceChildren();
        const matched = projectCache.filter(p => (p.repository + " " + p.name).toLowerCase().includes(input.value.toLowerCase()));
        for (const project of matched) grid.append(node("article", "project-card", node("div", "row", node("span", "project-symbol", "⌘"), badge(project.policy.mode), !project.policy.enabled ? badge("策略未启用", "warn") : null), node("h2", "", project.name), node("p", "meta", project.repository), node("p", "meta", `${project.workspace_count} 个工作区 · ${project.missing_workspaces} 个路径不可用`), node("div", "actions", button("进入项目", () => go("projects", project.id), true), button("查看任务", () => go("tasks", project.id)))));
        if (!matched.length) grid.append(empty("没有匹配项目", "可调整查找条件，或使用 project link 关联本地工作区。"));
      };
      input.addEventListener("input", draw); host.append(node("div", "toolbar", input), grid); draw(); return;
    }
    selectedProject = projectId;
    projectSelector(host, projectId, id => go("projects", id));
    const project = projectCache.find(p => p.id === projectId);
    if (!project) { host.append(empty("项目不在当前列表中", "请重新读取项目列表，或从 CLI 检查登记信息。")); return; }
    const workspaces = values(project.workspaces);
    if (!workspaces.length) { host.append(empty("尚无有效关联", "使用 project link 关联这个项目的工作区。")); return; }
    const selected = workspaces.find(w => w.id === bindingId) || workspaces[0];
    const select = node("select", ""); select.setAttribute("aria-label", "本地工作区");
    for (const workspace of workspaces) { const option = node("option", "", workspace.root); option.value = workspace.id; select.append(option); }
    select.value = selected.id; select.addEventListener("change", () => go("projects", projectId, select.value));
    const input = node("input", ""); input.type = "search"; input.placeholder = "关注哪个模块或问题？"; input.maxLength = 2000; input.setAttribute("aria-label", "导览关注点");
    const detail = node("div", "");
    let guideGeneration = 0;
    async function load() {
      const currentGuide = ++guideGeneration;
      detail.replaceChildren(empty("读取导览中", "正在核对工作区与代码来源…"));
      try {
        const result = await api("workspace", {project_id: projectId, binding_id: selected.id, focus: input.value});
        if (currentGuide !== guideGeneration) return;
        const guide = result.guide;
        detail.replaceChildren(node("div", "row", badge(guide.status, guide.status === "current" ? "good" : "warn"), badge(result.policy.mode), node("span", "meta", `分支 ${result.workspace.branch || "游离 HEAD"} · ${result.workspace.head.slice(0, 10)}${result.workspace.dirty ? " · 有本地修改" : ""}`)));
        detail.append(node("p", "meta", "导览来源 · " + date(guide.scanned_at)));
        if (guide.status !== "current") detail.append(node("div", "callout", "代码导览尚未就绪或已过期。请显式扫描后重新读取。"), command(result.commands.scan));
        const route = node("section", "panel panel-pad", section("建议阅读路线", "基于静态事实与仓库声明"));
        for (const step of values(guide.reading_path)) {
          const evidence = node("div", "");
          const actions = node("div", "actions", button("查看代码依据", async () => {
            evidence.replaceChildren(node("p", "meta", "正在读取证据…"));
            try {
              const r = await api("code", {project_id: projectId, binding_id: selected.id, evidence_id: step.source.evidence_id, start_line: String(step.source.line)});
              evidence.replaceChildren(r.status === "current_source" ? node("pre", "", r.text) : node("div", "callout", "代码依据已变化，请重新扫描后读取。"), r.truncated ? node("p", "meta", "当前展示部分代码；可在源文件中继续阅读。") : document.createDocumentFragment(), details("证据来源", r));
            }
            catch (error) { evidence.replaceChildren(errorBox(error)); }
          }));
          if (step.source.url) actions.append(link("在 GitHub 查看", step.source.url));
          route.append(node("div", "step", node("span", "step-number", step.step), node("div", "step-content", node("h3", "", step.path), node("p", "", step.summary || step.reason), node("div", "row", badge(step.language), node("span", "meta", step.reason)), actions, evidence)));
        }
        if (values(guide.reading_path).length) detail.append(route);
        const clientSelect = node("select", ""); clientSelect.setAttribute("aria-label", "Coding Agent 客户端");
        const clientNames = {codex: "Codex", "claude-code": "Claude Code", "copilot-vscode": "Copilot · VS Code"};
        for (const client of Object.keys(result.commands.mcp_clients)) { const option = node("option", "", clientNames[client] || client); option.value = client; clientSelect.append(option); }
        const preview = node("div", "", command(result.commands.mcp_clients[clientSelect.value]));
        clientSelect.addEventListener("change", () => preview.replaceChildren(command(result.commands.mcp_clients[clientSelect.value])));
        detail.append(details("入口、关联模块与覆盖说明", guide), section("在目标项目继续"), clientSelect, preview, node("p", "meta", "复制的是 POSIX 终端命令，保留当前 RepoSteward 配置来源。它预览客户端配置；实际会话由你使用的 Agent 提供。"));
      } catch (error) { if (error.name !== "AbortError" && currentGuide === guideGeneration) detail.replaceChildren(errorBox(error)); }
    }
    input.addEventListener("keydown", event => { if (event.key === "Enter") load(); });
    host.append(node("div", "toolbar", node("label", "", "本地工作区"), select), node("div", "toolbar", input, button("查阅导览", load)), detail);
    host.append(omitted(project.workspace_check_omitted, "个工作区")); await load();
  }

  async function taskPage(host, view, projectId, runId) {
    host.append(hero(view === "review" ? "让交付依据，可以核对。" : "从上次停下的地方继续。", view === "review" ? "查看历史验证、当前适用性和审阅轨迹。这里的历史判定不授予交付权限。" : "把目标、约束、决定与未完成工作带回你的 Coding Agent。", view === "review" ? "REVIEW EVIDENCE" : "TASK CONTINUITY"));
    if (!projectCache.length) { host.append(empty("还没有项目", "先关联一个本地项目，再查看任务与检查点。")); return; }
    projectId = projectId || selectedProject || projectCache[0].id; selectedProject = projectId;
    projectSelector(host, projectId, id => go(view, id));
    const project = projectCache.find(p => p.id === projectId);
    if (!project?.policy.task_access) { host.append(empty("项目策略未启用", "关联工作区后，还需要在 CLI 中配置仓库角色与贡献策略。")); return; }
    if (!runId) {
      const result = await api("tasks", {project_id: projectId});
      host.append(section("最近任务尝试", "每次尝试保留自己的记录"));
      if (!values(result.tasks).length) host.append(empty("这里还没有任务", "从已审阅 Issue 开始一次任务后，目标、检查点和证据会出现在这里。"));
      const panel = node("section", "panel");
      for (const task of values(result.tasks)) {
        const title = button(task.title || `Issue #${task.issue_number}`, () => go(view, projectId, task.id)); title.className = "task-button";
        panel.append(node("div", "item", node("div", "", node("div", "row", badge(task.status), node("span", "meta", `#${task.issue_number} · ${task.stage === "external" ? "外部 Agent" : "仓库维护"}`)), node("div", "item-title", title), node("div", "meta", date(task.updated_at))), button("查看" + (view === "review" ? "依据" : "上下文"), () => go(view, projectId, task.id))));
      }
      if (result.tasks.length) host.append(panel); host.append(omitted(result.omitted)); return;
    }
    host.append(node("div", "actions", button("返回任务列表", () => go(view, projectId)), button(view === "review" ? "任务上下文" : "审阅依据", () => go(view === "review" ? "tasks" : "review", projectId, runId))), node("p", "meta", "任务 · " + runId));
    if (view === "review") {
      const result = await api("review", {project_id: projectId, run_id: runId});
      const trace = result.trace;
      host.append(node("div", "callout", "以下为已有证据与审计记录。历史验证通过不等于当前可以提交或合并。"));
      host.append(node("section", "panel panel-pad", section("Issue 最新记录摘要", `#${trace.issue_number} · 包含该 Issue 的多次尝试`), pairs([["后续动作", trace.next_action], ["记录状态", name(trace.current?.status)], ["合并结果", name(trace.current?.merge_outcome)], ["证据 HEAD", trace.current?.head_sha || "未知"], ["覆盖", trace.complete ? "范围内完整" : "有省略或未知"]])));
      if (result.verification) for (const evidence of values(result.verification.evidence)) host.append(node("section", "panel panel-pad", node("div", "row", badge(evidence.outcome, evidence.outcome === "failed" ? "bad" : ""), badge(evidence.current_applicability, evidence.current_applicability === "matches" ? "good" : "warn")), node("p", "meta", `${evidence.profile} · ${date(evidence.updated_at)}`), list(evidence.validity, "未观察到已检查项的差异。"), details("验证证据", evidence)));
      host.append(section("任务轨迹", `${trace.stats.events} 条已展示事件`));
      const panel = node("section", "panel");
      for (const event of values(trace.events)) panel.append(node("div", "item", node("div", "", node("div", "row", badge(event.source || event.kind), node("span", "meta", date(event.occurred_at))), node("div", "item-title", event.summary || event.event || event.kind || event.id), details("事件依据", event))));
      host.append(panel, omitted(trace.stats.events_omitted), details("完整范围与来源", result)); return;
    }
    const result = await api("task", {project_id: projectId, run_id: runId});
    const c = result.context;
    if (!c) { host.append(empty("缺少接续上下文", "这次尝试尚未保存上下文或检查点，可先查看审阅轨迹。")); return; }
    const external = result.kind === "external";
    const pack = external ? c : (c.context_pack || {});
    const checkpoint = external ? c : c.checkpoint;
    const task = pack.task || {};
    host.append(node("section", "panel panel-pad", section(task.title || c.work_item?.title || "任务目标"), task.url ? link("查看原始 Issue", task.url) : document.createDocumentFragment(), node("p", "meta", external ? `检查点版本 ${c.revision} · ${name(c.remote_freshness)}` : `上下文保存于 ${date(c.context_metadata?.created_at)}`)));
    if (!external) host.append(node("div", "callout", "这是已保存的维护上下文，当前工作区适用性尚未重新核对。"));
    if (external && values(c.validity).length) host.append(node("div", "callout", "当前基线存在变化：", list(c.validity)));
    if (!checkpoint) host.append(empty("尚无检查点", "当前仍可阅读任务目标与原始上下文。"));
    const left = node("div", "", node("section", "panel panel-pad", section("未完成工作"), list(external ? c.open_work : checkpoint?.remaining)), node("section", "panel panel-pad", section("已经做出的决定"), list(checkpoint?.decisions)));
    const right = node("div", "", node("section", "panel panel-pad", section("下一步"), node("p", "", checkpoint?.next_action || "尚未记录"), section("阻塞"), list(checkpoint?.blockers, "尚未记录阻塞。")), node("section", "panel panel-pad", section("接续入口"), command(result.command)));
    host.append(node("div", "split", left, right));
    const contract = external ? c.contract : pack.task_contract;
    if (contract) host.append(node("section", "panel panel-pad", section("目标与验收约束"), node("p", "", contract.goal || "尚未记录目标"), node("h3", "", "验收条件"), list(contract.acceptance_criteria, "原始契约未单列验收条件。"), node("h3", "", "范围约束"), list(contract.scope_boundaries, "原始契约未单列范围约束。"), contract.source_requirements ? node("p", "", contract.source_requirements) : null, details("契约来源与版本", contract)));
    host.append(details("Agent 自述（尚未独立核验）", external ? c.agent_claims : {completed: checkpoint?.completed, tests_observed: checkpoint?.tests_observed}), details("完整接续数据、来源与省略说明", c), button("复制接续数据", () => copy(JSON.stringify(c, null, 2)), true));
  }

  async function settings(host) {
    host.append(hero("知道当前运行的是什么。", "核对安装、配置来源与台账状态；此页只诊断，不执行升级或认证。", "LOCAL DIAGNOSTICS"));
    const result = await api("settings");
    const install = result.installation, config = result.configuration;
    host.append(node("section", "panel panel-pad", section("当前安装"), pairs([["包版本", install.version], ["Python", install.python], ["代码位置", install.module_path], ["来源提交", install.source_revision || "wheel 未提供，请核对安装记录"]])));
    const panel = node("section", "panel panel-pad", section("本地台账"));
    for (const [key, database] of Object.entries(result.databases || {})) panel.append(node("div", "item", node("div", "", node("h3", "", key === "tasks" ? "任务数据库" : "项目注册表"), node("p", "meta", database.path), node("span", "meta", `当前 ${database.schema ?? "未知"} / 支持 ${database.supported_schema}`)), badge(database.status, database.status === "compatible" ? "good" : "warn")));
    host.append(panel, node("section", "panel panel-pad", section("生效配置与来源"), node("pre", "", JSON.stringify(config, null, 2))));
    if (values(result.next_actions).length) host.append(node("section", "panel panel-pad", section("诊断建议"), list(result.next_actions)));
    host.append(command("reposteward doctor --local"), node("p", "meta", "配置发生变化后需重启工作台。浏览器会话最长有效 12 小时，进程退出后立即失效。"));
  }

  async function render() {
    controller.abort(); controller = new AbortController(); const current = ++generation;
    let parts;
    try { parts = location.hash.slice(1).split("/").map(decodeURIComponent); } catch { parts = []; }
    const view = labels[parts[0]] ? parts[0] : "today";
    document.getElementById("page-label").textContent = labels[view];
    document.title = labels[view] + " · RepoSteward";
    for (const anchor of document.querySelectorAll("nav a")) {
      if (anchor.dataset.view === view) anchor.setAttribute("aria-current", "page"); else anchor.removeAttribute("aria-current");
    }
    content.replaceChildren(); content.setAttribute("aria-busy", "true");
    if (!token) {
      content.append(hero("连接你的本地工作空间。", "请使用终端打印的完整会话链接打开。链接只在本机、当前进程内有效。"), command("reposteward web"));
      connection.textContent = "会话未连接"; connection.className = "badge warn"; content.setAttribute("aria-busy", "false"); return;
    }
    const host = node("div", ""); content.append(host);
    const loading = node("p", "meta", "正在读取本地数据…"); host.append(loading);
    try {
      if (view !== "settings") {
        const result = await api("projects");
        if (current !== generation) return;
        projectCache = values(result.projects);
        if (result.omitted) host.append(omitted(result.omitted, "个项目"));
      }
      loading.remove(); connection.textContent = "本地已连接"; connection.className = "badge good";
      if (view === "today") await today(host);
      else if (view === "projects") await projects(host, parts[1], parts[2]);
      else if (view === "settings") await settings(host);
      else await taskPage(host, view, parts[1], parts[2]);
    } catch (error) {
      if (error.name !== "AbortError") { loading.remove(); host.append(errorBox(error)); }
    } finally { if (current === generation) content.setAttribute("aria-busy", "false"); }
  }
  document.querySelector(".skip").addEventListener("click", event => { event.preventDefault(); content.focus(); content.scrollIntoView(); });
  document.getElementById("reload").addEventListener("click", render);
  window.addEventListener("hashchange", render);
  render();
})();
