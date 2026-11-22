# 衣票墙（hangslip）

收衣贴号 → 工序推进 → 取衣核销关账 → 超期/丢失隔离互证。

- 前端 http://127.0.0.1:6200
- API http://127.0.0.1:11200

本地开发：

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ..\frontend
npm install
npm run dev
```
