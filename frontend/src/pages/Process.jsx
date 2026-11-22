import { useEffect, useState } from "react";
import { api } from "../api";

const NEXT = {
  已收: ["水洗", "烘干", "熨烫", "待取"],
  水洗: ["烘干", "熨烫", "待取"],
  烘干: ["熨烫", "待取"],
  熨烫: ["待取"],
};

export default function Process() {
  const [rows, setRows] = useState([]);
  const [detail, setDetail] = useState(null);
  const [msg, setMsg] = useState("");

  async function load() {
    const all = await api.挂牌列表();
    setRows(
      all.filter(
        (r) => !["已取", "超期隔离", "丢失隔离", "已并"].includes(r.status)
      )
    );
  }

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, []);

  async function go(ticket_no, to_status) {
    setMsg("");
    try {
      await api.推进(ticket_no, { to_status, operator: "工序员" });
      setMsg(`${ticket_no} → ${to_status}`);
      await load();
      if (detail?.ticket_no === ticket_no) await open(ticket_no);
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function open(ticket_no) {
    try {
      setDetail(await api.挂牌详情(ticket_no));
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
    <>
      <section className="panel">
        <h3>工序轨</h3>
        <p className={`msg ${msg.includes("→") ? "ok" : ""}`}>{msg}</p>
        <table>
          <thead>
            <tr>
              <th>票号</th>
              <th>门店</th>
              <th>顾客</th>
              <th>现行</th>
              <th>件数</th>
              <th>合并</th>
              <th>承诺日</th>
              <th>推进</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.ticket_no}>
                <td>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => open(r.ticket_no)}
                  >
                    {r.ticket_no}
                  </button>
                </td>
                <td>{r.store_code}</td>
                <td>{r.customer_name}</td>
                <td>
                  <span className={`badge ${r.status}`}>{r.status}</span>
                </td>
                <td>{r.item_qty}</td>
                <td>
                  {r.merge_groups?.length
                    ? `${r.merge_groups.length} 组 / ${r.merge_ok ? "闭合" : "差异"}`
                    : "—"}
                </td>
                <td>{r.due_date}</td>
                <td>
                  <div className="row">
                    {(NEXT[r.status] || []).map((s) => (
                      <button
                        key={s}
                        type="button"
                        className="ghost"
                        onClick={() => go(r.ticket_no, s)}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && (
        <section className="panel">
          <h3>
            票详情 · {detail.ticket_no}{" "}
            <span className={`badge ${detail.status}`}>{detail.status}</span>
          </h3>
          <p style={{ color: "var(--steam)", fontSize: "0.86rem" }}>
            顾客 {detail.customer_name} · 件数 {detail.item_qty} · 暗码{" "}
            {detail.pickup_code}
            {detail.merged_into ? ` · 已并入 ${detail.merged_into}` : ""}
            {detail.merge_code ? ` · 合并码 ${detail.merge_code}` : ""}
          </p>

          {detail.merge_origin && (
            <p className="msg">
              旧票退账：世代 {detail.merge_origin.group_id} · 退 {detail.merge_origin.child_qty} 件 ·
              实迁 {detail.merge_origin.migrated_qty} 件 ·{" "}
              {detail.merge_origin.merge_ok ? "重放闭合" : "重放不闭合"}
              {detail.merge_origin.merge_issues?.length
                ? `（${detail.merge_origin.merge_issues.join("；")}）`
                : ""}
            </p>
          )}

          {detail.merge_groups?.length ? (
            <table>
              <thead>
                <tr>
                  <th>世代 / 合并码</th>
                  <th>子票</th>
                  <th>迁入件数</th>
                  <th>核对</th>
                </tr>
              </thead>
              <tbody>
                {detail.merge_groups.map((g) => (
                  <tr key={g.group_id}>
                    <td>{g.merge_code || g.group_id}</td>
                    <td>{g.child_count} 张</td>
                    <td>{g.added_qty}</td>
                    <td>
                      <span className={`badge ${g.merge_ok ? "待取" : "超期隔离"}`}>
                        {g.merge_ok ? "闭合" : "差异"}
                      </span>
                      {g.merge_issues?.length ? (
                        <span style={{ color: "#f0a29d" }}>
                          {" "}
                          {g.merge_issues.join("；")}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}

          <h3>衣物件</h3>
          <table>
            <thead>
              <tr>
                <th>衣物</th>
                <th>来源票</th>
                <th>件数</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.items || []).map((i) => (
                <tr key={i.id}>
                  <td>{i.garment}</td>
                  <td>{i.origin_ticket_no || i.ticket_no}</td>
                  <td>{i.qty}</td>
                  <td>{i.remark || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>事件流水</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>变更</th>
                <th>操作人</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.events || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.occurred_at}</td>
                  <td>
                    {e.from_status || "—"} → {e.to_status}
                  </td>
                  <td>{e.operator}</td>
                  <td>{e.note || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
