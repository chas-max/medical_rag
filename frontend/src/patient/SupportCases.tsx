import { useEffect, useState } from "react";
import { request } from "./client";
import {
  categories,
  statuses,
  resolutions,
  supportCase,
  listSupport,
  changeSupport,
  type Category,
  type Resolution,
  type SupportCase,
} from "./support";
type Live = () => boolean;
type Props = {
  queue: boolean;
  account: string;
  busy: boolean;
  read: (operation: (live: Live) => Promise<void>) => void;
  mutate: (operation: (csrf: string, live: Live) => Promise<void>) => void;
};

export function SupportCases({ queue, account, busy, read, mutate }: Props) {
  const [own, setOwn] = useState<SupportCase[] | null>(null),
    [staff, setStaff] = useState<SupportCase[] | null>(null);
  const [detail, setDetail] = useState<SupportCase | null>(null),
    [category, setCategory] = useState<Category>("login_help");
  const [resolution, setResolution] = useState<Resolution>("guidance_provided");
  const [ownOffset, setOwnOffset] = useState(0),
    [queueOffset, setQueueOffset] = useState(0);
  useEffect(() => {
    read(async (live) => {
      const items = await listSupport(false);
      const pending = queue ? await listSupport(true) : null;
      if (live()) {
        setOwn(items);
        setStaff(pending);
      }
    });
  }, [queue, read]);
  const change = (
    id: string | null,
    operation: "create" | "cancel" | "take" | "resolve",
  ) => {
    setDetail(null);
    mutate(async (csrf, live) => {
      await changeSupport(
        id,
        operation,
        csrf,
        operation === "create" ? category : resolution,
      );
      if (!live()) return;
      const items = await listSupport(false);
      if (!live()) return;
      const pending = queue ? await listSupport(true) : null;
      if (live()) {
        setOwn(items);
        setStaff(pending);
        setOwnOffset(0);
        setQueueOffset(0);
      }
    });
  };
  const page = (isQueue: boolean, amount: number) => {
    const offset = Math.min(
      10000,
      Math.max(0, (isQueue ? queueOffset : ownOffset) + amount),
    );
    setDetail(null);
    read(async (live) => {
      const items = await listSupport(isQueue, offset);
      if (live()) {
        if (isQueue) {
          setStaff(items);
          setQueueOffset(offset);
        } else {
          setOwn(items);
          setOwnOffset(offset);
        }
      }
    });
  };
  const table = (items: SupportCase[] | null, isQueue: boolean) => (
    <>
      {items === null ? (
        <p role="status">正在读取支持请求…</p>
      ) : items.length === 0 ? (
        <p>暂无请求</p>
      ) : (
        <ul>
          {items.map((item) => (
            <li key={item.id}>
              <span>
                {categories[item.category]} · {statuses[item.status]}
              </span>
              {isQueue ? (
                <>
                  <p>请求账户：{item.requester_account_id}</p>
                  <p>受理账户：{item.assigned_account_id ?? "未分配"}</p>
                  {item.status === "open" && (
                    <button
                      disabled={busy}
                      onClick={() => change(item.id, "take")}
                    >
                      受理
                    </button>
                  )}
                  {item.status === "in_progress" &&
                    item.assigned_account_id === account && (
                      <button
                        disabled={busy}
                        onClick={() => change(item.id, "resolve")}
                      >
                        完成处理
                      </button>
                    )}
                </>
              ) : (
                <>
                  <button
                    disabled={busy}
                    onClick={() => {
                      setDetail(null);
                      read(async (live) => {
                        const data = supportCase(
                          await request(`/support/cases/${item.id}`),
                        );
                        if (live()) setDetail(data);
                      });
                    }}
                  >
                    查看详情
                  </button>
                  {["open", "in_progress"].includes(item.status) && (
                    <button
                      disabled={busy}
                      onClick={() => change(item.id, "cancel")}
                    >
                      取消请求
                    </button>
                  )}
                </>
              )}
              {item.resolution_code && (
                <p>{resolutions[item.resolution_code]}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      <button
        disabled={busy || (isQueue ? queueOffset : ownOffset) === 0}
        onClick={() => page(isQueue, -20)}
      >
        上一页{isQueue ? "队列" : "请求"}
      </button>
      <button
        disabled={
          busy || !items?.length || (isQueue ? queueOffset : ownOffset) >= 10000
        }
        onClick={() => page(isQueue, 20)}
      >
        下一页{isQueue ? "队列" : "请求"}
      </button>
    </>
  );
  return (
    <section>
      <h2>账户支持</h2>
      <p>
        仅接受固定问题类别，不接收医疗文字或附件。处理请求不会解锁账户、更改角色或绑定。
      </p>
      <p>
        需要当前有效登录；完全无法登录时，本页不是账户恢复渠道。开发限额：每账户24小时20次，最多5个未结束请求；取消不重置每日计数。
      </p>
      <label>
        问题类别
        <select
          value={category}
          disabled={busy}
          onChange={(event) => setCategory(event.target.value as Category)}
        >
          {Object.entries(categories).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <button disabled={busy} onClick={() => change(null, "create")}>
        创建支持请求
      </button>
      <h3>我的请求</h3>
      {table(own, false)}
      {detail && (
        <section>
          <h3>请求详情</h3>
          <p>
            {categories[detail.category]} · {statuses[detail.status]}
          </p>
          <p>创建时间：{detail.created_at}</p>
          <p>更新时间：{detail.updated_at}</p>
          {detail.resolution_code && (
            <p>{resolutions[detail.resolution_code]}</p>
          )}
        </section>
      )}
      {queue && (
        <section>
          <h3>机构客服队列</h3>
          <label>
            处理结果
            <select
              disabled={busy}
              value={resolution}
              onChange={(event) =>
                setResolution(event.target.value as Resolution)
              }
            >
              {Object.entries(resolutions).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {table(staff, true)}
        </section>
      )}
    </section>
  );
}
