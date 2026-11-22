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
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
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
            <th>承诺日</th>
            <th>推进</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.ticket_no}>
              <td>{r.ticket_no}</td>
              <td>{r.store_code}</td>
              <td>{r.customer_name}</td>
              <td>
                <span className={`badge ${r.status}`}>{r.status}</span>
              </td>
              <td>{r.item_qty}</td>
              <td>{r.due_date}</td>
              <td>
                <div className="row">
                  {(NEXT[r.status] || []).map((s) => (
                    <button key={s} type="button" className="ghost" onClick={() => go(r.ticket_no, s)}>
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
  );
}
