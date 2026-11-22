import { useEffect, useState } from "react";
import { api } from "../api";

const MERGEABLE = ["已收", "水洗", "烘干", "熨烫", "待取"];

const STATE_LABEL = {
  committed: "已落账",
  void: "已作废",
};

export default function Merge() {
  const [rows, setRows] = useState([]);
  const [groups, setGroups] = useState([]);
  const [ledger, setLedger] = useState(null);
  const [detail, setDetail] = useState(null);
  const [parent, setParent] = useState("");
  const [checked, setChecked] = useState({});
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);

  async function load() {
    const [all, list, keys] = await Promise.all([
      api.挂牌列表(),
      api.并票列表(),
      api.件键列表(),
    ]);
    setRows(all.filter((r) => MERGEABLE.includes(r.status)));
    setGroups(list);
    setLedger(keys);
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
      setOk(receipt.merge_ok === true);
      setMsg(
        `${receipt.group_id}：${receipt.child_count} 张子票并入 ${receipt.parent_ticket_no}，` +
          `父票态 ${receipt.parent_status}，件数 ${receipt.item_qty}（+${receipt.added_qty}），` +
          `在途水位 ${receipt.watermark_before} → ${receipt.watermark_after}，` +
          `合并码 ${receipt.merge_code}`
      );
      setChecked({});
      await load();
      await openDetail(receipt.group_id);
    } catch (e) {
      setOk(false);
      setMsg(e.message);
    }
  }

  async function openDetail(groupId) {
    try {
      setDetail(await api.并票详情(groupId));
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function reconcile() {
    setMsg("");
    setOk(false);
    try {
      const r = await api.并票签账();
      setOk(true);
      setMsg(
        `签账完成：作废 ${r.作废数} 个差异世代；水位 ${r.水位前} → ${r.水位后}` +
          (r.水位修正 ? "（已按票籍修正）" : "") +
          `；件键${r.件键闭合 ? "闭合" : "仍有散挂/双挂，见总览"}`
      );
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
          合并码、衣物件键迁移、旧票退账、事件、水位、待取墙在同一事务落账；
          半截整体回滚。合并码能否使用由世代重放核对裁决，差异未清一律作废。
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
              <th>并票</th>
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
                <td>
                  {r.merge_groups?.length
                    ? `${r.merge_groups.length} 组 / ${
                        r.merge_ok ? "闭合" : "有差异"
                      }`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row">
          <button type="button" onClick={submit} disabled={!parent}>
            并入父票
          </button>
          <button type="button" className="ghost" onClick={reconcile}>
            签账核对
          </button>
        </div>
      </section>

      <section className="panel">
        <h3>并票回执与合并码</h3>
        <table>
          <thead>
            <tr>
              <th>组号 / 合并码</th>
              <th>父票</th>
              <th>父票态</th>
              <th>子票（退账/迁入）</th>
              <th>件数</th>
              <th>水位</th>
              <th>世代核对</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.group_id}>
                <td>
                  <div>{g.group_id}</div>
                  <code style={{ fontSize: "0.78rem", color: "var(--steam)" }}>
                    {g.merge_code || "—"}
                  </code>
                </td>
                <td>{g.parent_ticket_no}</td>
                <td>
                  <span className={`badge ${g.parent_status}`}>{g.parent_status}</span>
                </td>
                <td>
                  {g.links
                    .map(
                      (l) =>
                        `${l.child_ticket_no}(${l.child_qty}退/${l.migrated_qty}迁)`
                    )
                    .join("、")}
                </td>
                <td>{g.item_qty}</td>
                <td>
                  {g.watermark_before} → {g.watermark_after}
                </td>
                <td>
                  <span className={`badge ${g.merge_ok ? "待取" : "超期隔离"}`}>
                    {g.merge_ok ? "闭合" : "差异"}
                  </span>
                  <span style={{ marginLeft: 6, color: "var(--steam)" }}>
                    {STATE_LABEL[g.state] || g.state}
                  </span>
                  {!g.merge_ok && g.merge_issues?.length ? (
                    <div style={{ fontSize: "0.78rem", color: "#f0a29d" }}>
                      {g.merge_issues.join("；")}
                    </div>
                  ) : null}
                </td>
                <td>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => openDetail(g.group_id)}
                  >
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {ledger && (
        <section className="panel">
          <h3>
            衣物件键台账{" "}
            <span className={`badge ${ledger.件键闭合 ? "待取" : "超期隔离"}`}>
              {ledger.件键闭合 ? "闭合" : `${ledger.未收口行} 行未收口`}
            </span>
          </h3>
          <p style={{ color: "var(--steam)", fontSize: "0.86rem" }}>
            件数合计 {ledger.件数合计} · 每行必须挂在现行父票名下，来源旧票不得再挂行。
          </p>
          <table>
            <thead>
              <tr>
                <th>现挂票</th>
                <th>来源票</th>
                <th>衣物</th>
                <th>件数</th>
                <th>核对</th>
              </tr>
            </thead>
            <tbody>
              {ledger.rows.map((i) => (
                <tr key={i.id}>
                  <td>{i.ticket_no}</td>
                  <td>{i.origin_ticket_no}</td>
                  <td>{i.garment}</td>
                  <td>{i.qty}</td>
                  <td style={{ color: i.ok ? "#9fd6bd" : "#f0a29d" }}>
                    {i.ok ? "收口" : i.issues.join("；")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {detail && (
        <section className="panel">
          <h3>
            合并码页 · {detail.merge_code || detail.group_id}{" "}
            <span className={`badge ${detail.merge_ok ? "待取" : "超期隔离"}`}>
              {detail.merge_ok ? "可核销" : "不可核销"}
            </span>
          </h3>
          <p style={{ color: "var(--steam)", fontSize: "0.86rem" }}>
            父票 {detail.parent_ticket_no}（{detail.parent?.status}）·
            件数 {detail.parent_item_qty} · 世代 {STATE_LABEL[detail.state] || detail.state}
          </p>
          {detail.merge_issues?.length ? (
            <p className="msg">{detail.merge_issues.join("；")}</p>
          ) : null}
          <table>
            <thead>
              <tr>
                <th>衣物</th>
                <th>来源票</th>
                <th>件数</th>
              </tr>
            </thead>
            <tbody>
              {(detail.items || []).map((i) => (
                <tr key={i.id}>
                  <td>{i.garment}</td>
                  <td>{i.origin_ticket_no || i.ticket_no}</td>
                  <td>{i.qty}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
