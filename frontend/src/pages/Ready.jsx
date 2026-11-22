import { useEffect, useState } from "react";
import { api } from "../api";

export default function Ready() {
  const [rows, setRows] = useState([]);
  const [codes, setCodes] = useState({});
  const [scan, setScan] = useState("");
  const [scanned, setScanned] = useState(null);
  const [msg, setMsg] = useState("");

  async function load() {
    setRows(await api.挂牌列表("?status=" + encodeURIComponent("待取")));
  }

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, []);

  async function pick(ticket_no) {
    setMsg("");
    try {
      await api.取衣(ticket_no, {
        pickup_code: codes[ticket_no] || "",
        operator: "柜员",
      });
      setMsg(`${ticket_no} 已取衣核销`);
      await load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function lookupCode() {
    setMsg("");
    setScanned(null);
    try {
      setScanned(await api.合并码扫码(scan.trim()));
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function pickByCode() {
    setMsg("");
    try {
      const row = await api.合并码取衣(scan.trim(), { operator: "柜员" });
      setMsg(`${row.ticket_no} 已凭合并码取衣核销`);
      setScanned(null);
      setScan("");
      await load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
    <>
      <section className="panel">
        <h3>取衣扫码 · 合并码</h3>
        <p className={`msg ${msg.includes("已取") || msg.includes("已凭") ? "ok" : ""}`}>
          {msg}
        </p>
        <div className="row">
          <input
            placeholder="扫合并码 / 世代号"
            value={scan}
            onChange={(e) => setScan(e.target.value)}
            style={{ minWidth: 260 }}
          />
          <button type="button" className="ghost" onClick={lookupCode} disabled={!scan.trim()}>
            核验世代
          </button>
          <button type="button" onClick={pickByCode} disabled={!scanned?.ok || scanned?.parent_status !== "待取"}>
            凭码取衣
          </button>
        </div>
        {scanned && (
          <p style={{ color: scanned.ok ? "#9fd6bd" : "#f0a29d", fontSize: "0.86rem" }}>
            合并码 {scanned.merge_code} → 父票 {scanned.parent_ticket_no}（
            {scanned.parent_status || "—"}）· 件数 {scanned.parent_item_qty} ·{" "}
            {scanned.ok
              ? scanned.parent_status === "待取"
                ? "世代重放闭合，可核销"
                : "世代重放闭合，但父票未到待取"
              : "差异未清，不可核销"}
            {scanned.issues?.length ? `：${scanned.issues.join("；")}` : ""}
          </p>
        )}
      </section>

      <section className="panel">
        <h3>待取核销</h3>
        <p className={`msg ${msg.includes("已取") ? "ok" : ""}`}>{msg}</p>
        <table>
          <thead>
            <tr>
              <th>票号</th>
              <th>顾客</th>
              <th>件数</th>
              <th>合并码</th>
              <th>承诺日</th>
              <th>暗码</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.ticket_no}>
                <td>{r.ticket_no}</td>
                <td>{r.customer_name}</td>
                <td>
                  {r.item_qty}
                  {r.children?.length ? `（含 ${r.children.length} 张并单）` : ""}
                </td>
                <td>
                  {r.merge_code ? (
                    <span
                      className={`badge ${r.merge_ok === false ? "超期隔离" : "待取"}`}
                    >
                      {r.merge_code}
                      {r.merge_ok === false ? "（差异）" : ""}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{r.due_date}</td>
                <td>
                  <input
                    placeholder="输入暗码"
                    value={codes[r.ticket_no] || ""}
                    onChange={(e) =>
                      setCodes({ ...codes, [r.ticket_no]: e.target.value })
                    }
                  />
                </td>
                <td>
                  <button type="button" onClick={() => pick(r.ticket_no)}>
                    核销取衣
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
