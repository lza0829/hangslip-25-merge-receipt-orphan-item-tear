import { useEffect, useState } from "react";
import { api } from "../api";

export default function Home() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .总览()
      .then(setD)
      .catch((e) => setErr(e.message));
  }, []);

  const cells = d
    ? [
        ["在途水位", d.在途水位],
        ["在途票", d.在途票],
        ["待取", d.待取],
        ["超期隔离", d.超期隔离],
        ["水洗中", d.水洗],
        ["件数合计", d.件数合计],
      ]
    : [];

  return (
    <>
      <div className="hero-strip">
        <section className="ticket-hero">
          <h2>今日挂衣</h2>
          <p>
            票墙左侧是工位入口。先开票贴号，再沿工序推到待取，顾客凭暗码核销；逾承诺日可进超期隔离，与现行票态互证。
          </p>
        </section>
        <div className="metrics">
          {cells.map(([k, v], i) => (
            <div className="metric" key={k} style={{ animationDelay: `${i * 0.05}s` }}>
              <b>{v ?? "—"}</b>
              <span>{k}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="msg">{err}</div>
      {d && (
        <>
          <section className="panel">
            <h3>合并账（口径：合并事务世代）</h3>
            <div className="row">
              <div className="metric">
                <b>{d.合并成功}</b>
                <span>合并成功</span>
              </div>
              <div className="metric">
                <b>{d.合并差异}</b>
                <span>合并差异</span>
              </div>
              <div className="metric">
                <b>{d.合并作废}</b>
                <span>已作废</span>
              </div>
              <div className="metric">
                <b>{d.并票件数}</b>
                <span>并票迁入件数</span>
              </div>
              <div className="metric">
                <b>{d.件数合计}</b>
                <span>件数合计</span>
              </div>
            </div>
            <p
              style={{
                fontSize: "0.86rem",
                color:
                  d.件键闭合 && d.水位闭合 && d.合并差异 === 0
                    ? "#9fd6bd"
                    : "#f0a29d",
              }}
            >
              件键{d.件键闭合 ? "闭合" : `未闭合（散挂 ${d.散挂件} / 孤儿 ${d.孤儿件} / 双挂 ${d.双挂件}）`}·
              水位{d.水位闭合 ? "与票籍一致" : "与票籍不符"}·
              {d.合并差异 === 0 ? "无未收口差异" : `有 ${d.合并差异} 个差异世代待签账`}
            </p>
            {d.合并差异明细?.length ? (
              <table>
                <thead>
                  <tr>
                    <th>组号</th>
                    <th>合并码</th>
                    <th>父票</th>
                    <th>差异</th>
                  </tr>
                </thead>
                <tbody>
                  {d.合并差异明细.map((g) => (
                    <tr key={g.group_id}>
                      <td>{g.group_id}</td>
                      <td>{g.merge_code || "—"}</td>
                      <td>{g.parent_ticket_no}</td>
                      <td style={{ color: "#f0a29d" }}>{g.issues.join("；")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </section>
          <section className="panel">
            <h3>工序分布</h3>
            <table>
              <thead>
                <tr>
                  <th>状态</th>
                  <th>票数</th>
                </tr>
              </thead>
              <tbody>
                {["已收", "水洗", "烘干", "熨烫", "待取", "已取", "超期隔离", "丢失隔离", "已并"].map(
                  (s) => (
                    <tr key={s}>
                      <td>
                        <span className={`badge ${s}`}>{s}</span>
                      </td>
                      <td>{d[s]}</td>
                    </tr>
                  )
                )}
              </tbody>
            </table>
          </section>
        </>
      )}
    </>
  );
}
