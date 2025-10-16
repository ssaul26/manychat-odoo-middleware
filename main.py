from fastapi import FastAPI, Request
import os, requests

app = FastAPI()

# Configuración: se leerán desde Railway
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

@app.get("/")
def home():
    return {"status": "API funcionando correctamente 🚀"}

@app.post("/consulta_inventario")
async def consulta_inventario(request: Request):
    data = await request.json()
    escuela = data.get("escuela")

    # 1️⃣ Login a Odoo
    login = requests.post(f"{ODOO_URL}/jsonrpc", json={
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "login",
            "args": [ODOO_DB, ODOO_USER, ODOO_PASSWORD],
        },
        "id": 1,
    }).json()

    uid = login.get("result")
    if not uid:
        return {"error": "No se pudo iniciar sesión en Odoo"}

    # 2️⃣ Buscar productos
    query = [[["categ_id.name", "ilike", escuela]]]
    fields = ["name", "list_price", "qty_available", "categ_id"]

    res = requests.post(f"{ODOO_URL}/jsonrpc", json={
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.template",
                "search_read",
                query,
                fields
            ],
        },
        "id": 2,
    }).json()

    productos = res.get("result", [])
    if not productos:
        return {"mensaje": f"No se encontraron productos para '{escuela}'."}

    salida = []
    for p in productos:
        salida.append({
            "nombre": p["name"],
            "precio": p["list_price"],
            "stock": p["qty_available"],
            "categoria": p["categ_id"][1] if p["categ_id"] else None
        })

    return {"productos": salida}
