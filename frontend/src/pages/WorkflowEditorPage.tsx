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
  runWorkflow,
  saveWorkflow,
  validateWorkflow,
  type NodeMetadata,
  type RunResult,
  type Workflow,
  type ValidationResult,
} from "../api/workflows";
import WorkflowNode from "../components/workflow/WorkflowNode";
import {
  autoLayout,
  compatible,
  toGraph,
  toWorkflow,
  type GraphNode,
} from "../workflow/graph";

const nodeTypes = { workflow: WorkflowNode };
const categories: Record<string, string> = {
  input: "输入",
  document: "文档处理",
  retrieval: "检索",
  fusion: "融合",
  rerank: "重排",
  evidence: "证据安全",
  generation: "模型生成",
  citation: "引用校验",
  output: "输出",
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
  const [run, setRun] = useState<RunResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [traceOpen, setTraceOpen] = useState(true);
  const [testQuestion, setTestQuestion] = useState("高血压患者如何管理？");

  useEffect(() => {
    let alive = true;
    Promise.all([getNodes(), getWorkflow(workflowId), getWorkflows()])
      .then(([types, workflow, available]) => {
        if (!alive) return;
        setCatalog(types);
        setOriginal(workflow);
        setWorkflows(available);
        const graph = toGraph(workflow, types);
        setNodes(graph.nodes);
        setEdges(graph.edges);
        setValidation(null);
        setRun(null);
        setSelectedId(null);
        setError("");
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
  const grouped = useMemo(
    () =>
      Object.entries(categories)
        .map(([key, title]) => ({
          key,
          title,
          items: catalog.filter((item) => item.category === key),
        }))
        .filter((group) => group.items.length),
    [catalog],
  );
  const changed = () => {
    setValidation(null);
    setRun(null);
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
      if (!compatible(connection, nodes, catalog, edges)) {
        setError("端口类型不匹配、输入已连接或存在环路");
        return;
      }
      setEdges((previous) =>
        addEdge(
          { ...connection, id: crypto.randomUUID(), type: "smoothstep" },
          previous,
        ),
      );
      setError("");
      changed();
    },
    [nodes, catalog, edges],
  );

  function addNode(type: NodeMetadata, position?: { x: number; y: number }) {
    const id = `${type.type.replaceAll(".", "-")}-${crypto.randomUUID().slice(0, 6)}`;
    const node: GraphNode = {
      id,
      type: "workflow",
      position: position || {
        x: 180 + Math.random() * 100,
        y: 180 + Math.random() * 100,
      },
      data: {
        nodeType: type.type,
        metadata: type,
        config: Object.fromEntries(
          Object.entries(type.config_schema)
            .filter(([, schema]) => schema.default !== undefined)
            .map(([key, schema]) => [key, schema.default!]),
        ),
      },
    };
    setNodes((previous) => [...previous, node]);
    setSelectedId(id);
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
  function remove() {
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
      setWorkflows(await getWorkflows());
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
  async function execute() {
    if (!original) return;
    setBusy(true);
    setError("");
    setTraceOpen(true);
    try {
      const result = await runWorkflow(original.id, {
        question: testQuestion,
        provider: "deepseek",
        top_k: 5,
      });
      setRun(result);
      setNodes((previous) =>
        previous.map((node) => ({
          ...node,
          data: {
            ...node.data,
            traceStatus: result.trace.find((item) => item.node_id === node.id)
              ?.status,
          },
        })),
      );
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "运行失败");
    } finally {
      setBusy(false);
    }
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
          <select
            aria-label="切换工作流"
            value={workflowId}
            onChange={(event) => navigate(`/workflows/${event.target.value}`)}
          >
            {workflows.length ? (
              workflows.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))
            ) : (
              <option value={workflowId}>{workflowId}</option>
            )}
          </select>
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
          <p className="panel-hint">拖动节点到画布，或点击添加</p>
          <div className="palette-scroll">
            {grouped.map((group) => (
              <section key={group.key} className="palette-group">
                <h3>{group.title}</h3>
                {group.items.map((item) => (
                  <button
                    key={item.type}
                    draggable
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
                    <span className="palette-plus">＋</span>
                  </button>
                ))}
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
                {traceOpen ? "收起" : "展开"}运行记录
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
              isValidConnection={(connection) =>
                compatible(
                  {
                    source: connection.source,
                    target: connection.target,
                    sourceHandle: connection.sourceHandle || null,
                    targetHandle: connection.targetHandle || null,
                  },
                  nodes,
                  catalog,
                  edges,
                )
              }
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
          {traceOpen && (
            <div className="trace-panel">
              <div className="trace-heading">
                <div>
                  <span className="eyebrow">INSPECT & RUN</span>
                  <h2>运行与校验</h2>
                </div>
                <div className="test-run">
                  <input
                    aria-label="测试问题"
                    value={testQuestion}
                    onChange={(event) => setTestQuestion(event.target.value)}
                    placeholder="输入测试问题"
                  />
                  <button
                    className="button-primary"
                    onClick={() => void execute()}
                    disabled={busy || !original}
                  >
                    ▷ 运行已发布版
                  </button>
                </div>
              </div>
              {error && (
                <p className="editor-notice" role="status">
                  {error}
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
              {run ? (
                <div className="trace-list">
                  <span className="trace-result">
                    运行 {run.status === "success" ? "成功" : "失败"} · #
                    {run.run_id.slice(0, 8)}
                  </span>
                  {run.trace.map((item) => (
                    <button
                      key={item.node_id}
                      onClick={() => setSelectedId(item.node_id)}
                    >
                      <span className={`trace-dot ${item.status}`} />
                      {nodes.find((node) => node.id === item.node_id)?.data
                        .metadata?.title || item.node_id}
                      <small>
                        {item.status} · {item.duration_ms}ms
                      </small>
                      {item.error && (
                        <span className="trace-error">
                          {item.error.message}
                        </span>
                      )}
                    </button>
                  ))}
                  {run.output.answer && (
                    <p className="trace-answer">{run.output.answer}</p>
                  )}
                </div>
              ) : (
                !validation &&
                !error && (
                  <p className="empty-trace">
                    校验当前画布，或运行已发布版本以查看逐节点轨迹。
                  </p>
                )
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
              <div className="inspector-buttons">
                <button onClick={duplicate}>⊞ 复制节点</button>
                <button className="danger" onClick={remove}>
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
