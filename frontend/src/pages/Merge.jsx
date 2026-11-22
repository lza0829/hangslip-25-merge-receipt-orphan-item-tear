import { useEffect, useState } from "react";
import { api } from "../api";

const MERGEABLE = ["已收", "水洗", "烘干", "熨烫", "待取"];

export default function Merge() {
  const [rows, setRows] = useState([]);
  const [groups, setGroups] = useState([]);
  const [parent, setParent] = useState("");
  const [checked, setChecked] = useState({});
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);

  async function load() {
    const [all, list] = await Promise.all([api.挂牌列表(), api.并票列表()]);
    setRows(all.filter((r) => MERGEABLE.includes(r.status)));
    setGroups(list);
  }

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, []);

  function toggle(no) {
    setChecked((c) => ({ ...c, [no]: !c[no] }));
  }

  async function submit() {
    setMsg("");
    setOk(false);
    const children = rows
      .filter((r) => checked[r.ticket_no] && r.ticket_no !== parent)
      .map((r) => r.ticket_no);
    try {
      const receipt = await api.并票({
        parent_ticket_no: parent,
        child_ticket_nos: children,
        operator: "店长",
        note: "同顾客多开票并单",
      });
      setOk(true);
      setMsg(
        `${receipt.group_id}：${receipt.child_count} 张子票并入 ${receipt.parent_ticket_no}，` +
          `父票态 ${receipt.parent_status}，件数 ${receipt.item_qty}（+${receipt.added_qty}），` +
          `在途水位 ${receipt.watermark_before} → ${receipt.watermark_after}`
      );
      setChecked({});
      await load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
    <>
      <section className="panel">
        <h3>并票给同一顾客</h3>
        <p className={`msg ${ok ? "ok" : ""}`}>{msg}</p>
        <p style={{ color: "var(--steam)", fontSize: "0.86rem" }}>
          选定父票后勾选同顾客的子票。子票关账为「已并」退出在途；衣物件整批迁入父票；
          父票取最慢工序态，此后待取、取衣、水位只认父票。
        </p>
        <table>
          <thead>
            <tr>
              <th>父票</th>
              <th>并入</th>
              <th>票号</th>
              <th>顾客</th>
              <th>现行</th>
              <th>件数</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.ticket_no}>
                <td>
                  <input
                    type="radio"
                    name="parent"
                    checked={parent === r.ticket_no}
                    onChange={() => setParent(r.ticket_no)}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    disabled={parent === r.ticket_no}
                    checked={!!checked[r.ticket_no]}
                    onChange={() => toggle(r.ticket_no)}
                  />
                </td>
                <td>{r.ticket_no}</td>
                <td>{r.customer_name}</td>
                <td>
                  <span className={`badge ${r.status}`}>{r.status}</span>
                </td>
                <td>{r.item_qty}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row">
          <button type="button" onClick={submit} disabled={!parent}>
            并入父票
          </button>
        </div>
      </section>

      <section className="panel">
        <h3>并票回执</h3>
        <table>
          <thead>
            <tr>
              <th>组号</th>
              <th>父票</th>
              <th>父票态</th>
              <th>子票</th>
              <th>件数</th>
              <th>水位</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.group_id}>
                <td>{g.group_id}</td>
                <td>{g.parent_ticket_no}</td>
                <td>
                  <span className={`badge ${g.parent_status}`}>{g.parent_status}</span>
                </td>
                <td>
                  {g.links
                    .map((l) => `${l.child_ticket_no}(${l.child_status}/${l.child_qty}件)`)
                    .join("、")}
                </td>
                <td>{g.item_qty}</td>
                <td>
                  {g.watermark_before} → {g.watermark_after}
                </td>
                <td>{g.merged_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
