import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  Controls,
  MiniMap,
  type Connection,
  type Edge,
  type EdgeChange,
  type NodeChange,
  useReactFlow,
  ReactFlowProvider,
} from "@xyflow/react";
import {
  getNodes,
  getWorkflow,
  getWorkflows,
  saveWorkflow,
  validateWorkflow,
  type NodeActivation,
  type NodeMetadata,
  type Workflow,
  type ValidationResult,
} from "../api/workflows";
import WorkflowNode from "../components/workflow/WorkflowNode";
import {
  autoLayout,
  createGraphNode,
  explainConnection,
  fixedSiblings,
  toGraph,
  toWorkflow,
  withFixedNodes,
  type GraphNode,
} from "../workflow/graph";
import {
  selectAnswerWorkflows,
  writeCurrentWorkflowId,
} from "../workflow/selection";

const nodeTypes = { workflow: WorkflowNode };
const categories: Record<string, string> = {
  input: "输入",
  question: "问题处理",
  retrieval: "检索",
  fusion: "融合",
  rerank: "重排",
  evidence: "证据安全",
  generation: "模型生成",
  citation: "引用校验",
  document: "文档处理",
  output: "输出",
};

/** 已知分类的展示顺序；没列出来的新分类排在末尾，但仍然会出现在节点库里。 */
const categoryOrder = Object.keys(categories);

/** 需要用户显式确认的操作：切换工作流、删除固定节点。 */
type ConfirmAction = {
  title: string;
  lines: string[];
  confirmLabel: string;
  onConfirm: () => void;
  altLabel?: string;
  onAlt?: () => void;
};

function Editor() {
  const { workflowId = "medical-answer" } = useParams();
  const navigate = useNavigate();
  const { screenToFlowPosition, fitView, setViewport } = useReactFlow();
  const [catalog, setCatalog] = useState<NodeMetadata[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [original, setOriginal] = useState<Workflow | null>(null);
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [traceOpen, setTraceOpen] = useState(true);
  const [notice, setNotice] = useState("");
  const [connectHint, setConnectHint] = useState("");
  const [pendingWorkflow, setPendingWorkflow] = useState("");
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([getNodes(), getWorkflow(workflowId), getWorkflows()])
      .then(([types, workflow, available]) => {
        if (!alive) return;
        setCatalog(types);
        setOriginal(workflow);
        setWorkflows(selectAnswerWorkflows(available));
        const graph = toGraph(workflow, types);
        const fixed = withFixedNodes(graph.nodes, types);
        setNodes(fixed.nodes);
        setEdges(graph.edges);
        setValidation(null);
        setSelectedId(null);
        setError("");
        setNotice(
          fixed.added.length
            ? `已补回固定节点：${fixed.added.map((item) => item.title).join("、")}`
            : "",
        );
        window.setTimeout(
          () => setViewport({ x: 65, y: 60, zoom: 0.78 }, { duration: 350 }),
          50,
        );
      })
      .catch((problem) => {
        if (alive) setError(problem.message);
      });
    return () => {
      alive = false;
    };
  }, [workflowId, setViewport]);

  const selected = nodes.find((node) => node.id === selectedId);
  const selectedMeta = catalog.find(
    (item) => item.type === selected?.data.nodeType,
  );
  // 固定节点只有在同类别还剩别的实例时才可删，否则画布会缺入口或出口。
  const selectedIsLastFixed = Boolean(
    selected && selectedMeta?.fixed && !fixedSiblings(selected, nodes),
  );
  const selectedActivation = selected?.data.activation ?? null;
  // 分组来自节点目录本身：后端新增一个分类（例如新的问题处理节点）就自动出现在画布里。
  const grouped = useMemo(() => {
    const keys = [...new Set(catalog.map((item) => item.category))];
    const rank = (key: string) => {
      const index = categoryOrder.indexOf(key);
      return index < 0 ? categoryOrder.length : index;
    };
    return keys
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
      .map((key) => ({
        key,
        title: categories[key] || key,
        items: catalog.filter((item) => item.category === key),
      }));
  }, [catalog]);
  const dirty = useMemo(() => {
    if (!original) return false;
    const current = toWorkflow(original, nodes, edges, original.status);
    return (
      JSON.stringify([current.nodes, current.edges]) !==
      JSON.stringify([original.nodes, original.edges])
    );
  }, [original, nodes, edges]);
  const changed = () => {
    setValidation(null);
  };
  const onNodesChange = useCallback((changes: NodeChange<GraphNode>[]) => {
    setNodes((previous) => applyNodeChanges(changes, previous));
    changed();
  }, []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((previous) => applyEdgeChanges(changes, previous));
    changed();
  }, []);
  const onConnect = useCallback(
    (connection: Connection) => {
      const reason = explainConnection(connection, nodes, catalog, edges);
      if (reason) {
        // 连线被拒必须当场说清楚：ReactFlow 的 isValidConnection 只会静默取消。
        setConnectHint(reason);
        return;
      }
      setEdges((previous) =>
        addEdge(
          { ...connection, id: crypto.randomUUID(), type: "smoothstep" },
          previous,
        ),
      );
      setConnectHint("");
      setError("");
      changed();
    },
    [nodes, catalog, edges],
  );

  function addNode(type: NodeMetadata, position?: { x: number; y: number }) {
    const node = createGraphNode(
      type,
      position || {
        x: 180 + Math.random() * 100,
        y: 180 + Math.random() * 100,
      },
    );
    setNodes((previous) => [...previous, node]);
    setSelectedId(node.id);
    changed();
  }

  function duplicate() {
    if (!selected || !selectedMeta) return;
    const id = `${selected.data.nodeType.replaceAll(".", "-")}-${crypto.randomUUID().slice(0, 6)}`;
    setNodes((previous) => [
      ...previous,
      {
        ...selected,
        id,
        selected: false,
        position: { x: selected.position.x + 64, y: selected.position.y + 64 },
        data: { ...selected.data, config: { ...selected.data.config } },
      },
    ]);
    setSelectedId(id);
    changed();
  }
  function deleteSelected() {
    if (!selectedId) return;
    setNodes((previous) => previous.filter((node) => node.id !== selectedId));
    setEdges((previous) =>
      previous.filter(
        (edge) => edge.source !== selectedId && edge.target !== selectedId,
      ),
    );
    setSelectedId(null);
    changed();
  }
  function remove() {
    if (!selected) return;
    const meta = selected.data.metadata;
    if (!meta?.fixed) {
      deleteSelected();
      return;
    }
    const group = categories[meta.category] || meta.category;
    if (selectedIsLastFixed) {
      setError(`「${meta.title}」是固定${group}节点，工作流必须保留一个`);
      return;
    }
    setConfirm({
      title: `删除固定${group}节点「${meta.title}」？`,
      lines: [
        `输入与输出节点常驻工作流，删除后仍需保留至少一个${group}节点。`,
        `该节点上的 ${edges.filter((edge) => edge.source === selected.id || edge.target === selected.id).length} 条连线会一起移除。`,
      ],
      confirmLabel: "仍然删除",
      onConfirm: deleteSelected,
    });
  }
  function updateConfig(
    nodeId: string,
    key: string,
    value: string | number | boolean,
  ) {
    setNodes((previous) =>
      previous.map((node) =>
        node.id === nodeId
          ? {
              ...node,
              data: {
                ...node.data,
                config: { ...node.data.config, [key]: value },
              },
            }
          : node,
      ),
    );
    changed();
  }
  /** 路由条件写在节点实例上，所以换端口或改期望值都不需要动节点类型。 */
  function setActivation(
    nodeId: string,
    activation: NodeActivation | null,
  ) {
    setNodes((previous) =>
      previous.map((node) =>
        node.id === nodeId ? { ...node, data: { ...node.data, activation } } : node,
      ),
    );
    changed();
  }
  async function check() {
    if (!original) return;
    setBusy(true);
    setError("");
    try {
      setValidation(await validateWorkflow(toWorkflow(original, nodes, edges)));
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "校验失败");
    } finally {
      setBusy(false);
    }
  }
  async function save(status: Workflow["status"]) {
    if (!original) return;
    setBusy(true);
    setError("");
    try {
      const draft = toWorkflow(original, nodes, edges, status);
      if (status === "published") {
        const result = await validateWorkflow(draft);
        setValidation(result);
        if (!result.valid) {
          setError("发布前请修复校验问题");
          return;
        }
      }
      const saved = await saveWorkflow(draft);
      setOriginal(saved);
      setWorkflows(selectAnswerWorkflows(await getWorkflows()));
      setError(
        status === "published"
          ? `已发布 v${saved.version}`
          : `草稿 v${saved.version} 已保存`,
      );
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }
  function switchTo(targetId: string) {
    writeCurrentWorkflowId(targetId);
    navigate(`/workflows/${targetId}`);
  }
  /** 这里是唯一的工作流切换入口，问答页同步使用它，因此切换必须经确认。 */
  function requestSwitch(targetId: string) {
    if (!targetId || targetId === workflowId) {
      setPendingWorkflow("");
      return;
    }
    setPendingWorkflow(targetId);
    const target = workflows.find((item) => item.id === targetId);
    const label = target ? `「${target.name} · v${target.version}」` : `「${targetId}」`;
    setConfirm({
      title: `切换到${label}？`,
      lines: [
        "问答页会立刻改用该工作流回答，问答页本身不再提供切换。",
        ...(dirty ? ["当前画布有未保存修改，切换后这些修改会丢失。"] : []),
      ],
      confirmLabel: "确认切换",
      onConfirm: () => switchTo(targetId),
      ...(dirty ? { altLabel: "先保存草稿", onAlt: () => void save("draft") } : {}),
    });
  }
  return (
    <div className="editor-layout">
      <div className="editor-header">
        <div>
          <span className="eyebrow">WORKFLOW STUDIO / 工作流画布</span>
          <div className="editor-title">
            <h1>{original?.name || "加载工作流…"}</h1>
            <span className="version">
              v{original?.version || "—"} · {original?.status || "加载中"}
            </span>
          </div>
        </div>
        <div className="editor-actions">
          <label className="workflow-switch">
            <span className="switch-caption">唯一切换入口</span>
            <select
              aria-label="切换工作流"
              value={pendingWorkflow || workflowId}
              onChange={(event) => requestSwitch(event.target.value)}
            >
              {!workflows.some((item) => item.id === workflowId) && (
                <option value={workflowId}>
                  {original?.name || workflowId}
                </option>
              )}
              {workflows.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <button
            className="button-ghost"
            onClick={() => void check()}
            disabled={busy || !original}
          >
            ✓ 校验
          </button>
          <button
            className="button-ghost"
            onClick={() => void save("draft")}
            disabled={busy || !original}
          >
            保存草稿
          </button>
          <button
            className="button-primary"
            onClick={() => void save("published")}
            disabled={busy || !original}
          >
            发布新版本 ↗
          </button>
        </div>
      </div>
      <div className="editor-body">
        <aside className="palette">
          <div className="panel-heading">
            <h2>节点库</h2>
            <span>{catalog.length} 个节点</span>
          </div>
          <p className="panel-hint">
            拖动节点到画布，或点击添加；输入与输出节点常驻工作流。
          </p>
          <div className="palette-scroll">
            {grouped.map((group) => (
              <section key={group.key} className="palette-group">
                <h3>{group.title}</h3>
                {group.items.map((item) => {
                  const resident =
                    Boolean(item.fixed) &&
                    nodes.some((node) => node.data.nodeType === item.type);
                  return (
                    <button
                      key={item.type}
                      draggable={!item.fixed}
                      disabled={resident}
                      title={
                        item.fixed
                          ? "固定节点：常驻画布，不可删除"
                          : "拖动或点击添加"
                      }
                      onDragStart={(event) => {
                        event.dataTransfer.setData(
                          "application/zhiyuan-node",
                          item.type,
                        );
                        event.dataTransfer.effectAllowed = "move";
                      }}
                      onClick={() => addNode(item)}
                    >
                      <span className={`palette-icon category-${item.category}`}>
                        ◇
                      </span>
                      <span>
                        <strong>{item.title}</strong>
                        <small>{item.type}</small>
                      </span>
                      <span
                        className={item.fixed ? "palette-fixed" : "palette-plus"}
                      >
                        {item.fixed ? (resident ? "常驻" : "固定") : "＋"}
                      </span>
                    </button>
                  );
                })}
              </section>
            ))}
          </div>
          <div className="palette-foot">✳ &nbsp;节点可复用 · 工作流可演进</div>
        </aside>
        <div className="canvas-area">
          <div className="canvas-toolbar">
            <span>
              <span className="status-dot" /> DAG 画布{" "}
              <span className="muted">
                / {nodes.length} 节点 · {edges.length} 连线
              </span>
            </span>
            <div>
              <button
                onClick={() => {
                  setNodes(autoLayout(nodes, edges));
                  changed();
                  window.setTimeout(
                    () => fitView({ padding: 0.18, duration: 300 }),
                    50,
                  );
                }}
              >
                自动布局
              </button>
              <button onClick={() => fitView({ padding: 0.18, duration: 300 })}>
                适应画布 ⛶
              </button>
              <button onClick={() => setTraceOpen((value) => !value)}>
                {traceOpen ? "收起" : "展开"}校验记录
              </button>
            </div>
          </div>
          <div
            className="flow-wrap"
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
            }}
            onDrop={(event) => {
              event.preventDefault();
              const type = event.dataTransfer.getData(
                "application/zhiyuan-node",
              );
              const meta = catalog.find((item) => item.type === type);
              if (meta)
                addNode(
                  meta,
                  screenToFlowPosition({ x: event.clientX, y: event.clientY }),
                );
            }}
          >
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              isValidConnection={() => true}
              onNodeClick={(_, node) => setSelectedId(node.id)}
              onPaneClick={() => setSelectedId(null)}
              minZoom={0.25}
              maxZoom={1.8}
              deleteKeyCode={["Backspace", "Delete"]}
            >
              <Background color="#dbe7e1" gap={22} size={1} />
              <Controls showInteractive={false} />
              <MiniMap
                pannable
                zoomable
                nodeColor="#c0d7cf"
                style={{ width: 140, height: 80 }}
              />
            </ReactFlow>
          </div>
          {connectHint && (
            <p className="editor-notice connect-hint" role="status">
              这条线连不上：{connectHint}
              <button onClick={() => setConnectHint("")} aria-label="关闭连线提示">
                ×
              </button>
            </p>
          )}
          {traceOpen && (
            <div className="trace-panel">
              <div className="trace-heading">
                <div>
                  <span className="eyebrow">INSPECT & VALIDATE</span>
                  <h2>工作流校验</h2>
                </div>
              </div>
              {error && (
                <p className="editor-notice" role="status">
                  {error}
                </p>
              )}
              {notice && (
                <p className="editor-notice fixed-notice" role="status">
                  {notice}
                  <button onClick={() => setNotice("")} aria-label="关闭固定节点提示">
                    ×
                  </button>
                </p>
              )}
              {validation && (
                <div
                  className={`validation ${validation.valid ? "valid" : ""}`}
                >
                  {validation.valid
                    ? "✓ 工作流校验通过"
                    : `发现 ${validation.issues.length} 个校验问题`}
                  {!validation.valid && (
                    <div className="issue-list">
                      {validation.issues.map((issue, i) => (
                        <button
                          key={i}
                          onClick={() => setSelectedId(issue.node_id || null)}
                        >
                          {issue.node_id || issue.edge_id || "全局"} ·{" "}
                          {issue.message}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {!validation &&
                !error &&
                !notice && (
                  <p className="empty-trace">校验当前画布以查看校验结果。</p>
                )}
            </div>
          )}
        </div>
        <aside className="inspector">
          <div className="panel-heading">
            <h2>属性面板</h2>
            <span>INSPECTOR</span>
          </div>
          {selected && selectedMeta ? (
            <div className="inspector-content">
              <span
                className={`inspector-icon category-${selectedMeta.category}`}
              >
                ◇
              </span>
              <h3>{selectedMeta.title}</h3>
              <p>{selectedMeta.type}</p>
              {selectedMeta.fixed && (
                <p className="inspector-fixed">
                  🔒 固定{categories[selectedMeta.category] || selectedMeta.category}节点
                  ·{" "}
                  {selectedIsLastFixed
                    ? "本工作流必须保留它，不可删除"
                    : "常驻画布，删除需确认"}
                </p>
              )}
              <div className="divider" />
              <label className="field-label">
                节点 ID
                <input readOnly value={selected.id} />
              </label>
              <span className="field-title">节点参数</span>
              {Object.keys(selectedMeta.config_schema).length ? (
                Object.entries(selectedMeta.config_schema).map(
                  ([key, schema]) => (
                    <label className="field-label" key={key}>
                      {key}
                      {schema.type === "boolean" ? (
                        <input
                          type="checkbox"
                          checked={Boolean(
                            selected.data.config[key] ?? schema.default,
                          )}
                          onChange={(event) =>
                            updateConfig(selected.id, key, event.target.checked)
                          }
                        />
                      ) : schema.enum ? (
                        <select
                          value={String(
                            selected.data.config[key] ?? schema.default ?? "",
                          )}
                          onChange={(event) =>
                            updateConfig(
                              selected.id,
                              key,
                              schema.type === "integer" ||
                                schema.type === "number"
                                ? Number(event.target.value)
                                : event.target.value,
                            )
                          }
                        >
                          {schema.enum.map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          type={
                            schema.type === "number" ||
                            schema.type === "integer"
                              ? "number"
                              : "text"
                          }
                          step={schema.type === "integer" ? 1 : "any"}
                          min={schema.minimum}
                          max={schema.maximum}
                          value={String(
                            selected.data.config[key] ?? schema.default ?? "",
                          )}
                          onChange={(event) =>
                            updateConfig(
                              selected.id,
                              key,
                              schema.type === "number" ||
                                schema.type === "integer"
                                ? Number(event.target.value)
                                : event.target.value,
                            )
                          }
                        />
                      )}
                    </label>
                  ),
                )
              ) : (
                <p className="panel-hint">此节点无需额外配置</p>
              )}
              <div className="divider" />
              <span className="field-title">执行条件（路由）</span>
              <p className="panel-hint">
                指定入参等于期望值时本节点才执行，未执行的分支在运行记录里是
                inactive。不设置就是无条件执行——想完全不路由，删掉问题路由节点即可。
              </p>
              <label className="field-label">
                端口
                <select
                  value={selectedActivation?.port ?? ""}
                  onChange={(event) => {
                    const port = event.target.value;
                    setActivation(
                      selected.id,
                      port
                        ? { port, equals: selectedActivation?.equals ?? "" }
                        : null,
                    );
                  }}
                >
                  <option value="">不设置</option>
                  {Object.entries(selectedMeta.inputs).map(([name, port]) => (
                    <option key={name} value={name}>
                      {name} · {port.dtype}
                      {port.optional ? "（可不连线）" : ""}
                    </option>
                  ))}
                </select>
              </label>
              {selectedActivation && (
                <label className="field-label">
                  期望值
                  <input
                    value={String(selectedActivation.equals)}
                    onChange={(event) =>
                      setActivation(selected.id, {
                        port: selectedActivation.port,
                        equals: event.target.value,
                      })
                    }
                  />
                </label>
              )}
              <div className="divider" />
              <div className="inspector-buttons">
                <button onClick={duplicate}>⊞ 复制节点</button>
                <button
                  className="danger"
                  onClick={remove}
                  disabled={selectedIsLastFixed}
                  title={
                    selectedIsLastFixed
                      ? "固定节点：工作流必须保留至少一个同类节点"
                      : undefined
                  }
                >
                  ⌫ 删除节点
                </button>
              </div>
            </div>
          ) : (
            <div className="inspector-empty">
              <div>◇</div>
              <h3>选择一个节点</h3>
              <p>点击画布上的节点，查看端口、编辑参数或管理节点。</p>
            </div>
          )}
          <div className="inspector-foot">
            <span className="small-label">安全提示</span>
            <p>
              发布前必须经过证据门控与引用校验。工作流定义不会包含 API 密钥。
            </p>
            <Link to="/chat">返回医疗问答 ↗</Link>
          </div>
        </aside>
      </div>
      {confirm && (
        <div
          className="confirm-overlay"
          role="presentation"
          onClick={() => {
            setConfirm(null);
            setPendingWorkflow("");
          }}
        >
          <div
            className="confirm-card"
            role="dialog"
            aria-modal="true"
            aria-label={confirm.title}
            onClick={(event) => event.stopPropagation()}
          >
            <span className="eyebrow">CONFIRM · 需要确认</span>
            <h2>{confirm.title}</h2>
            <ul>
              {confirm.lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <div className="confirm-actions">
              <button
                className="button-ghost"
                onClick={() => {
                  setConfirm(null);
                  setPendingWorkflow("");
                }}
              >
                取消
              </button>
              {confirm.altLabel && (
                <button
                  className="button-ghost"
                  onClick={() => {
                    confirm.onAlt?.();
                    setConfirm(null);
                    setPendingWorkflow("");
                  }}
                >
                  {confirm.altLabel}
                </button>
              )}
              <button
                className="button-primary"
                onClick={() => {
                  const run = confirm.onConfirm;
                  setConfirm(null);
                  setPendingWorkflow("");
                  run();
                }}
              >
                {confirm.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function WorkflowEditorPage() {
  return (
    <ReactFlowProvider>
      <Editor />
    </ReactFlowProvider>
  );
}
